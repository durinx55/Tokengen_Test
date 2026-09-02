"""Interfaz de línea de comandos de TokenGen Community Edition."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import COMMUNITY_MAX_COLORS, DEFAULT_PALETTE, PROFILES, Filament, TokenSpec

BANNER = f"""
  ████ TokenGen {__version__} · Community Edition
  imagen → 3MF multiparte, con validación de imprimibilidad
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tokengen",
        description="Genera tokens/llaveros/medallas multicolor listos para AMS/MMU a partir de una imagen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ejemplos:\n"
            "  tokengen build logo.png --width-mm 45 --colors 3 --keyring\n"
            "  tokengen build arte.jpg --mode relief --profile 0.4-pla-fino -o salida/\n"
            "  tokengen check logo.png --width-mm 20     # sólo valida, no exporta\n"
            "  tokengen demo                             # genera una imagen de prueba y la procesa\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"TokenGen {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("image", help="imagen de entrada (PNG/JPG/WebP)")
        sp.add_argument("-o", "--out", default="out", help="carpeta de salida (por defecto: out/)")
        sp.add_argument("--width-mm", type=float, default=50.0, help="ancho de la pieza en mm")
        sp.add_argument("--colors", type=int, default=3,
                        help=f"cantidad de colores (máx. {COMMUNITY_MAX_COLORS} en Community)")
        sp.add_argument("--mode", choices=["inlay", "relief"], default="inlay",
                        help="inlay = mosaico coplanar · relief = escalonado en Z")
        sp.add_argument("--base-mm", type=float, default=0.8, help="espesor de la placa base")
        sp.add_argument("--color-mm", type=float, default=0.6, help="espesor de la banda de color")
        sp.add_argument("--profile", choices=list(PROFILES), default="0.4-pla-ams")
        sp.add_argument("--palette", metavar="JSON",
                        help='paleta de filamentos: [{"name":"Red","rgb":[193,32,42],"slot":3}]')
        sp.add_argument("--stock-palette", action="store_true",
                        help="usar la paleta Bambu PLA Basic incluida")
        sp.add_argument("--keyring", action="store_true", help="perforar agujero de llavero")
        sp.add_argument("--keyring-d", type=float, default=3.5, help="diámetro del agujero en mm")
        sp.add_argument("--keyring-margin", type=float, default=2.0, help="pared alrededor del agujero")
        sp.add_argument("--simplify-mm", type=float, default=0.05, help="tolerancia de simplificación")
        sp.add_argument("--bleed-mm", type=float, default=0.0, help="sangrado por color (+/-)")
        sp.add_argument("--fillet-mm", type=float, default=0.0, help="redondeo del contorno externo")
        sp.add_argument("--max-px", type=int, default=900, help="resolución de trabajo")
        sp.add_argument("--despeckle", type=int, default=6, help="tamaño mínimo de mancha en px")
        sp.add_argument("--keep-background", action="store_true", help="no recortar el fondo")
        sp.add_argument("--json", action="store_true", help="salida en JSON, sin texto")

    b = sub.add_parser("build", help="genera el 3MF multiparte")
    common(b)
    b.add_argument("--no-preview", action="store_true", help="no generar preview PNG/HTML")
    b.add_argument("--strict", action="store_true", help="salir con código 1 si hay errores")

    c = sub.add_parser("check", help="valida imprimibilidad sin exportar")
    common(c)

    sub.add_parser("demo", help="genera una imagen de ejemplo y la procesa entera")
    sub.add_parser("profiles", help="lista los perfiles de impresión disponibles")

    return p


def _spec_from_args(a) -> TokenSpec:
    palette = None
    if a.palette:
        raw = json.loads(open(a.palette, encoding="utf-8").read()) if os.path.exists(a.palette) \
            else json.loads(a.palette)
        palette = [Filament(f["name"], tuple(f["rgb"]), f.get("slot", i + 1))
                   for i, f in enumerate(raw)]
    elif a.stock_palette:
        palette = DEFAULT_PALETTE

    return TokenSpec(
        width_mm=a.width_mm,
        base_mm=a.base_mm,
        color_mm=a.color_mm,
        mode=a.mode,
        colors=a.colors,
        profile=PROFILES[a.profile],
        palette=palette,
        keyring=(a.keyring_d, a.keyring_margin) if a.keyring else None,
        simplify_mm=a.simplify_mm,
        bleed_mm=a.bleed_mm,
        fillet_mm=a.fillet_mm,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "profiles":
        for key, p in PROFILES.items():
            print(f"  {key:<16} {p.name}")
            print(f"{'':18}boquilla {p.nozzle_mm} mm · capa {p.layer_height_mm} mm · "
                  f"detalle mín. {p.min_feature_mm} mm · hueco mín. ⌀{p.min_hole_mm} mm")
        return 0

    if args.cmd == "demo":
        from examples.make_sample import make_sample  # type: ignore
        path = make_sample("demo_input.png")
        argv2 = ["build", path, "-o", "out", "--width-mm", "45", "--colors", "3",
                 "--keyring", "--stock-palette"]
        return main(argv2)

    from .pipeline import run

    quiet = getattr(args, "json", False)
    log = (lambda *_: None) if quiet else print
    if not quiet:
        print(BANNER)

    spec = _spec_from_args(args)

    if args.cmd == "check":
        result = run(args.image, args.out, spec, max_px=args.max_px,
                     despeckle_px=args.despeckle,
                     drop_background=not args.keep_background,
                     write_preview=False, on_log=log)
        if quiet:
            print(json.dumps(result.report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print("\n" + result.report.render())
        return 0 if result.report.printable else 1

    result = run(args.image, args.out, spec, max_px=args.max_px,
                 despeckle_px=args.despeckle,
                 drop_background=not args.keep_background,
                 write_preview=not args.no_preview, on_log=log)

    if quiet:
        print(json.dumps(
            {"outputs": result.outputs, "validation": result.report.to_dict()},
            indent=2, ensure_ascii=False))
    else:
        print("\n" + result.report.render())
        print("\n  Archivos:")
        for k, v in result.outputs.items():
            print(f"   · {k:<12} {v}")
        total = sum(result.timings.values())
        print(f"\n  Tiempo total: {total:.2f} s")

    if getattr(args, "strict", False) and not result.report.printable:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
