#!/usr/bin/env python3
"""savepick - decide whether to restore a ludusavi backup before a game launches.

Ludusavi's `wrap` restores the other machine's backup on every launch, without
checking whether that backup is older than the save already on this machine.
On 2026-09-03 that rolled a Dark Souls II save back by 14 hours.

savepick sits in front of `wrap`. It compares the two save timestamps, restores
only when the backup is genuinely newer, and asks when the live save is newer.
Ludusavi still does all the file work, including the backup on exit.

Steam launch option:
  Windows: C:/Python313/python.exe C:/Users/you/.local/bin/savepick.py -- %command%
  Linux:   /usr/bin/python3 /home/deck/.local/bin/savepick.py -- %command%

Switches before the `--`:
  --tree NAME     sync a whole save set instead of one Steam game
  --borderless    take the frame off the game's window (Windows only)
  --no-sync       launch without touching saves, for --borderless alone

Fail-safe rule: every failure resolves to "do not restore". A skipped restore
costs one manual sync. A wrong restore costs hours of play.
"""

import glob
import json
import os
import select
import struct
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# savepick is the name of this file and of the save-picking step. Blockslot is
# the project it belongs to, and it is the only name worth showing a player, so
# every window carries it.
APP_TITLE = "BlockSlot"

RESTORE = "restore"
SKIP = "skip"
ASK = "ask"

# Filesystem and clock granularity differ between NTFS and ext4, and ludusavi
# preserves mtime on restore. Anything inside this window is the same save.
TOLERANCE_SECONDS = 2.0

# The dialog auto-continues with the newest save so a two-in-a-row session on one
# machine does not turn into a click-through habit.
DIALOG_TIMEOUT_SECONDS = 30

METADATA_NAMES = {"mapping.yaml", "registry.yaml"}

def _log_path():
    """Where the log lives, the same place the Blockslot window reads it.

    Not /tmp on Linux: SteamOS clears it on every restart, and on 2026-09-25
    a Deck restart two minutes after a DS2 session took that session's log
    with it. Windows keeps %TEMP% across restarts, so it stays there.
    """
    if sys.platform == "win32":
        return Path(tempfile.gettempdir()) / "savepick.log"
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "blockslot" / "savepick.log"


LOG_PATH = _log_path()
LOG_ROTATE_BYTES = 2 * 1024 * 1024

# Whether the status window was left showing its log. Written by the window
# itself, read by the next one, so the choice survives a launch.
WINDOW_STATE_PATH = Path(tempfile.gettempdir()) / "savepick-window.txt"


def log(message):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, message)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # A log that outlives restarts has to stop somewhere. One older copy
        # is kept, which is plenty to read back a bad launch.
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(str(LOG_PATH), str(LOG_PATH) + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    # Steam launches this with pythonw.exe, where sys.stderr is None.
    if sys.stderr is not None:
        try:
            sys.stderr.write(line + "\n")
        except (OSError, ValueError):
            pass


def is_windows():
    return os.name == "nt"


# Windows gives every console program its own black window, and pythonw only
# hides python's own. Under RetroBat that means a flash per ludusavi call and
# per dialog, on top of a fullscreen frontend. CREATE_NO_WINDOW stops it. A
# WinForms window opened BY the hidden powershell still shows: the flag hides
# the console, not the form.
CREATE_NO_WINDOW = 0x08000000


def no_window():
    """Keyword args that keep a child process from opening a console."""
    if is_windows():
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


SW_HIDE = 0

# Room for the pids GetConsoleProcessList reports. Only the count matters.
CONSOLE_PIDS = 16


def hide_own_console():
    """Hide the console this process was given, when nothing else shares it.

    pythonw opens no console, but the launcher decides which python runs us.
    Steam's shortcut uses python.exe, so a black window sat behind RetroBat
    for the whole session. GetConsoleProcessList counts every process attached
    to this console. A count of 1 means Windows made the console for us alone,
    so hiding it loses nothing. Started from a terminal the count includes the
    shell, and the output stays where you can read it.
    """
    if not is_windows():
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        window = kernel32.GetConsoleWindow()
        if not window:
            return
        pids = (ctypes.c_uint * CONSOLE_PIDS)()
        attached = kernel32.GetConsoleProcessList(pids, CONSOLE_PIDS)
        if attached == 1:
            ctypes.windll.user32.ShowWindow(window, SW_HIDE)
            log("hid our own console window")
        else:
            log("console shared with %d process(es); leaving it open" % attached)
    except Exception as exc:
        log("could not hide the console: %s" % exc)


def ludusavi_binary():
    override = os.environ.get("SAVEPICK_LUDUSAVI")
    if override:
        return override
    if is_windows():
        return str(Path.home() / ".local" / "bin" / "ludusavi.exe")
    return str(Path.home() / ".local" / "bin" / "ludusavi")


def run_json(args, timeout=120):
    """Run ludusavi and parse its --api JSON. Returns None on any failure.

    A nonzero exit is NOT treated as failure here. ludusavi returns nonzero
    when any single entry fails and still prints a full, useful report.
    """
    cmd = [ludusavi_binary()] + args
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, **no_window()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log("ludusavi failed to run: %s" % exc)
        return None
    out = proc.stdout or ""
    start = out.find("{")
    if start < 0:
        log("no JSON from: %s (exit %s) %s" % (" ".join(args), proc.returncode, proc.stderr[:200]))
        return None
    try:
        return json.loads(out[start:])
    except ValueError as exc:
        log("bad JSON from %s: %s" % (" ".join(args), exc))
        return None


def decide(live, backup, tolerance=TOLERANCE_SECONDS):
    """Return RESTORE, SKIP or ASK from two mtimes (epoch seconds, or None)."""
    if backup is None:
        return SKIP
    if live is None:
        return RESTORE
    if abs(backup - live) <= tolerance:
        return SKIP
    if backup > live:
        return RESTORE
    return ASK


def resolve_ask(answer):
    """Turn the dialog's answer into a decision. None (no answer) is not consent."""
    return RESTORE if answer is True else SKIP


def newest_mtime_of(directory, basenames):
    """Newest mtime under `directory`, ignoring ludusavi metadata.

    When `basenames` is given, only those filenames count. If none of them are
    present, fall back to every non-metadata file rather than returning None,
    so a wrong guess never reads as "no save here".
    """
    directory = Path(directory)
    if not directory.is_dir():
        return None
    matched = []
    everything = []
    for path in directory.rglob("*"):
        if not path.is_file() or path.name in METADATA_NAMES:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        everything.append(mtime)
        if basenames and path.name in basenames:
            matched.append(mtime)
    if matched:
        return max(matched)
    if everything:
        return max(everything)
    return None


def pick_newest_backup(backups):
    """Newest backup by its recorded `when`, not by list order."""
    if not backups:
        return None
    def key(entry):
        raw = (entry.get("when") or "").replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    return sorted(backups, key=key)[-1]


def game_name_for_appid(appid):
    # --no-manifest-update matters here: a game launch must never block on a
    # network fetch. `find` updates the manifest by default.
    data = run_json(["--no-manifest-update", "find", "--api", "--steam-id", str(appid)])
    if not data:
        return None
    games = data.get("games") or {}
    if not games:
        return None
    return sorted(games, key=lambda name: -(games[name].get("score") or 0))[0]


def save_paths_from_preview(entry):
    """Paths of the files ludusavi tags as saves, from one preview entry.

    Only tagged saves count. Ludusavi lists more than saves: for Dark Souls II
    it lists Steam screenshots, and it gives those no tags at all. On
    2026-09-05 three screenshots taken at 1:56 PM made the Deck's live save
    look 20 hours newer than it was, and the dialog offered to keep it over a
    real save from the other machine.

    The one fallback: if no file in the whole set carries any tag, ludusavi is
    not telling us, so take every file. Returning nothing there would read as
    "fresh install", and decide() restores on that.
    """
    files = entry.get("files") or {}
    tagged = [p for p, info in files.items() if "save" in (info.get("tags") or [])]
    if tagged:
        return tagged
    if any(info.get("tags") for info in files.values()):
        return []
    return list(files)


def live_save_files(game):
    """Paths of the live files ludusavi tags as saves."""
    data = run_json(["--no-manifest-update", "backup", "--preview", "--api", game])
    if not data:
        return []
    return save_paths_from_preview((data.get("games") or {}).get(game) or {})


def newest_live_mtime(paths):
    times = []
    for path in paths:
        try:
            times.append(Path(path).stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def newest_backup_info(game, basenames, cfg):
    """(label, mtime, backup_name, peer_dir) for the newest backup anywhere.

    Every peer device directory is read by path, so this device never depends
    on ludusavi's own restore.path and never has to be reconfigured when a new
    device joins. With one peer this does what it always did. With three, the
    newest save wins and the label says which device it came from.

    The label is never inferred from the operating system. That guess could
    only ever describe two devices and it called any Linux peer a Steam Deck.
    """
    best_label = best_mtime = best_name = best_peer = None
    for peer in peer_dirs(cfg):
        data = run_json(["--no-manifest-update", "backups",
                         "--path", str(peer), "--api", game])
        if not data:
            log("could not read backups from peer %s; skipping it" % peer.name)
            continue
        entry = (data.get("games") or {}).get(game) or {}
        newest = pick_newest_backup(entry.get("backups") or [])
        if not newest:
            continue
        root = Path(entry.get("backupPath") or str(peer))
        name = newest.get("name") or "."
        directory = root if name == "." else root / name
        mtime = newest_mtime_of(directory, basenames)
        if mtime is None:
            continue
        if best_mtime is None or mtime > best_mtime:
            best_label = "%s backup" % device_label(cfg, peer.name)
            best_mtime, best_name, best_peer = mtime, name, peer
    return best_label, best_mtime, best_name, best_peer


def human_time(mtime):
    if mtime is None:
        return "unknown"
    return datetime.fromtimestamp(mtime).strftime("%b %d, %Y  %I:%M %p").replace(" 0", " ")


WINDOWS_ASK = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = "__TITLE__"
$form.Size = New-Object System.Drawing.Size(660,330)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$body = New-Object System.Windows.Forms.Label
$body.Text = @'
__TEXT__
'@
$body.Font = New-Object System.Drawing.Font("Consolas",10)
$body.Location = New-Object System.Drawing.Point(20,20)
$body.Size = New-Object System.Drawing.Size(610,200)
$form.Controls.Add($body)

$keep = New-Object System.Windows.Forms.Button
$keep.Text = "__KEEP__"
$keep.Location = New-Object System.Drawing.Point(210,240)
$keep.Size = New-Object System.Drawing.Size(200,34)
$keep.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($keep)
$form.AcceptButton = $keep
$form.CancelButton = $keep

$rest = New-Object System.Windows.Forms.Button
$rest.Text = "__REST__"
$rest.Location = New-Object System.Drawing.Point(420,240)
$rest.Size = New-Object System.Drawing.Size(200,34)
$rest.DialogResult = [System.Windows.Forms.DialogResult]::No
$form.Controls.Add($rest)

$script:left = __TIMEOUT__
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
  $script:left = $script:left - 1
  if ($script:left -le 0) { $timer.Stop(); $form.DialogResult = [System.Windows.Forms.DialogResult]::OK; $form.Close() }
})
$timer.Start()

$result = $form.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::No) { Write-Output "RESTORE" } else { Write-Output "KEEP" }
'''


def end_dialog(proc):
    """Close a dialog the pad has already answered."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_powershell_dialog(script, watch_pad=False):
    """Run a WinForms dialog script. Returns "KEEP", "RESTORE" or None.

    With watch_pad, XInput is read while the window is up. WinForms takes no
    notice of a gamepad, so in a controller-only session the dialog would sit
    there with no way to answer it. The pad is polled directly instead, and a
    press closes the window. Without that the window has to be clicked.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="ascii", errors="replace")
    handle.write(script)
    handle.close()
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", handle.name]
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, text=True,
                                    **no_window())
        except (OSError, subprocess.SubprocessError) as exc:
            log("windows dialog failed: %s" % exc)
            return None
        if watch_pad:
            answer = watch_windows_gamepad(
                proc, time.monotonic() + DIALOG_TIMEOUT_SECONDS + 30)
            if answer is not None:
                end_dialog(proc)
                return "RESTORE" if answer else "KEEP"
        try:
            out, err = proc.communicate(timeout=DIALOG_TIMEOUT_SECONDS + 120)
        except subprocess.TimeoutExpired:
            log("windows dialog timed out")
            end_dialog(proc)
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            log("windows dialog failed: %s" % exc)
            end_dialog(proc)
            return None
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    lines = (out or "").strip().splitlines()
    answer = lines[-1].strip() if lines else ""
    if answer in ("KEEP", "RESTORE"):
        return answer
    log("windows dialog gave no usable answer: %r %s" % (answer, (err or "")[:200]))
    return None


def ask_windows(game, live_text, backup_label, backup_text, headline=None,
                keep_label=None, restore_label=None):
    """Two-button dialog worded the same as the Deck's zenity one.

    Returns True to restore, False to keep, None on failure. Closing the window
    counts as keeping, so no accident can roll you back.
    """
    script = (WINDOWS_ASK
              .replace("__TITLE__", "%s - Save conflict" % APP_TITLE)
              .replace("__TEXT__", conflict_text(game, live_text, backup_label,
                                                 backup_text, headline))
              .replace("__KEEP__", keep_label or KEEP_LABEL)
              .replace("__REST__", restore_label or RESTORE_LABEL)
              .replace("__TIMEOUT__", str(DIALOG_TIMEOUT_SECONDS)))
    answer = run_powershell_dialog(script, watch_pad=True)
    if answer == "RESTORE":
        return True
    if answer == "KEEP":
        return False
    return None


KEEP_LABEL = "Keep this device (newest)"
RESTORE_LABEL = "Restore the older backup"

# Steam's Linux Runtime injects these so the game sees its own bundled libraries.
# A host GTK binary like zenity picks them up and dies with exit 255. Strip them
# for the dialog only; the game still launches with Steam's environment intact.
RUNTIME_VARS = (
    "LD_PRELOAD", "LD_LIBRARY_PATH", "GTK_PATH", "GTK_IM_MODULE_FILE",
    "GIO_MODULE_DIR", "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR",
    "GCONV_PATH", "LOCPATH", "XDG_DATA_DIRS", "GSETTINGS_SCHEMA_DIR",
    "LIBGL_DRIVERS_PATH", "PYTHONPATH", "PYTHONHOME", "SDL_DYNAMIC_API",
)


def host_tool(name):
    """Absolute path to the system's own dialog program.

    Steam's Linux Runtime puts its bundled copies first on PATH. Its `zenity` is
    an old GTK2 build that dies with "libgtk-x11-2.0.so.0: cannot open shared
    object file" once Steam's library paths are stripped, and it renders list
    widgets badly even when it does run. Always prefer the host's own binary.
    """
    for candidate in ("/usr/bin/%s" % name, "/bin/%s" % name):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name


def host_env():
    """A copy of the environment with Steam's runtime overrides removed."""
    env = dict(os.environ)
    dropped = [name for name in RUNTIME_VARS if name in env]
    for name in dropped:
        del env[name]
    if dropped:
        log("stripped Steam runtime vars for the dialog: %s" % ", ".join(dropped))
    log("display vars: DISPLAY=%r WAYLAND_DISPLAY=%r GDK_BACKEND=%r"
        % (env.get("DISPLAY"), env.get("WAYLAND_DISPLAY"), env.get("GDK_BACKEND")))
    # gamescope does not present a plain Wayland client, so a GTK3 dialog opens
    # invisibly. Steam's old GTK2 zenity had no Wayland support and showed up
    # over XWayland, which is why the first attempt was visible. Force X11.
    env.pop("WAYLAND_DISPLAY", None)
    env["GDK_BACKEND"] = "x11"
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"
        log("no DISPLAY in the environment; falling back to :0")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    return env


def conflict_text(game, live_text, backup_label, backup_text, headline=None):
    """The wording both platforms show, so every device reads the same.

    Both rows are padded to a common width so the two dates start in the same
    column. Comparing them at a glance is the only thing this dialog is for, and
    a ragged pair makes that harder. Padding needs a monospace font to mean
    anything: Windows sets Consolas on the label, and zenity_markup() wraps this
    in a <tt> span for the Deck.
    """
    rows = (("This device:", live_text, "" if headline else "   (newest)"),
            ("%s:" % backup_label, backup_text, ""))
    label_width = max(len(label) for label, _, _ in rows)
    date_width = max(len(date) for _, date, _ in rows)
    lines = [
        ("%s   %s%s" % (label.ljust(label_width), date.ljust(date_width), suffix)).rstrip()
        for label, date, suffix in rows
    ]
    return (
        "%s\n\n"
        "%s\n\n"
        "%s\n%s\n\n"
        "A = keep this device.   B = %s.\n"
        "Keeping this device in %d seconds."
        % (game, headline or "The backup is OLDER than the save on this device.",
           lines[0], lines[1],
           "use the other save" if headline else "restore the backup",
           DIALOG_TIMEOUT_SECONDS)
    )


def pango_escape(text):
    """Escape the three characters Pango treats as markup. Ampersand first."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def zenity_markup(game, live_text, backup_label, backup_text, headline=None):
    """The same words as the Windows dialog, in a monospace span.

    zenity renders Pango markup by default (it offers --no-markup to turn it
    off), so a game name holding & or < would otherwise break the dialog.
    """
    body = conflict_text(game, live_text, backup_label, backup_text, headline)
    return "<tt>%s</tt>" % pango_escape(body)


# Linux joystick API. Each event is 8 bytes: u32 time, s16 value, u8 type, u8 number.
JS_EVENT_SIZE = 8
JS_EVENT_BUTTON = 0x01
JS_EVENT_INIT = 0x80
BUTTON_A = 0
BUTTON_B = 1


def parse_js_event(data):
    """(type, number, value) from one joystick event, or None if unusable."""
    if not data or len(data) < JS_EVENT_SIZE:
        return None
    _stamp, value, etype, number = struct.unpack("IhBB", data[:JS_EVENT_SIZE])
    return etype, number, value


def gamepad_choice(event):
    """True to restore (B), False to keep (A), None for anything else.

    Events flagged JS_EVENT_INIT are the synthetic startup state the kernel
    sends when the device is opened. They are not presses and must be ignored,
    or the dialog would answer itself the instant it appeared.
    """
    if event is None:
        return None
    etype, number, value = event
    if etype & JS_EVENT_INIT:
        return None
    if not etype & JS_EVENT_BUTTON or value != 1:
        return None
    if number == BUTTON_A:
        return False
    if number == BUTTON_B:
        return True
    return None


# JSIOCGNAME(len): read the joystick's name. _IOC(READ, 'j', 0x13, len).
def _jsiocgname(length):
    return 0x80000000 | (length << 16) | (ord("j") << 8) | 0x13


GAMEPAD_HINTS = ("x-box", "xbox", "gamepad", "controller", "steam deck", "pad")


def looks_like_a_gamepad(name):
    lowered = (name or "").lower()
    if "mouse" in lowered or "keyboard" in lowered or "touch" in lowered:
        return False
    return any(hint in lowered for hint in GAMEPAD_HINTS)


def joystick_name(fd):
    # fcntl is Linux only, so import it here rather than at module scope.
    # A top-level import made savepick fail to start on Windows entirely,
    # which would have stopped the game launching at all.
    import fcntl
    buf = bytearray(128)
    try:
        fcntl.ioctl(fd, _jsiocgname(len(buf)), buf)
    except OSError:
        return ""
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


_LOGGED_PADS = set()


def open_gamepads():
    """Every readable joystick that looks like a gamepad.

    Taking the first /dev/input/js* was wrong: on the Steam Deck js0 is
    "Mouse passthrough (absolute)" and the real pad is js1,
    "Microsoft X-Box 360 pad 0". Match by name, and watch them all, because the
    numbering moves when controllers connect.
    """
    named = []
    others = []
    for path in sorted(glob.glob("/dev/input/js*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        name = joystick_name(fd)
        if looks_like_a_gamepad(name):
            # Log each pad once per process. The exit wait rescans every couple
            # of seconds, and that buried every useful line in the log.
            if (path, name) not in _LOGGED_PADS:
                _LOGGED_PADS.add((path, name))
                log("gamepad: %s (%s)" % (path, name))
            named.append(fd)
        else:
            others.append((fd, path, name))
    if named:
        for fd, _p, _n in others:
            try:
                os.close(fd)
            except OSError:
                pass
        return named
    # Nothing matched by name. Watch everything rather than watching nothing.
    for _fd, path, name in others:
        log("gamepad fallback: watching %s (%s)" % (path, name))
    return [fd for fd, _p, _n in others]


# ---- Windows controllers -------------------------------------------------
# XInput reports every pad Windows knows about, including anything Steam Input
# has re-presented as an Xbox controller.
XINPUT_GAMEPAD_A = 0x1000
XINPUT_GAMEPAD_B = 0x2000
XINPUT_MAX_PADS = 4


def _xinput_library():
    import ctypes
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.windll.LoadLibrary(name)
        except OSError:
            continue
    return None


def xinput_buttons():
    """Bitmask of buttons held on any connected pad, or None if XInput is absent."""
    import ctypes

    class _Pad(ctypes.Structure):
        _fields_ = [("wButtons", ctypes.c_ushort),
                    ("bLeftTrigger", ctypes.c_ubyte),
                    ("bRightTrigger", ctypes.c_ubyte),
                    ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                    ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]

    class _State(ctypes.Structure):
        _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _Pad)]

    lib = _xinput_library()
    if lib is None:
        return None
    held = 0
    found = False
    state = _State()
    for index in range(XINPUT_MAX_PADS):
        if lib.XInputGetState(index, ctypes.byref(state)) == 0:
            found = True
            held |= state.Gamepad.wButtons
    return held if found else None


# The legacy joystick API. It reports DirectInput pads that XInput never lists,
# and it reads a pad that another process has already taken over. Slot names
# come out of the "Microsoft PC-joystick driver" shim, so the button order is
# the standard one: bit 0 is A, bit 1 is B.
JOY_RETURNALL = 0x000000FF
WINMM_A = 0x0001
WINMM_B = 0x0002


def winmm_buttons():
    """Bitmask of buttons held on any legacy joystick, or None if none answers."""
    import ctypes

    class _JoyInfoEx(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint),
                    ("dwXpos", ctypes.c_uint), ("dwYpos", ctypes.c_uint),
                    ("dwZpos", ctypes.c_uint), ("dwRpos", ctypes.c_uint),
                    ("dwUpos", ctypes.c_uint), ("dwVpos", ctypes.c_uint),
                    ("dwButtons", ctypes.c_uint), ("dwButtonNumber", ctypes.c_uint),
                    ("dwPOV", ctypes.c_uint), ("dwReserved1", ctypes.c_uint),
                    ("dwReserved2", ctypes.c_uint)]

    try:
        winmm = ctypes.windll.winmm
    except (OSError, AttributeError):
        return None
    info = _JoyInfoEx()
    info.dwSize = ctypes.sizeof(_JoyInfoEx)
    info.dwFlags = JOY_RETURNALL
    held = 0
    found = False
    try:
        slots = winmm.joyGetNumDevs()
    except OSError:
        return None
    for slot in range(slots):
        if winmm.joyGetPosEx(slot, ctypes.byref(info)) == 0:
            found = True
            held |= info.dwButtons
    return held if found else None


# What the two APIs agree on, so the watcher never has to know which one spoke.
PAD_A = 0x1
PAD_B = 0x2


def windows_pad_state(sources=None):
    """A and B held, merged from both APIs, or None if no pad answers either.

    Two APIs, because one is not enough. XInput misses a DirectInput-only pad
    entirely, and Steam Input can hold the pad in a way that leaves XInput
    reporting a connected device that never changes. Pass a list as sources to
    collect the name of whichever API reported the press.
    """
    mask = 0
    found = False
    try:
        xinput = xinput_buttons()
    except Exception as exc:
        log("xinput unreadable: %s" % exc)
        xinput = None
    if xinput is not None:
        found = True
        bits = 0
        bits |= PAD_A if xinput & XINPUT_GAMEPAD_A else 0
        bits |= PAD_B if xinput & XINPUT_GAMEPAD_B else 0
        if bits and sources is not None:
            sources.append("xinput")
        mask |= bits
    try:
        winmm = winmm_buttons()
    except Exception as exc:
        log("winmm unreadable: %s" % exc)
        winmm = None
    if winmm is not None:
        found = True
        bits = 0
        bits |= PAD_A if winmm & WINMM_A else 0
        bits |= PAD_B if winmm & WINMM_B else 0
        if bits and sources is not None:
            sources.append("winmm")
        mask |= bits
    return mask if found else None


def pad_choice(mask):
    """True for B, False for A, None for anything else. A wins a double press."""
    if not mask:
        return None
    if mask & PAD_A:
        return False
    if mask & PAD_B:
        return True
    return None


def xinput_choice(buttons):
    """True for B, False for A, None for anything else."""
    if not buttons:
        return None
    if buttons & XINPUT_GAMEPAD_A:
        return False
    if buttons & XINPUT_GAMEPAD_B:
        return True
    return None


def watch_windows_gamepad(proc, deadline):
    """Read the pad while a dialog is up. Returns True/False, or None.

    Every reading is logged the first time it appears, because a dialog that
    ignores the pad looks exactly like a dialog with no pad attached, and only
    the log tells the two apart.
    """
    seen_release = False
    last = "start"
    while proc.poll() is None and time.monotonic() < deadline:
        sources = []
        mask = windows_pad_state(sources)
        reading = "no pad" if mask is None else hex(mask)
        if sources:
            reading = "%s from %s" % (reading, " and ".join(sources))
        if reading != last:
            log("pad: %s" % reading)
            last = reading
        if mask is None:
            return None
        # Wait for both buttons to be released once, so a press still held from
        # quitting the game does not answer the dialog the moment it appears.
        if not seen_release:
            if not mask:
                seen_release = True
            time.sleep(0.1)
            continue
        answer = pad_choice(mask)
        if answer is not None:
            log("pad answered: %s" % ("B" if answer else "A"))
            return answer
        time.sleep(0.1)
    return None


def run_dialog_with_gamepad(cmd, env):
    """Show a zenity dialog and watch the gamepad at the same time.

    In Game Mode the controller drives Steam, not a stray GTK window, so the
    dialog's own buttons cannot be reached with the sticks or the d-pad. Read
    the pad directly instead. Returns (returncode, stdout, stderr, pad_answer).
    The Windows dialogs read XInput inside run_powershell_dialog instead.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    deadline = time.monotonic() + DIALOG_TIMEOUT_SECONDS + 30

    pads = open_gamepads()
    if not pads:
        log("no readable gamepad; the dialog needs a touch, a mouse, or the timeout")
    next_scan = time.monotonic() + 2.0
    answer = None
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            # A pad woken up or plugged in after the dialog opened still counts.
            if time.monotonic() >= next_scan:
                next_scan = time.monotonic() + 2.0
                if len(pads) != len(glob.glob("/dev/input/js*")):
                    for fd in pads:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    pads = open_gamepads()
            if not pads:
                time.sleep(0.2)
                continue
            ready, _w, _x = select.select(pads, [], [], 0.2)
            for fd in ready:
                try:
                    answer = gamepad_choice(parse_js_event(os.read(fd, JS_EVENT_SIZE)))
                except OSError:
                    pads = [p for p in pads if p != fd]
                    continue
                if answer is not None:
                    log("gamepad answered: %s" % ("B" if answer else "A"))
                    break
            if answer is not None:
                break
    finally:
        for fd in pads:
            try:
                os.close(fd)
            except OSError:
                pass

    if answer is not None:
        end_dialog(proc)
        return None, "", "", answer

    out, err = proc.communicate()
    return proc.returncode, out or "", err or "", None


def zenity_candidates():
    """Zenity binaries to try, best first, each with the environment it needs.

    The ONE combination ever observed to draw a visible dialog on the Deck in
    Game Mode is the host's own zenity with Steam's environment left completely
    alone. Stripping Steam's library paths, or forcing GDK_BACKEND, produced a
    process that ran its full timeout with nothing on screen.

    Steam's bundled zenity is not a candidate at all: it is a GTK2 build and the
    Deck has no GTK2, so it dies on libgtk-x11-2.0.so.0 either way.
    """
    host = host_tool("zenity")
    candidates = [(host, dict(os.environ))]
    cleaned = host_env()
    if cleaned != os.environ:
        candidates.append((host, cleaned))
    return candidates


# Exit codes meaning zenity never got as far as drawing anything.
STARTUP_FAILURES = (126, 127, 255)


def ask_linux(game, live_text, backup_label, backup_text, headline=None,
              keep_label=None, restore_label=None):
    """Ask on Linux. True to restore, False to keep, None when nothing answered.

    Only the extra button restores. Cancel, the window close button and the
    timeout all keep the newest save, so no accident can roll you back.
    """
    text = conflict_text(game, live_text, backup_label, backup_text, headline)
    markup = zenity_markup(game, live_text, backup_label, backup_text, headline)
    KEEP = keep_label or KEEP_LABEL
    REST = restore_label or RESTORE_LABEL

    for binary, env in zenity_candidates():
        cmd = [
            binary, "--question", "--title=%s - Save conflict" % APP_TITLE,
            "--text=%s" % markup, "--no-wrap", "--width=640",
            "--ok-label=%s" % KEEP,
            "--cancel-label=Cancel",
            "--extra-button=%s" % REST,
            "--timeout=%d" % DIALOG_TIMEOUT_SECONDS,
        ]
        try:
            code, out, err, pad = run_dialog_with_gamepad(cmd, env)
        except (OSError, subprocess.SubprocessError) as exc:
            log("zenity %s unavailable: %s" % (binary, exc))
            continue
        if pad is not None:
            return pad
        answer = out.strip()
        if code == 5:
            log("zenity %s timed out, keeping the newest save" % binary)
            return False
        if code == 1 and answer == REST:
            return True
        if code in (0, 1):
            return False
        log("zenity %s exit %s, stderr=%r" % (binary, code, err[:200]))
        if code not in STARTUP_FAILURES:
            break

    # kdialog fallback. --yesno gives 0 for the yes button and 1 for the no
    # button, but it ALSO exits non-zero on its own errors. Treating any
    # non-zero as "no" once made savepick restore an older save with nobody
    # asked, so read an answer only when kdialog clearly gave one.
    kdialog = [host_tool("kdialog"), "--title", "%s - Save conflict" % APP_TITLE,
               "--yesno", text,
               "--yes-label", KEEP, "--no-label", REST]
    try:
        proc = subprocess.run(kdialog, capture_output=True, text=True, env=host_env(),
                              timeout=DIALOG_TIMEOUT_SECONDS)
        answer = read_kdialog(proc.returncode, proc.stderr or "")
        if answer is None:
            log("kdialog exit %s, stderr=%r" % (proc.returncode, (proc.stderr or "")[:200]))
        return answer
    except (OSError, subprocess.SubprocessError) as exc:
        log("kdialog unavailable: %s" % exc)
    return None


def read_kdialog(returncode, stderr):
    """True to restore, False to keep, None when kdialog did not give an answer."""
    if stderr.strip():
        return None
    if returncode == 0:
        return False
    if returncode == 1:
        return True
    return None


def ask_user(game, live_mtime, backup_label, backup_mtime, **wording):
    live_text = human_time(live_mtime)
    backup_text = human_time(backup_mtime)
    log("asking: live=%s  %s=%s" % (live_text, backup_label, backup_text))
    if is_windows():
        return ask_windows(game, live_text, backup_label, backup_text, **wording)
    return ask_linux(game, live_text, backup_label, backup_text, **wording)


# ---------------------------------------------------------------- exit backup

SYNC_WAIT_SECONDS = 180
SYNC_OFFLINE_GRACE = 25


def config_path():
    if is_windows():
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "savepick.json"
    return Path.home() / ".config" / "savepick.json"


def load_config():
    """Optional settings, chiefly the Syncthing credentials. Missing is fine."""
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tree_roots(game):
    """Every device's root directory for one save set, keyed by device directory.

    A save set is a whole directory tree rather than one game's save files, so
    ludusavi's manifest knows nothing about it and there is nothing to look up.
    Each device's root is recorded, including this one's, because a peer's root
    is a path belonging to a different operating system and cannot be resolved
    locally.
    """
    trees = load_config().get("trees") or {}
    return (trees.get(game) or {}).get("roots") or {}


def tree_allowed(game):
    """The restore allow list for one save set, or None for every file.

    The default is TREE_RESTORE_EXTENSIONS, which is right for a frontend whose
    tree mixes battery saves with save states and screenshots.

    A set holding one game's save directory and nothing else can say
    "extensions": "*" instead. shadPS4 writes userdata0000 and backup0000, with
    no extension anywhere, so an extension allow list would refuse all of it.
    """
    trees = load_config().get("trees") or {}
    value = (trees.get(game) or {}).get("extensions")
    if value is None:
        return TREE_RESTORE_EXTENSIONS
    if value == "*":
        return None
    return frozenset(str(ext).lower().lstrip(".") for ext in value)


def tree_aliases(game):
    """Groups of system directory names that mean the same system.

    Every frontend names its systems differently, so the same save sits under
    a different directory on each device. Measured between RetroBat and
    RetroDECK on 2026-09-16: sg-1000 and sg1000 both hold
    "Ys - The Vanished Omens (UE) [!].srm", and one hyphen is the only reason
    it had never crossed. gc and gamecube both hold Metroid Prime.

    A group is a plain list of names. Nothing is guessed from the names
    themselves, because a guess that folds two real systems together would
    overwrite one save with another.
    """
    trees = load_config().get("trees") or {}
    return (trees.get(game) or {}).get("system_aliases") or []


def tree_always_dirs(game):
    """Top level directories where every file counts as save data.

    An extension allow list cannot cover MAME. It names each nvram dump after
    the chip it came from: at28c16, ioasic, nov0, smpc_smem, 0_eagle1_bram.
    The list is unbounded. The directory is the only honest boundary there, and
    the same holds for any launcher that keeps a whole subtree of save data.
    """
    trees = load_config().get("trees") or {}
    return set((trees.get(game) or {}).get("always_dirs") or [])


def top_dirs(index):
    """The first path component of every file in an index that has one."""
    return {rel.split("/", 1)[0] for rel in index if "/" in rel}


def alias_path(rel, groups, local_tops):
    """A peer path rewritten to the system name this device uses.

    Returns the path unchanged when no group covers it, and None when a group
    covers it but this device uses none of that group's names. Refusing is the
    right answer there: copying into a directory the frontend never reads helps
    nobody, and picking a name for it would be a guess.
    """
    if "/" not in rel:
        return rel
    head, tail = rel.split("/", 1)
    for group in groups:
        if head not in group:
            continue
        if head in local_tops:
            return rel
        for name in group:
            if name in local_tops:
                return "%s/%s" % (name, tail)
        return None
    return rel


def local_tree_root(game, cfg):
    """This device's root for one save set, or None when it is not configured."""
    root = tree_roots(game).get(cfg.get("device_dir") or "")
    return Path(root) if root else None


def backup_prefix(root_text):
    """Where a ludusavi backup keeps `root_text`, relative to the backup directory.

    ludusavi mirrors the whole source path under a drive directory. A Windows
    root C:\\Users\\you\\Apps\\RetroBat\\saves is stored as
    drive-C/Users/you/Apps/RetroBat/saves, and a POSIX root
    /run/media/mmcblk0p1/retrodeck/saves as
    drive-0/run/media/mmcblk0p1/retrodeck/saves.

    The text is parsed by hand and never through Path, because a backup written
    on one operating system is read on the other, where the local Path class
    parses the foreign path wrongly.
    """
    text = root_text.replace("\\", "/")
    if len(text) > 2 and text[0].isalpha() and text[1] == ":" and text[2] == "/":
        return "drive-%s/%s" % (text[0].upper(), text[3:].strip("/"))
    return "drive-0/%s" % text.strip("/")


# Battery saves and memory cards only. These are emulator data formats and they
# travel between builds.
#
# A save state does not. It is tied to the exact core build that wrote it, so a
# foreign state either fails to load or silently resumes at the wrong point.
# Neither belongs in an automatic restore, so auto, state1 and every other
# state file stays out, along with screenshots and sync databases.
#
# bin, dat and metadata are deliberately absent until someone checks which
# emulator writes each one. Backup still covers them; only restore does not.
TREE_RESTORE_EXTENSIONS = frozenset((
    "srm", "sav", "mcr", "brm", "bkr", "bcr", "smpc", "ldci",
))


def index_tree(root):
    """Relative path -> mtime for every file under root.

    Paths use forward slashes on every operating system, because this index is
    compared against one built from a backup written elsewhere. An unreadable
    root reads as empty, which resolves to no restore.
    """
    index = {}
    root = Path(root)
    try:
        entries = root.rglob("*")
    except OSError as exc:
        log("tree: cannot read %s: %s" % (root, exc))
        return index
    for path in entries:
        try:
            if not path.is_file():
                continue
            index[path.relative_to(root).as_posix()] = path.stat().st_mtime
        except OSError:
            continue
    return index


def is_restorable(relpath, allowed=TREE_RESTORE_EXTENSIONS, always_dirs=()):
    """True when this file may be copied from a peer.

    The check is on the extension alone, so a file type nobody has vetted can
    never cross by accident. An allow list of None turns the check off, for a
    save set that holds nothing but one game's save directory, and always_dirs
    turns it off for named top level directories. See tree_always_dirs.
    """
    if allowed is None:
        return True
    if always_dirs and "/" in relpath and relpath.split("/", 1)[0] in always_dirs:
        return True
    name = relpath.rsplit("/", 1)[-1]
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in allowed


def peer_tree_index(game, cfg):
    """Relative path -> (mtime, source, device label) across every peer.

    The newest copy of a path anywhere wins, and the label says which device it
    came from. A peer with no recorded root is skipped rather than guessed at,
    because a wrong root would silently produce an empty index that reads as
    "nothing to restore".
    """
    best = {}
    roots = tree_roots(game)
    for peer in peer_dirs(cfg):
        root_text = roots.get(peer.name)
        if not root_text:
            log("tree: no root recorded for peer %s; skipping it" % peer.name)
            continue
        data = run_json(["--no-manifest-update", "backups",
                         "--path", str(peer), "--api", game])
        if not data:
            log("tree: could not read backups from peer %s; skipping it" % peer.name)
            continue
        entry = (data.get("games") or {}).get(game) or {}
        newest = pick_newest_backup(entry.get("backups") or [])
        if not newest:
            continue
        root = Path(entry.get("backupPath") or str(peer))
        name = newest.get("name") or "."
        directory = root if name == "." else root / name
        base = directory / backup_prefix(root_text)
        label = device_label(cfg, peer.name)
        for rel, mtime in index_tree(base).items():
            current = best.get(rel)
            if current is None or mtime > current[0]:
                best[rel] = (mtime, base / rel, label)
    return best


def tree_dirs(index):
    """Every directory that holds at least one file in an index."""
    dirs = set()
    for rel in index:
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):
            dirs.add("/".join(parts[:i + 1]))
    return dirs


def path_fits(rel, dirs):
    """True when a path this device does not have still belongs in its tree.

    A save at <system>/<file> always fits. A system directory that is not here
    is one this device has not played, not a different layout. A system name
    never carries a dot: psx, snes, tg16, sg-1000, mame-sa.

    Every other shape needs its parent directory to be here already, because
    the two frontends disagree about layout in two ways. One nests some cores,
    writing 3do/opera/per_game/<rom>.0.srm where the other writes
    3do/<rom>.srm. The other sorts saves by content, writing
    <game>.m3u/<game>.srm, a directory the first never reads. Measured on
    2026-09-16: without this rule a first merge carried 142 nested files one
    way and 714 sort-by-content files the other.
    """
    parts = rel.split("/")
    if len(parts) == 1:
        return True
    if len(parts) == 2 and "." not in parts[0]:
        return True
    return "/".join(parts[:-1]) in dirs


def merge_plan(local, peers, allowed=TREE_RESTORE_EXTENSIONS,
               tolerance=TOLERANCE_SECONDS, dirs=None, aliases=(),
               always_dirs=()):
    """Which peer files to copy over this device's tree.

    A file is copied when its extension is allowed and either a peer's copy is
    newer by more than the tolerance, or this device has no copy of it and the
    path shape fits this tree. See path_fits.

    The comparison is per file on purpose. Restoring a whole backup would roll
    back every game played elsewhere since that backup was taken: play one game
    here, another there, and the newer tree wins everywhere, including for the
    game it knows nothing about. That is the rollback of 2026-09-03 again, at
    the scale of a whole frontend.

    Nothing is deleted and nothing is decided by a dialog. A prompt listing
    hundreds of files could not be answered.
    """
    if dirs is None:
        dirs = tree_dirs(local)
    tops = top_dirs(local) if aliases else set()
    plan = []
    for rel in sorted(peers):
        target = alias_path(rel, aliases, tops) if aliases else rel
        if target is None:
            continue
        # The allow list is checked on the aliased path, because always_dirs
        # names the directory THIS device uses, not the peer's name for it.
        if not is_restorable(target, allowed, always_dirs):
            continue
        peer_mtime, source, label = peers[rel]
        local_mtime = local.get(target)
        if local_mtime is None:
            # A set that trusts every file trusts every shape too. There is no
            # layout clash to guard against inside one game's save directory,
            # and shadPS4 keeps a save's metadata one level deeper, at
            # SPRJ0005/sce_sys/param.sfo. Refusing that delivers a save without
            # the file that describes it.
            if allowed is not None and not path_fits(target, dirs):
                continue
        elif peer_mtime - local_mtime <= tolerance:
            continue
        plan.append((target, source, label, peer_mtime, local_mtime))
    return sorted(plan, key=lambda row: row[0])


def overwrite_paths(plan, root):
    """The local files a merge plan would replace.

    A file the plan only adds has nothing to lose, so it is left out. The vault
    then holds a short list instead of a copy of the whole tree, which is what
    makes snapshotting a frontend affordable.
    """
    root = Path(root)
    return [root / rel for rel, _src, _label, _peer, local_mtime in plan
            if local_mtime is not None]


def apply_merge(plan, root, dry_run=False):
    """Copy the planned files into the tree. Returns (copied, failed).

    copy2 is what keeps the mtime, and the mtime is the whole comparison: a
    file copied with a fresh timestamp would look newer than the save it came
    from and win every later comparison against the device that wrote it.

    A failed copy is counted and logged, never raised. The frontend still has
    to start.
    """
    root = Path(root)
    copied = failed = 0
    for rel, source, label, peer_mtime, local_mtime in plan:
        log("tree: %s from %s (%s over %s)"
            % (rel, label, human_time(peer_mtime), human_time(local_mtime)))
        if dry_run:
            copied += 1
            continue
        target = root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
            copied += 1
        except OSError as exc:
            log("tree: could not copy %s: %s" % (rel, exc))
            failed += 1
    return copied, failed


# Every device shares ONE folder and owns ONE directory inside it. These are
# the keys savepick cannot work without. The old two-folder shape had
# incoming_folder/peer_id instead, and it fails this check, which resolves to
# "not configured", which already means no restore and a normal launch.
SYNC_KEYS = ("url", "apikey", "folder", "hub_id", "device_dir")


def sync_settings():
    """The syncthing block, or None when it is missing or in the old shape."""
    cfg = load_config().get("syncthing") or {}
    if not all(cfg.get(key) for key in SYNC_KEYS):
        return None
    return cfg


def device_label(cfg, dirname):
    """The display name for a device directory.

    Never guessed. The old code read the backup's os field and called any
    Linux peer a Steam Deck, which could only ever describe two devices. An
    unnamed directory shows as its own name.
    """
    return ((cfg.get("device_names") or {}).get(dirname)) or dirname


def hub_label(cfg):
    """What to call the server in a message."""
    return cfg.get("hub_name") or "the server"


def syncthing_get(cfg, path):
    base_url = cfg.get("url")
    if not base_url:
        log("syncthing query failed: no url configured")
        return None
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"X-API-Key": cfg.get("apikey")})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except Exception as exc:
        log("syncthing query failed: %s" % exc)
        return None


def syncthing_post(cfg, path, payload=None, timeout=10):
    """POST to Syncthing. True on success, False on any failure. Never raises."""
    url = cfg["url"].rstrip("/") + path
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"X-API-Key": cfg["apikey"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as exc:
        # A timeout on /rest/db/scan is expected, not a failure: the scan is
        # synchronous and outlives the request, and it carries on server-side,
        # which is all the poke wanted. urllib reports it as socket.timeout,
        # TimeoutError or a URLError wrapping one, so match on all three.
        if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in str(exc):
            log("syncthing post %s is still running server-side" % path)
        else:
            log("syncthing post %s failed: %s" % (path, exc))
        return False


def syncthing_patch(cfg, path, payload):
    """PATCH to Syncthing. The config endpoints refuse POST with a 405."""
    url = cfg["url"].rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="PATCH",
                                 headers={"X-API-Key": cfg["apikey"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:
        log("syncthing patch %s failed: %s" % (path, exc))
        return False


def folder_root(cfg):
    """Where the shared folder lives on this device, from Syncthing's config.

    Syncthing is the one authority on this. Keeping a second copy of the path
    in savepick.json would be a second thing to get wrong on a move.
    """
    folder = cfg.get("folder")
    if not folder:
        return None
    data = syncthing_get(cfg, "/rest/config/folders/%s" % folder)
    if not data:
        return None
    path = data.get("path")
    if not path:
        return None
    return Path(path).expanduser()


def peer_dirs(cfg):
    """Every device directory in the shared folder except this device's own.

    Each device owns exactly one directory and writes nowhere else, which is
    what keeps two ludusavi mapping.yaml indexes from ever meeting. Anything
    starting with a dot is Syncthing's own (.stfolder, .stversions).
    """
    root = folder_root(cfg)
    if root is None:
        return []
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        log("cannot read the shared folder: %s" % exc)
        return []
    mine = (cfg.get("device_dir") or "").casefold()
    return [p for p in entries
            if p.name.casefold() != mine and not p.name.startswith(".")]


def hub_connected(cfg):
    """True when Syncthing has a live connection to the hub."""
    conns = syncthing_get(cfg, "/rest/system/connections") or {}
    return bool((conns.get("connections") or {})
                .get(cfg["hub_id"], {}).get("connected"))


REMOTE_NEED_PAGES = 8
REMOTE_NEED_PER_PAGE = 200


def remote_need(cfg, prefix=None):
    """What the hub still needs, as (items, bytes), or None if unreadable.

    `prefix` limits the answer to one directory, so a backlog somewhere else
    in the folder cannot hold up this game's confirmation.

    This replaces /rest/db/completion, which cannot be trusted for this.
    2026-09-21 on the Deck (Syncthing v2.1.2): completion said 99.85% with
    8.2 MB, 20 items and 28 deletes outstanding, while remoteneed said the hub
    needed nothing and the hub already held the save on disk. savepick sat at
    "99%" for its full 180 seconds and then told him NOT SYNCED for a save
    that had arrived. remoteneed agreed with the disk on both machines.
    """
    items = 0
    total = 0
    for page in range(1, REMOTE_NEED_PAGES + 1):
        data = syncthing_get(
            cfg, "/rest/db/remoteneed?folder=%s&device=%s&page=%d&perpage=%d"
            % (urllib.parse.quote(cfg["folder"]), cfg["hub_id"],
               page, REMOTE_NEED_PER_PAGE))
        if data is None:
            return None
        rows = data.get("files")
        if rows is None:
            return None
        for row in rows:
            name = str(row.get("name") or "").replace("\\", "/")
            if prefix and not name.startswith(prefix):
                continue
            items += 1
            total += row.get("size") or 0
        if len(rows) < REMOTE_NEED_PER_PAGE:
            return items, total
    # More pages than we will read. Saying "nothing left" from a partial
    # answer is the one mistake that matters here.
    log("syncthing: the hub needs more than %d items; not reading further"
        % (REMOTE_NEED_PAGES * REMOTE_NEED_PER_PAGE))
    return max(items, 1), total


def hub_holds(cfg, rel):
    """True when Syncthing says the hub holds this exact file.

    Positive proof. "The hub needs nothing" is also true in the moment before
    the hub has heard about the file at all, so it is never enough on its own.
    """
    data = syncthing_get(cfg, "/rest/db/file?folder=%s&file=%s"
                         % (urllib.parse.quote(cfg["folder"]),
                            urllib.parse.quote(rel)))
    if not data:
        return None
    for entry in data.get("availability") or []:
        if entry.get("id") == cfg["hub_id"]:
            return True
    return False


def newest_backup_file(cfg, game):
    """One file from this game's newest backup here, relative to the folder.

    One file is enough to prove the hub took the backup, and walking a whole
    save set to pick a better one would cost more than the wait it guards.
    """
    root = folder_root(cfg)
    name = backup_dir_name(cfg, game) if root is not None else None
    if not name:
        return None
    mine = root / (cfg.get("device_dir") or "") / name
    try:
        backups = sorted(p for p in mine.iterdir()
                         if p.is_dir() and p.name.startswith("backup-"))
    except OSError:
        return None
    if not backups:
        return None
    for dirpath, _dirs, files in os.walk(str(backups[-1])):
        for entry in sorted(files):
            full = Path(dirpath) / entry
            return str(full.relative_to(root)).replace(os.sep, "/")
    return None


def sync_progress(cfg):
    """(percent, connected) for the hub, or (None, None) if it cannot be read.

    The fallback for a Syncthing with no /rest/db/remoteneed. The percentage
    is of the WHOLE folder, so one save is a rounding error in it.
    """
    comp = syncthing_get(cfg, "/rest/db/completion?folder=%s&device=%s"
                         % (cfg["folder"], cfg["hub_id"]))
    if comp is None:
        return None, None
    percent = int(comp.get("completion") or 0)
    if (comp.get("needBytes") or 0) > 0:
        percent = min(percent, 99)
    return percent, hub_connected(cfg)


def wait_for_sync(spinner, game=None):
    """Wait until the hub really holds the backup that just ran.

    True when Syncthing says the hub has it. False when it does not get there
    in time or the hub goes offline. None when there are no Syncthing
    settings, so there is nothing to wait for.

    Two questions, and both have to answer yes:

      the hub HOLDS a file from this backup   (positive proof)
      the hub NEEDS nothing else from it      (nothing still queued)

    The first alone would pass while the rest of a save set is still moving.
    The second alone would pass in the moment before the hub has even heard
    of the backup. Neither is asked of /rest/db/completion any more, because
    that number lied on the Deck: see remote_need().
    """
    cfg = sync_settings()
    if cfg is None:
        log("no syncthing settings; not waiting for the server")
        return None
    hub = hub_label(cfg)
    prefix = None
    sample = None
    if game:
        name = backup_dir_name(cfg, game)
        if name:
            prefix = "%s/%s/" % (cfg.get("device_dir") or "", name)
        sample = newest_backup_file(cfg, game)
        if sample is None:
            log("syncthing: no backup file to track for %s" % game)
    started = time.monotonic()
    biggest = 0
    shown = None
    holds = False
    while time.monotonic() - started < SYNC_WAIT_SECONDS:
        need = remote_need(cfg, prefix)
        if need is None:
            return wait_for_sync_by_completion(spinner, cfg, hub, started, shown)
        items, bytes_left = need

        if sample is not None and not holds:
            answer = hub_holds(cfg, sample)
            if answer is None:
                return wait_for_sync_by_completion(spinner, cfg, hub, started, shown)
            holds = answer

        if items == 0 and (holds or sample is None):
            log("syncthing: %s has the backup" % hub)
            spinner.update("%s has your save." % hub, 100)
            return True

        if not hub_connected(cfg) and time.monotonic() - started > SYNC_OFFLINE_GRACE:
            log("syncthing: %s went offline with %d item(s) left" % (hub, items))
            return False

        # The bar measures THIS backup, not the 5.6 GB folder around it.
        # Against the folder, one save moved the number by a hundredth of a
        # percent and the window read 99% from the first second.
        biggest = max(biggest, bytes_left)
        if biggest > 0:
            percent = max(0, min(99, int(100.0 * (biggest - bytes_left) / biggest)))
            text = ("Sending to %s ... %d%% (%s to go)"
                    % (hub, percent, human_bytes(bytes_left)))
        elif items > 0:
            percent = None
            text = "Sending %d item(s) to %s ..." % (items, hub)
        else:
            percent = None
            text = "Waiting for %s to confirm ..." % hub
        if text != shown:
            log("syncthing: %s" % text)
            shown = text
        spinner.update(text, percent)
        time.sleep(1.5)
    log("syncthing: gave up waiting for %s" % hub)
    return False


def wait_for_sync_by_completion(spinner, cfg, hub, started, shown):
    """The old folder-completion wait, for a Syncthing without remoteneed.

    It cannot tell this game's bytes from the folder's, so its percentage is
    of the whole folder. It is a fallback and nothing else.
    """
    log("syncthing: no remoteneed answer; falling back to folder completion")
    while time.monotonic() - started < SYNC_WAIT_SECONDS:
        percent, connected = sync_progress(cfg)
        if percent is None:
            return None
        if percent >= 100:
            log("syncthing: %s is up to date" % hub)
            spinner.update("%s has your save." % hub, 100)
            return True
        if not connected and time.monotonic() - started > SYNC_OFFLINE_GRACE:
            log("syncthing: %s is offline at %d%%" % (hub, percent))
            return False
        if percent != shown:
            spinner.update("Sending to %s ... %d%%" % (hub, percent), percent)
            shown = percent
        time.sleep(1.5)
    log("syncthing: gave up waiting for %s" % hub)
    return False


# ---------------------------------------------------------------- pad cancel
# The Deck's dialogs do not always draw under gamescope. On 2026-09-05 the
# conflict dialog ran its full timeout with nothing on screen. A wait with no
# time cap and an invisible window would mean the game never starts, so the
# gamepad has to be able to end it on its own.

PAD_RESCAN_SECONDS = 10.0


class PadCancel:
    """B on any pad means "stop waiting and play now"."""

    def __init__(self):
        self._fds = []
        self._scanned = 0.0
        # Linux reads discrete press events, so nothing is "still held".
        self._seen_release = not is_windows()

    def _rescan(self):
        now = time.monotonic()
        if self._fds and now - self._scanned < PAD_RESCAN_SECONDS:
            return
        self.close()
        self._scanned = now
        try:
            self._fds = open_gamepads()
        except Exception as exc:
            log("pad cancel unavailable: %s" % exc)
            self._fds = []

    def _pressed_linux(self):
        self._rescan()
        for fd in self._fds:
            while True:
                try:
                    data = os.read(fd, JS_EVENT_SIZE)
                except (BlockingIOError, OSError):
                    break
                if not data:
                    break
                if gamepad_choice(parse_js_event(data)) is True:
                    log("pad cancel: B")
                    return True
        return False

    def _pressed_windows(self):
        mask = windows_pad_state()
        if mask is None:
            return False
        # Ignore whatever is still held from starting the game.
        if not self._seen_release:
            if not mask:
                self._seen_release = True
            return False
        if pad_choice(mask) is True:
            log("pad cancel: B")
            return True
        return False

    def pressed(self):
        try:
            return self._pressed_windows() if is_windows() else self._pressed_linux()
        except Exception as exc:
            log("pad cancel failed: %s" % exc)
            return False

    def close(self):
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds = []


class SpinnerOrPadCancel:
    """Either the spinner's Cancel button or B on the pad ends the wait."""

    def __init__(self, spinner):
        self._spinner = spinner
        self._pad = PadCancel()

    def pressed(self):
        if self._spinner is not None and self._spinner.cancelled():
            log("spinner cancelled")
            return True
        return self._pad.pressed()

    def close(self):
        self._pad.close()


# ------------------------------------------------- incoming sync, before we look
# The 2026-09-05 rollback: Syncthing finished pulling the other machine's
# backup at 13:57:15. savepick had compared timestamps at 13:56:04 and offered
# to keep a save 20 hours older. It was right about what was on disk. The disk
# was 71 seconds stale.
#
# So savepick asks Syncthing directly and waits for its own answer. There is no
# time cap, by his decision: the wait ends when Syncthing says it is done, when
# you cancel, or when Syncthing errors.

SYNC_POLL_SECONDS = 1.5
SYNC_SETTLE_SECONDS = 6.0
# The settle window is the one part of the wait whose length is known, so the
# bar fills across it. It is redrawn faster than the poll interval for that.
SETTLE_TICK_SECONDS = 0.25
SYNC_MISS_LIMIT = 5
SCAN_POKE_TIMEOUT = 3
# Syncthing retries a failed pull about once a minute. Give its own retry a
# full turn plus a margin. If the errors are still there after that, waiting
# longer just hangs on a spinner, so savepick stops waiting and launches on the
# save already here.
#
# The old code reverted the folder at this point. That was safe only while the
# incoming folder was receive-only and owned nothing. The shared folder holds
# this device's own backups, so a revert could discard a session that has not
# reached the hub yet.
SYNC_HEAL_AFTER_SECONDS = 90.0


def incoming_ready(status):
    """True when Syncthing says the incoming folder holds everything.

    Every condition comes from Syncthing's own /rest/db/status. savepick never
    infers this from file times or from the passage of time.
    """
    if not status:
        return False
    if status.get("state") != "idle":
        return False
    if (status.get("needBytes") or 0) > 0:
        return False
    if (status.get("needTotalItems") or 0) > 0:
        return False
    if (status.get("errors") or 0) > 0:
        return False
    if status.get("error"):
        return False
    return True


def sync_percent(status):
    """How much of the incoming folder is here, 0 to 100. Spinner text only."""
    if not status:
        return 0
    total = status.get("globalBytes") or 0
    need = status.get("needBytes") or 0
    if total <= 0:
        return 100 if need == 0 else 0
    return max(0, min(100, int(round(100.0 * (total - need) / total))))


def human_bytes(count):
    """A byte count a player can read. Never more than one decimal place."""
    value = float(count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0


def wait_message(status, hub):
    """What the window says about one status, and how full the bar is.

    Returns (text, percent). A percent of None means there is nothing real to
    measure, so the bar pulses instead of claiming a number.

    The old text said "Getting saves from <hub> ... 100%" for a whole 51
    second scan, because the percent counts BYTES left to transfer and there
    were none left. One line, one number, and both of them true, for a window
    that was not waiting on a transfer at all. It read as a hang.

    So the state Syncthing reports is what the window says it is waiting for.
    """
    status = status or {}
    need_bytes = status.get("needBytes") or 0
    need_items = status.get("needTotalItems") or 0
    state = status.get("state") or "unknown"
    if need_bytes > 0:
        # 7.9 MB left of 5.6 GB rounds to 100, and "100% (7.9 MB to go)" is a
        # sentence that argues with itself. Seen on the Deck 2026-09-21.
        # Nothing left to copy is the only thing allowed to say 100.
        percent = min(sync_percent(status), 99)
        return ("Copying saves from %s, %d%% (%s to go)"
                % (hub, percent, human_bytes(need_bytes)), percent)
    if need_items > 0:
        # Deletes and directories weigh nothing, so there is no percentage to
        # show here. 2026-09-10: a prune of two old backups was exactly this.
        return ("Applying %d change(s) from %s ..." % (need_items, hub), None)
    if state == "scanning":
        return ("Nothing to copy from %s. Checking this device's files ..." % hub,
                None)
    if state == "syncing":
        return ("Finishing up with %s ..." % hub, None)
    return ("Checking with %s (%s) ..." % (hub, state), None)


def settle_percent(waited, settle):
    """How full the bar is through the settle window, 0 to 100."""
    if settle <= 0:
        return 100
    return max(0, min(100, int(round(100.0 * waited / settle))))


def backup_dir_name(cfg, game):
    """The directory ludusavi keeps one game in, asked of ludusavi itself.

    The name is the same under every device directory, because ludusavi builds
    it from the game's own name. It is never built here: ludusavi rewrites the
    characters a filesystem refuses, which is why the hub holds
    "Dark Souls II_ Scholar of the First Sin".

    This device is asked first, then each peer, because a game played only on
    another device has no backup directory here to name.
    """
    lookups = [[]]
    lookups.extend(["--path", str(peer)] for peer in peer_dirs(cfg))
    for extra in lookups:
        data = run_json(["--no-manifest-update", "backups", "--api"]
                        + list(extra) + [game])
        entry = ((data or {}).get("games") or {}).get(game) or {}
        path = (entry.get("backupPath") or "").replace("\\", "/").rstrip("/")
        if path:
            return path.rsplit("/", 1)[-1]
    return None


def scan_subs(cfg, game, devices=None):
    """The paths a scan has to cover for one game, relative to the folder.

    `devices` names the device directories to cover. The default is this
    device's own, which is the only one anything here ever writes to: ludusavi
    writes a backup and prunes old ones, both inside it and both at exit.
    Every other device's directory is written by Syncthing itself, and
    Syncthing indexes what it writes.

    Scanning the whole folder cost 51.6 seconds on the Windows PC on
    2026-09-21 with nothing to transfer and nothing changed: 28,889 files and
    14,087 directories, every retained backup of every game for every device.
    Measured the same day for one game across two device directories: 0.5
    seconds for Dark Souls II, 41 seconds for the RetroBat save set, which
    holds 4,696 files per backup. Half of that 41 seconds was the peer's
    directory, which no scan here can ever learn anything about.

    An empty list means there is nothing of this game to scan.
    """
    root = folder_root(cfg)
    if root is None:
        return []
    name = backup_dir_name(cfg, game)
    if not name:
        log("syncthing: ludusavi names no backup directory for %s; no scan" % game)
        return []
    if devices is None:
        devices = [cfg.get("device_dir") or ""]
    wanted = {str(d).casefold() for d in devices if d}
    try:
        here = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        log("cannot read the shared folder: %s" % exc)
        return []
    # Anything starting with a dot is Syncthing's own (.stfolder,
    # .stversions). It is never a device and is never scanned as one.
    return ["%s/%s" % (d.name, name) for d in here
            if not d.name.startswith(".")
            and d.name.casefold() in wanted and (d / name).is_dir()]


SCAN_WAIT_SECONDS = 120.0


def wait_for_scan(cfg, spinner=None, poll=SYNC_POLL_SECONDS,
                  limit=SCAN_WAIT_SECONDS):
    """Wait until Syncthing has finished looking at what just changed.

    True when the folder went idle, False when it did not in time.

    /rest/db/completion answers from the index, not from the disk. A backup
    written seconds ago is not in the index yet, so the hub needs nothing of
    it and the exit wait would call that done. It is not done. It has not been
    seen. So the scan is asked for and waited out before the hub is asked
    anything.
    """
    folder = cfg["folder"]
    started = time.monotonic()
    while time.monotonic() - started < limit:
        status = syncthing_get(cfg, "/rest/db/status?folder=%s" % folder)
        if status is None:
            return False
        if status.get("state") == "idle":
            return True
        if spinner is not None:
            spinner.update("Adding your save to %s ..." % folder, None)
        time.sleep(poll)
    log("syncthing: %s is still scanning after %.0fs; carrying on" % (folder, limit))
    return False


def poke_syncthing(cfg, subs=None):
    """Ask Syncthing to look now, rather than waiting for its own schedule.

    `subs` names the folder paths to scan. None scans the whole folder, which
    is a minute or more on a hub this size. An empty list scans nothing.
    """
    folder = cfg["folder"]
    syncthing_post(cfg, "/rest/system/resume?device=%s" % cfg["hub_id"])
    folder_cfg = syncthing_get(cfg, "/rest/config/folders/%s" % folder)
    if folder_cfg and folder_cfg.get("paused"):
        log("syncthing: %s is paused; resuming it" % folder)
        syncthing_patch(cfg, "/rest/config/folders/%s" % folder, {"paused": False})
    if subs is not None and not subs:
        # Asked for explicitly. The caller says what it means by it.
        return
    query = "/rest/db/scan?folder=%s" % urllib.parse.quote(folder)
    if subs:
        query += "".join("&sub=%s" % urllib.parse.quote(sub) for sub in subs)
        log("syncthing: scanning %s" % ", ".join(subs))
    # /rest/db/scan is synchronous and a scan can take longer than the request.
    # The scan carries on server-side, so a short timeout here is the poke we
    # wanted, not a failure.
    syncthing_post(cfg, query, timeout=SCAN_POKE_TIMEOUT)


def wait_for_incoming(spinner=None, cancel=None,
                      poll=SYNC_POLL_SECONDS, settle=SYNC_SETTLE_SECONDS,
                      heal_after=SYNC_HEAL_AFTER_SECONDS):
    """Wait until Syncthing says this machine holds the other machine's backups.

    True   Syncthing confirmed it.
    False  you cancelled.
    None   no Syncthing settings, or Syncthing could not be read.

    Only True is a green light. main() refuses to restore on anything else.

    The settle window exists because a folder reads idle with nothing needed in
    the seconds right after a connection, before the peer's index arrives.
    Declaring victory there would repeat the original bug with extra steps.
    """
    cfg = sync_settings()
    if cfg is None:
        log("no syncthing settings; cannot confirm the backups are current")
        return None
    hub = hub_label(cfg)
    folder = cfg["folder"]
    # No scan before a launch. Nothing on this device writes to the shared
    # folder except the exit backup, which scans its own directory when it
    # runs. Every peer directory here is written by Syncthing and indexed by
    # Syncthing. A scan could not change one answer below, and asking for the
    # whole folder was the entire 51 to 126 second wait he was looking at.
    poke_syncthing(cfg, [])

    connected_since = None
    ready_since = None
    errors_since = None
    misses = 0
    shown = None
    while True:
        if cancel is not None and cancel.pressed():
            log("syncthing: you cancelled the wait for %s" % hub)
            return False

        if connected_since is None:
            conns = syncthing_get(cfg, "/rest/system/connections")
            if conns is None:
                misses += 1
                if misses >= SYNC_MISS_LIMIT:
                    log("syncthing: cannot read the API; giving up")
                    return None
                time.sleep(poll)
                continue
            misses = 0
            peer_state = (conns.get("connections") or {}).get(cfg["hub_id"]) or {}
            if not peer_state.get("connected"):
                if shown != "offline":
                    log("syncthing: waiting for %s to connect" % hub)
                    shown = "offline"
                if spinner is not None:
                    spinner.update("Waiting for %s to connect ..." % hub, None)
                time.sleep(poll)
                continue
            connected_since = time.monotonic()
            log("syncthing: %s is connected" % hub)
            if spinner is not None:
                spinner.update("Checking %s for new saves ..." % hub, None)
            # Never judge the folder on the same pass that saw the connection.
            time.sleep(poll)
            continue

        status = syncthing_get(cfg, "/rest/db/status?folder=%s" % folder)
        if status is None:
            misses += 1
            if misses >= SYNC_MISS_LIMIT:
                log("syncthing: cannot read %s; giving up" % folder)
                return None
            time.sleep(poll)
            continue
        misses = 0

        # The folder-level error STRING means the folder itself has stopped:
        # a missing path, a marker gone, no space. Syncthing will not retry
        # that on its own, so neither do we.
        if status.get("error"):
            log("syncthing: %s has stopped (%s)" % (folder, status.get("error")))
            return None

        # A pull-error COUNT is a different animal. Syncthing retries those by
        # itself, about once a minute, and they routinely clear.
        #
        # 2026-09-10 22:13:59: the desktop's ludusavi retention deleted two old
        # backups, Syncthing carried the deletes here, and its first pass could
        # not remove the directories because it tried the parents before the
        # children. 9 pull errors, "will be retried (wait=1m1s)". savepick
        # sampled 20 seconds in, gave up, and told him it could not get an
        # answer. Syncthing finished at 22:14:39, 40 seconds later.
        #
        # So a pull error is "not ready yet", never "give up". incoming_ready()
        # still refuses to green-light while the count is above zero, so this
        # can only ever end in a real recovery or a cancel.
        errors = status.get("errors") or 0
        if errors > 0:
            ready_since = None
            if errors_since is None:
                errors_since = time.monotonic()
            if shown != ("errors", errors):
                log("syncthing: %s has %d item(s) it could not sync yet; "
                    "waiting for its retry" % (folder, errors))
                shown = ("errors", errors)
            if spinner is not None:
                waited = int(time.monotonic() - errors_since)
                spinner.update("%s is retrying %d item(s), %ds of %ds ...\n"
                               "B or Cancel plays on this device now."
                               % (hub, errors, waited, int(heal_after)), None)
            if time.monotonic() - errors_since >= heal_after:
                log("syncthing: %s is still stuck after %.0fs; "
                    "launching on the save already here" % (folder, heal_after))
                return None
            time.sleep(poll)
            continue
        errors_since = None

        if incoming_ready(status):
            if ready_since is None:
                ready_since = time.monotonic()
            waited = time.monotonic() - ready_since
            if waited >= settle:
                log("syncthing: %s is current with %s" % (folder, hub))
                if spinner is not None:
                    spinner.update("Up to date with %s." % hub, 100)
                return True
            if spinner is not None:
                # The one wait that is always the same length, so the bar can
                # show it filling rather than pulse at nothing.
                spinner.update("Up to date with %s. Making sure, %ds ..."
                               % (hub, max(1, int(round(settle - waited)))),
                               settle_percent(waited, settle))
            time.sleep(min(poll, SETTLE_TICK_SECONDS))
            continue

        ready_since = None
        text, percent = wait_message(status, hub)
        if text != shown:
            log("syncthing: %s" % text)
            shown = text
        if spinner is not None:
            spinner.update(text, percent)
        time.sleep(poll)


# The status window. One line of status, a pulsing bar, and a log pane that
# stays hidden until you ask for it. The pane tails savepick's own log file,
# so it reports exactly what the run is doing and nothing has to be piped
# into it. The window is the only thing on screen: every child process runs
# with CREATE_NO_WINDOW, so no console flashes behind it.
WINDOWS_SPINNER = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
# A Marquee progress bar is drawn by comctl32 v6 and by nothing else. Without
# this line WinForms falls back to the classic control, which draws the bar as
# an empty box and never animates it. That is what the window had been doing:
# a minute of real work behind a bar that looked broken.
[System.Windows.Forms.Application]::EnableVisualStyles()
$ErrorActionPreference = "SilentlyContinue"

$statusFile = "__STATUS__"
$logFile = "__LOG__"
$stateFile = "__STATE__"
$collapsedHeight = 196
$expandedHeight = 530

$form = New-Object System.Windows.Forms.Form
$form.Text = "__TITLE__"
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.FormBorderStyle = "FixedDialog"
$form.ControlBox = $false
$form.Size = New-Object System.Drawing.Size(600,$collapsedHeight)

$label = New-Object System.Windows.Forms.Label
$label.Text = "__TEXT__"
$label.Location = New-Object System.Drawing.Point(20,22)
$label.Size = New-Object System.Drawing.Size(545,40)
$form.Controls.Add($label)

# A Marquee bar is drawn by comctl32 v6 and by nothing else. Where visual
# styles are off it degrades to an empty box that never moves, which is what
# his window had been showing for a minute at a time. Measured on the Windows
# PC 2026-09-21: RenderWithVisualStyles is False there even after asking for
# them. So where the marquee cannot animate, the bar is swept by hand.
$script:canMarquee = [System.Windows.Forms.Application]::RenderWithVisualStyles
$script:pulseValue = 0
$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Minimum = 0
$bar.Maximum = 100
if ($script:canMarquee) {
  $bar.Style = "Marquee"
  $bar.MarqueeAnimationSpeed = 30
} else {
  $bar.Style = "Continuous"
}
$bar.Location = New-Object System.Drawing.Point(20,72)
$bar.Size = New-Object System.Drawing.Size(545,24)
$form.Controls.Add($bar)

$details = New-Object System.Windows.Forms.Button
$details.Location = New-Object System.Drawing.Point(20,110)
$details.Size = New-Object System.Drawing.Size(110,28)
$form.Controls.Add($details)

$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.ReadOnly = $true
$box.WordWrap = $false
$box.ScrollBars = "Both"
$box.Font = New-Object System.Drawing.Font("Consolas",8.25)
$box.BackColor = [System.Drawing.Color]::White
$box.Location = New-Object System.Drawing.Point(20,150)
$box.Size = New-Object System.Drawing.Size(545,340)
$box.Visible = $false
$form.Controls.Add($box)

$script:open = $false
if (Test-Path $stateFile) {
  if ((Get-Content $stateFile -Raw).Trim() -eq "1") { $script:open = $true }
}
$script:lastLog = ""

function Sync-Layout {
  $box.Visible = $script:open
  if ($script:open) {
    $form.Height = $expandedHeight
    $details.Text = "Hide log"
  } else {
    $form.Height = $collapsedHeight
    $details.Text = "Show log"
  }
}

function Sync-Log {
  if (-not $script:open) { return }
  if (-not (Test-Path $logFile)) { return }
  $lines = Get-Content $logFile -Tail 400
  if ($lines -eq $null) { return }
  $text = ($lines -join "`r`n")
  if ($text -ne $script:lastLog) {
    $script:lastLog = $text
    $box.Text = $text
    $box.SelectionStart = $box.Text.Length
    $box.ScrollToCaret()
  }
}

$details.Add_Click({
  $script:open = -not $script:open
  Sync-Layout
  Sync-Log
  Set-Content -Path $stateFile -Value ([int][bool]$script:open) -Encoding ascii
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 300
# Every status line ends in "|<percent>", or "|-" for a bar that pulses.
# A line that does not match is a half-written file, so the window keeps what
# it has rather than flickering the bar back to pulsing for one tick.
$timer.Add_Tick({
  if (Test-Path $statusFile) {
    $line = (Get-Content $statusFile -Raw)
    if ($line -match "__DONE__") { $timer.Stop(); $form.Close(); return }
    elseif ($line -match "^(.*)\|(-|\d{1,3})\s*$") {
      $label.Text = $matches[1].Trim()
      $pct = $matches[2]
      if ($pct -eq "-") {
        if ($script:canMarquee) {
          if ($bar.Style -ne "Marquee") {
            $bar.Style = "Marquee"
            $bar.MarqueeAnimationSpeed = 30
          }
        } else {
          # One sweep every four seconds. It says "still working" without
          # claiming a number savepick does not have.
          if ($bar.Style -ne "Continuous") { $bar.Style = "Continuous" }
          $script:pulseValue = ($script:pulseValue + 8) % 104
          $bar.Value = [Math]::Min(100, $script:pulseValue)
        }
      } else {
        if ($bar.Style -ne "Continuous") {
          $bar.MarqueeAnimationSpeed = 0
          $bar.Style = "Continuous"
        }
        $value = [Math]::Min(100, [int]$pct)
        # Windows animates a progress bar TOWARDS its value, so a bar told to
        # jump is still crawling when the window closes. Overshooting by one
        # and coming back lands it at once.
        if ($value -lt 100) { $bar.Value = $value + 1 }
        else { $bar.Value = 100 }
        $bar.Value = $value
      }
    }
  }
  Sync-Log
})
$timer.Start()
Sync-Layout
Sync-Log
__CANCELBTN__
$form.ShowDialog() | Out-Null
'''

WINDOWS_SPINNER_CANCEL = r'''
$form.ControlBox = $true
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = "Play now"
$cancel.Location = New-Object System.Drawing.Point(445,110)
$cancel.Size = New-Object System.Drawing.Size(120,28)
$cancel.Add_Click({ $timer.Stop(); $form.Close() })
$form.Controls.Add($cancel)
$form.CancelButton = $cancel
'''


def status_line(text, percent):
    """One line the window can parse: the text, then the bar position.

    A percent of None means the bar pulses. Anything else fills it.
    """
    mark = "-" if percent is None else str(max(0, min(100, int(percent))))
    return "%s|%s" % (text.replace("\n", " "), mark)


def ps_path(path):
    """A path as a powershell double-quoted string body."""
    return str(path).replace("\\", "\\\\")


class Spinner:
    """A pulsing progress window. Every method is safe if the window never opened."""

    def __init__(self, title, text, cancellable=False):
        self._proc = None
        self._status = None
        self._script = None
        self._cancellable = cancellable
        try:
            if is_windows():
                self._start_windows(title, text)
            else:
                self._start_linux(title, text)
        except Exception as exc:
            log("spinner unavailable: %s" % exc)

    def _start_linux(self, title, text):
        candidates = zenity_candidates()
        if not candidates:
            return
        binary, env = candidates[0]
        cmd = [binary, "--progress", "--pulsate", "--auto-close",
               "--title=%s" % title, "--text=%s" % text, "--width=520"]
        if not self._cancellable:
            cmd.insert(4, "--no-cancel")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, env=env)

    def cancelled(self):
        """True once the user closed a cancellable spinner.

        A spinner that never opened can never be cancelled, so this stays
        False and the wait carries on under the gamepad and the log.
        """
        if not self._cancellable or self._proc is None:
            return False
        return self._proc.poll() is not None

    def _start_windows(self, title, text):
        status = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                             encoding="ascii", errors="replace")
        status.write(status_line(text, None))
        status.close()
        self._status = status.name
        script = tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                             encoding="ascii", errors="replace")
        script.write(WINDOWS_SPINNER
                     .replace("__TITLE__", title)
                     .replace("__TEXT__", text)
                     .replace("__CANCELBTN__",
                              WINDOWS_SPINNER_CANCEL if self._cancellable else "")
                     .replace("__STATUS__", ps_path(self._status))
                     .replace("__LOG__", ps_path(LOG_PATH))
                     .replace("__STATE__", ps_path(WINDOW_STATE_PATH)))
        script.close()
        self._script = script.name
        self._proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", self._script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **no_window())

    def update(self, text, percent=None):
        """Say what is happening, and how full the bar is.

        percent None pulses the bar, which is the honest answer whenever
        Syncthing has given no number to show.
        """
        line = text.replace("\n", " ")
        if self._status:
            try:
                with open(self._status, "w", encoding="ascii", errors="replace") as handle:
                    handle.write(status_line(line, percent))
            except OSError:
                pass
            return
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write("#%s\n" % line)
                self._proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def close(self):
        if self._status:
            self.update("__DONE__")
        elif self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()
            except (OSError, ValueError):
                pass
        if self._proc:
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
            except Exception:
                pass
        for path in (self._status, self._script):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


WINDOWS_WARN = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = "__TITLE__"
$form.Size = New-Object System.Drawing.Size(660,300)
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$body = New-Object System.Windows.Forms.Label
$body.Text = @'
__TEXT__
'@
$body.Font = New-Object System.Drawing.Font("Consolas",10)
$body.Location = New-Object System.Drawing.Point(20,20)
$body.Size = New-Object System.Drawing.Size(610,180)
$form.Controls.Add($body)

$ok = New-Object System.Windows.Forms.Button
$ok.Text = "OK"
$ok.Location = New-Object System.Drawing.Point(420,210)
$ok.Size = New-Object System.Drawing.Size(200,34)
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($ok)
$form.AcceptButton = $ok
$form.CancelButton = $ok

$script:left = __TIMEOUT__
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
  $script:left = $script:left - 1
  if ($script:left -le 0) { $timer.Stop(); $form.Close() }
})
$timer.Start()
$form.ShowDialog() | Out-Null
Write-Output "KEEP"
'''

WARNING_TIMEOUT_SECONDS = 60


def show_warning(title, text):
    """A single-button warning. Never blocks a launch: it always times out."""
    log("warning shown: %s" % text.replace("\n", " | "))
    if is_windows():
        script = (WINDOWS_WARN
                  .replace("__TITLE__", title)
                  .replace("__TEXT__", text)
                  .replace("__TIMEOUT__", str(WARNING_TIMEOUT_SECONDS)))
        run_powershell_dialog(script, watch_pad=True)
        return
    for binary, env in zenity_candidates():
        cmd = [binary, "--warning", "--title=%s" % title,
               "--text=%s" % text, "--width=620",
               "--timeout=%d" % WARNING_TIMEOUT_SECONDS]
        try:
            code, _out, err, _pad = run_dialog_with_gamepad(cmd, env)
        except (OSError, subprocess.SubprocessError) as exc:
            log("warning dialog unavailable on %s: %s" % (binary, exc))
            continue
        if code in (0, 1, 5) or code is None:
            return
        log("warning dialog exit %s, stderr=%r" % (code, err[:200]))
        if code not in STARTUP_FAILURES:
            return


def run_backup(game):
    """Back this game up. Returns (failed, detail).

    stdin is closed for the same reason as run_json: a ludusavi that inherits
    a live pipe can sit waiting on it instead of working.
    """
    cmd = [ludusavi_binary(), "--no-manifest-update", "backup", "--force", game]
    log("exit backup: %s" % " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                              stdin=subprocess.DEVNULL, **no_window())
        return proc.returncode != 0, (proc.stderr or proc.stdout or "")[:200]
    except (OSError, subprocess.SubprocessError) as exc:
        return True, str(exc)


def backup_on_exit(game):
    """Always back up, then wait until the other machine actually has it.

    There is no question here on purpose. Backing up only reads the save, and
    declining is the one action that leaves the other machine with stale data,
    which is exactly the failure this whole tool exists to prevent.
    """
    spinner = Spinner(APP_TITLE, "Backing up %s ..." % game)
    warning = None
    try:
        failed, detail = run_backup(game)

        if failed:
            log("exit backup failed: %s" % detail)
            warning = ("Backup FAILED\n\n"
                       "%s\n\n"
                       "Your save on this device is untouched, but nothing was\n"
                       "written to the sync folder. Do not play this game on\n"
                       "another device until this is sorted out." % game)
            return

        cfg = sync_settings()
        hub = hub_label(cfg or {})
        if cfg:
            # ludusavi has just written a backup and may have pruned an old
            # one. Ask Syncthing to look at that directory, and wait for it,
            # before asking whether the hub has the save: the hub is asked
            # from the index, and what has not been scanned is not in it.
            subs = scan_subs(cfg, game)
            if subs:
                spinner.update("Adding your save to the sync folder ...", None)
                poke_syncthing(cfg, subs)
                wait_for_scan(cfg, spinner)
            else:
                log("syncthing: nothing of %s in the shared folder here" % game)
        result = wait_for_sync(spinner, game)
        if result is True:
            spinner.update("Done. %s has this session." % hub)
            time.sleep(2)
        elif result is False:
            warning = ("NOT SYNCED\n\n"
                       "%s is backed up on this device, but Syncthing could not\n"
                       "reach %s.\n\n"
                       "Your progress is safe here. Do NOT play this game on\n"
                       "another device until it syncs, or you will lose this\n"
                       "session." % (game, hub))
        else:
            spinner.update("Backed up.")
            time.sleep(2)
    finally:
        spinner.close()
        if warning:
            show_warning("Save sync", warning)


# ---------------------------------------------------------------------------
# The vault
#
# ludusavi's own retention keeps the last N BACKUPS. The vault keeps the last N
# LIVE saves, copied aside in the moment before a restore overwrites them. That
# covers the one thing retention cannot: progress that no backup ever captured,
# after a crash or a failed exit backup.
#
# savepick only ever writes here. It never reads the vault and never restores
# from it. Recovery is a manual copy, guided by each snapshot's manifest.json.
# ---------------------------------------------------------------------------

VAULT_KEEP = 10

# manifest.json is ours. A save file with that name gets renamed instead.
VAULT_RESERVED = "manifest.json"


def vault_root():
    """Where snapshots live. Deliberately outside the Syncthing folders."""
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "savepick" / "vault"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return Path(base) / "savepick" / "vault"


def vault_slug(text):
    """A game name no filesystem will argue with."""
    kept = "".join(c if (c.isalnum() or c in "-_. ") else "_" for c in text)
    return kept.strip(" .") or "game"


def vault_dir_for(game, vault=None):
    root = Path(vault) if vault is not None else vault_root()
    return root / vault_slug(game)


def vault_snapshots(game, vault=None):
    """This game's snapshot directories, oldest first. Names sort by time."""
    directory = vault_dir_for(game, vault)
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if p.is_dir()),
                  key=lambda p: p.name)


def _new_snapshot_dir(game, vault=None):
    """Create and return an empty snapshot directory."""
    parent = vault_dir_for(game, vault)
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for index in range(100):
        candidate = parent / ("%s-%02d" % (stamp, index))
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise OSError("no free snapshot name under %s" % parent)


def _stored_name(name, used):
    """Keep the real filename where possible, so recovery is an obvious copy."""
    if name not in used:
        return name
    index = 1
    while ("%d_%s" % (index, name)) in used:
        index += 1
    return "%d_%s" % (index, name)


def needs_snapshot(action, live_mtime):
    """Only a restore that would overwrite an existing live save needs one.

    A fresh install has nothing to lose, so it must not be blocked.
    """
    return action == RESTORE and live_mtime is not None


def gate_restore_on_snapshot(action, ok):
    """A restore we could not take a snapshot for becomes a skip.

    His rule, and the project's: a skipped restore costs one manual sync, a
    lost live save costs hours.
    """
    if action != RESTORE:
        return action
    return RESTORE if ok else SKIP


def snapshot_live_saves(game, live_paths, vault=None, keep=VAULT_KEEP):
    """Copy the live save files aside. True only when every file was copied.

    An empty file list is a failure, not an empty snapshot. We cannot promise
    protection we did not give. A partial snapshot is deleted, because half a
    snapshot still looks like a recovery point.
    """
    if not live_paths:
        log("vault: ludusavi reported no live save files")
        return False
    directory = None
    try:
        directory = _new_snapshot_dir(game, vault)
        used = {VAULT_RESERVED}
        entries = []
        for path in live_paths:
            source = Path(path)
            stored = _stored_name(source.name, used)
            used.add(stored)
            shutil.copy2(source, directory / stored)
            entries.append({"stored": stored, "original": str(source)})
        manifest = {
            "game": game,
            "taken": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "Copied before a restore. Copy each file back to 'original'.",
            "files": entries,
        }
        (directory / VAULT_RESERVED).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as exc:
        log("vault: snapshot failed: %s" % exc)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        return False
    log("vault: kept %d live save file(s) in %s" % (len(entries), directory))
    try:
        prune_vault(game, keep=keep, vault=vault)
    except Exception as exc:
        # Housekeeping only. The snapshot already landed, which is the point.
        log("vault: prune failed: %s" % exc)
    return True


def prune_vault(game, keep=VAULT_KEEP, vault=None):
    """Delete all but the newest `keep` snapshots. Stray files are left alone."""
    snapshots = vault_snapshots(game, vault)
    for old in snapshots[:max(0, len(snapshots) - keep)]:
        shutil.rmtree(old, ignore_errors=True)
        log("vault: dropped old snapshot %s" % old.name)


# A restore of a large save over a slow disk. Generous, because the only thing
# worse than waiting is killing a restore half way through.
RESTORE_TIMEOUT_SECONDS = 900


def save_landed(game, backup_mtime, tolerance=TOLERANCE_SECONDS):
    """After a restore, did the live save end up matching the backup?

    This is the only question worth asking, and it is the reason savepick runs
    the restore itself instead of leaving it to wrap.

    ludusavi returns nonzero when ANY entry fails. A backup carries more than
    the save: on 2026-09-11 at 00:27 the Deck's backup held three Steam
    screenshots at Deck-only paths under /home/deck, which have no home on
    Windows. The .sl2 restored perfectly and the save was exactly right. wrap
    still exited 1, put up "Failed to restore save data", and never started
    the game.

    So savepick asks the disk, not the exit code.
    """
    if backup_mtime is None:
        return False
    live = newest_live_mtime(live_save_files(game))
    if live is None:
        return False
    return abs(live - backup_mtime) <= tolerance


def restore_now(game, backup_mtime, backup_name, peer_dir):
    """Restore this game ourselves. True when the save itself landed.

    --path pins the restore to the peer device savepick compared, and --backup
    pins it to that exact backup. Without them ludusavi would take whatever the
    config points at and whatever is newest there, so a sync landing between
    the comparison and the launch would restore something savepick never
    looked at.
    """
    if peer_dir is None:
        log("restore: no peer directory to restore from")
        return False
    args = ["--no-manifest-update", "restore", "--force", "--api",
            "--path", str(peer_dir)]
    if backup_name and backup_name != ".":
        args += ["--backup", backup_name]
    args.append(game)
    log("restoring: %s" % " ".join(args))
    data = run_json(args, timeout=RESTORE_TIMEOUT_SECONDS)
    if data is None:
        log("restore: ludusavi gave no readable answer")
    else:
        entry = (data.get("games") or {}).get(game) or {}
        for path, info in (entry.get("files") or {}).items():
            if info.get("failed"):
                log("restore FAILED: %s (%s)" % (path, info.get("error")))
    landed = save_landed(game, backup_mtime)
    log("restore: the save %s" % ("landed" if landed else "did NOT land"))
    return landed


def warn_downgraded(game):
    """You chose to go back to an older save. Say where the newer one is.

    This replaces ludusavi's --ask-downgrade, which only wrap has and which
    savepick can no longer reach now that wrap does no restoring. That flag
    asked "are you sure" a second time, which is the same question savepick's
    own dialog already asked with both dates on screen.

    Naming the vault snapshot is worth more. It is the one thing that turns a
    mis-pressed B from a lost session into a file copy.
    """
    where = "the savepick vault"
    try:
        snapshots = vault_snapshots(game)
        if snapshots:
            where = str(snapshots[-1])
    except Exception as exc:
        log("vault: could not name the newest snapshot: %s" % exc)
    show_warning(APP_TITLE,
                 "You kept the older backup for %s.\n\n"
                 "The newer save that was on this device is NOT gone. It was\n"
                 "copied aside first, here:\n\n%s\n\n"
                 "Copy the files back by hand to undo this." % (game, where))


def warn_restore_incomplete(game):
    """Say it plainly. Never let him play a save he did not expect."""
    show_warning(APP_TITLE,
                 "The restore did not finish.\n\n"
                 "%s starts on the save that was already on this device,\n"
                 "not the one from the backup.\n\n"
                 "Your previous save is also kept in the savepick vault.\n"
                 "The log says which files failed." % game)


GAME_FAILED_TO_START = 127
GAME_KILL_FAILED = 126

# How long the game gets to close its save files after we pass a stop signal
# on. Past this it is killed, because an exit backup that never runs loses the
# whole session.
GAME_STOP_SECONDS = 30

# The running game, so a signal handler can reach it.
CHILD = None
# Every stop signal we have taken, in order. A repeat is a force quit.
SIGNALS_SEEN = []
# One monotonic time, set when the first stop signal goes out.
STOP_DEADLINE = []
# Set by main from --borderless: hold the game's window borderless.
BORDERLESS = False


# ------------------------------------------------------------- the foreground
# Windows hands the foreground to whoever had it last, and savepick is started
# by Steam rather than by a click, so the game opens BEHIND Steam and he
# alt-tabs to it every time.
#
# SetForegroundWindow on its own is refused and returns success while doing
# nothing, which is the same trap as [[windows-screen-capture]]. What lifts
# the lock is AttachThreadInput: with this process's input queue attached to
# the foreground window's thread, this process counts as the foreground one
# and the call is honoured.
#
# The game is not always the process savepick started. RetroBat starts its
# own frontend, and the shadPS4 launcher is a python script. So the whole
# process tree under the child is eligible.
FOCUS_DEADLINE_SECONDS = 40.0
FOCUS_POLL_SECONDS = 0.5
FOCUS_SETTLE_SECONDS = 1.0


def process_tree(root_pid, parents):
    """root_pid and every process descended from it, from a {pid: parent} map.

    A game that launches through a wrapper is two or three processes deep, and
    only the last one owns a window.
    """
    tree = {root_pid}
    # A parent map can hold a cycle after pid reuse. Walking it a bounded
    # number of times cannot hang, and one pass per process is plenty.
    for _round in range(len(parents) + 1):
        grew = False
        for pid, parent in parents.items():
            if parent in tree and pid not in tree:
                tree.add(pid)
                grew = True
        if not grew:
            break
    return tree


def choose_window(windows, pids):
    """The window to raise, from (hwnd, pid, owner, title, visible) tuples.

    An owned window is a dialog or a tooltip, and a window with no title is
    usually a message-only helper. Neither is what a player is looking at.
    """
    for hwnd, pid, owner, title, visible in windows:
        if pid in pids and visible and not owner and (title or "").strip():
            return hwnd
    return None


def windows_process_parents():
    """{pid: parent pid} for every running process, or {} if it cannot be read."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    parents = {}
    try:
        entry = ENTRY()
        entry.dwSize = ctypes.sizeof(ENTRY)
        ok = kernel32.Process32First(snap, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return parents


def windows_top_level_windows():
    """Every top level window, as choose_window wants them."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    def keep(hwnd, _param):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        found.append((hwnd, int(pid.value),
                      int(user32.GetWindow(hwnd, 4) or 0),  # GW_OWNER
                      title, bool(user32.IsWindowVisible(hwnd))))
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(proto(keep), 0)
    return found


def windows_raise(hwnd):
    """Put one window in front, past the foreground lock. True if it took."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    front = user32.GetForegroundWindow()
    ours = user32.GetWindowThreadProcessId(hwnd, None)
    theirs = user32.GetWindowThreadProcessId(front, None) if front else 0
    mine = kernel32.GetCurrentThreadId()
    attached = []
    for thread in (theirs, ours):
        if thread and thread != mine and user32.AttachThreadInput(mine, thread, True):
            attached.append(thread)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread in attached:
            user32.AttachThreadInput(mine, thread, False)
    return bool(user32.GetForegroundWindow() == hwnd)


def linux_process_parents():
    """{pid: parent pid} from /proc, or {} when it cannot be read."""
    parents = {}
    for entry in glob.glob("/proc/[0-9]*/stat"):
        try:
            with open(entry, "r") as handle:
                text = handle.read()
        except OSError:
            continue
        # The command name sits in brackets and can hold spaces and brackets
        # of its own, so the fields after it are counted from the LAST ')'.
        close = text.rfind(")")
        if close < 0:
            continue
        fields = text[close + 2:].split()
        if len(fields) < 2:
            continue
        try:
            parents[int(text[:text.index(" ")])] = int(fields[1])
        except ValueError:
            continue
    return parents


def linux_window_ids(pids, run=None):
    """Window ids owned by these processes, newest last, from xdotool.

    xdotool finds a window by its _NET_WM_PID. A game that does not set it is
    invisible here, which is why nothing below treats an empty answer as a
    failure worth reporting twice.
    """
    runner = run or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, env=host_env()))
    found = []
    for pid in sorted(pids):
        try:
            done = runner([host_tool("xdotool"), "search", "--onlyvisible",
                           "--pid", str(pid)])
        except (OSError, subprocess.SubprocessError):
            return []
        for line in (done.stdout or "").split():
            if line.strip().isdigit():
                found.append(line.strip())
    return found


def linux_focus_is_ours(run=None):
    """False when the compositor does not publish which window is active.

    gamescope does not. It chooses the focused window itself, from what Steam
    tells it, and there is nothing here to reinforce. Measured on the Deck
    2026-09-21, on both :0 and :1:

        xdotool getactivewindow
        XGetWindowProperty[_NET_ACTIVE_WINDOW] failed (code=1)

    A desktop session answers it, so Desktop Mode still gets the raise. Game
    Mode stops here rather than retrying for forty seconds and then logging a
    failure that was never savepick's to have.
    """
    runner = run or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, env=host_env()))
    try:
        done = runner([host_tool("xdotool"), "getactivewindow"])
    except (OSError, subprocess.SubprocessError):
        return False
    return (done.stdout or "").strip().isdigit()


def linux_raise(window_id, run=None):
    """Ask the window manager to activate one window. True if it took.

    Under gamescope this may be ignored outright: gamescope picks the focused
    window itself and Steam tells it which app is in front. That is fine. The
    call costs nothing and the log says what happened.
    """
    runner = run or (lambda cmd: subprocess.run(
        cmd, capture_output=True, text=True, timeout=10, env=host_env()))
    tool = host_tool("xdotool")
    try:
        runner([tool, "windowactivate", "--sync", window_id])
        done = runner([tool, "getactivewindow"])
    except (OSError, subprocess.SubprocessError):
        return False
    return (done.stdout or "").strip() == str(window_id)


def focus_the_game(pid, deadline=FOCUS_DEADLINE_SECONDS,
                   poll=FOCUS_POLL_SECONDS, settle=FOCUS_SETTLE_SECONDS):
    """Raise the game's window once it exists. Never raises, never blocks a launch.

    It gives up the moment it succeeds, so a player who alt-tabs away a second
    later keeps what they chose. Everything here is best effort: a game that
    never opens a window, or a window this cannot reach, costs nothing but the
    deadline.
    """
    if not is_windows() and not os.environ.get("DISPLAY"):
        return False
    started = time.monotonic()
    time.sleep(settle)
    while time.monotonic() - started < deadline:
        try:
            if is_windows():
                pids = process_tree(pid, windows_process_parents())
                target = choose_window(windows_top_level_windows(), pids)
                raised = bool(target) and windows_raise(target)
            else:
                if not linux_focus_is_ours():
                    log("focus: the compositor owns the focus here; "
                        "leaving it alone")
                    return False
                pids = process_tree(pid, linux_process_parents())
                ids = linux_window_ids(pids)
                target = ids[-1] if ids else None
                raised = bool(target) and linux_raise(target)
            if raised:
                log("focus: raised the game's window")
                return True
        except Exception as exc:
            log("focus: gave up (%s)" % exc)
            return False
        time.sleep(poll)
    log("focus: no window of the game took the foreground in %.0fs" % deadline)
    return False


def watch_for_the_game_window(proc):
    """Run focus_the_game beside the game, never in front of it."""
    pid = getattr(proc, "pid", None)
    if not pid or (not is_windows() and not os.environ.get("DISPLAY")):
        return None
    thread = threading.Thread(target=focus_the_game, args=(pid,), daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- borderless
#
# A game like Dark Souls II offers exclusive fullscreen or a window with a title
# bar, and no borderless mode. --borderless takes the frame off the game's
# window and fits it to the monitor it is on. The game must be set to windowed
# in its own options; an exclusive fullscreen game has no frame to take off.
#
# It is independent of save sync. --no-sync runs a game through here for this
# alone, so a game Steam Cloud already covers never has its saves touched.
#
# Windows only. gamescope already shows every game fullscreen on the Deck.
#
# The watch lasts as long as the game. A launcher or splash window can come
# first, and some games put their frame back after a resolution change, so a
# window that has its frame again gets it taken off again.
BORDERLESS_POLL_SECONDS = 2.0
BORDERLESS_SETTLE_SECONDS = 1.0

WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WS_FRAME_BITS = (WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX
                 | WS_MAXIMIZEBOX | WS_SYSMENU)
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_FRAME_BITS = (WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE
                    | WS_EX_CLIENTEDGE | WS_EX_STATICEDGE)


def borderless_style(style, ex_style):
    """The window style with every part of the frame taken off."""
    return style & ~WS_FRAME_BITS, ex_style & ~WS_EX_FRAME_BITS


def is_borderless(style, ex_style, rect, monitor):
    """True when there is no frame left and the window covers its monitor."""
    return (not (style & WS_FRAME_BITS) and not (ex_style & WS_EX_FRAME_BITS)
            and tuple(rect) == tuple(monitor))


def choose_game_window(windows, pids, area):
    """The biggest window choose_window would accept, or None.

    A game can own a small window beside its main one (a crash reporter, an
    overlay), and the biggest is the one being played. `area(hwnd)` is a seam
    so a test can say how big each window is.
    """
    best = None
    best_area = -1
    for hwnd, pid, owner, title, visible in windows:
        if pid in pids and visible and not owner and (title or "").strip():
            size = area(hwnd)
            if size > best_area:
                best, best_area = hwnd, size
    return best


def _user32_for_borderless():
    """user32 with the pointer sized calls typed, so 64 bit styles survive."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    for name in ("GetWindowLongPtrW", "SetWindowLongPtrW"):
        func = getattr(user32, name, None)
        if func is None:
            continue
        func.restype = ctypes.c_ssize_t
        if name == "GetWindowLongPtrW":
            func.argtypes = [wintypes.HWND, ctypes.c_int]
        else:
            func.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    return user32


def windows_dpi_aware():
    """Ask for real pixels on this thread, never scaled ones.

    Without it, a 150 percent display reports its monitor as two thirds of
    its size, and the game is fitted to a rectangle that is too small.
    """
    import ctypes

    user32 = ctypes.windll.user32
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except AttributeError:
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except AttributeError:
        return False


def windows_window_rect(hwnd):
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def windows_monitor_rect(hwnd):
    """The whole monitor the window is mostly on, taskbar included."""
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not monitor:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def windows_make_borderless(hwnd):
    """Take the frame off one window and fit it to its monitor.

    Returns "done" when this changed it, "already" when there was nothing to
    do, or None when it cannot be done now (minimised, gone, refused).
    """
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    SW_RESTORE = 9
    SWP_NOZORDER = 0x0004
    SWP_NOOWNERZORDER = 0x0200
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040

    user32 = _user32_for_borderless()
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    monitor = windows_monitor_rect(hwnd)
    rect = windows_window_rect(hwnd)
    if monitor is None or rect is None:
        return None
    if is_borderless(style, ex_style, rect, monitor):
        return "already"
    if user32.IsZoomed(hwnd):
        # A maximised window keeps its maximised size on top of whatever is
        # set below, and snaps back to it later.
        user32.ShowWindow(hwnd, SW_RESTORE)
    new_style, new_ex = borderless_style(style, ex_style)
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, new_style)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_ex)
    left, top, right, bottom = monitor
    user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                        SWP_NOZORDER | SWP_NOOWNERZORDER | SWP_FRAMECHANGED
                        | SWP_SHOWWINDOW)
    after = windows_window_rect(hwnd)
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if after is not None and is_borderless(style, ex_style, after, monitor):
        return "done"
    return None


def _window_area(hwnd):
    rect = windows_window_rect(hwnd)
    if rect is None:
        return 0
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def keep_the_game_borderless(proc, poll=BORDERLESS_POLL_SECONDS,
                             settle=BORDERLESS_SETTLE_SECONDS,
                             make=None, find=None):
    """Hold the game's window borderless until the game exits.

    Never raises and never blocks the game. `make` and `find` are seams for a
    test; the real ones call Windows.
    """
    if make is None:
        make = windows_make_borderless
    if find is None:
        def find():
            pids = process_tree(proc.pid, windows_process_parents())
            return choose_game_window(windows_top_level_windows(), pids,
                                      _window_area)
    if make is windows_make_borderless:
        try:
            windows_dpi_aware()
        except Exception as exc:
            log("borderless: could not ask for real pixels (%s)" % exc)
    time.sleep(settle)
    fixed = {}
    while proc.poll() is None:
        try:
            target = find()
            if target:
                result = make(target)
                if result == "done":
                    fixed[target] = fixed.get(target, 0) + 1
                    if fixed[target] == 1:
                        log("borderless: took the frame off the game's window")
                    elif fixed[target] in (2, 10, 100):
                        log("borderless: the game put its frame back; "
                            "taken off again (%d times)" % fixed[target])
        except Exception as exc:
            log("borderless: gave up (%s)" % exc)
            return False
        time.sleep(poll)
    return bool(fixed)


def watch_to_keep_borderless(proc):
    """Run keep_the_game_borderless beside the game, when it was asked for."""
    if not BORDERLESS:
        return None
    if not is_windows():
        log("borderless: nothing to do here; this is for Windows only")
        return None
    if not getattr(proc, "pid", None):
        return None
    thread = threading.Thread(target=keep_the_game_borderless, args=(proc,),
                              daemon=True)
    thread.start()
    return thread


def launch(command, do_restore=False):
    """Start the game. ludusavi is not in this path at all.

    savepick does the restore (restore_now) and the exit backup
    (backup_on_exit) itself, which left `ludusavi wrap` with no job except
    spawning this same command. It was not free, though. It carried two ways
    to stop the game starting, and both of them happened:

      wrap returns nonzero when ANY entry of a save job fails. On 2026-09-11
      three Steam screenshots in the Deck's backup had no home on Windows, so
      wrap reported a restore that had actually worked as a failure, showed
      "Failed to restore save data", and never ran DS2.

      With `--infer steam` and no SteamAppId in the environment, wrap cannot
      work out which game this is and blocks on a GUI prompt with no timeout.
      Proven on the Deck on 2026-09-11: the command never ran and the process
      had to be killed. savepick's own no-SteamAppId path fed straight into
      that, so the one case where savepick gives up early was also the one
      case where the game would never start.

    A tool that owns the save decision must not also hold a veto over the game
    starting. `do_restore` stays in the signature only so old callers read the
    same; nothing here restores.

    The child is held in a module global so a signal handler can pass the
    signal on instead of killing it. subprocess.call cannot do that: it kills
    the child on any exception, which on 2026-09-17 ended two Bloodborne
    sessions on the Deck with no exit backup.
    """
    global CHILD
    log("launching: %s" % " ".join(command))
    try:
        proc = subprocess.Popen(command)
    except OSError as exc:
        # Nothing left to fall back to. Say so loudly rather than return 0 and
        # let Steam show a silent no-op.
        log("the game failed to start: %s" % exc)
        return GAME_FAILED_TO_START
    CHILD = proc
    watch_for_the_game_window(proc)
    watch_to_keep_borderless(proc)
    try:
        code = wait_for_child(proc)
    finally:
        CHILD = None
    log("the game exited with %s" % code)
    return code


def wait_for_child(proc):
    """Wait for the game, but never wait forever once we have asked it to stop.

    A signal handler sets STOP_DEADLINE. Until then this waits as long as the
    session lasts. After it, the game gets GAME_STOP_SECONDS to close its
    save files, then it is killed, so the exit backup still runs.
    """
    while True:
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        except KeyboardInterrupt:
            continue
        if STOP_DEADLINE and time.monotonic() >= STOP_DEADLINE[0]:
            log("the game did not stop in %ds; killing it" % GAME_STOP_SECONDS)
            try:
                proc.kill()
            except OSError as exc:
                log("could not kill the game: %s" % exc)
                return GAME_KILL_FAILED
            return proc.wait()


def confirm_incoming_sync():
    """Show a spinner and wait for Syncthing. Returns wait_for_incoming's answer."""
    hub = hub_label(sync_settings() or {})
    spinner = Spinner(APP_TITLE,
                      "Checking with %s ...\nB or Cancel plays on this device now."
                      % hub,
                      cancellable=True)
    cancel = SpinnerOrPadCancel(spinner)
    try:
        return wait_for_incoming(spinner, cancel)
    finally:
        cancel.close()
        spinner.close()


def warn_unconfirmed(synced):
    """Say plainly why savepick will not restore. Never silently carry on."""
    hub = hub_label(sync_settings() or {})
    if synced is False:
        head = "You stopped the sync check."
    else:
        head = "savepick could not get an answer from Syncthing."
    show_warning(APP_TITLE,
                 "%s\n\n"
                 "So savepick cannot tell whether the backups on %s are\n"
                 "current. It will NOT restore. The game starts on the save\n"
                 "that is already on this device.\n\n"
                 "If you played on another device, quit now and let Syncthing\n"
                 "finish."
                 % (head, hub))


def split_command(argv):
    if "--" not in argv:
        return []
    return argv[argv.index("--") + 1:]


def head_of(argv):
    """The switches before `--`. What comes after is the game's own."""
    if "--" not in argv:
        return list(argv)
    return argv[:argv.index("--")]


def tree_name(argv):
    """The save set named by --tree, or None for the single game path.

    A frontend is a non-Steam shortcut, so its Steam id maps to nothing in
    ludusavi's manifest. The save set is named outright instead.
    """
    if "--tree" not in argv:
        return None
    index = argv.index("--tree") + 1
    if index >= len(argv) or argv[index] == "--":
        return None
    return argv[index]


def stop_child(signum):
    """Pass a stop signal to the game, and start the clock on its grace period."""
    global STOP_DEADLINE
    if not STOP_DEADLINE:
        STOP_DEADLINE = [time.monotonic() + GAME_STOP_SECONDS]
    proc = CHILD
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.send_signal(signum)
    except OSError as exc:
        log("could not pass signal %s to the game: %s" % (signum, exc))
        return False
    return True


def trace_signals():
    """Keep the exit backup alive when something asks us to stop.

    Steam, gamescope and a frontend all signal the whole process group on
    quit, so the game gets the same signal we do. This handler used to raise
    SystemExit, which killed the game mid-write and skipped the exit backup:
    two Bloodborne sessions on the Deck on 2026-09-17 were played and then
    lost that way. It now passes the signal on and returns, so the normal
    exit path runs and the session reaches the other device.

    A second signal of the same kind is a real force quit and is obeyed.
    """
    import signal
    def handler(signum, _frame):
        SIGNALS_SEEN.append(signum)
        if SIGNALS_SEEN.count(signum) > 1:
            log("second signal %s; giving up on the exit backup" % signum)
            raise SystemExit(128 + signum)
        log("received signal %s; stopping the game, then backing up" % signum)
        stop_child(signum)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------- store mode
#
# docs/superpowers/specs/2026-09-24-store-and-daemon-design.md. When
# savepick.json has a "store" section, saves go to the store through the
# daemon (slotd.py beside this file) and Syncthing is not asked anything.
# A save set (--tree) goes to the store too, merged file by file.

STORE_EXIT_NOTE_SECONDS = 1.2


def store_settings():
    return load_config().get("store") or None


def store_worker():
    """(worker, store name) from slotd.connect, or (None, reason)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import slotd
        import slotstore
    except ImportError as exc:
        return None, "slotd.py is missing beside savepick.py (%s)" % exc
    worker, how = slotd.connect(config_path=str(config_path()), log=log)
    if worker is None:
        return None, how
    log("store: using the %s worker" % how)
    return worker, slotstore.store_name(store_settings())


def hash_files(paths):
    """SHA-256 of each save file. What "the same save" means across devices."""
    import hashlib
    out = set()
    for path in paths:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            continue
        out.add(digest.hexdigest())
    return out


def restored_hashes(backup_dir):
    """The save hashes inside a fetched backup, without ludusavi's metadata."""
    paths = []
    for folder, _dirs, files in os.walk(str(backup_dir)):
        for name in files:
            if name not in METADATA_NAMES:
                paths.append(os.path.join(folder, name))
    return hash_files(paths)


def when_text(iso_text):
    if not iso_text:
        return "unknown"
    try:
        stamp = datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return iso_text
    return human_time(stamp)


def played_end_ts(choice):
    text = choice.get("played_end") or choice.get("created")
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


class KeepAwake(object):
    """Ask the OS not to sleep while a save uploads.

    Windows honours ES_SYSTEM_REQUIRED against idle sleep. A lid or a power
    button still wins; logind's delay inhibitor buys a few seconds on Linux.
    Either way the queue is on disk, so a cut-off upload carries on at wake.
    """

    def __init__(self):
        self.proc = None
        self.windows = False

    def __enter__(self):
        try:
            if is_windows():
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self.windows = True
            elif shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep", "--mode=delay",
                     "--who=BlockSlot", "--why=Uploading a save", "sleep", "600"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
        except Exception as exc:
            log("keep awake: %s" % exc)
        return self

    def __exit__(self, *_exc):
        try:
            if self.windows:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception as exc:
            log("keep awake release: %s" % exc)
        return False


def handoff_dir(prefix):
    """A temp folder the Blockslot service writes into and this user reads.

    Not tempfile.mkdtemp: since Python 3.12.4 its folders on Windows get an
    ACL of SYSTEM, Administrators and OWNER RIGHTS only. A file the service
    (LocalSystem) writes there is owned by SYSTEM, so ludusavi, running from
    Steam with an ordinary token, cannot read it and restores nothing. That
    is what happened to DS2 on 2026-09-25. A plain makedirs inherits the
    Temp folder's ACL, which names this user.
    """
    import secrets
    path = os.path.join(tempfile.gettempdir(), "%s%s" % (prefix, secrets.token_hex(6)))
    os.makedirs(path)
    return path


def store_restore(worker, game, choice, live_paths):
    """Fetch one snapshot and restore it. True when the save itself landed."""
    if live_paths and not snapshot_live_saves(game, live_paths):
        log("store: no vault snapshot, so no restore")
        return False
    where = handoff_dir("blockslot-restore-")
    try:
        worker.fetch(game, choice["id"], where)
        wanted = restored_hashes(where)
        args = ["--no-manifest-update", "restore", "--force", "--api",
                "--path", where, game]
        ok = False
        for attempt in (1, 2):
            log("restoring (attempt %d): %s" % (attempt, " ".join(args)))
            log_restore_answer(game, run_json(args, timeout=RESTORE_TIMEOUT_SECONDS))
            live = live_save_files(game)
            landed = hash_files(live)
            ok = bool(wanted) and wanted <= landed
            if ok:
                break
            # Say exactly what is on disk, so the next failure explains
            # itself. On 2026-09-25 a restore inside a Steam launch wrote
            # nothing, the same one run by hand worked, and nothing said why.
            for path in live:
                try:
                    stat = os.stat(path)
                    log("store: live %s is %s, written %s"
                        % (path, sorted(hash_files([path]))[0][:12], human_time(stat.st_mtime)))
                except (OSError, IndexError):
                    log("store: live %s cannot be read" % path)
            log("store: wanted %s" % ", ".join(sorted(h[:12] for h in wanted)))
            time.sleep(2)
        log("store: the save %s" % ("landed" if ok else "did NOT land"))
        if ok:
            worker.set_base(game, choice["id"])
        return ok
    except Exception as exc:
        log("store: restore failed: %s" % exc)
        return False
    finally:
        shutil.rmtree(where, ignore_errors=True)


def log_restore_answer(game, data):
    """ludusavi's own account of a restore, file by file, into the log."""
    if data is None:
        log("restore: ludusavi gave no readable answer")
        return
    entry = (data.get("games") or {}).get(game) or {}
    log("restore: ludusavi says %s / %s" % (entry.get("decision"), entry.get("change")))
    for path, info in (entry.get("files") or {}).items():
        log("restore: %s %s%s" % (info.get("change"), path,
                                  (" FAILED: %s" % info.get("error")) if info.get("failed") else ""))


def store_before_launch(worker, name, game):
    """Everything before the game starts. Never raises; never blocks for long."""
    spinner = Spinner(APP_TITLE, "Checking %s ..." % name)
    try:
        live_paths = live_save_files(game)
        answer = worker.decide(game, sorted(hash_files(live_paths)))
    except Exception as exc:
        answer = {"action": "unknown", "error": str(exc)}
    finally:
        spinner.close()
    action = answer.get("action")
    log("store: %s -> %s" % (game, action))

    if action == "unknown":
        show_warning(APP_TITLE,
                     "Could not check %s.\n\n%s\n\n"
                     "%s starts on the save that is on this device. If you\n"
                     "played somewhere else since, quit now and let it sync."
                     % (name, answer.get("error") or "", game))
        return
    if action == "launch":
        if answer.get("adopt"):
            worker.set_base(game, answer["adopt"])
        return
    if action == "restore":
        choice = answer["restore"]
        spinner = Spinner(APP_TITLE, "Getting your %s save for %s ..."
                          % (choice.get("device"), game))
        try:
            landed = store_restore(worker, game, choice, live_paths)
        finally:
            spinner.close()
        if not landed:
            warn_restore_incomplete(game)
        return
    if action == "wait":
        pending = answer.get("pending") or [{}]
        who = pending[0].get("device") or "another device"
        show_warning(APP_TITLE,
                     "%s has a newer save of %s that it has not finished\n"
                     "uploading.\n\n"
                     "Playing here now starts from an older save. Quit, let\n"
                     "%s finish, then start the game again."
                     % (who, game, who))
        return
    if action == "ask":
        choices = sorted(answer.get("choices") or [], key=lambda c: played_end_ts(c) or 0)
        other = choices[-1] if choices else None
        if other is None:
            return
        live_mtime = newest_live_mtime(live_paths)
        picked = ask_user(game, live_mtime, "%s save" % other.get("device"),
                          played_end_ts(other),
                          headline="These two saves are different, and both were played.",
                          keep_label="Keep this device",
                          restore_label="Use the %s save" % other.get("device"))
        if picked is True:
            spinner = Spinner(APP_TITLE, "Getting the %s save ..." % other.get("device"))
            try:
                worker.choose(game, other["id"])
                landed = store_restore(worker, game, other, live_paths)
            finally:
                spinner.close()
            if not landed:
                warn_restore_incomplete(game)
        else:
            # Keeping this device is a decision too. The next save names every
            # other head as a parent, so the fork closes when it uploads.
            log("store: kept this device over %s" % other["id"])
            losers = [c["id"] for c in choices]
            worker.set_base(game, answer.get("base"), merge=losers)


def store_after_exit(worker, name, game, played, mode="game", started_on=None):
    """Back up, hand the save to the daemon, and say plainly where it is."""
    where = tempfile.mkdtemp(prefix="blockslot-backup-")
    try:
        cmd = [ludusavi_binary(), "--no-manifest-update", "backup", "--force",
               "--path", where, game]
        log("exit backup: %s" % " ".join(cmd))
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                  stdin=subprocess.DEVNULL, **no_window())
            failed = done.returncode != 0 or not os.listdir(where)
        except (OSError, subprocess.SubprocessError) as exc:
            failed, done = True, None
            log("exit backup: %s" % exc)
        if failed:
            show_warning(APP_TITLE,
                         "Backup FAILED\n\n%s\n\nYour save on this device is "
                         "untouched, but it was not\nsaved for your other devices." % game)
            return
        if started_on is not None and restored_hashes(where) == started_on:
            # The save is exactly what the game started on: nothing to send.
            # On 2026-09-25 a 7 second launch uploaded an unchanged save with
            # no parent and made a second DS2 head for no reason.
            log("store: %s did not change this session; nothing to upload" % game)
            return
        staged = worker.stage(game, where, played=played, mode=mode)
    finally:
        shutil.rmtree(where, ignore_errors=True)

    wait_for_upload(worker, name, game, staged["snap"])


# ---------------------------------------------------------------- libraries
#
# An emulator's saves folder (a "tree" in savepick.json) is split into one
# save per game by saveunits.py, and each game has its own history on the
# store: restored when another device is newer, uploaded when it changed.
# A tree with "one_game" set is a single game, such as Bloodborne in shadPS4.

LIBRARY_LIST = 5


def saveunits_module():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import saveunits
    return saveunits


def tree_setting(game, key, default=None):
    return ((load_config().get("trees") or {}).get(game) or {}).get(key, default)


def library_units(game, root):
    """{unit name: {"unit": info, "rels": [...], "hashes": [...]}} on this device."""
    import slotstore
    su = saveunits_module()
    allowed = tree_allowed(game)
    always = tree_always_dirs(game)
    aliases = tree_aliases(game)
    index = index_tree(root)
    rels = sorted(rel for rel in index if is_restorable(rel, allowed, always))
    one_game = tree_setting(game, "one_game")
    if one_game:
        system = tree_setting(game, "system", "")
        groups = {su.unit_id(system, one_game): rels} if rels else {}
    else:
        groups = su.split(rels, aliases)
    out = {}
    for unit_id, members in groups.items():
        system, _sep, title = unit_id.partition("/")
        system = "" if system == "content" else system
        info = {"library": game, "id": unit_id, "title": su.display(system, title),
                "system": system,
                "label": tree_setting(game, "label") or su.label_for(system)}
        out[slotstore.library_unit_name(game, unit_id)] = {
            "unit": info, "rels": members,
            "hashes": sorted(hash_files([str(Path(root) / rel) for rel in members]))}
    return out


def library_restore_items(files, root, local_index, game):
    """Blob fetches that lay another device's copy of a game out here.

    The same rules the per-file merge used: a system folder is renamed to
    this device's alias, and a path whose layout this device does not use is
    skipped rather than written where no emulator reads it.
    """
    aliases = tree_aliases(game)
    allowed = tree_allowed(game)
    tops = top_dirs(local_index) if aliases else set()
    dirs = tree_dirs(local_index)
    items, skipped = [], []
    for record in files:
        rel = record["path"]
        target = alias_path(rel, aliases, tops) if aliases else rel
        if target is None or (allowed is not None and target not in local_index
                              and not path_fits(target, dirs)):
            skipped.append(rel)
            continue
        items.append({"sha256": record["sha256"], "path": str(Path(root) / target),
                      "mtime": record.get("mtime"), "rel": target})
    return items, skipped


def store_library_before_launch(worker, name, game, root):
    """Every game in the library that is newer elsewhere comes here first."""
    spinner = Spinner(APP_TITLE, "Checking %s ..." % name)
    try:
        units = library_units(game, root)
        answer = worker.library(game, {unit: {"hashes": data["hashes"], "unit": data["unit"]["id"]}
                                       for unit, data in units.items()})
    except Exception as exc:
        spinner.close()
        log("library: store not readable: %s" % exc)
        show_warning(APP_TITLE, "Could not check %s.\n\n%s\n\n%s starts on the saves "
                     "on this device." % (name, exc, game))
        return None
    todo = answer.get("units") or {}
    log("library %s: %d game(s) here, %d on the store, %d adopted, %d need something"
        % (game, len(units), answer.get("store_units", 0), answer.get("adopted", 0), len(todo)))
    local_index = index_tree(root)
    restored, failed, asked, waiting = [], 0, [], []
    single = bool(tree_setting(game, "one_game"))
    try:
        for unit_name, entry in sorted(todo.items()):
            action = entry.get("action")
            if action == "restore":
                choice = entry["restore"]
            elif action == "ask" and single:
                choices = sorted(entry.get("choices") or [], key=lambda c: played_end_ts(c) or 0)
                other = choices[-1]
                spinner.close()
                picked = ask_user(game, newest_live_mtime(
                                      [str(Path(root) / r) for r in units.get(unit_name, {}).get("rels", [])]),
                                  "%s save" % other.get("device"), played_end_ts(other),
                                  headline="These two saves are different, and both were played.",
                                  keep_label="Keep this device",
                                  restore_label="Use the %s save" % other.get("device"))
                spinner = Spinner(APP_TITLE, "Getting the %s save ..." % other.get("device"))
                if picked is not True:
                    worker.set_base(unit_name, None, merge=[c["id"] for c in choices])
                    continue
                worker.choose(unit_name, other["id"])
                choice = other
            elif action == "ask":
                asked.append(((entry.get("choices") or [{}])[0].get("unit") or {}).get("title")
                             or unit_name)
                continue
            elif action == "wait":
                waiting.append(unit_name)
                continue
            else:
                continue
            items, skipped = library_restore_items(choice["files"], root, local_index, game)
            for rel in skipped:
                log("library: %s does not fit this device's layout; skipped" % rel)
            replacing = [item["path"] for item in items if item["rel"] in local_index]
            if replacing and not snapshot_live_saves(game, replacing):
                log("library: no vault snapshot, so %s stays as it is" % unit_name)
                failed += 1
                continue
            if items:
                result = worker.fetch_blobs([{k: item[k] for k in ("sha256", "path", "mtime")}
                                             for item in items])
                for path, why in result.get("failed") or []:
                    log("library: could not write %s: %s" % (path, why))
                if result.get("failed"):
                    failed += 1
                    continue
            # The base moves even when nothing fitted, so a game this device
            # cannot use is not offered again at every launch.
            worker.set_base(unit_name, choice["id"])
            title = (choice.get("unit") or {}).get("title") or unit_name
            if not items:
                log("library: %s from %s uses a layout this device does not; "
                    "nothing written" % (title, choice.get("device")))
                continue
            restored.append(title)
            log("library: %s from %s" % (title, choice.get("device")))
    except Exception as exc:
        log("library: restore failed: %s" % exc)
        failed += 1
    finally:
        spinner.close()
    log("library %s: %d restored, %d failed, %d with two saves, %d still uploading elsewhere"
        % (game, len(restored), failed, len(asked), len(waiting)))
    if failed:
        warn_restore_incomplete(game)
    notes = []
    if asked:
        notes.append("%d game%s have a different save on another device, and both "
                     "were played:\n  %s\nThis device's saves were kept. Choose in "
                     "BlockSlot." % (len(asked), "" if len(asked) == 1 else "s",
                                     "\n  ".join(sorted(asked)[:LIBRARY_LIST])))
    if waiting:
        notes.append("%d game%s have a newer save another device is still uploading."
                     % (len(waiting), "" if len(waiting) == 1 else "s"))
    if notes:
        show_warning(APP_TITLE, "%s\n\n%s" % (game, "\n\n".join(notes)))
    return {"new_here": set(answer.get("new_here") or [])}


def store_library_after_exit(worker, name, game, root, before, played):
    """Upload every game in the library whose save changed, and only those."""
    after = library_units(game, root)
    changed = [unit for unit, data in after.items()
               if data["hashes"] != (before.get(unit) or {}).get("hashes")
               or unit in (before.get("__new_here__") or set())]
    log("library %s: %d game(s) changed this session" % (game, len(changed)))
    if not changed:
        return
    last = None
    for unit in changed:
        where = tempfile.mkdtemp(prefix="blockslot-unit-")
        try:
            for rel in after[unit]["rels"]:
                target = Path(where) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(Path(root) / rel), str(target))
            last = worker.stage(unit, where, played=played, mode="library",
                                unit=after[unit]["unit"])["snap"]
        finally:
            shutil.rmtree(where, ignore_errors=True)
    wait_for_upload(worker, name, game, last)


def wait_for_upload(worker, name, game, snap):
    """The exit wait and its messages, shared by games and libraries."""
    spinner = Spinner(APP_TITLE, "Saving to %s ...\nB or Cancel finishes in the background."
                      % name, cancellable=True)
    cancel = SpinnerOrPadCancel(spinner)
    result = {"state": "uploading"}
    try:
        with KeepAwake():
            while True:
                result = worker.wait(snap, 2)
                if result.get("state") != "uploading":
                    break
                total = result.get("total") or 0
                if total:
                    spinner.update("Saving to %s ... %s of %s\n"
                                   "B or Cancel finishes in the background."
                                   % (name, human_bytes(result.get("done") or 0),
                                      human_bytes(total)),
                                   int(100 * (result.get("done") or 0) / total))
                if cancel.pressed():
                    log("store: left the upload to the daemon")
                    break
            if result.get("state") == "committed":
                spinner.update("Saved to %s." % name, 100)
                time.sleep(STORE_EXIT_NOTE_SECONDS)
    except Exception as exc:
        result = {"state": "offline", "message": str(exc)}
    finally:
        cancel.close()
        spinner.close()
    state = result.get("state")
    log("store: exit upload %s (%s)" % (state, result.get("message") or snap))
    if state == "offline":
        show_warning(APP_TITLE,
                     "NOT UPLOADED YET\n\n"
                     "%s is saved on this device, but %s could not be\n"
                     "reached.\n\n"
                     "It will upload by itself when this device is online.\n"
                     "Until then, do not play it on another device." % (game, name))
    elif state == "refused":
        show_warning(APP_TITLE,
                     "NOT UPLOADED\n\n%s refused the save:\n%s\n\n"
                     "%s is saved on this device and stays queued.\n"
                     "Fix the store settings and it will upload."
                     % (name, result.get("message") or "", game))
    elif state == "uploading":
        show_warning(APP_TITLE,
                     "Still uploading %s to %s.\n\n"
                     "It carries on in the background. Keep this device\n"
                     "awake and online until it finishes." % (game, name))


def main_store_library(game, command):
    worker, name = store_worker()
    if worker is None:
        log("store: %s; launching without a restore" % name)
        show_warning(APP_TITLE, "The save store is not working:\n%s\n\n"
                     "%s starts on the saves on this device." % (name, game))
        return launch(command)
    me = None
    try:
        import slotd
        _settings, me = slotd.load_settings(str(config_path()))
    except Exception as exc:
        log("library: cannot read the device name: %s" % exc)
    root_text = tree_roots(game).get(me or "")
    if not root_text:
        log("library: no root for %s on %s; launching without a restore" % (game, me))
        return launch(command)
    root = Path(root_text)
    answer = store_library_before_launch(worker, name, game, root)
    before = library_units(game, root)
    before["__new_here__"] = (answer or {}).get("new_here") or set()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    code = launch(command)
    played = {"start": started,
              "end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        store_library_after_exit(worker, name, game, root, before, played)
    except Exception as exc:
        log("store: exit step failed: %s" % exc)
    return code


def main_store(game, command):
    worker, name = store_worker()
    if worker is None:
        log("store: %s; launching without a restore" % name)
        show_warning(APP_TITLE, "The save store is not working:\n%s\n\n"
                     "%s starts on the save on this device." % (name, game))
        return launch(command)
    store_before_launch(worker, name, game)
    try:
        started_on = hash_files(live_save_files(game))
    except Exception as exc:
        log("store: cannot read the save the game starts on: %s" % exc)
        started_on = None
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    code = launch(command)
    played = {"start": started,
              "end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        store_after_exit(worker, name, game, played, started_on=started_on)
    except Exception as exc:
        log("store: exit step failed: %s" % exc)
    return code


def main_tree(game, command, dry_run=False):
    """Merge a whole save tree, then launch the frontend.

    There is no conflict dialog here. The decision is per file, and a prompt
    listing hundreds of files could not be answered.
    """
    cfg = sync_settings() or {}
    root = local_tree_root(game, cfg) if cfg else None
    synced = confirm_incoming_sync()
    if synced is not True or not cfg or root is None:
        if root is None:
            log("tree: no root configured for %s; launching without a restore" % game)
        else:
            warn_unconfirmed(synced)
            log("tree: incoming sync not confirmed; launching on the local saves")
        code = launch(command, do_restore=False)
        try:
            backup_on_exit(game)
        except Exception as exc:
            log("exit backup step failed: %s" % exc)
        return code

    spinner = Spinner(APP_TITLE, "Checking your saves for %s ..." % game)
    copied = failed = 0
    try:
        local = index_tree(root)
        peers = peer_tree_index(game, cfg)
        plan = merge_plan(local, peers, allowed=tree_allowed(game),
                          aliases=tree_aliases(game),
                          always_dirs=tree_always_dirs(game))
        log("tree: %d local file(s), %d on peers, %d to copy"
            % (len(local), len(peers), len(plan)))
        replacing = overwrite_paths(plan, root)
        if replacing and not snapshot_live_saves(game, [str(p) for p in replacing]):
            log("tree: no snapshot, so no copy; launching on the local saves")
            plan = []
        if plan:
            copied, failed = apply_merge(plan, root, dry_run=dry_run)
    except Exception as exc:
        # The frontend still has to start. A merge that blew up copied nothing
        # it had not already logged, and the local saves are untouched.
        log("tree: merge failed: %s" % exc)
        failed += 1
    finally:
        spinner.close()

    log("tree: %d copied, %d failed" % (copied, failed))
    if failed:
        warn_restore_incomplete(game)

    code = launch(command, do_restore=False)
    try:
        backup_on_exit(game)
    except Exception as exc:
        log("exit backup step failed: %s" % exc)
    return code


def main(argv):
    global BORDERLESS
    hide_own_console()
    trace_signals()
    command = split_command(argv)
    if not command:
        log("no game command given after --; nothing to launch")
        return 2

    head = head_of(argv)
    BORDERLESS = "--borderless" in head
    if "--no-sync" in head:
        # Borderless only. No Syncthing, no ludusavi, no save touched, and no
        # window before the game: it starts at once.
        log("no-sync: launching without touching saves")
        return launch(command)

    game = tree_name(argv)
    if game:
        if store_settings():
            return main_store_library(game, command)
        return main_tree(game, command, dry_run="--dry-run" in argv)

    appid = os.environ.get("SteamAppId") or os.environ.get("SteamGameId")
    if not appid:
        log("no SteamAppId in the environment; launching without a restore")
        return launch(command, do_restore=False)

    game = game_name_for_appid(appid)
    if not game:
        log("ludusavi does not recognise steam id %s; launching without a restore" % appid)
        return launch(command, do_restore=False)

    if store_settings():
        return main_store(game, command)

    synced = confirm_incoming_sync()
    cfg = sync_settings()
    if synced is not True or not cfg:
        warn_unconfirmed(synced)
        log("incoming sync not confirmed; launching on this device's save")
        code = launch(command, do_restore=False)
        try:
            backup_on_exit(game)
        except Exception as exc:
            log("exit backup step failed: %s" % exc)
        return code

    live_paths = live_save_files(game)
    live_mtime = newest_live_mtime(live_paths)
    basenames = {Path(p).name for p in live_paths}
    backup_label, backup_mtime, backup_name, peer_dir = newest_backup_info(
        game, basenames, cfg)

    action = decide(live_mtime, backup_mtime)
    log("%s: live=%s backup=%s (%s from %s) -> %s"
        % (game, human_time(live_mtime), human_time(backup_mtime), backup_name,
           peer_dir.name if peer_dir else "nowhere", action))

    asked = action == ASK
    if asked:
        action = resolve_ask(ask_user(game, live_mtime, backup_label, backup_mtime))
        log("after asking -> %s" % action)

    if needs_snapshot(action, live_mtime):
        action = gate_restore_on_snapshot(
            action, snapshot_live_saves(game, live_paths))
        if action != RESTORE:
            log("vault: no snapshot, so no restore; launching on the live save")

    if action == RESTORE:
        # savepick owns every restore, so that a file it does not care about
        # can never stop the game from starting. wrap only runs the game.
        #
        # The spinner matters: wrap used to show its own notification here, and
        # a restore with no feedback at all would read as a hang in Game Mode.
        spinner = Spinner(APP_TITLE, "Getting your save for %s ..." % game)
        try:
            landed = restore_now(game, backup_mtime, backup_name, peer_dir)
        finally:
            spinner.close()
        if not landed:
            warn_restore_incomplete(game)
        elif asked:
            # You answered B against an older backup. Say where the newer save
            # went, which is more use than a second "are you sure".
            warn_downgraded(game)

    code = launch(command)
    try:
        backup_on_exit(game)
    except Exception as exc:
        # The game already ran and exited. Nothing here is worth failing over,
        # and the save on this machine is untouched either way.
        log("exit backup step failed: %s" % exc)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
