"""A gamepad, turned into the key presses the window already understands.

In Game Mode there is no keyboard and no mouse. Steam maps a controller to
SOMETHING for a non-Steam window, but what it maps depends on the layout the
player has applied, and a save tool that only works under one layout is a save
tool that does not work.

So the pad is read directly, the same way the engine reads it, and every press
is turned into the key the window binds anyway. Nothing above this file knows a
pad exists.

    d-pad / left stick   Up Down Left Right
    A                    Return
    B                    Escape
    X                    space        (pick the row under the cursor)
    Y                    a            (select everything shown)
    LB / RB              Prior / Next (a page at a time)
    Start                F5           (refresh)

Absent hardware, an unreadable device and a platform with no support all end
the same way: the thread stops and the window carries on as a normal window.
"""

import glob
import os
import struct
import sys
import threading
import time

POLL_SECONDS = 1 / 60.0
REPEAT_FIRST = 0.42
REPEAT_NEXT = 0.11
RESCAN_SECONDS = 4.0

# Directions repeat while held. Buttons fire once per press.
REPEATING = {"Up", "Down", "Left", "Right", "Prior", "Next"}

# js event types
JS_BUTTON = 0x01
JS_AXIS = 0x02
JS_INIT = 0x80
JS_EVENT = struct.Struct("IhBB")

AXIS_DEADZONE = 18000

# The standard xpad button order, which is what SteamOS presents.
JS_BUTTONS = {0: "Return", 1: "Escape", 2: "space", 3: "a",
              4: "Prior", 5: "Next", 7: "F5"}

XINPUT_KEYS = (
    (0x0001, "Up"), (0x0002, "Down"), (0x0004, "Left"), (0x0008, "Right"),
    (0x0010, "F5"), (0x0100, "Prior"), (0x0200, "Next"),
    (0x1000, "Return"), (0x2000, "Escape"), (0x4000, "space"), (0x8000, "a"),
)


def attach(window):
    """Start reading a pad for this window. Returns the reader, or None."""
    reader = _reader_for_platform()
    if reader is None:
        return None
    pump = Pump(window, reader)
    pump.start()
    return pump


def _reader_for_platform():
    if sys.platform == "win32":
        return XInputReader()
    if sys.platform.startswith("linux"):
        return JoystickReader()
    return None


class Pump(object):
    """Polls a reader on a thread and posts keys on the Tk thread."""

    def __init__(self, window, reader):
        self.window = window
        self.reader = reader
        self.thread = None
        self.stop = threading.Event()
        self._held = {}

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()

    def _run(self):
        last_scan = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if now - last_scan > RESCAN_SECONDS:
                last_scan = now
                try:
                    self.reader.rescan()
                except Exception:
                    pass
            try:
                pressed = self.reader.poll()
            except Exception:
                pressed = set()
            self._emit(pressed, now)
            time.sleep(POLL_SECONDS)

    def _emit(self, pressed, now):
        for key in list(self._held):
            if key not in pressed:
                self._held.pop(key, None)
        for key in pressed:
            due = self._held.get(key)
            if due is None:
                self._held[key] = now + REPEAT_FIRST
                self._send(key)
            elif key in REPEATING and now >= due:
                self._held[key] = now + REPEAT_NEXT
                self._send(key)

    def _send(self, key):
        window = self.window

        def fire():
            try:
                widget = window.focus_get() or window
                widget.event_generate("<%s>" % key)
            except Exception:
                pass
        try:
            window.after(0, fire)
        except Exception:
            self.stop.set()


class JoystickReader(object):
    """/dev/input/js*, the API that needs no permissions beyond read.

    Matching by name matters: on a Steam Deck js0 is "Mouse passthrough" and
    the pad is js1. The engine learned that the hard way, and this follows it.
    """

    NAME_HINTS = ("pad", "controller", "gamepad", "joystick", "xbox",
                  "playstation", "steam", "nintendo", "dual")

    def __init__(self):
        self.devices = {}
        self.buttons = {}
        self.axes = {}
        self.rescan()

    def rescan(self):
        for path in sorted(glob.glob("/dev/input/js*")):
            if path in self.devices:
                continue
            try:
                handle = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            if not self._looks_like_a_pad(handle):
                os.close(handle)
                continue
            self.devices[path] = handle
            self.buttons[path] = set()
            self.axes[path] = {}

    def _looks_like_a_pad(self, handle):
        name = self._name(handle).lower()
        if not name:
            return True
        return any(hint in name for hint in self.NAME_HINTS)

    def _name(self, handle):
        try:
            import fcntl
            buffer = bytearray(128)
            # JSIOCGNAME(len)
            request = 0x80006A13 + (len(buffer) << 16)
            fcntl.ioctl(handle, request, buffer)
            return bytes(buffer).split(b"\x00")[0].decode("utf-8", "replace")
        except Exception:
            return ""

    def poll(self):
        pressed = set()
        for path, handle in list(self.devices.items()):
            self._drain(path, handle)
            for number in self.buttons.get(path, ()):
                key = JS_BUTTONS.get(number)
                if key:
                    pressed.add(key)
            for number, value in (self.axes.get(path) or {}).items():
                key = _axis_key(number, value)
                if key:
                    pressed.add(key)
        return pressed

    def _drain(self, path, handle):
        while True:
            try:
                data = os.read(handle, JS_EVENT.size)
            except BlockingIOError:
                return
            except OSError:
                self._forget(path, handle)
                return
            if not data or len(data) < JS_EVENT.size:
                return
            _time, value, kind, number = JS_EVENT.unpack(data)
            kind &= ~JS_INIT
            if kind == JS_BUTTON:
                held = self.buttons.setdefault(path, set())
                if value:
                    held.add(number)
                else:
                    held.discard(number)
            elif kind == JS_AXIS:
                self.axes.setdefault(path, {})[number] = value

    def _forget(self, path, handle):
        self.devices.pop(path, None)
        self.buttons.pop(path, None)
        self.axes.pop(path, None)
        try:
            os.close(handle)
        except OSError:
            pass


def _axis_key(number, value):
    """The direction an axis is pushed, if it is pushed far enough.

    Axes 0 and 1 are the left stick, 6 and 7 are the d-pad on an xpad. Both are
    treated the same, because a player expects both to move a menu.
    """
    if number in (0, 6):
        if value < -AXIS_DEADZONE:
            return "Left"
        if value > AXIS_DEADZONE:
            return "Right"
    elif number in (1, 7):
        if value < -AXIS_DEADZONE:
            return "Up"
        if value > AXIS_DEADZONE:
            return "Down"
    return None


class XInputReader(object):
    """XInput, which is how Windows reports a modern pad."""

    def __init__(self):
        self.library = None
        self.rescan()

    def rescan(self):
        if self.library is not None:
            return
        import ctypes
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.library = ctypes.windll.LoadLibrary(name)
                return
            except OSError:
                continue

    def poll(self):
        if self.library is None:
            return set()
        import ctypes

        class Pad(ctypes.Structure):
            _fields_ = [("wButtons", ctypes.c_ushort),
                        ("bLeftTrigger", ctypes.c_ubyte),
                        ("bRightTrigger", ctypes.c_ubyte),
                        ("sThumbLX", ctypes.c_short),
                        ("sThumbLY", ctypes.c_short),
                        ("sThumbRX", ctypes.c_short),
                        ("sThumbRY", ctypes.c_short)]

        class State(ctypes.Structure):
            _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", Pad)]

        pressed = set()
        state = State()
        for index in range(4):
            if self.library.XInputGetState(index, ctypes.byref(state)) != 0:
                continue
            held = state.Gamepad.wButtons
            for mask, key in XINPUT_KEYS:
                if held & mask:
                    pressed.add(key)
            x = state.Gamepad.sThumbLX
            y = state.Gamepad.sThumbLY
            if x < -AXIS_DEADZONE:
                pressed.add("Left")
            elif x > AXIS_DEADZONE:
                pressed.add("Right")
            if y < -AXIS_DEADZONE:
                pressed.add("Down")
            elif y > AXIS_DEADZONE:
                pressed.add("Up")
        return pressed
