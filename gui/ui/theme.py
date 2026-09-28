"""Colours, fonts and sizes, in one place and scaled to the screen.

The same window has to be readable on a 1280x800 handheld held at arm's length
and on a 4K desktop monitor, so nothing here is a fixed pixel count that was
eyeballed once. Everything is derived from one base size, and the base size
comes from the window.
"""

# Dark on purpose. This runs over a game library, often in Game Mode, often at
# night, and a white panel in that context is a flashbang.
BG = "#0e1117"
PANEL = "#161b26"
PANEL_HI = "#1e2532"
ROW = "#151a24"
ROW_ALT = "#11161f"
ROW_SELECTED = "#1d2a3d"
LINE = "#242c3a"

TEXT = "#e6edf3"
TEXT_DIM = "#8b98a8"
TEXT_FAINT = "#5b6675"

ACCENT = "#4da3ff"
ACCENT_DEEP = "#1f6feb"
GOOD = "#57d364"
WARN = "#e3b341"
BAD = "#f2685c"

FOCUS = "#ffffff"



class Metrics(object):
    """Sizes for one window, derived from its height.

    800 is the Steam Deck. Anything taller gets proportionally bigger text, up
    to a cap, because past a point bigger stops helping and starts wasting the
    list.

    height and width are at 100 percent, and `dpi` is the display's scale
    (core/uiscale). Font sizes are points, which the display scales by
    itself, so they use only the window's scale. Everything else is pixels,
    so `scale`, and every size derived from it, carries the display's scale
    too. Without that, 200 percent meant double-size text in a 100 percent
    layout.
    """

    def __init__(self, height=800, width=1280, dpi=1.0):
        text_scale = max(0.85, min(1.9, height / 800.0))
        self.dpi = dpi
        self.text_scale = text_scale
        self.scale = scale = text_scale * dpi
        self.base = int(round(15 * text_scale))
        self.small = int(round(12.5 * text_scale))
        self.large = int(round(20 * text_scale))
        self.huge = int(round(27 * text_scale))
        self.row_height = int(round(44 * scale))
        self.pad = int(round(12 * scale))
        self.gap = int(round(8 * scale))
        self.nav_width = int(round(190 * scale))
        self.button_height = int(round(40 * scale))
        self.radius = int(round(6 * scale))
        self.width = width
        self.height = height

    def px(self, value):
        """A length given in 100 percent pixels, at this display's scale.

        For the places that place text by its point size (a second line
        drawn `small * 0.95` below the middle): points grow with the
        display, so the offset has to grow with it.
        """
        return value * self.dpi

    def font(self, size="base", bold=False):
        family = FAMILY
        points = {"small": self.small, "base": self.base, "large": self.large,
                  "huge": self.huge}[size]
        return (family, points, "bold") if bold else (family, points)

    def mono(self, size="small"):
        points = {"small": self.small, "base": self.base, "large": self.large,
                  "huge": self.huge}[size]
        return (MONO, points)


# Resolved at import time by ui.app, which knows what tk can actually load.
FAMILY = "TkDefaultFont"
MONO = "TkFixedFont"

PREFERRED_FAMILIES = ("Inter", "Segoe UI Variable Text", "Segoe UI",
                      "SF Pro Text", "Noto Sans", "DejaVu Sans", "Helvetica")
PREFERRED_MONO = ("JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono",
                  "DejaVu Sans Mono", "Menlo", "Courier New")


def pick_fonts(available):
    """Choose the nicest font the system actually has.

    A missing family silently falls back to something Tk picks, which on Linux
    is often a bitmap font that looks two decades old. Asking first is cheap.
    """
    global FAMILY, MONO
    names = set(available or ())
    for family in PREFERRED_FAMILIES:
        if family in names:
            FAMILY = family
            break
    for family in PREFERRED_MONO:
        if family in names:
            MONO = family
            break
    return FAMILY, MONO
