"""Generate the brand assets: the Enable Banking mark on an iOS-style squircle.

Run from the repository root:

    python scripts/generate_brand.py

Writes `custom_components/enablebanking/brand/`: `icon.svg`, then `icon.png`,
`icon@2x.png`, `logo.png` and `logo@2x.png` rendered from it. `icon.svg` is
build output like the rest — change this script and re-run it, never edit the
SVG by hand, or the next run silently reverts the edit.

Needs an SVG renderer: `rsvg-convert` (brew install librsvg) is preferred
because it is what Home Assistant's own brand tooling uses; `cairosvg` and
ImageMagick are accepted fallbacks. Nothing else here is a dependency.

Two pieces of geometry below are taken from elsewhere rather than invented,
and both are easy to get subtly wrong.

**The mark** comes from Enable Banking's own logo SVG at
https://enablebanking.com/img/logo-animated.3f45d6c4.svg — a stroked square
and four diagonal strokes on a 64x64 viewBox. Their file animates the square
and flies a dot around it; neither belongs in a static icon, so only the
static geometry is kept. One correction: their square is declared at x=24
y=24, centring it on (30,30) while the blades are symmetric about (32,32).
An <animate> overrides that on the first frame so their file never renders it,
but copied verbatim it puts the square visibly off-centre. x=26 centres it,
matching their published logo.

**The squircle** is not a superellipse. Apple's icon mask is a rounded
rectangle with continuous curvature, as produced by
`UIBezierPath(roundedRect:cornerRadius:)`; a superellipse bulges along what
should be flat edges, and side by side the difference is obvious. This is the
construction Figma exposes as "corner smoothing", at the iOS values.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "custom_components" / "enablebanking" / "brand"

SIZE = 512
BACKGROUND = "#000000"
FOREGROUND = "#FFFFFF"

#: Fraction of the canvas the mark spans. iOS icons leave the glyph a similar
#: amount of air; filling the square edge to edge reads as cramped at 32px.
MARK_FRACTION = 0.56

#: Apple's app-icon corner as a fraction of the width, and Figma's equivalent
#: corner-smoothing value.
CORNER_RADIUS_RATIO = 0.2237
CORNER_SMOOTHING = 0.6

#: Home Assistant serves the 256 and its retina double; larger is wasted bytes
#: on every load of the integrations list.
RENDERS = (("icon.png", 256), ("icon@2x.png", 512), ("logo.png", 256), ("logo@2x.png", 512))


def squircle_path(size: float) -> str:
    """SVG path data for a square with continuously curved corners."""
    radius = size * CORNER_RADIUS_RATIO
    budget = size / 2

    # Keep the corner inside the space available to it: without the cap a large
    # radius produces a self-intersecting path rather than a clipped one.
    smoothing = min(CORNER_SMOOTHING, budget / radius - 1)
    p = min((1 + smoothing) * radius, budget)

    arc_measure = math.radians(90 * (1 - smoothing))
    arc = math.sin(arc_measure / 2) * radius * math.sqrt(2)

    angle_alpha = (math.pi / 2 - arc_measure) / 2
    angle_beta = math.radians(45 * smoothing)
    c = radius * math.tan(angle_alpha / 2) * math.cos(angle_beta)
    d = c * math.tan(angle_beta)

    # The straight run into the corner is split 2:1 between the two off-curve
    # control points. That is what ramps curvature smoothly instead of jumping
    # from 0 to 1/r the way a circular corner does.
    b = (p - arc - c - d) / 3
    a = 2 * b

    def f(value: float) -> str:
        return f"{value:.4f}"

    return " ".join(
        [
            f"M {f(size - p)} 0",
            f"c {f(a)} 0 {f(a + b)} 0 {f(a + b + c)} {f(d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(arc)}",
            f"c {f(d)} {f(c)} {f(d)} {f(b + c)} {f(d)} {f(a + b + c)}",
            f"L {f(size)} {f(size - p)}",
            f"c 0 {f(a)} 0 {f(a + b)} {f(-d)} {f(a + b + c)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(arc)}",
            f"c {f(-c)} {f(d)} {f(-(b + c))} {f(d)} {f(-(a + b + c))} {f(d)}",
            f"L {f(p)} {f(size)}",
            f"c {f(-a)} 0 {f(-(a + b))} 0 {f(-(a + b + c))} {f(-d)}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(-arc)} {f(-arc)}",
            f"c {f(-d)} {f(-c)} {f(-d)} {f(-(b + c))} {f(-d)} {f(-(a + b + c))}",
            f"L 0 {f(p)}",
            f"c 0 {f(-a)} 0 {f(-(a + b))} {f(d)} {f(-(a + b + c))}",
            f"a {f(radius)} {f(radius)} 0 0 1 {f(arc)} {f(-arc)}",
            f"c {f(c)} {f(-d)} {f(b + c)} {f(-d)} {f(a + b + c)} {f(-d)}",
            "Z",
        ]
    )


def build_svg() -> str:
    """The icon: the white mark, centred, on a black squircle."""
    scale = SIZE * MARK_FRACTION / 64.0
    offset = (SIZE - 64 * scale) / 2.0
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" '
        f'viewBox="0 0 {SIZE} {SIZE}">\n'
        f"  <!-- Generated by scripts/generate_brand.py. Do not edit; re-run it. -->\n"
        f'  <path d="{squircle_path(SIZE)}" fill="{BACKGROUND}"/>\n'
        f'  <g transform="translate({offset:.4f} {offset:.4f}) scale({scale:.6f})"\n'
        f'     fill="none" stroke="{FOREGROUND}" stroke-width="6">\n'
        f'    <rect x="26" y="26" width="12" height="12"/>\n'
        f'    <path d="M3 3 L20 20 M44 44 L61 61 M61 3 L44 20 M20 44 L3 61"/>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def rasterise(svg_path: Path, out_path: Path, size: int) -> None:
    """Render the SVG at one size, with whichever renderer is installed."""
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size), str(svg_path), "-o", str(out_path)],
            check=True,
        )
        return

    try:
        import cairosvg
    except ImportError:
        pass
    else:
        cairosvg.svg2png(
            url=str(svg_path), write_to=str(out_path), output_width=size, output_height=size
        )
        return

    if shutil.which("magick"):
        subprocess.run(
            [
                "magick",
                "-background",
                "none",
                str(svg_path),
                "-resize",
                f"{size}x{size}",
                str(out_path),
            ],
            check=True,
        )
        return

    sys.exit(
        "No SVG renderer found. Install one of:\n"
        "  brew install librsvg      (rsvg-convert, preferred)\n"
        "  pip install cairosvg\n"
        "  brew install imagemagick"
    )


def main() -> None:
    """Write the SVG source and render every PNG beside it."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = OUT_DIR / "icon.svg"
    svg_path.write_text(build_svg())
    print(svg_path.relative_to(ROOT))

    # logo.* duplicates icon.* deliberately: the wordmark is illegible at the
    # sizes Home Assistant renders these at, and the repository already shipped
    # the two as byte-identical files.
    for name, size in RENDERS:
        target = OUT_DIR / name
        rasterise(svg_path, target, size)
        print(f"{target.relative_to(ROOT)}  {size}x{size}")


if __name__ == "__main__":
    main()
