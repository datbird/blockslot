#!/usr/bin/env python3
"""Render Blockslot's icons from the SVG sources in assets/.

    python3 tools/make_icon.py

A development tool, not part of the app: it needs cairosvg and Pillow, which
nothing that ships depends on. The SVGs are the source of truth:

    assets/icon.svg        the app icon (the designer's, arrowheads fixed)
    assets/icon-small.svg  the same mark simplified for 16 to 32 pixels
    assets/icon-tray.svg   the small mark with no tile, for the tray

It writes:

    assets/blockslot.ico       the exe and window icon, every Windows size
    assets/blockslot-tray.ico  the notification area icon
    assets/blockslot.png       256 px, for the window and the Deck
    assets/blockslot-1024.png  the full-size mark, for stores and docs
"""

import io
from pathlib import Path

import cairosvg
from PIL import Image

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SMALL = 32          # at or below this, the simplified mark is used


def render(svg, size):
    png = cairosvg.svg2png(url=str(ASSETS / svg), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def write_ico(path, images):
    """One .ico holding each size, so Windows never scales a 256 down to 16."""
    biggest = images[-1]
    biggest.save(str(path), format="ICO", sizes=[im.size for im in images],
                 append_images=images[:-1])


def main():
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    app = [render("icon-small.svg" if s <= SMALL else "icon.svg", s) for s in sizes]
    write_ico(ASSETS / "blockslot.ico", app)
    tray_sizes = (16, 20, 24, 32, 40, 48, 64)
    tray = [render("icon-tray.svg", s) for s in tray_sizes]
    write_ico(ASSETS / "blockslot-tray.ico", tray)
    render("icon.svg", 256).save(str(ASSETS / "blockslot.png"))
    render("icon.svg", 1024).save(str(ASSETS / "blockslot-1024.png"))
    for name in ("blockslot.ico", "blockslot-tray.ico", "blockslot.png", "blockslot-1024.png"):
        print("wrote", ASSETS / name)


if __name__ == "__main__":
    main()
