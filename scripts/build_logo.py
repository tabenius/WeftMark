#!/usr/bin/env python3
"""Render canonical WeftMark SVG logo variants to PNG and WebP.

The SVG files remain the source of truth. Raster artifacts are generated at a
requested square size (512 px by default) using CairoSVG and Pillow.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from io import BytesIO

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
GENERATED = ASSETS / "generated"

VARIANTS = {
    "weftmark": ASSETS / "weftmark.svg",
    "weftmark-on-black": ASSETS / "weftmark-on-black.svg",
}


def render_variant(name: str, svg_path: Path, size: int) -> tuple[Path, Path]:
    GENERATED.mkdir(parents=True, exist_ok=True)
    png_path = GENERATED / f"{name}-{size}.png"
    webp_path = GENERATED / f"{name}-{size}.webp"

    png_bytes = cairosvg.svg2png(
        url=str(svg_path), output_width=size, output_height=size
    )
    png_path.write_bytes(png_bytes)

    with Image.open(BytesIO(png_bytes)) as image:
        image.convert("RGBA").save(webp_path, "WEBP", lossless=True, method=6)

    return png_path, webp_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=512, help="square raster size")
    args = parser.parse_args()
    if args.size < 16:
        parser.error("--size must be at least 16")

    for name, svg_path in VARIANTS.items():
        if not svg_path.exists():
            raise SystemExit(f"missing SVG source: {svg_path}")
        png_path, webp_path = render_variant(name, svg_path, args.size)
        print(png_path.relative_to(ROOT))
        print(webp_path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
