"""Generate assets/recoder.ico — dark rounded square with a record dot.

Run once (or after tweaking): `uv run python scripts/make_icon.py`.
The .ico is committed so shortcut installs never need to regenerate it.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "recoder.ico"

BG = (23, 26, 33, 255)        # panel dark (matches the UI)
RING = (91, 141, 239, 255)    # accent blue
DOT = (229, 72, 77, 255)      # record red


def draw(size: int) -> Image.Image:
    s = size * 4  # supersample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = s // 5
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=BG)
    # outer ring
    ring_w = max(s // 16, 4)
    pad = s // 5
    d.ellipse((pad, pad, s - pad, s - pad), outline=RING, width=ring_w)
    # record dot
    dot_pad = s * 9 // 25
    d.ellipse((dot_pad, dot_pad, s - dot_pad, s - dot_pad), fill=DOT)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [draw(sz) for sz in sizes]
    images[-1].save(OUT, format="ICO", sizes=[(sz, sz) for sz in sizes])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
