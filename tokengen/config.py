"""Configuración del pipeline: perfiles de impresión, paletas de filamento y spec del token.

Todo el pipeline trabaja en **milímetros** una vez vectorizado. El único lugar
donde existen píxeles es en `raster.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal, Sequence

# Edición Community: límite de colores. La edición Pro levanta este techo,
# agrega optimización de torre de purga y batch/CLI de configurador.
COMMUNITY_MAX_COLORS = 4

Mode = Literal["inlay", "relief"]


@dataclass(frozen=True)
class PrintProfile:
    """Restricciones físicas de la impresora. De acá salen casi todas las validaciones."""

    name: str = "0.4mm / PLA / AMS"
    nozzle_mm: float = 0.4
    layer_height_mm: float = 0.2
    # Ancho mínimo de una pared para que el slicer la extruya (≈1 perímetro).
    min_feature_mm: float = 0.42
    # Hueco mínimo entre dos regiones para que no se fusionen al imprimir.
    min_gap_mm: float = 0.45
    # Diámetro mínimo de un agujero interior para que no se tape.
    min_hole_mm: float = 0.8
    # Área mínima de una isla suelta (mm²) para que valga la pena imprimirla.
    min_island_mm2: float = 1.0
    # Voladizo máximo sin soporte de la capa de abajo.
    max_overhang_mm: float = 0.8

    @property
    def min_feature_area_mm2(self) -> float:
        return self.min_feature_mm * self.min_feature_mm


PROFILES: dict[str, PrintProfile] = {
    "0.4-pla-ams": PrintProfile(),
    "0.4-pla-fino": PrintProfile(
        name="0.4mm / PLA / detalle fino",
        layer_height_mm=0.1,
        min_feature_mm=0.40,
        min_gap_mm=0.42,
        min_hole_mm=0.7,
        min_island_mm2=0.6,
    ),
    "0.6-pla-rapido": PrintProfile(
        name="0.6mm / PLA / rápido",
        nozzle_mm=0.6,
        layer_height_mm=0.3,
        min_feature_mm=0.62,
        min_gap_mm=0.65,
        min_hole_mm=1.2,
        min_island_mm2=2.2,
        max_overhang_mm=1.2,
    ),
}


@dataclass(frozen=True)
class Filament:
    name: str
    rgb: tuple[int, int, int]
    slot: int = 0  # slot de AMS/MMU


# Paleta de ejemplo: Bambu PLA Basic. Reemplazable con --palette archivo.json
DEFAULT_PALETTE: list[Filament] = [
    Filament("Black", (20, 20, 22), 1),
    Filament("White", (245, 245, 245), 2),
    Filament("Red", (193, 32, 42), 3),
    Filament("Yellow", (243, 195, 42), 4),
    Filament("Blue", (26, 82, 156), 5),
    Filament("Green", (22, 133, 78), 6),
    Filament("Gray", (140, 142, 146), 7),
    Filament("Orange", (233, 118, 27), 8),
]


@dataclass
class TokenSpec:
    """Parámetros geométricos de la pieza a generar."""

    width_mm: float = 50.0
    base_mm: float = 0.8          # espesor de la placa base (todo el silueteado)
    color_mm: float = 0.6         # espesor de cada banda de color por encima de la base
    mode: Mode = "inlay"          # inlay = mosaico coplanar | relief = escalonado en Z
    colors: int = 4
    profile: PrintProfile = field(default_factory=lambda: PROFILES["0.4-pla-ams"])
    palette: Sequence[Filament] | None = None   # None = paleta adaptativa de la imagen
    # Agujero para llavero: (diámetro_mm, margen_al_borde_mm) o None
    keyring: tuple[float, float] | None = None
    # Simplificación de contornos en mm (Douglas-Peucker). 0 = sin simplificar.
    simplify_mm: float = 0.05
    # Sangrado: expande/contrae cada región de color para compensar el flujo.
    bleed_mm: float = 0.0
    # Redondeo de esquinas del contorno externo.
    fillet_mm: float = 0.0

    def total_height_mm(self, n_layers: int) -> float:
        if self.mode == "relief":
            return self.base_mm + self.color_mm * max(n_layers - 1, 1)
        return self.base_mm + self.color_mm

    def to_dict(self) -> dict:
        d = asdict(self)
        d["profile"] = asdict(self.profile)
        if self.palette:
            d["palette"] = [asdict(f) for f in self.palette]
        return d
