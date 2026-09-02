"""Genera una imagen de prueba con casos difíciles a propósito.

Incluye deliberadamente: trazo fino (para disparar `thin_features`), un hueco
interior chico (`micro_holes`), un punto suelto minúsculo (`tiny_islands`) y dos
regiones casi tocándose (`micro_gaps`). Sirve para ver el validador en acción.
"""

from PIL import Image, ImageDraw


def make_sample(path: str = "demo_input.png", size: int = 700) -> str:
    img = Image.new("RGB", (size, size), (255, 255, 255))
    d = ImageDraw.Draw(img)

    m = size * 0.06
    # cuerpo principal (color 1)
    d.rounded_rectangle([m, m, size - m, size - m], radius=size * 0.14, fill=(26, 82, 156))

    # anillo (color 2) con hueco interior grande
    cx = cy = size / 2
    r = size * 0.29
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(243, 195, 42))
    d.ellipse([cx - r * 0.52, cy - r * 0.52, cx + r * 0.52, cy + r * 0.52], fill=(26, 82, 156))

    # hueco interior deliberadamente chico → micro_holes
    d.ellipse([cx - 5, cy + r * 0.72, cx + 5, cy + r * 0.72 + 10], fill=(26, 82, 156))

    # trazo fino → thin_features
    d.line([m * 2.2, size * 0.80, size - m * 2.2, size * 0.80], fill=(193, 32, 42), width=3)

    # dos bloques casi tocándose → micro_gaps
    d.rectangle([size * 0.20, size * 0.13, size * 0.44, size * 0.20], fill=(193, 32, 42))
    d.rectangle([size * 0.445, size * 0.13, size * 0.70, size * 0.20], fill=(243, 195, 42))

    # isla minúscula → tiny_islands
    d.ellipse([size * 0.80, size * 0.30, size * 0.80 + 5, size * 0.30 + 5], fill=(193, 32, 42))

    img.save(path)
    return path


if __name__ == "__main__":
    print(make_sample())
