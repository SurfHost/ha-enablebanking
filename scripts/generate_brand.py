"""Generate HACS brand assets (icon + logo) for the Enable Banking integration.

Produces a 256x256 bank-building silhouette on a blue background.
Outputs: custom_components/enablebanking/brand/icon.png and logo.png
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
BG = (25, 118, 210, 255)  # Material Blue 700
FG = (255, 255, 255, 255)  # White
SHADOW = (0, 0, 0, 40)

OUT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "enablebanking" / "brand"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def draw_bank(img: Image.Image) -> None:
    draw = ImageDraw.Draw(img, "RGBA")

    # Layout (all values in pixels within 256x256).
    margin_x = 32
    top = 64          # where the pediment starts
    roof_bottom = 100  # bottom of triangle / top of entablature
    entablature_h = 10
    base_top = 196
    base_h = 16

    # --- Roof (triangular pediment) ---
    left = margin_x
    right = SIZE - margin_x
    apex_x = SIZE // 2
    draw.polygon(
        [(left, roof_bottom), (apex_x, top), (right, roof_bottom)],
        fill=FG,
    )

    # --- Entablature (horizontal band under the roof) ---
    draw.rectangle(
        [(left - 6, roof_bottom), (right + 6, roof_bottom + entablature_h)],
        fill=FG,
    )

    # --- Columns (5 evenly spaced) ---
    column_top = roof_bottom + entablature_h + 6
    column_bottom = base_top - 6
    column_count = 5
    column_width = 20
    span = right - left
    gap = (span - column_count * column_width) / (column_count - 1)
    for i in range(column_count):
        cx_left = left + int(i * (column_width + gap))
        draw.rectangle(
            [(cx_left, column_top), (cx_left + column_width, column_bottom)],
            fill=FG,
        )

    # --- Base / steps ---
    draw.rectangle(
        [(left - 10, base_top), (right + 10, base_top + base_h)],
        fill=FG,
    )
    # A second, wider step underneath for depth.
    draw.rectangle(
        [(left - 18, base_top + base_h), (right + 18, base_top + base_h + 6)],
        fill=FG,
    )


def build(path: Path) -> None:
    img = Image.new("RGBA", (SIZE, SIZE), BG)
    draw_bank(img)
    img.save(path, "PNG", optimize=True)
    print(f"wrote {path}")


if __name__ == "__main__":
    build(OUT_DIR / "icon.png")
    build(OUT_DIR / "logo.png")
