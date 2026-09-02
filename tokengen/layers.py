"""Etapa 3 — unión acumulativa (shapely).

Este es el módulo que separa a TokenGen de un "image to STL" cualquiera.

**El problema.** Cada color se traza por separado a partir de su propia máscara.
Dos regiones que en la imagen eran vecinas píxel a píxel, al vectorizarse por
separado quedan con bordes que *casi* coinciden: sobran micro-solapes de
0,02 mm y faltan micro-huecos de 0,03 mm. En pantalla no se ven. En el slicer,
cada uno de esos huecos es una pared de aire que rompe la capa, y cada solape
es geometría auto-intersectada que dispara "non-manifold".

**La solución.** No se hace `union` de todo junto: se hace una pasada
*acumulativa* en orden de prioridad. Cada color reclama únicamente lo que
ningún color anterior reclamó:

    reclamado = ∅
    para cada color c en orden de prioridad:
        region[c] = geom[c] − reclamado
        reclamado = reclamado ∪ region[c]

El resultado es una **partición exacta** de la silueta: sin solapes por
construcción, y los restos no reclamados (los micro-huecos) se absorben al
final en el color base. La suma de las regiones es idénticamente la silueta.

El orden de prioridad importa: por defecto va de menor a mayor área, para que
el detalle fino (texto, líneas) gane sobre el fondo y no se lo coma la
tolerancia. El fondo, que es el más grande, absorbe los restos.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from .config import Filament, TokenSpec

__all__ = ["ColorLayer", "LayerStack", "build_stack"]


@dataclass
class ColorLayer:
    index: int
    rgb: tuple[int, int, int]
    geometry: MultiPolygon      # sólido que se extruye
    z0: float
    z1: float
    order: int
    filament: Filament | None = None
    is_base: bool = False
    visible: MultiPolygon | None = None  # parte que el ojo ve desde arriba

    def __post_init__(self):
        if self.visible is None:
            self.visible = self.geometry

    @property
    def name(self) -> str:
        slot = f"AMS{self.filament.slot}" if self.filament else f"C{self.index}"
        label = self.filament.name if self.filament else "#%02x%02x%02x" % self.rgb
        return f"{slot}_{label}"

    @property
    def area_mm2(self) -> float:
        return self.geometry.area


@dataclass
class LayerStack:
    layers: list[ColorLayer]
    silhouette: MultiPolygon
    spec: TokenSpec
    absorbed_mm2: float = 0.0            # área de restos reabsorbidos
    overlap_removed_mm2: float = 0.0     # área de solapes eliminados
    notes: list[str] = field(default_factory=list)

    @property
    def height_mm(self) -> float:
        return max((l.z1 for l in self.layers), default=0.0)

    @property
    def bounds_mm(self):
        return self.silhouette.bounds


def build_stack(
    geoms: dict[int, MultiPolygon],
    colors: list[tuple[int, int, int]],
    spec: TokenSpec,
    filaments: list[Filament] | None = None,
) -> LayerStack:
    """Arma la partición de colores y les asigna alturas."""
    live = {i: g for i, g in geoms.items() if not g.is_empty and g.area > 0}
    if not live:
        raise ValueError("No quedó ninguna región de color tras el trazado.")

    silhouette = _as_mp(unary_union(list(live.values())).buffer(0))

    # Cierre morfológico: sella micro-huecos del contorno externo antes de partir.
    seal = max(spec.profile.nozzle_mm * 0.25, 0.05)
    silhouette = _as_mp(silhouette.buffer(seal).buffer(-seal))

    if spec.fillet_mm > 0:
        r = spec.fillet_mm
        silhouette = _as_mp(silhouette.buffer(-r).buffer(r * 2).buffer(-r))

    if spec.keyring:
        silhouette = _punch_keyring(silhouette, *spec.keyring)

    # ---- unión acumulativa -------------------------------------------------
    order = sorted(live, key=lambda i: live[i].area)      # detalle fino primero
    base_idx = order[-1]                                   # el más grande es la base

    claimed = Polygon()
    regions: dict[int, MultiPolygon] = {}
    overlap = 0.0

    for i in order:
        g = live[i].intersection(silhouette)
        if spec.bleed_mm:
            g = g.buffer(spec.bleed_mm)
        raw_area = g.area
        g = g.difference(claimed)
        overlap += max(raw_area - g.area, 0.0)
        g = _clean(g)
        if g.is_empty:
            continue
        regions[i] = _as_mp(g)
        claimed = claimed.union(g)

    leftover = _clean(silhouette.difference(claimed))
    absorbed = leftover.area
    if not leftover.is_empty:
        base = regions.get(base_idx, MultiPolygon())
        regions[base_idx] = _as_mp(_clean(unary_union([base, leftover])))

    # ---- asignación de alturas --------------------------------------------
    # De mayor a menor área: el fondo abajo, el detalle fino arriba.
    z_order = sorted(regions, key=lambda i: regions[i].area, reverse=True)
    layers: list[ColorLayer] = []

    # Segunda unión acumulativa, ahora de arriba hacia abajo: la banda j abarca
    # su color y TODOS los de más arriba. Así cada banda contiene a la de encima
    # (apoyo total, cero voladizos) y el color visible en cada punto es el de la
    # banda más alta que lo cubre. Es lo que permite imprimir un color por franja
    # de Z: un solo cambio de filamento por banda y purga mínima.
    cumulative: dict[int, MultiPolygon] = {}
    acc = MultiPolygon()
    for i in reversed(z_order):
        acc = _as_mp(_clean(unary_union([acc, regions[i]])))
        cumulative[i] = acc

    for pos, i in enumerate(z_order):
        if spec.mode == "relief":
            solid = cumulative[i]
            z0 = 0.0 if pos == 0 else spec.base_mm + spec.color_mm * (pos - 1)
            z1 = spec.base_mm if pos == 0 else z0 + spec.color_mm
        else:
            # Mosaico coplanar: placa base completa + una única banda de color.
            solid = silhouette if i == base_idx else regions[i]
            z0 = 0.0 if i == base_idx else spec.base_mm
            z1 = spec.base_mm if i == base_idx else spec.base_mm + spec.color_mm

        layers.append(
            ColorLayer(
                index=i,
                rgb=colors[i] if i < len(colors) else (128, 128, 128),
                geometry=solid,
                z0=z0,
                z1=z1,
                order=pos,
                filament=filaments[i] if filaments and i < len(filaments) else None,
                is_base=(i == base_idx),
                visible=regions[i],
            )
        )

    notes = []
    if absorbed > 0:
        notes.append(
            f"Unión acumulativa: {absorbed:.3f} mm² de micro-huecos reabsorbidos en la base."
        )
    if overlap > 0:
        notes.append(f"Unión acumulativa: {overlap:.3f} mm² de solapes recortados.")

    return LayerStack(
        layers=layers,
        silhouette=silhouette,
        spec=spec,
        absorbed_mm2=absorbed,
        overlap_removed_mm2=overlap,
        notes=notes,
    )


def _punch_keyring(sil: MultiPolygon, diameter: float, margin: float) -> MultiPolygon:
    """Perfora el agujero de llavero en el punto más alto y centrado de la silueta."""
    minx, miny, maxx, maxy = sil.bounds
    cx = (minx + maxx) / 2
    r = diameter / 2
    cy = maxy - margin - r

    # Bajar hasta encontrar una posición donde el agujero quede rodeado de material.
    step = max(diameter * 0.2, 0.4)
    for _ in range(60):
        ring = Point(cx, cy).buffer(r + margin, quad_segs=32)
        if sil.contains(ring):
            return _as_mp(sil.difference(Point(cx, cy).buffer(r, quad_segs=32)))
        cy -= step
        if cy < miny:
            break
    return sil  # sin lugar seguro: se deja sin agujero y la validación lo reporta


def _clean(g):
    if g.is_empty:
        return g
    if not g.is_valid:
        g = g.buffer(0)
    return g


def _as_mp(g) -> MultiPolygon:
    if g.is_empty:
        return MultiPolygon()
    if g.geom_type == "Polygon":
        return MultiPolygon([g])
    if g.geom_type == "MultiPolygon":
        return g
    parts = [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon" and p.area > 0]
    return MultiPolygon(parts) if parts else MultiPolygon()
