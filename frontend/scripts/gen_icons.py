"""Generate placeholder PWA icons for Gilbert.

Solid dark background (#0a0a0a, matching the SPA canvas) with a
centered white "G" glyph. The maskable variant insets the glyph to
~58% of the canvas so OS masking can crop to a circle/squircle
without clipping the glyph.

Usage:

    uv run python frontend/public/icons/_gen.py frontend/public/icons
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


BG = (10, 10, 10)  # #0a0a0a — matches the .dark canvas
FG = (255, 255, 255)


def find_bold_font() -> str | None:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def load_font(size: int) -> ImageFont.ImageFont:
    path = find_bold_font()
    if path is not None:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw_g(size: int, glyph_scale: float, out: Path) -> None:
    """Render a centered white G on a dark canvas."""
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)

    target_h = int(size * glyph_scale)
    font_px = target_h
    font = load_font(font_px)
    for _ in range(20):
        bbox = draw.textbbox((0, 0), "G", font=font)
        glyph_w = bbox[2] - bbox[0]
        glyph_h = bbox[3] - bbox[1]
        if glyph_h <= target_h and glyph_w <= int(size * glyph_scale):
            break
        font_px = int(font_px * 0.92)
        font = load_font(font_px)

    bbox = draw.textbbox((0, 0), "G", font=font)
    glyph_w = bbox[2] - bbox[0]
    glyph_h = bbox[3] - bbox[1]
    x = (size - glyph_w) // 2 - bbox[0]
    y = (size - glyph_h) // 2 - bbox[1]
    draw.text((x, y), "G", fill=FG, font=font)
    img.save(out, format="PNG", optimize=True)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: _gen.py <out_dir>", file=sys.stderr)
        sys.exit(1)
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    draw_g(180, 0.72, out_dir / "gilbert-180.png")
    draw_g(192, 0.72, out_dir / "gilbert-192.png")
    draw_g(512, 0.72, out_dir / "gilbert-512.png")
    draw_g(512, 0.58, out_dir / "gilbert-512-maskable.png")

    for name in (
        "gilbert-180.png",
        "gilbert-192.png",
        "gilbert-512.png",
        "gilbert-512-maskable.png",
    ):
        p = out_dir / name
        print(f"wrote {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
