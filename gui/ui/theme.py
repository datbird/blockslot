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
    """

    def __init__(self, height=800, width=1280):
        scale = max(0.85, min(1.9, height / 800.0))
        self.scale = scale
        self.base = int(round(15 * scale))
        self.small = int(round(12.5 * scale))
        self.large = int(round(20 * scale))
        self.huge = int(round(27 * scale))
        self.row_height = int(round(44 * scale))
        self.pad = int(round(12 * scale))
        self.gap = int(round(8 * scale))
        self.nav_width = int(round(190 * scale))
        self.button_height = int(round(40 * scale))
        self.radius = int(round(6 * scale))
        self.width = width
        self.height = height

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
