"""Draw Splicer's icon: a strip of film cut in two, in the app's accent blue.

Run this only to change the icon; the result is committed.

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "splicer.ico"
SIZES = (256, 128, 64, 48, 32, 16)
N = 1024  # draw big, downscale smooth

BG = (43, 45, 49, 255)
ACCENT = (88, 101, 242, 255)
HOLE = (43, 45, 49, 255)
LIGHT = (230, 230, 230, 255)


def strip(d: ImageDraw.ImageDraw, x0: float, x1: float, top: float, bottom: float) -> None:
    d.rounded_rectangle((x0, top, x1, bottom), radius=N * 0.03, fill=ACCENT)
    hole, gap = N * 0.045, N * 0.03
    x = x0 + gap
    while x + hole <= x1 - gap / 2:
        for y in (top + gap * 0.9, bottom - gap * 0.9 - hole):
            d.rounded_rectangle((x, y, x + hole, y + hole), radius=N * 0.008, fill=HOLE)
        x += hole + gap


def main() -> int:
    image = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((32, 32, N - 32, N - 32), radius=200, fill=BG)
    top, bottom = N * 0.33, N * 0.67
    # Two pieces of film with a clean gap between them, nudged apart
    strip(d, N * 0.12, N * 0.45, top - N * 0.03, bottom - N * 0.03)
    strip(d, N * 0.55, N * 0.88, top + N * 0.03, bottom + N * 0.03)
    d.line((N * 0.5, N * 0.18, N * 0.5, N * 0.82), fill=LIGHT, width=int(N * 0.025))
    OUT.parent.mkdir(exist_ok=True)
    image.resize((256, 256), Image.LANCZOS).save(OUT, sizes=[(s, s) for s in SIZES])
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
