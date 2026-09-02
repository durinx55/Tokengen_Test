"""Etapa 4 — triangulación (earcut) y extrusión (trimesh).

Cada capa de color se convierte en un sólido cerrado independiente. Son piezas
separadas a propósito: el 3MF multiparte necesita un objeto por filamento para
que el slicer sepa a qué extrusor mandar cada uno.

`mapbox_earcut` se usa como motor de triangulación porque es O(n log n),
tolera polígonos con muchos huecos y no exige que la entrada sea convexa —
justo el caso de un logo trazado.
"""

from __future__ import annotations

import numpy as np
import trimesh
from shapely.geometry import MultiPolygon, Polygon

__all__ = ["extrude_layer", "stack_to_meshes", "recenter"]


def extrude_layer(geometry: MultiPolygon, z0: float, z1: float) -> trimesh.Trimesh | None:
    """Extruye una MultiPolygon entre z0 y z1 y devuelve una única malla cerrada."""
    height = z1 - z0
    if height <= 0 or geometry.is_empty:
        return None

    parts: list[trimesh.Trimesh] = []
    for poly in _iter_polygons(geometry):
        for piece in _sanitize(poly):
            m = _extrude_one(piece, height)
            if m is not None:
                parts.append(m)

    if not parts:
        return None

    # Cada sólido se limpia por separado y después se concatenan SIN volver a
    # soldar vértices: dos regiones vecinas comparten borde exacto tras la unión
    # acumulativa, y soldarlas crearía aristas no-manifold (3 caras por arista).
    mesh = trimesh.util.concatenate(parts) if len(parts) > 1 else parts[0]
    mesh.apply_translation([0.0, 0.0, z0])
    return mesh


def _extrude_one(poly: Polygon, height: float) -> trimesh.Trimesh | None:
    for engine in ("earcut", None):
        try:
            m = (
                trimesh.creation.extrude_polygon(poly, height, engine=engine)
                if engine
                else trimesh.creation.extrude_polygon(poly, height)
            )
        except Exception:
            continue
        if not len(m.faces):
            continue
        # Sólo se toca la malla si hace falta: quitarle caras degeneradas a una
        # malla ya estanca le abre agujeros.
        if m.is_watertight:
            m.fix_normals()
            return m
        _cleanup(m)
        if not m.is_watertight:
            try:
                trimesh.repair.fill_holes(m)
                trimesh.repair.fix_normals(m)
            except Exception:
                pass
        return m if len(m.faces) else None
    return None


def _sanitize(poly: Polygon, min_ring_area: float = 1e-4):
    """Repara auto-intersecciones y descarta anillos degenerados antes de triangular.

    earcut da resultados no-manifold cuando el polígono se auto-toca en un punto
    (típico tras un `difference` con tolerancia). Un `buffer(0)` separa esos
    pinzamientos en polígonos independientes.
    """
    if poly.is_empty or poly.area <= min_ring_area:
        return

    fixed = poly if poly.is_valid else poly.buffer(0)
    for p in _iter_polygons(fixed):
        if p.area <= min_ring_area:
            continue
        holes = [r for r in p.interiors if Polygon(r).area > min_ring_area]
        yield Polygon(p.exterior, holes)


def _cleanup(mesh: trimesh.Trimesh) -> None:
    """Limpieza tolerante entre versiones de trimesh (la API cambió en 4.x).

    Ojo: NO se llama a `merge_vertices()` con la tolerancia por defecto. Trimesh
    la deriva de la escala de la malla, y en una pieza de 45 mm eso alcanza para
    colapsar el anillo de un hueco de 0,4 mm² — la malla queda no-manifold justo
    en el detalle fino que nos importa. `extrude_polygon` ya devuelve índices
    correctos, así que no hace falta soldar nada.
    """
    try:  # trimesh >= 4
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
    except AttributeError:  # pragma: no cover — trimesh 3.x
        mesh.remove_degenerate_faces()
        mesh.remove_duplicate_faces()
    mesh.remove_unreferenced_vertices()
    mesh.fix_normals()


def stack_to_meshes(stack) -> list[tuple[object, trimesh.Trimesh]]:
    """Devuelve [(ColorLayer, Trimesh), ...] en orden de impresión."""
    out = []
    for layer in stack.layers:
        m = extrude_layer(layer.geometry, layer.z0, layer.z1)
        if m is not None and len(m.faces):
            out.append((layer, m))
    return out


def recenter(meshes: list[tuple[object, trimesh.Trimesh]]) -> None:
    """Centra el conjunto en XY y lo apoya en Z=0 (in place)."""
    if not meshes:
        return
    allb = np.array([m.bounds for _, m in meshes])
    lo = allb[:, 0, :].min(axis=0)
    hi = allb[:, 1, :].max(axis=0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    for _, m in meshes:
        m.apply_translation(shift)


def mesh_stats(meshes) -> dict:
    total_v = sum(len(m.vertices) for _, m in meshes)
    total_f = sum(len(m.faces) for _, m in meshes)
    watertight = all(m.is_watertight for _, m in meshes)
    volume = sum(abs(m.volume) for _, m in meshes)
    return {
        "parts": len(meshes),
        "vertices": total_v,
        "triangles": total_f,
        "watertight": bool(watertight),
        "volume_mm3": round(volume, 2),
        # PLA ≈ 1,24 g/cm³
        "estimated_grams": round(volume / 1000.0 * 1.24, 2),
    }


def _iter_polygons(geom):
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        yield from geom.geoms
    else:
        for g in getattr(geom, "geoms", []):
            if isinstance(g, Polygon):
                yield g
