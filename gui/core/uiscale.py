"""How much bigger than 100 percent the window has to draw.

Fonts are given in points, so they grow with the display's scale by
themselves. Every other size in the window (row heights, padding, the nav
rail, the window itself) is a pixel count, and a pixel count does not grow.
On a Linux laptop (GNOME at 200 percent, the window under Xwayland) that gave
double-size text in a 100 percent layout: a tiny window, clipped buttons and
columns on top of each other. This works out the factor the fonts get, so the
pixel sizes can be multiplied by the same one.

Where the fonts' scale comes from:

- macOS (aqua): Tk measures in points, and the system turns points into
  Retina pixels for the whole window. Nothing to do: the factor is 1.
- Windows: Tk's own scaling follows the system DPI when the process is DPI
  aware (96 dpi is 100 percent). A process that is not DPI aware sees 96
  and Windows stretches the whole window instead, which also needs 1.
- X11: Tk draws text through Xft, and Xft sizes it from the Xft.dpi
  resource, which GNOME and KDE set from the desktop's scale (192 at 200
  percent). Tk's own scaling reads the X server's idea of the screen's size
  instead, and under Xwayland that stays at 96 dpi whatever the scale, so
  it cannot be trusted for this. Xft.dpi when it is set, else Tk's scaling.

BLOCKSLOT_SCALE=1.5 overrides all of it, for a desktop that gets it wrong.
"""

import os
import re

BASE_DPI = 96.0
ENV = "BLOCKSLOT_SCALE"
LOWEST = 1.0
HIGHEST = 4.0
# Below this it is 100 percent (the smallest scale a desktop offers is 125).
SNAP = 1.1

_XFT_DPI = re.compile(r"^Xft\.dpi:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.MULTILINE)


def _clamp(value):
    return max(LOWEST, min(HIGHEST, value))


def factor(windowing, tk_scaling, xft_dpi=None, override=None):
    """The multiplier for pixel sizes, 1.0 at 100 percent.

    windowing is Tk's windowing system ("x11", "win32", "aqua"), tk_scaling
    what `tk scaling` says (pixels per point), xft_dpi the X resource when
    there is one. override is BLOCKSLOT_SCALE's text.
    """
    if override:
        try:
            return _clamp(float(override))
        except ValueError:
            pass
    if windowing == "aqua":
        return 1.0
    if windowing == "x11" and xft_dpi:
        dpi = float(xft_dpi)
    else:
        try:
            dpi = float(tk_scaling) * 72.0
        except (TypeError, ValueError):
            return 1.0
    value = round(dpi / BASE_DPI, 2)
    if value < SNAP:
        # An X server that reports the monitor's real size says 92 or 100
        # dpi at 100 percent. That is not a scale anyone chose, and the
        # layout stays exactly as it has always been.
        return 1.0
    return _clamp(value)


def parse_xft_dpi(resources):
    """Xft.dpi from an X resource string (what `xrdb -query` prints)."""
    found = _XFT_DPI.search(resources or "")
    if not found:
        return None
    value = float(found.group(1))
    return value if value > 0 else None


def read_xft_dpi(display=None):
    """Xft.dpi from the X server Tk is drawing on, or None.

    Read with libX11 through ctypes: the same RESOURCE_MANAGER property that
    Xft reads, from the display DISPLAY names. Standard library only, and
    nothing is started. Any failure means "not known".
    """
    try:
        import ctypes
        import ctypes.util
        x11 = None
        for candidate in ("libX11.so.6", ctypes.util.find_library("X11")):
            if not candidate:
                continue
            try:
                x11 = ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        if x11 is None:
            return None
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XResourceManagerString.restype = ctypes.c_char_p
        x11.XResourceManagerString.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        target = display if display is not None else os.environ.get("DISPLAY")
        handle = x11.XOpenDisplay(target.encode() if target else None)
        if not handle:
            return None
        try:
            text = x11.XResourceManagerString(handle)
        finally:
            x11.XCloseDisplay(handle)
        return parse_xft_dpi(text.decode("utf-8", "replace") if text else "")
    except Exception:
        return None


def for_window(root, environ=None, xft_reader=read_xft_dpi):
    """The factor for an open Tk window."""
    environ = os.environ if environ is None else environ
    windowing = str(root.tk.call("tk", "windowingsystem"))
    tk_scaling = float(root.tk.call("tk", "scaling"))
    xft_dpi = None
    if windowing == "x11":
        xft_dpi = xft_reader(root.winfo_screen() or None)
    return factor(windowing, tk_scaling, xft_dpi, environ.get(ENV))


def window_size(size, factor_, screen, fullscreen=False):
    """(width, height) in real pixels for a size asked for at 100 percent.

    Grown, but not past the screen: a 1280x800 window at 200 percent wants
    2560x1600, which a 2880x1620 panel cannot show with its top bar. It is
    shrunk evenly instead, so the layout keeps its shape, and never below
    what was asked for. At 100 percent the size is exactly what was asked.
    """
    screen_w, screen_h = screen
    if fullscreen:
        return screen_w, screen_h
    if factor_ <= 1.0:
        return int(size[0]), int(size[1])
    width, height = size[0] * factor_, size[1] * factor_
    room_w, room_h = screen_w * 0.95, screen_h * 0.90
    if screen_w > 0 and screen_h > 0 and (width > room_w or height > room_h):
        shrink = max(1.0 / factor_, min(room_w / width, room_h / height))
        width, height = width * shrink, height * shrink
    return int(round(width)), int(round(height))
