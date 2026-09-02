"""Etapa 1 — Pillow + numpy.

Entrada: una imagen cualquiera (PNG/JPG, con o sin alfa).
Salida: un mapa de índices (H, W) uint8 + la lista de colores, listo para trazar.

Decisiones que importan para imprimir (y que un "conversor de imagen a STL"
genérico no toma):

* La cuantización se hace **sobre los filamentos que realmente tenés cargados**,
  no sobre colores arbitrarios. De nada sirve un modelo de 12 colores si el AMS
  tiene 4 slots.
* El despeckle se hace en píxeles ANTES de vectorizar: cada mancha de 3 px que
  sobrevive se convierte en un polígono que el slicer no va a poder extruir.
* El fondo se recorta por flood-fill desde los bordes, no por umbral global,
  para no comerse zonas claras dentro del dibujo.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

from .config import Filament

__all__ = ["QuantizedImage", "load_image", "quantize", "masks_from_index"]


@dataclass
class QuantizedImage:
    index: np.ndarray                 # (H, W) uint8, 255 = fondo/transparente
    colors: list[tuple[int, int, int]]
    filaments: list[Filament] | None  # slot asignado si se usó paleta fija
    size: tuple[int, int]             # (W, H) en px

    @property
    def used_indices(self) -> list[int]:
        vals = np.unique(self.index)
        return [int(v) for v in vals if v != 255]


def load_image(
    path: str,
    max_px: int = 1024,
    drop_background: bool = True,
    bg_tolerance: int = 18,
) -> tuple[Image.Image, np.ndarray]:
    """Carga, normaliza tamaño y devuelve (RGB, máscara booleana de pieza)."""
    img = Image.open(path)

    alpha = None
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = np.array(rgba)[..., 3]
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    img = img.convert("RGB")

    # Reescalado manteniendo aspecto. LANCZOS mantiene bordes limpios para trazar.
    w, h = img.size
    if max(w, h) > max_px:
        scale = max_px / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        if alpha is not None:
            alpha = np.array(
                Image.fromarray(alpha).resize(img.size, Image.LANCZOS)
            )

    if alpha is not None:
        mask = alpha > 127
    elif drop_background:
        mask = ~_background_mask(np.array(img), bg_tolerance)
    else:
        mask = np.ones(img.size[::-1], dtype=bool)

    return img, mask


def _background_mask(arr: np.ndarray, tol: int) -> np.ndarray:
    """Flood-fill 4-conexo desde los 4 bordes, con tolerancia de color.

    Se propaga sólo por píxeles parecidos al color del marco: así una zona blanca
    *encerrada* dentro del dibujo no se confunde con fondo.
    """
    from scipy import ndimage

    h, w = arr.shape[:2]
    ref = np.concatenate([arr[0, :], arr[-1, :], arr[:, 0], arr[:, -1]]).reshape(-1, 3)
    ref_color = np.median(ref, axis=0)
    close = np.abs(arr.astype(np.int16) - ref_color).max(axis=2) <= tol

    seeds = np.zeros((h, w), dtype=bool)
    seeds[0, :] = seeds[-1, :] = True
    seeds[:, 0] = seeds[:, -1] = True
    seeds &= close

    if not seeds.any():
        return np.zeros((h, w), dtype=bool)
    return ndimage.binary_propagation(seeds, mask=close)


def quantize(
    img: Image.Image,
    mask: np.ndarray,
    n_colors: int,
    palette: list[Filament] | None = None,
    despeckle_px: int = 6,
    smooth: bool = True,
) -> QuantizedImage:
    """Reduce la imagen a `n_colors`, opcionalmente anclados a filamentos reales."""
    work = img.filter(ImageFilter.MedianFilter(3)) if smooth else img

    if palette:
        index, colors, filaments = _snap_to_palette(np.array(work), mask, palette, n_colors)
    else:
        index, colors = _adaptive_quantize(work, mask, n_colors)
        filaments = None

    if despeckle_px > 0:
        index = _despeckle(index, despeckle_px)

    return QuantizedImage(index=index, colors=colors, filaments=filaments, size=img.size)


def _adaptive_quantize(img: Image.Image, mask: np.ndarray, n: int):
    pal_img = img.quantize(colors=n, method=Image.MEDIANCUT, dither=Image.NONE)
    idx = np.array(pal_img, dtype=np.uint8)
    raw = pal_img.getpalette()[: n * 3]
    colors = [tuple(raw[i * 3 : i * 3 + 3]) for i in range(n)]

    # Reindexar por frecuencia descendente: el índice 0 es siempre el color dominante.
    counts = np.bincount(idx[mask].ravel(), minlength=n)
    order = np.argsort(-counts)
    remap = np.full(256, 255, dtype=np.uint8)
    for new, old in enumerate(order):
        remap[old] = new
    idx = remap[idx]
    colors = [colors[o] for o in order]

    idx = np.where(mask, idx, np.uint8(255))
    return idx, colors


def _snap_to_palette(arr: np.ndarray, mask: np.ndarray, palette: list[Filament], n: int):
    """Asigna cada píxel al filamento más cercano en un espacio perceptual barato.

    Se usan pesos 3:6:1 sobre RGB (aproximación a luminancia) en vez de distancia
    euclídea plana, que confunde amarillos con verdes claros.
    """
    w = np.array([3.0, 6.0, 1.0])
    pal = np.array([f.rgb for f in palette], dtype=np.float64)
    px = arr.reshape(-1, 3).astype(np.float64)

    d = (((px[:, None, :] - pal[None, :, :]) ** 2) * w).sum(axis=2)
    nearest = np.argmin(d, axis=1).reshape(arr.shape[:2]).astype(np.uint8)

    # Quedarse con los n filamentos más usados dentro de la pieza.
    counts = np.bincount(nearest[mask].ravel(), minlength=len(palette))
    keep = list(np.argsort(-counts)[:n])
    keep = [k for k in keep if counts[k] > 0]

    sub = pal[keep]
    d2 = (((px[:, None, :] - sub[None, :, :]) ** 2) * w).sum(axis=2)
    idx = np.argmin(d2, axis=1).reshape(arr.shape[:2]).astype(np.uint8)
    idx = np.where(mask, idx, np.uint8(255))

    filaments = [palette[k] for k in keep]
    return idx, [f.rgb for f in filaments], filaments


def _despeckle(index: np.ndarray, min_px: int) -> np.ndarray:
    """Absorbe componentes conexas más chicas que `min_px` en su vecino dominante."""
    from scipy import ndimage  # dependencia liviana, ya requerida por trimesh

    out = index.copy()
    for val in np.unique(index):
        if val == 255:
            continue
        m = index == val
        lab, n = ndimage.label(m)
        if n == 0:
            continue
        sizes = ndimage.sum(m, lab, range(1, n + 1))
        for i, size in enumerate(sizes, start=1):
            if size >= min_px:
                continue
            blob = lab == i
            grown = ndimage.binary_dilation(blob) & ~blob
            neigh = out[grown]
            neigh = neigh[neigh != 255]
            if neigh.size:
                vals, cnt = np.unique(neigh, return_counts=True)
                out[blob] = vals[np.argmax(cnt)]
    return out


def masks_from_index(q: QuantizedImage) -> dict[int, np.ndarray]:
    return {i: (q.index == i) for i in q.used_indices}
