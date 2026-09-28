"""Where Blockslot's own files live on each operating system.

One module so that no other file has to ask what platform it is on. The engine
and its config already have homes, chosen when savepick was hand-installed, and
those homes are kept: a GUI that moved them would orphan two working devices.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

ENGINE_NAME = "savepick.py"


def is_frozen():
    """True when this is the built executable rather than the source tree."""
    return getattr(sys, "frozen", False)


def bundle_dir():
    """Where a built executable unpacked its data files, or None.

    PyInstaller puts them in a temporary directory and names it in
    sys._MEIPASS. Everything that ships beside the code, the game index and
    the engine, has to be looked for there first when frozen.
    """
    return getattr(sys, "_MEIPASS", None)


def resource(name):
    """A file that ships with Blockslot: index/games.json, engine/savepick.py.

    Looked for in the bundle first, then beside the source tree, so the same
    call works from the exe and from a git checkout.
    """
    here = bundle_dir()
    if here:
        found = Path(here) / name
        if found.is_file():
            return found
    root = Path(__file__).resolve().parents[2]
    found = root / name
    return found if found.is_file() else None


def is_windows():
    return sys.platform == "win32"


def no_window():
    """Keyword arguments that stop a child process opening a console window."""
    return {"creationflags": 0x08000000} if is_windows() else {}


def is_mac():
    return sys.platform == "darwin"


def is_linux():
    return sys.platform.startswith("linux")


def in_snap(environ=None):
    """True inside a snap's confinement, such as a game snap Steam started.

    The Steam snap sets HOME to ~/snap/steam/common and points XDG_CONFIG_HOME
    and XDG_DATA_HOME there too, so everything that asks for "home" gets the
    snap's private copy. snapd keeps the real one in SNAP_REAL_HOME.
    """
    environ = os.environ if environ is None else environ
    return bool(environ.get("SNAP_NAME") and environ.get("SNAP_REAL_HOME"))


def home(environ=None):
    """The person's real home, also from inside the Steam snap.

    Blockslot's own files (savepick.json, the queue, the log, the deployed
    engine) live in one place per person. A picker started by snap Steam that
    looked under the snap's HOME would find no settings, and a daemon started
    from there would queue saves where the real daemon never looks.
    """
    environ = os.environ if environ is None else environ
    if in_snap(environ):
        return Path(environ["SNAP_REAL_HOME"])
    return Path.home()


def xdg_dir(variable, default, environ=None):
    """An XDG base directory, or `default` under the real home.

    Ignored inside a snap, where the XDG variables name the snap's own
    private folders.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(variable)
    if value and not in_snap(environ):
        return Path(value)
    return home(environ) / default


# Where the Steam snap keeps its own home. A game snap Steam starts, and the
# launch option in front of it, see this folder as $HOME. Blockslot's own
# files stay in the real home (the snap's steam-support interface lets it read
# them, probed on an Ubuntu laptop; see docs/linux-desktop.md), but ludusavi
# runs with the snap's HOME, so its config and manifest are the ones in here.
SNAP_STEAM_HOME = ("snap", "steam", "common")


def steam_sandbox_home(base=None):
    """The home the Steam snap gives its games, when that Steam is the one in use.

    None everywhere else, and inside a snap, where $HOME already is that
    folder. A native Steam with its own userdata wins, the same order
    steamdir looks in, so a PC with both keeps using the native one.
    """
    if not is_linux() or in_snap() or os.environ.get("SNAP"):
        return None
    base = Path(base) if base is not None else home()
    snap = base.joinpath(*SNAP_STEAM_HOME)
    if not (snap / ".local" / "share" / "Steam" / "userdata").is_dir():
        return None
    if (base / ".local" / "share" / "Steam" / "userdata").is_dir():
        return None
    return snap


def bin_dir():
    """Where the engine is deployed. The same path savepick already uses."""
    return home() / ".local" / "bin"


def engine_path():
    return bin_dir() / ENGINE_NAME


def config_path():
    """savepick.json, in the place the engine reads it from."""
    if is_windows():
        base = os.environ.get("APPDATA") or str(home())
        return Path(base) / "savepick.json"
    return home() / ".config" / "savepick.json"


def state_dir():
    """Blockslot's own cache and window state, never the engine's."""
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(home())
        return Path(base) / "Blockslot"
    if is_mac():
        return home() / "Library" / "Application Support" / "Blockslot"
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "blockslot"


def mac_logs_dir():
    """~/Library/Logs/BlockSlot: where the Mac's daemon writes, and where
    Console.app looks for a program's logs."""
    return home() / "Library" / "Logs" / "BlockSlot"


def cache_path(name):
    return state_dir() / name


def log_path():
    """The engine's log, which the GUI only ever reads (savepick._log_path)."""
    import tempfile
    if is_windows():
        return Path(tempfile.gettempdir()) / "savepick.log"
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "blockslot" / "savepick.log"


def ludusavi_path():
    name = "ludusavi.exe" if is_windows() else "ludusavi"
    return bin_dir() / name


def engine_in_exe():
    """True when the built program carries the engine and a launch option
    runs it: `Blockslot.exe --pick` on Windows, and
    `Blockslot.app/Contents/MacOS/Blockslot --pick` on a Mac.

    Only the frozen Windows and Mac builds do this. Everywhere else, and from
    a git checkout, a wrap names python and the deployed savepick.py, as it
    always has. A Windows PC with the exe may have no python at all, and a
    Mac without Apple's developer tools has only the /usr/bin/python3 stub,
    which opens an install dialog in place of the game (mac_python). So on
    both the built program is the engine's host.
    """
    return is_frozen() and (is_windows() or is_mac())


def launch_program():
    """The program a wrap names when the built program hosts the engine.

    On a Mac that is the binary inside the bundle,
    Blockslot.app/Contents/MacOS/Blockslot, which is what sys.executable
    already is in a PyInstaller .app.
    """
    return Path(sys.executable)


def in_temporary_place(program=None):
    """True when `program` (the running exe) sits somewhere that will vanish.

    A launch option names the exe by its full path. Run from %TEMP%, or from
    the folder Explorer unpacks a zip into when you open the exe without
    extracting it first, every game turned on would point at a file that is
    gone after the next cleanup, and those games would stop starting.

    A Mac has two more. An app opened from Downloads while it still carries
    the quarantine flag runs from a random read-only copy under
    .../AppTranslocation/<uuid>/, a new one each launch. An app opened from
    a disk image runs from /Volumes/<image>/, gone when it is ejected. A
    launch option or LaunchAgent that named either would break, so both
    count as temporary, and the answer is to move it to /Applications.
    """
    import tempfile
    program = Path(program or sys.executable)
    try:
        where = program.resolve()
    except OSError:
        where = program
    folders = [part.lower() for part in where.parts[:-1]]
    if any(part.endswith(".zip") for part in folders):
        return True
    # Resolved on a Mac, so "/Volumes/Macintosh HD", a link to /, is not
    # taken for a disk image. As given elsewhere, where resolving a POSIX
    # path would put a drive letter in front of it.
    if is_mac() and mac_temporary(where if os.name != "nt" else program):
        return True
    temps = {os.environ.get("TEMP"), os.environ.get("TMP"),
             tempfile.gettempdir()}
    for temp in temps:
        if not temp:
            continue
        try:
            base = Path(temp).resolve()
        except OSError:
            base = Path(temp)
        if str(where).lower().startswith(str(base).lower() + os.sep):
            return True
    return False


def mac_temporary(program):
    """True for a Mac path under App Translocation or on a mounted volume.

    Read from the path's text in POSIX form, so it answers the same on any
    OS. Translocation lives under /private/var/folders/..., and /var links
    there, so a folder named AppTranslocation anywhere in the path counts.
    """
    text = str(program).replace("\\", "/")
    parts = [part for part in text.split("/") if part]
    if "AppTranslocation" in parts[:-1]:
        return True
    return text.startswith("/Volumes/")


def temporary_advice():
    """What to tell someone whose copy is in_temporary_place."""
    if is_mac():
        return ("Move BlockSlot.app into your Applications folder, then open "
                "it from there.")
    return "Move Blockslot.exe somewhere it can stay, then open it from there."


class NoRealPython(RuntimeError):
    """A Mac with nothing but the /usr/bin/python3 stub to name."""


MAC_NO_PYTHON = (
    "this Mac has no python 3 to run BlockSlot with: /usr/bin/python3 is "
    "only the installer for Apple's command line tools. Use the BlockSlot "
    "app, or run `xcode-select --install`, or install python from "
    "python.org or Homebrew, then try again.")


def python_for_launch():
    """The interpreter a launch option should name.

    pythonw.exe on Windows, because python.exe opens a console window that then
    sits behind the game for the whole session.

    The running program is only the answer when it IS python. The built
    Blockslot.exe is not, and neither is Decky's loader, which is a frozen
    binary that the plugin runs inside. Naming either one in a launch option
    would have Steam start it in place of the game.

    On a Mac it never names the /usr/bin/python3 stub with no developer
    tools behind it. That raises NoRealPython instead: a launch option or a
    LaunchAgent naming the stub would open Apple's install dialog each time.
    """
    running = Path(sys.executable)
    is_python = running.name.lower().startswith("python") and not is_frozen()
    if is_windows():
        beside = running.parent / "pythonw.exe"
        if is_python and beside.is_file():
            return beside
        if is_python:
            return running
        found = shutil.which("pythonw.exe") or shutil.which("python.exe")
        return Path(found) if found else Path("pythonw.exe")
    if is_mac():
        found = mac_python(running if is_python else None)
        if not mac_python_is_real(found):
            raise NoRealPython(MAC_NO_PYTHON)
        return found
    if is_python:
        return running
    found = shutil.which("python3")
    return Path(found) if found else Path("/usr/bin/python3")


# The one python every snap base and every Linux desktop has at the same path.
# A POSIX path on every platform, because it names a file inside the snap.
SNAP_PYTHON = PurePosixPath("/usr/bin/python3")


def python_for_steam(kind):
    """The interpreter a launch option names, for a Steam of this `kind`.

    Snap Steam starts a game inside its own mount namespace, where /usr is
    the snap's base (core24) and not this machine's. A python from a venv,
    /usr/local or /opt that this window runs from does not exist in there,
    so the game would not start at all. /usr/bin/python3 exists on both
    sides: the host's here, core24's 3.12 in there, and the engine is
    written for 3.9 and up. Found on an Ubuntu laptop on 2026-09-28 (steamdir.SNAP).
    """
    if kind == "snap" and not is_windows() and not is_mac():
        return SNAP_PYTHON
    return python_for_launch()


# Every Mac has /usr/bin/python3, and on a Mac without Apple's command line
# tools it is not python at all: it is a stub that opens "install the
# developer tools?" and exits. A launch option that named it would show that
# dialog in place of every game. Measured on a macOS 26.6 VM (2026-09-28):
# `xcode-select -p` fails and /Library/Developer/CommandLineTools is absent.
MAC_DEVELOPER_DIRS = ("/Library/Developer/CommandLineTools",
                      "/Applications/Xcode.app/Contents/Developer")
MAC_PYTHONS = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3",
               "/Library/Frameworks/Python.framework/Versions/Current/bin/python3")
MAC_STUB = "/usr/bin/python3"


_SELECTED = []


def mac_selected_developer_dir():
    """The folder `xcode-select -p` names, or None when it fails.

    It fails on a Mac with no developer tools, which is the Mac where the
    stub is only an installer. Elsewhere it may name an Xcode kept outside
    /Applications. Asking opens no dialog, and the answer does not change
    while BlockSlot runs, so it is asked once.
    """
    if not is_mac():
        return None
    if not _SELECTED:
        answer = None
        try:
            done = subprocess.run(["/usr/bin/xcode-select", "-p"],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode == 0 and done.stdout.strip():
                answer = done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            answer = None
        _SELECTED.append(answer)
    return _SELECTED[0]


def mac_stub_works(exists=os.path.isfile, selected=None):
    """True when /usr/bin/python3 leads to a real python: the command line
    tools or Xcode are installed, so the stub hands over to theirs.

    `selected` is the folder xcode-select names. Left out, xcode-select is
    asked, but only when `exists` is the real test, so a test's fake file
    system is never mixed with this Mac's own answer.
    """
    folders = list(MAC_DEVELOPER_DIRS)
    if selected is None and exists is os.path.isfile:
        selected = mac_selected_developer_dir()
    if selected:
        folders.append(str(selected).rstrip("/"))
    return any(exists(folder + "/usr/bin/python3") for folder in folders)


def mac_python(running=None, exists=os.path.isfile):
    """The python a Mac launch option or LaunchAgent should name.

    The running interpreter is right unless it lives inside the developer
    tools, whose versioned path changes with each update of them; the stub
    in /usr/bin follows those updates. Then Homebrew and python.org, whose
    paths are stable. The stub is the answer of last resort, and
    `mac_python_is_real` says whether it will work.
    """
    if running is not None:
        text = str(running)
        if not any(text.startswith(folder) for folder in MAC_DEVELOPER_DIRS) \
                and Path(text) != Path(MAC_STUB):
            return Path(text)
    if mac_stub_works(exists):
        return Path(MAC_STUB)
    for candidate in MAC_PYTHONS:
        if exists(candidate):
            return Path(candidate)
    return Path(MAC_STUB)


def mac_python_is_real(python, exists=os.path.isfile):
    """False for the stub on a Mac with no developer tools behind it."""
    return Path(python) != Path(MAC_STUB) or mac_stub_works(exists)
