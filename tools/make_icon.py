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
    assets/blockslot.icns      the Mac app's icon, every size Finder asks for

The .icns is written here rather than with Apple's iconutil so that it can be
made on any machine and committed: the Mac build then needs nothing but
PyInstaller. It is the plain container, each size a PNG, which is what
iconutil itself writes.
"""

import io
import struct
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


# (type, pixels) for each PNG an .icns carries; the @2x types are the same
# pixels as the next size up, which Finder uses on a Retina screen.
ICNS_TYPES = (
    (b"icp4", 16), (b"icp5", 32), (b"ic11", 32), (b"icp6", 64),
    (b"ic12", 64), (b"ic07", 128), (b"ic13", 256), (b"ic08", 256),
    (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024),
)


def png_bytes(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def write_icns(path):
    """An .icns of PNGs: 'icns', its length, then (type, length, data) each."""
    cache = {}
    body = b""
    for kind, size in ICNS_TYPES:
        if size not in cache:
            cache[size] = png_bytes(
                render("icon-small.svg" if size <= SMALL else "icon.svg", size))
        data = cache[size]
        body += kind + struct.pack(">I", len(data) + 8) + data
    with open(str(path), "wb") as handle:
        handle.write(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main():
    sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
    app = [render("icon-small.svg" if s <= SMALL else "icon.svg", s) for s in sizes]
    write_ico(ASSETS / "blockslot.ico", app)
    tray_sizes = (16, 20, 24, 32, 40, 48, 64)
    tray = [render("icon-tray.svg", s) for s in tray_sizes]
    write_ico(ASSETS / "blockslot-tray.ico", tray)
    render("icon.svg", 256).save(str(ASSETS / "blockslot.png"))
    render("icon.svg", 1024).save(str(ASSETS / "blockslot-1024.png"))
    write_icns(ASSETS / "blockslot.icns")
    for name in ("blockslot.ico", "blockslot-tray.ico", "blockslot.png",
                 "blockslot-1024.png", "blockslot.icns"):
        print("wrote", ASSETS / name)


if __name__ == "__main__":
    main()
