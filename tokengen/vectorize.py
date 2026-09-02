"""Etapa 2 — vtracer (raster → contornos) y conversión a geometría shapely en mm.

vtracer devuelve SVG. No usamos una librería de SVG completa a propósito: el
subconjunto que emite vtracer es chico y conocido (M / L / C / Q / Z + un
`transform="translate(...)"`), y parsearlo nosotros evita una dependencia pesada
y nos deja controlar la tolerancia de aplanado de las Bézier — que es lo que
después determina cuántos triángulos tiene la malla.

Incluye un trazador de respaldo en numpy/shapely puro por si vtracer no está
instalado (útil en CI o en entornos sin toolchain de Rust).
"""

from __future__ import annotations

import re

import numpy as np
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import unary_union
from shapely.validation import make_valid

__all__ = ["trace_mask", "HAVE_VTRACER"]

try:  # pragma: no cover
    import vtracer

    HAVE_VTRACER = True
except ImportError:  # pragma: no cover
    HAVE_VTRACER = False


# ---------------------------------------------------------------- API pública


def trace_mask(
    mask: np.ndarray,
    mm_per_px: float,
    simplify_mm: float = 0.05,
    filter_speckle: int = 4,
    corner_threshold: int = 60,
    curve_tolerance_mm: float = 0.03,
) -> MultiPolygon:
    """Traza una máscara booleana y devuelve geometría **en mm**, eje Y hacia arriba."""
    if not mask.any():
        return MultiPolygon()

    if HAVE_VTRACER:
        rings = _trace_vtracer(mask, filter_speckle, corner_threshold, curve_tolerance_mm / mm_per_px)
    else:
        rings = None

    if rings:
        geom = _rings_to_polygons(rings)
    else:
        geom = _trace_fallback(mask)

    h = mask.shape[0]
    # px → mm y volteo de Y (en imagen crece hacia abajo; en el modelo, hacia arriba)
    geom = _affine(geom, mm_per_px, h)

    if simplify_mm > 0:
        geom = geom.simplify(simplify_mm, preserve_topology=True)

    return _as_multipolygon(_clean(geom))


# ------------------------------------------------------------------- vtracer


def _trace_vtracer(mask, filter_speckle, corner_threshold, curve_tol_px):
    h, w = mask.shape
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = np.where(mask[..., None], 0, 255)  # figura negra sobre blanco
    rgba[..., 3] = 255
    pixels = [tuple(int(v) for v in p) for p in rgba.reshape(-1, 4)]

    svg = vtracer.convert_pixels_to_svg(
        pixels,
        size=(w, h),
        colormode="binary",
        hierarchical="stacked",
        mode="spline",
        filter_speckle=int(filter_speckle),
        corner_threshold=int(corner_threshold),
        path_precision=3,
    )
    return _parse_svg(svg, curve_tol_px)


_PATH_RE = re.compile(r'<path[^>]*\sd="([^"]*)"[^>]*>')
_TRANSLATE_RE = re.compile(r"translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)")
_TOKEN_RE = re.compile(r"([MmLlHhVvCcQqZz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")


def _parse_svg(svg: str, curve_tol_px: float) -> list[np.ndarray]:
    rings: list[np.ndarray] = []
    for tag in re.finditer(r"<path\b[^>]*>", svg):
        chunk = tag.group(0)
        d = _PATH_RE.search(chunk)
        if not d:
            continue
        dx = dy = 0.0
        t = _TRANSLATE_RE.search(chunk)
        if t:
            dx, dy = float(t.group(1)), float(t.group(2))
        for ring in _path_to_rings(d.group(1), curve_tol_px):
            if len(ring) >= 3:
                rings.append(ring + np.array([dx, dy]))
    return rings


def _path_to_rings(d: str, tol: float) -> list[np.ndarray]:
    """Convierte un atributo `d` en anillos de puntos, aplanando Bézier."""
    toks = _TOKEN_RE.findall(d)
    stream: list = [c if c else float(n) for c, n in toks]

    rings, cur = [], []
    cx = cy = sx = sy = 0.0
    cmd = None
    i = 0

    def flush():
        nonlocal cur
        if len(cur) >= 3:
            rings.append(np.asarray(cur, dtype=float))
        cur = []

    while i < len(stream):
        tok = stream[i]
        if isinstance(tok, str):
            cmd = tok
            i += 1
            if cmd in "Zz":
                flush()
                cx, cy = sx, sy
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        c = cmd.upper()

        def take(n):
            nonlocal i
            vals = stream[i : i + n]
            i += n
            return [float(v) for v in vals]

        if c == "M":
            x, y = take(2)
            if rel:
                x, y = cx + x, cy + y
            flush()
            cx, cy = sx, sy = x, y
            cur = [(cx, cy)]
            cmd = "l" if rel else "L"  # coords extra tras M son lineTo
        elif c == "L":
            x, y = take(2)
            if rel:
                x, y = cx + x, cy + y
            cur.append((x, y))
            cx, cy = x, y
        elif c == "H":
            (x,) = take(1)
            x = cx + x if rel else x
            cur.append((x, cy))
            cx = x
        elif c == "V":
            (y,) = take(1)
            y = cy + y if rel else y
            cur.append((cx, y))
            cy = y
        elif c == "C":
            x1, y1, x2, y2, x, y = take(6)
            if rel:
                x1, y1, x2, y2, x, y = (cx + x1, cy + y1, cx + x2, cy + y2, cx + x, cy + y)
            cur.extend(_flatten_cubic((cx, cy), (x1, y1), (x2, y2), (x, y), tol))
            cx, cy = x, y
        elif c == "Q":
            x1, y1, x, y = take(4)
            if rel:
                x1, y1, x, y = cx + x1, cy + y1, cx + x, cy + y
            # elevar cuadrática a cúbica
            c1 = (cx + 2 / 3 * (x1 - cx), cy + 2 / 3 * (y1 - cy))
            c2 = (x + 2 / 3 * (x1 - x), y + 2 / 3 * (y1 - y))
            cur.extend(_flatten_cubic((cx, cy), c1, c2, (x, y), tol))
            cx, cy = x, y
        else:
            i += 1  # comando no soportado: se ignora

    flush()
    return rings


def _flatten_cubic(p0, p1, p2, p3, tol: float) -> list[tuple[float, float]]:
    """Aplana una Bézier cúbica con N pasos derivados de la cuerda y la tolerancia."""
    p0, p1, p2, p3 = (np.asarray(p, dtype=float) for p in (p0, p1, p2, p3))
    chord = np.linalg.norm(p3 - p0)
    poly = np.linalg.norm(p1 - p0) + np.linalg.norm(p2 - p1) + np.linalg.norm(p3 - p2)
    err = max(poly - chord, 1e-9)
    n = int(np.clip(np.ceil(np.sqrt(err / max(tol, 1e-4)) * 3), 2, 24))
    t = np.linspace(0, 1, n + 1)[1:][:, None]
    mt = 1 - t
    pts = mt**3 * p0 + 3 * mt**2 * t * p1 + 3 * mt * t**2 * p2 + t**3 * p3
    return [tuple(p) for p in pts]


# -------------------------------------------------------- anillos → polígonos


def _rings_to_polygons(rings: list[np.ndarray]) -> MultiPolygon:
    """Resuelve el anidamiento (exterior / hueco / isla dentro del hueco) por paridad.

    vtracer emite los huecos como subpaths adicionales del mismo `path`, sin
    indicar cuál es cuál. La profundidad de anidamiento decide: par = material,
    impar = hueco.
    """
    polys = []
    for r in rings:
        try:
            p = Polygon(r)
        except Exception:
            continue
        if not p.is_valid:
            p = make_valid(p)
        if p.is_empty or p.area <= 0:
            continue
        polys.append(p)

    if not polys:
        return MultiPolygon()

    polys.sort(key=lambda p: p.area, reverse=True)
    depth = []
    for i, p in enumerate(polys):
        d = 0
        rep = p.representative_point()
        for j in range(i):
            if polys[j].contains(rep):
                d += 1
        depth.append(d)

    # Se procesa por profundidad CRECIENTE, alternando unión y diferencia.
    # El orden importa: una isla a profundidad 2 vive dentro de un hueco a
    # profundidad 1. Si se unieran todos los sólidos primero y se restaran los
    # huecos al final, el hueco se comería la isla que contiene — que es
    # exactamente el disco central de un anillo, o un punto de color dentro de
    # una letra hueca.
    geom = Polygon()
    for d, p in sorted(zip(depth, polys), key=lambda t: t[0]):
        geom = geom.union(p) if d % 2 == 0 else geom.difference(p)

    return _as_multipolygon(_clean(geom))


# ------------------------------------------------------------------- respaldo


def _trace_fallback(mask: np.ndarray) -> MultiPolygon:
    """Trazador sin dependencias: une los píxeles por tramos de fila (run-length)."""
    boxes = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        for x0, x1 in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
            boxes.append(box(x0, y, x1, y + 1))
    if not boxes:
        return MultiPolygon()
    return _as_multipolygon(unary_union(boxes).buffer(0.01).buffer(-0.01).simplify(0.35))


# ---------------------------------------------------------------- utilidades


def _affine(geom, mm_per_px: float, height_px: int):
    from shapely.affinity import affine_transform

    # [a, b, d, e, xoff, yoff] → x' = a·x + b·y + xoff ; y' = d·x + e·y + yoff
    return affine_transform(geom, [mm_per_px, 0, 0, -mm_per_px, 0, height_px * mm_per_px])


def _clean(geom):
    if geom.is_empty:
        return geom
    if not geom.is_valid:
        geom = make_valid(geom)
    return geom.buffer(0)


def _as_multipolygon(geom) -> MultiPolygon:
    if geom.is_empty:
        return MultiPolygon()
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and g.area > 0]
    return MultiPolygon(parts) if parts else MultiPolygon()
