"""The Windows host for the Blockslot daemon: `Blockslot.exe --daemon`.

The daemon (engine/slotd.py) owns the store and the upload queue. On Windows it
runs at login with no window, so the notification-area icon is the only way a
person can see it: whether every save is up, whether one is waiting for a
network, or whether the store has refused the credentials. See "The daemon" in
docs/superpowers/specs/2026-09-24-store-and-daemon-design.md.

Two halves:

- Plain functions that turn Daemon.status() into words, menus and balloons.
  They run and are tested on every platform.
- `Tray`, the Win32 icon itself, with ctypes and nothing else. Its window,
  its message loop and every Shell_NotifyIconW call live on one thread of
  its own, because a window belongs to the thread that created it.

Linux and macOS get no tray: `--daemon` there runs the daemon and blocks.

Standard library only; Python 3.9.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from gui.core import paths  # noqa: E402

POLL_SECONDS = 3
TIP_LIMIT = 127         # szTip is 128 WCHARs with the terminator
INFO_LIMIT = 255
INFO_TITLE_LIMIT = 63

IDLE = "idle"
UPLOADING = "uploading"
WAITING = "waiting"
OFFLINE = "offline"
REFUSED = "refused"
PAUSED = "paused"

MENU_OPEN = 1
MENU_UPLOAD = 2
MENU_PAUSE = 3
MENU_QUIT = 4
SEPARATOR = 0


# ------------------------------------------------------------------ the words


def _saves(count):
    return "1 save" if count == 1 else "%d saves" % count


def state_of(status):
    """Which one thing the icon says. The worst news wins.

    A refusal needs a person (a token expired), so it beats everything. Paused
    beats uploading because the person chose it and should see it stuck.
    """
    status = status or {}
    queued = status.get("queued") or []
    error = status.get("error") or {}
    if error.get("kind") == "refused":
        return REFUSED
    if status.get("paused"):
        return PAUSED
    if not queued:
        return IDLE
    if any(item.get("progress") for item in queued):
        return UPLOADING
    if error.get("kind") == "offline":
        return OFFLINE
    return WAITING


def tooltip(status):
    """The hover text. Short, because Windows cuts it at 127 characters."""
    status = status or {}
    queued = status.get("queued") or []
    state = state_of(status)
    if state == REFUSED:
        text = "BlockSlot: uploads refused. %s" % (status["error"].get("message") or "")
    elif state == PAUSED:
        text = "BlockSlot: uploads paused"
        if queued:
            text += ", %s waiting" % _saves(len(queued))
    elif state == IDLE:
        text = "BlockSlot: all saves uploaded"
    elif state == UPLOADING:
        text = "BlockSlot: %s" % uploading_text(queued)
    elif state == OFFLINE:
        text = "BlockSlot: %s queued, offline" % _saves(len(queued))
    else:
        text = "BlockSlot: %s waiting to upload" % _saves(len(queued))
    return clip(text.strip(), TIP_LIMIT)


def uploading_text(queued):
    """"uploading 2 of 3 (40%)": which save of the queue, and how far in."""
    for number, item in enumerate(queued, 1):
        progress = item.get("progress")
        if progress:
            done, total = progress[0], progress[1]
            text = "uploading %d of %d" % (number, len(queued))
            if total:
                text += " (%d%%)" % int(100 * done / total)
            return text
    return "uploading"


def clip(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + "..."


def menu_items(paused):
    """The right-click menu, as (id, label). id 0 is a separator."""
    return [
        (MENU_OPEN, "Open BlockSlot"),
        (SEPARATOR, ""),
        (MENU_UPLOAD, "Upload now"),
        (MENU_PAUSE, "Resume uploads" if paused else "Pause uploads"),
        (SEPARATOR, ""),
        (MENU_QUIT, "Quit"),
    ]


class Watcher(object):
    """Turns a run of status() answers into the few moments worth a balloon.

    A balloon is an interruption, so there are two: saves that were stuck
    offline have now gone up (the player may be waiting to switch device), and
    the store refused the uploads (nothing moves until a person acts).
    Everything else lives in the tooltip.
    """

    def __init__(self):
        self.stranded = set()       # snap ids seen queued while offline
        self.refusal = None         # the message last shown for a refusal

    def update(self, status):
        """Returns a list of (title, text, kind), kind "info" or "error"."""
        status = status or {}
        queued = set(item.get("id") for item in status.get("queued") or [])
        error = status.get("error") or {}
        out = []
        if error.get("kind") == "offline":
            self.stranded |= queued
        # A snapshot leaves the queue only when it is committed, so one that
        # was stranded and is gone now is on the store, whatever came after.
        landed = self.stranded - queued
        if landed:
            out.append(("Saves uploaded",
                        clip("%s that waited for a connection %s now on %s."
                             % (_saves(len(landed)),
                                "is" if len(landed) == 1 else "are",
                                status.get("store") or "the store"),
                             INFO_LIMIT),
                        "info"))
            self.stranded -= landed
        if error.get("kind") == "refused":
            message = error.get("message") or "no reason given"
            if message != self.refusal:
                out.append(("BlockSlot uploads refused",
                            clip("%s refused the upload: %s. Saves stay queued "
                                 "on this device." % (status.get("store") or "The store",
                                                     message.rstrip(".")),
                                 INFO_LIMIT),
                            "error"))
            self.refusal = message
        else:
            self.refusal = None
        return out


def gui_command(frozen=None, executable=None, script=None, python=None,
                config=None):
    """What "Open BlockSlot" runs: the same program, without --daemon."""
    frozen = paths.is_frozen() if frozen is None else frozen
    if frozen:
        argv = [str(executable or sys.executable)]
    else:
        argv = [str(python or paths.python_for_launch()),
                str(script or HERE / "blockslot.py")]
    if config:
        argv += ["--config", str(config)]
    return argv


def open_gui(config=None):
    """Start the window as its own process, so closing it leaves the tray."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if paths.is_windows():
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(gui_command(config=config), **kwargs)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ the host


def load_slotd():
    """The daemon module that ships with this copy of Blockslot.

    The exe carries it under engine/ in its bundle; a checkout has it beside
    gui/. Put on sys.path rather than copied, so slotd finds slotstore next
    to it the same way it does when run on its own.
    """
    found = paths.resource("engine/slotd.py")
    folder = found.parent if found else HERE.parent / "engine"
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    import slotd
    return slotd


def _modules_slotd_needs():
    """Never called. It is here for PyInstaller, which finds modules by
    reading imports in the code it builds. slotd and slotstore ship as data
    files, so nothing it reads imports http.server or gzip, and the exe
    would start the daemon only to fail on its first import.
    """
    import base64, datetime, errno, gzip, hashlib, hmac, http.server  # noqa
    import io, re, secrets, shlex, socket, tempfile  # noqa
    import urllib.error, urllib.parse, urllib.request  # noqa
    import concurrent.futures, ssl  # noqa


class ServiceProxy(object):
    """The Blockslot service, seen through its HTTP face, shaped like a Daemon.

    TrayHost reads `paused`, `stopping` and status() and sets `paused` and
    `kick`; this answers those over localhost so one TrayHost serves both a
    daemon in this process and the service in session 0.
    """

    stopping = False

    def __init__(self, find_client):
        self.find_client = find_client
        self._paused = False
        self.kick = self
        self.lock = threading.Lock()

    def _client(self):
        return self.find_client()

    def status(self):
        client = self._client()
        if client is None:
            return {"error": {"kind": "offline",
                              "message": "the BlockSlot service is not running"}}
        answer = client.status()
        self._paused = bool(answer.get("paused"))
        return answer

    def set(self):
        """kick.set(): ask the service to upload now."""
        client = self._client()
        if client is not None:
            client.kick()

    @property
    def paused(self):
        return self._paused

    @paused.setter
    def paused(self, value):
        client = self._client()
        if client is not None:
            self._paused = bool(client.pause(value).get("paused"))


def run_tray(config=None):
    """`--tray`: the icon for the Blockslot service, in the user's session.

    The service holds the daemon in session 0, where no icon can be shown.
    This process holds only the icon. When no service is installed it falls
    back to --daemon, which hosts the daemon here, so a PC without admin
    rights still gets both.
    """
    slotd = load_slotd()
    settings, _device = slotd.load_settings(config)
    if not settings:
        return 0
    if not settings.get("service"):
        return run_daemon(config)
    state_dir = settings.get("state_dir") or slotd.default_state_dir()
    proxy = ServiceProxy(lambda: slotd._client_from_info(state_dir))
    host = TrayHost(proxy, config=config)
    host.run()
    return 0


def run_daemon(config=None, with_tray=None):
    """`--daemon`. Returns the exit code.

    Exits 0 at once, and quietly, when there is nothing to do: no store set
    up, or a daemon already answering. Login starts this every time, so
    neither is an error.
    """
    slotd = load_slotd()
    settings, device = slotd.load_settings(config)
    if not settings:
        return 0
    state_dir = settings.get("state_dir") or slotd.default_state_dir()
    if slotd._client_from_info(state_dir):
        return 0
    try:
        daemon = slotd.daemon_from_settings(settings, device, state_dir,
                                            config or slotd.default_config_path())
    except (slotd.ss.StoreError, KeyError) as exc:
        print("the store settings are wrong: %s" % exc, file=sys.stderr)
        return 2
    if with_tray is None:
        with_tray = paths.is_windows()
    if not with_tray:
        slotd.serve(daemon, state_dir)
        return 0
    return _run_with_tray(slotd, daemon, state_dir, config)


def _run_with_tray(slotd, daemon, state_dir, config):
    server = threading.Thread(target=slotd.serve, args=(daemon, state_dir),
                              name="slotd", daemon=True)
    server.start()
    host = TrayHost(daemon, config=config)
    try:
        host.run()
        if host.tray.failed is not None:
            # No window at all (no desktop to put it on). The daemon is still
            # worth having, so it runs on without an icon until killed.
            server.join()
    finally:
        stop_daemon(daemon, state_dir)
    return 0


def stop_daemon(daemon, state_dir, grace=15.0):
    """Stop uploads, let one in flight finish, and withdraw daemon.json.

    An upload is not cut off in the middle of a blob when it can be helped
    (a cut one is safe anyway: the queue is on disk), and no picker is left
    pointed at a port that is about to vanish.
    """
    daemon.paused = True
    if daemon.lock.acquire(timeout=grace):
        daemon.lock.release()
    daemon.shutdown()
    info_path = os.path.join(state_dir, "daemon.json")
    try:
        # Read, close, then delete: Windows will not delete an open file.
        with open(info_path, "r", encoding="utf-8") as handle:
            ours = json.load(handle).get("pid") == os.getpid()
        if ours:
            os.unlink(info_path)
    except (OSError, ValueError):
        pass


class TrayHost(object):
    """Joins a Daemon to a Tray: polls status, answers the menu."""

    def __init__(self, daemon, config=None, tray=None):
        self.daemon = daemon
        self.config = config
        self.watcher = Watcher()
        self.tray = tray or Tray(on_command=self.command, on_tick=self.tick,
                                 menu=lambda: menu_items(self.daemon.paused),
                                 on_open=self.open)

    def run(self):
        self.tray.run()

    def tick(self):
        """Once every POLL_SECONDS, on the tray thread."""
        if self.daemon.stopping:
            # Stopped from outside (the Store screen restarts the daemon on
            # new settings). The icon goes with it, or every settings change
            # would leave one more dead icon beside the new one.
            self.tray.stop()
            return
        try:
            status = self.daemon.status()
        except Exception as exc:
            status = {"error": {"kind": "offline", "message": str(exc)}}
        self.tray.set_tip(tooltip(status))
        for title, text, kind in self.watcher.update(status):
            self.tray.balloon(title, text, kind)

    def open(self):
        open_gui(self.config)

    def command(self, item):
        if item == MENU_OPEN:
            self.open()
        elif item == MENU_UPLOAD:
            # Asking for an upload now is a later and plainer wish than an
            # earlier pause, so it lifts the pause.
            self.daemon.paused = False
            self.daemon.kick.set()
        elif item == MENU_PAUSE:
            self.daemon.paused = not self.daemon.paused
            if not self.daemon.paused:
                self.daemon.kick.set()
        elif item == MENU_QUIT:
            self.tray.stop()
            return
        self.tick()


# ------------------------------------------------------------------ Win32


def _win32():
    """ctypes declarations, built once and only on Windows.

    Every handle is pointer sized, so each function that takes or returns one
    is declared. Left to ctypes' default of int, a 64-bit HWND or HICON is cut
    in half and the call fails with no error at all.
    """
    import ctypes
    from ctypes import wintypes as w

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR),
                    ("hIconSm", w.HICON)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
                    ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
                    ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                    ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
                    ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def declare(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    api = type("Win32", (object,), {})()
    api.ctypes = ctypes
    api.w = w
    api.WNDPROC = WNDPROC
    api.WNDCLASSEXW = WNDCLASSEXW
    api.NOTIFYICONDATAW = NOTIFYICONDATAW
    api.GetModuleHandleW = declare(kernel32.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
    api.RegisterClassExW = declare(user32.RegisterClassExW, w.ATOM,
                                   ctypes.POINTER(WNDCLASSEXW))
    api.UnregisterClassW = declare(user32.UnregisterClassW, w.BOOL, w.LPCWSTR,
                                   w.HINSTANCE)
    api.CreateWindowExW = declare(user32.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR,
                                  w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU,
                                  w.HINSTANCE, w.LPVOID)
    api.DestroyWindow = declare(user32.DestroyWindow, w.BOOL, w.HWND)
    api.DefWindowProcW = declare(user32.DefWindowProcW, LRESULT, w.HWND, w.UINT,
                                 w.WPARAM, w.LPARAM)
    api.PostMessageW = declare(user32.PostMessageW, w.BOOL, w.HWND, w.UINT,
                               w.WPARAM, w.LPARAM)
    api.PostQuitMessage = declare(user32.PostQuitMessage, None, ctypes.c_int)
    api.GetMessageW = declare(user32.GetMessageW, w.BOOL, ctypes.POINTER(w.MSG),
                              w.HWND, w.UINT, w.UINT)
    api.TranslateMessage = declare(user32.TranslateMessage, w.BOOL,
                                   ctypes.POINTER(w.MSG))
    api.DispatchMessageW = declare(user32.DispatchMessageW, LRESULT,
                                   ctypes.POINTER(w.MSG))
    api.RegisterWindowMessageW = declare(user32.RegisterWindowMessageW, w.UINT,
                                         w.LPCWSTR)
    api.ChangeWindowMessageFilterEx = declare(user32.ChangeWindowMessageFilterEx,
                                              w.BOOL, w.HWND, w.UINT, w.DWORD,
                                              w.LPVOID)
    api.SetTimer = declare(user32.SetTimer, ctypes.c_size_t, w.HWND, ctypes.c_size_t,
                           w.UINT, w.LPVOID)
    api.KillTimer = declare(user32.KillTimer, w.BOOL, w.HWND, ctypes.c_size_t)
    api.CreatePopupMenu = declare(user32.CreatePopupMenu, w.HMENU)
    api.AppendMenuW = declare(user32.AppendMenuW, w.BOOL, w.HMENU, w.UINT,
                              ctypes.c_size_t, w.LPCWSTR)
    api.SetMenuDefaultItem = declare(user32.SetMenuDefaultItem, w.BOOL, w.HMENU,
                                     w.UINT, w.UINT)
    api.TrackPopupMenu = declare(user32.TrackPopupMenu, w.BOOL, w.HMENU, w.UINT,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
                                 w.LPVOID)
    api.DestroyMenu = declare(user32.DestroyMenu, w.BOOL, w.HMENU)
    api.GetCursorPos = declare(user32.GetCursorPos, w.BOOL, ctypes.POINTER(w.POINT))
    api.SetForegroundWindow = declare(user32.SetForegroundWindow, w.BOOL, w.HWND)
    api.LoadImageW = declare(user32.LoadImageW, w.HANDLE, w.HINSTANCE, w.LPCWSTR,
                             w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)
    api.LoadIconW = declare(user32.LoadIconW, w.HICON, w.HINSTANCE, w.LPVOID)
    api.DestroyIcon = declare(user32.DestroyIcon, w.BOOL, w.HICON)
    api.GetSystemMetrics = declare(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    api.ExtractIconExW = declare(shell32.ExtractIconExW, w.UINT, w.LPCWSTR,
                                 ctypes.c_int, ctypes.POINTER(w.HICON),
                                 ctypes.POINTER(w.HICON), w.UINT)
    api.Shell_NotifyIconW = declare(shell32.Shell_NotifyIconW, w.BOOL, w.DWORD,
                                    ctypes.POINTER(NOTIFYICONDATAW))
    return api


_API = None


def win32():
    global _API
    if _API is None:
        _API = _win32()
    return _API


WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF = {"info": 0x1, "warning": 0x2, "error": 0x3}
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512
MSGFLT_ALLOW = 1
TIMER_ID = 1


class Tray(object):
    """One notification-area icon with a menu, on a thread of its own.

    The window is a hidden top-level window, never shown, rather than a
    message-only one (HWND_MESSAGE). A message-only window gets no broadcast
    messages, and TaskbarCreated, which is how Explorer says it restarted and
    every icon must be added again, is a broadcast.
    """

    def __init__(self, on_command, on_tick=None, menu=None, on_open=None,
                 icon_path=None, tick_seconds=POLL_SECONDS, uid=1):
        self.on_command = on_command
        self.on_tick = on_tick
        self.menu = menu or (lambda: [])
        self.on_open = on_open
        self.icon_path = icon_path
        self.tick_seconds = tick_seconds
        self.uid = uid
        self.hwnd = None
        self.hicon = None
        self.added = False
        self.tip = "BlockSlot"
        self.ready = threading.Event()
        self.error = None           # the last exception from a callback
        self.failed = None          # why the window could not be made
        self._taskbar_created = 0
        self._thread = None
        self._proc = None
        self._class = "BlockslotTray%d_%d" % (os.getpid(), id(self))

    # -- from any thread

    def run(self):
        """Run the loop on its own thread and block until Quit."""
        self.start()
        self._thread.join()

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="tray", daemon=True)
        self._thread.start()
        self.ready.wait(10)
        return self

    def stop(self):
        """Ask the tray thread to remove the icon and end its loop."""
        if self.hwnd:
            win32().PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    # -- the tray thread

    def set_tip(self, text):
        self.tip = clip(text, TIP_LIMIT)
        if self.added:
            self._notify(NIM_MODIFY, NIF_TIP)
        else:
            # Explorer was not there yet (early at login). Try again now.
            self._add()

    def balloon(self, title, text, kind="info"):
        if not self.added:
            return False
        data = self._data(NIF_INFO)
        data.szInfo = clip(text, INFO_LIMIT)
        data.szInfoTitle = clip(title, INFO_TITLE_LIMIT)
        data.dwInfoFlags = NIIF.get(kind, NIIF["info"])
        return bool(win32().Shell_NotifyIconW(NIM_MODIFY, win32().ctypes.byref(data)))

    def _loop(self):
        api = win32()
        try:
            self._create_window()
            self.hicon = self._load_icon()
            self._add()
            api.SetTimer(self.hwnd, TIMER_ID, int(self.tick_seconds * 1000), None)
        except Exception as exc:
            self.failed = exc
            self.ready.set()
            return
        self.ready.set()
        if self.on_tick:
            self._safe(self.on_tick)
        msg = api.w.MSG()
        while api.GetMessageW(api.ctypes.byref(msg), None, 0, 0) > 0:
            api.TranslateMessage(api.ctypes.byref(msg))
            api.DispatchMessageW(api.ctypes.byref(msg))
        api.UnregisterClassW(self._class, api.GetModuleHandleW(None))

    def _create_window(self):
        api = win32()
        instance = api.GetModuleHandleW(None)
        self._proc = api.WNDPROC(self._wndproc)   # kept, or ctypes frees it
        wc = api.WNDCLASSEXW()
        wc.cbSize = api.ctypes.sizeof(api.WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = instance
        wc.lpszClassName = self._class
        if not api.RegisterClassExW(api.ctypes.byref(wc)):
            raise OSError("RegisterClassExW failed: %d" % api.ctypes.get_last_error())
        self._taskbar_created = api.RegisterWindowMessageW("TaskbarCreated")
        self.hwnd = api.CreateWindowExW(0, self._class, "Blockslot", 0, 0, 0, 0, 0,
                                        None, None, instance, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed: %d" % api.ctypes.get_last_error())
        # An elevated process does not hear TaskbarCreated from a normal
        # Explorer unless it says it wants to.
        api.ChangeWindowMessageFilterEx(self.hwnd, self._taskbar_created,
                                        MSGFLT_ALLOW, None)

    def _load_icon(self):
        """assets/blockslot-tray.ico (no tile, so it suits a light or a dark
        taskbar), else the app icon, else the exe's own, else the stock one."""
        api = win32()
        width = api.GetSystemMetrics(SM_CXSMICON)
        height = api.GetSystemMetrics(SM_CYSMICON)
        path = (self.icon_path or paths.resource("assets/blockslot-tray.ico")
                or paths.resource("assets/blockslot.ico"))
        if path:
            icon = api.LoadImageW(None, str(path), IMAGE_ICON, width, height,
                                  LR_LOADFROMFILE)
            if icon:
                return icon
        if paths.is_frozen():
            small = api.w.HICON()
            if api.ExtractIconExW(sys.executable, 0, None, api.ctypes.byref(small), 1) \
                    and small:
                return small
        return api.LoadIconW(None, api.ctypes.c_void_p(IDI_APPLICATION))

    def _data(self, flags):
        api = win32()
        data = api.NOTIFYICONDATAW()
        data.cbSize = api.ctypes.sizeof(api.NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = self.uid
        data.uFlags = flags
        return data

    def _notify(self, message, flags):
        api = win32()
        data = self._data(flags)
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self.hicon
        data.szTip = self.tip
        return bool(api.Shell_NotifyIconW(message, api.ctypes.byref(data)))

    def _add(self):
        self.added = self._notify(NIM_ADD, NIF_MESSAGE | NIF_ICON | NIF_TIP)
        if not self.added:
            # Still there from before an Explorer restart that was missed.
            self.added = self._notify(NIM_MODIFY, NIF_MESSAGE | NIF_ICON | NIF_TIP)
        return self.added

    def _remove(self):
        if self.hwnd:
            self._notify(NIM_DELETE, 0)
        self.added = False

    def _show_menu(self):
        api = win32()
        items = self.menu()
        if not items:
            return
        menu = api.CreatePopupMenu()
        try:
            for item, label in items:
                if item == SEPARATOR:
                    api.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    api.AppendMenuW(menu, MF_STRING, item, label)
            api.SetMenuDefaultItem(menu, items[0][0], 0)
            point = api.w.POINT()
            api.GetCursorPos(api.ctypes.byref(point))
            # Without the foreground, the menu does not close when a person
            # clicks elsewhere. The WM_NULL after it is the documented fix
            # for the menu that then closes on the second click.
            api.SetForegroundWindow(self.hwnd)
            chosen = api.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                        point.x, point.y, 0, self.hwnd, None)
            api.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            api.DestroyMenu(menu)
        if chosen:
            self._safe(self.on_command, chosen)

    def _safe(self, fn, *args):
        """An exception must not escape into Windows through the WNDPROC."""
        try:
            fn(*args)
        except Exception as exc:
            self.error = exc

    def _wndproc(self, hwnd, msg, wparam, lparam):
        api = win32()
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif event == WM_LBUTTONUP and self.on_open:
                    # One click opens the window, as he asked. A double click
                    # sends a button-up too, so it is not handled twice.
                    self._safe(self.on_open)
                return 0
            if msg == WM_TIMER and wparam == TIMER_ID:
                if self.on_tick:
                    self._safe(self.on_tick)
                return 0
            if msg == self._taskbar_created and msg:
                self.added = False
                self._add()
                return 0
            if msg == WM_CLOSE:
                api.KillTimer(hwnd, TIMER_ID)
                self._remove()
                api.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.hwnd = None
                api.PostQuitMessage(0)
                return 0
        except Exception as exc:
            self.error = exc
        return api.DefWindowProcW(hwnd, msg, wparam, lparam)


if __name__ == "__main__":
    # A quick look at the icon without a daemon: python gui/tray.py
    tray = Tray(on_command=lambda item: tray.stop() if item == MENU_QUIT else None,
                menu=lambda: menu_items(False))
    tray.run()
