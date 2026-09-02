"""Validaciones de imprimibilidad.

Esta es la parte que evita la iteración "exporto → slicéo → sale mal → vuelvo a
empezar". Todas las comprobaciones se hacen sobre geometría 2D en mm ANTES de
generar la malla, que es cuando todavía se puede corregir barato.

Cada regla responde a un modo de falla real en impresión multicolor:

| Regla                | Falla que evita                                              |
|----------------------|--------------------------------------------------------------|
| `thin_features`      | Detalle más fino que un perímetro: el slicer lo omite en silencio |
| `micro_gaps`         | Separación sub-boquilla: dos colores se funden y se pierde el borde |
| `micro_holes`        | Agujero que el slicer tapa al no poder trazar la pared interna |
| `tiny_islands`       | Islas sueltas que se despegan de la cama o el AMS arrastra      |
| `stacking_order`     | Color flotando sin material debajo (imposible de apilar)       |
| `overhangs`          | Alero que excede lo puenteable sin soporte                     |
| `layer_quantization` | Bandas de color que no son múltiplo de la altura de capa       |
| `keyring`            | Agujero de llavero con pared demasiado fina al borde           |
| `size_sanity`        | Pieza fuera de rango útil / demasiados triángulos              |
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from .config import PrintProfile
from .layers import LayerStack

Severity = Literal["error", "warning", "info"]

__all__ = ["Issue", "ValidationReport", "validate_stack"]


@dataclass
class Issue:
    code: str
    severity: Severity
    message: str
    layer: str | None = None
    count: int = 0
    area_mm2: float = 0.0
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    issues: list[Issue]
    stats: dict

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def printable(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "printable": self.printable,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "stats": self.stats,
            "issues": [i.to_dict() for i in self.issues],
        }

    def render(self) -> str:
        icons = {"error": "✗", "warning": "!", "info": "·"}
        lines = []
        for i in self.issues:
            where = f" [{i.layer}]" if i.layer else ""
            extra = f" ({i.count} zonas, {i.area_mm2:.2f} mm²)" if i.count else ""
            lines.append(f" {icons[i.severity]} {i.code}{where}: {i.message}{extra}")
            if i.hint:
                lines.append(f"     → {i.hint}")
        if not lines:
            lines.append(" ✓ Sin observaciones.")
        verdict = "IMPRIMIBLE" if self.printable else "NO IMPRIMIBLE"
        return "\n".join(lines) + f"\n\n Veredicto: {verdict}"


# --------------------------------------------------------------------------- #


def validate_stack(stack: LayerStack, profile: PrintProfile | None = None) -> ValidationReport:
    p = profile or stack.spec.profile
    issues: list[Issue] = []

    for layer in stack.layers:
        issues += _thin_features(layer, p)
        issues += _micro_holes(layer, p)
        issues += _tiny_islands(layer, p)

    issues += _micro_gaps(stack, p)
    issues += _stacking_order(stack, p)
    issues += _overhangs(stack, p)
    issues += _layer_quantization(stack, p)
    issues += _keyring(stack, p)
    issues += _size_sanity(stack, p)

    for note in stack.notes:
        issues.append(Issue("cumulative_union", "info", note))

    minx, miny, maxx, maxy = stack.bounds_mm
    stats = {
        "profile": p.name,
        "size_mm": [round(maxx - minx, 2), round(maxy - miny, 2), round(stack.height_mm, 2)],
        "colors": len(stack.layers),
        "silhouette_area_mm2": round(stack.silhouette.area, 2),
        "mode": stack.spec.mode,
    }
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i.severity])
    return ValidationReport(issues=issues, stats=stats)


# ------------------------------------------------------------------ reglas --


def _thin_features(layer, p: PrintProfile) -> list[Issue]:
    """Erosión de media boquilla: lo que desaparece no se puede extruir."""
    geom = layer.visible
    r = p.min_feature_mm / 2
    eroded = geom.buffer(-r)
    lost = geom.difference(eroded.buffer(r)) if not eroded.is_empty else geom

    if eroded.is_empty:
        return [
            Issue(
                "thin_features", "error",
                f"toda la región es más fina que {p.min_feature_mm} mm y no se va a imprimir",
                layer=layer.name, count=1, area_mm2=round(geom.area, 3),
                hint="Agrandá la pieza (--width-mm), engrosá el trazo o fusioná este color con el vecino.",
            )
        ]

    lost = _clean(lost)
    if lost.is_empty or lost.area < p.min_feature_area_mm2:
        return []

    parts = _significant_parts(lost, p.min_feature_area_mm2)
    if not parts:
        return []

    ratio = lost.area / max(geom.area, 1e-6)
    sev: Severity = "error" if ratio > 0.25 else "warning"
    return [
        Issue(
            "thin_features", sev,
            f"detalle por debajo de {p.min_feature_mm} mm ({ratio:.1%} del color visible)",
            layer=layer.name, count=len(parts), area_mm2=round(lost.area, 3),
            hint=f"Escalá ×{p.min_feature_mm / max(_min_width(parts), 1e-3):.1f} o simplificá el arte.",
        )
    ]


def _micro_holes(layer, p: PrintProfile) -> list[Issue]:
    """Huecos interiores más chicos que el diámetro mínimo se tapan solos."""
    bad = []
    for poly in _polys(layer.geometry):
        for ring in poly.interiors:
            hole = Polygon(ring)
            if hole.area <= 0:
                continue
            if hole.buffer(-p.min_hole_mm / 2).is_empty:
                bad.append(hole)
    if not bad:
        return []
    return [
        Issue(
            "micro_holes", "warning",
            f"huecos interiores por debajo de ⌀{p.min_hole_mm} mm; el slicer los va a rellenar",
            layer=layer.name, count=len(bad),
            area_mm2=round(sum(h.area for h in bad), 3),
            hint="Agrandalos, o eliminalos a propósito para que el resultado sea predecible.",
        )
    ]


def _tiny_islands(layer, p: PrintProfile) -> list[Issue]:
    parts = [g for g in _polys(layer.visible) if g.area < p.min_island_mm2]
    if not parts:
        return []
    return [
        Issue(
            "tiny_islands", "warning",
            f"islas menores a {p.min_island_mm2} mm²; riesgo de despegue y de arrastre por el AMS",
            layer=layer.name, count=len(parts),
            area_mm2=round(sum(g.area for g in parts), 3),
            hint="Subí --despeckle o aumentá el tamaño de la pieza.",
        )
    ]


def _micro_gaps(stack: LayerStack, p: PrintProfile) -> list[Issue]:
    """Separaciones entre colores por debajo de la boquilla: los bordes se funden."""
    issues = []
    same_z = {}
    for l in stack.layers:
        same_z.setdefault(round(l.z1, 3), []).append(l)
    if stack.spec.mode == "relief":  # en relieve los colores no comparten plano
        same_z = {}

    for z, group in same_z.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                d = a.visible.distance(b.visible)
                if 0 < d < p.min_gap_mm:
                    issues.append(
                        Issue(
                            "micro_gaps", "warning",
                            f"separación de {d:.3f} mm entre {a.name} y {b.name} "
                            f"(mínimo {p.min_gap_mm} mm)",
                            hint="Los colores se van a tocar. Aplicá --bleed negativo o unificalos.",
                        )
                    )
    return issues


def _stacking_order(stack: LayerStack, p: PrintProfile) -> list[Issue]:
    """Ningún color puede empezar en el aire: necesita material debajo."""
    if stack.spec.mode != "relief":
        # En inlay todo apoya en la placa base salvo que la base no cubra.
        base = next((l for l in stack.layers if l.is_base), None)
        if base is None:
            return []
        issues = []
        for l in stack.layers:
            if l.is_base or l.z0 <= 0:
                continue
            unsupported = _clean(l.geometry.difference(base.geometry.buffer(1e-6)))
            if unsupported.area > p.min_feature_area_mm2:
                issues.append(
                    Issue(
                        "stacking_order", "error",
                        "hay material de color que arranca sin placa base debajo",
                        layer=l.name, count=len(_polys(unsupported)),
                        area_mm2=round(unsupported.area, 3),
                        hint="La base debe cubrir la silueta completa. Revisá --fillet o --keyring.",
                    )
                )
        return issues

    # Modo relieve: cada capa se apoya en la unión de las inferiores.
    issues = []
    ordered = sorted(stack.layers, key=lambda l: l.z1)
    for k, l in enumerate(ordered):
        if k == 0:
            continue
        below = unary_union([o.geometry for o in ordered[:k]])
        top_band = l.geometry
        floating = _clean(top_band.difference(below.buffer(1e-6)))
        if floating.area > p.min_feature_area_mm2 * 4:
            issues.append(
                Issue(
                    "stacking_order", "error",
                    f"la banda a z={l.z1:.2f} mm queda flotando sobre aire",
                    layer=l.name, count=len(_polys(floating)),
                    area_mm2=round(floating.area, 3),
                    hint="Invertí el orden de apilado o pasá a --mode inlay.",
                )
            )
    return issues


def _overhangs(stack: LayerStack, p: PrintProfile) -> list[Issue]:
    """Alero de una capa respecto de la de abajo, más allá de lo puenteable."""
    if stack.spec.mode != "relief":
        return []
    issues = []
    ordered = sorted(stack.layers, key=lambda l: l.z1)
    for k, l in enumerate(ordered):
        if k == 0:
            continue
        below = unary_union([o.geometry for o in ordered[:k]])
        over = _clean(l.geometry.difference(below.buffer(p.max_overhang_mm)))
        if over.area > p.min_feature_area_mm2 * 4:
            issues.append(
                Issue(
                    "overhangs", "warning",
                    f"voladizo mayor a {p.max_overhang_mm} mm sin apoyo",
                    layer=l.name, count=len(_polys(over)), area_mm2=round(over.area, 3),
                    hint="Va a necesitar soporte, o achicá el escalonado con --color-mm.",
                )
            )
    return issues


def _layer_quantization(stack: LayerStack, p: PrintProfile) -> list[Issue]:
    """Las alturas deben caer en múltiplos de la altura de capa."""
    issues = []
    lh = p.layer_height_mm
    for value, label in ((stack.spec.base_mm, "--base-mm"), (stack.spec.color_mm, "--color-mm")):
        n = value / lh
        if abs(n - round(n)) > 1e-6:
            snapped = max(round(n), 1) * lh
            issues.append(
                Issue(
                    "layer_quantization", "warning",
                    f"{label}={value} mm no es múltiplo de la altura de capa {lh} mm",
                    hint=f"El slicer va a redondear a {snapped:.2f} mm. Usá ese valor para que el color salga exacto.",
                )
            )
        if value < lh:
            issues.append(
                Issue(
                    "layer_quantization", "error",
                    f"{label}={value} mm es menor a una capa ({lh} mm): ese color no existe en el laminado",
                    hint=f"Subilo a {lh:.2f} mm como mínimo; 2 capas si querés que tape bien.",
                )
            )
    return issues


def _keyring(stack: LayerStack, p: PrintProfile) -> list[Issue]:
    if not stack.spec.keyring:
        return []
    diameter, margin = stack.spec.keyring
    issues = []
    holes = [Polygon(r) for poly in _polys(stack.silhouette) for r in poly.interiors]
    if not holes:
        issues.append(
            Issue(
                "keyring", "error",
                "se pidió agujero de llavero pero no se encontró lugar con material suficiente",
                hint="Reducí --keyring-d o agrandá la pieza.",
            )
        )
    if margin < p.min_feature_mm * 3:
        issues.append(
            Issue(
                "keyring", "warning",
                f"pared de {margin} mm alrededor del agujero (recomendado ≥ {p.min_feature_mm * 3:.1f} mm)",
                hint="Con menos de 3 perímetros el llavero se rompe al tironear.",
            )
        )
    return issues


def _size_sanity(stack: LayerStack, p: PrintProfile) -> list[Issue]:
    minx, miny, maxx, maxy = stack.bounds_mm
    w, h = maxx - minx, maxy - miny
    issues = []
    if max(w, h) < 10:
        issues.append(
            Issue("size_sanity", "warning", f"pieza muy chica ({w:.1f}×{h:.1f} mm); el detalle se pierde",
                  hint="Subí --width-mm a 25 mm o más para un llavero típico.")
        )
    if max(w, h) > 250:
        issues.append(
            Issue("size_sanity", "warning", f"pieza de {w:.0f}×{h:.0f} mm; revisá que entre en la cama",
                  hint="Una Bambu X1/P1 admite hasta 256 mm.")
        )
    if stack.height_mm < p.layer_height_mm * 3:
        issues.append(
            Issue("size_sanity", "warning",
                  f"altura total {stack.height_mm:.2f} mm: menos de 3 capas, la pieza va a ser frágil")
        )
    return issues


# ------------------------------------------------------------- utilidades --


def _polys(geom):
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if g.area > 0]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and g.area > 0]


def _significant_parts(geom, min_area):
    return [g for g in _polys(geom) if g.area >= min_area]


def _min_width(parts) -> float:
    """Ancho característico aproximado: 2·área/perímetro de la parte más fina."""
    widths = [2 * g.area / g.length for g in parts if g.length > 0]
    return min(widths) if widths else 0.1


def _clean(g):
    if g.is_empty:
        return g
    return g if g.is_valid else g.buffer(0)
