"""Where Blockslot's own files live on each operating system.

One module so that no other file has to ask what platform it is on. The engine
and its config already have homes, chosen when savepick was hand-installed, and
those homes are kept: a GUI that moved them would orphan two working devices.
"""

import os
import shutil
import sys
from pathlib import Path

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


def home():
    return Path.home()


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
    base = os.environ.get("XDG_STATE_HOME") or str(home() / ".local" / "state")
    return Path(base) / "blockslot"


def cache_path(name):
    return state_dir() / name


def log_path():
    """The engine's log, which the GUI only ever reads (savepick._log_path)."""
    import tempfile
    if is_windows():
        return Path(tempfile.gettempdir()) / "savepick.log"
    base = os.environ.get("XDG_STATE_HOME") or str(home() / ".local" / "state")
    return Path(base) / "blockslot" / "savepick.log"


def ludusavi_path():
    name = "ludusavi.exe" if is_windows() else "ludusavi"
    return bin_dir() / name


def engine_in_exe():
    """True when Blockslot.exe carries the engine and a launch option runs it.

    Only the frozen Windows build does this. Everywhere else, and from a git
    checkout on Windows, a wrap names python and the deployed savepick.py, as
    it always has: the Deck and the Mac have python, and a Windows PC with
    the exe may have none, so there the exe itself is the engine's host.
    """
    return is_frozen() and is_windows()


def launch_program():
    """The program a Windows wrap names when the exe hosts the engine."""
    return Path(sys.executable)


def in_temporary_place(program=None):
    """True when `program` (the running exe) sits somewhere that will vanish.

    A launch option names the exe by its full path. Run from %TEMP%, or from
    the folder Explorer unpacks a zip into when you open the exe without
    extracting it first, every game turned on would point at a file that is
    gone after the next cleanup, and those games would stop starting.
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


def python_for_launch():
    """The interpreter a launch option should name.

    pythonw.exe on Windows, because python.exe opens a console window that then
    sits behind the game for the whole session.

    The running program is only the answer when it IS python. The built
    Blockslot.exe is not, and neither is Decky's loader, which is a frozen
    binary that the plugin runs inside. Naming either one in a launch option
    would have Steam start it in place of the game.
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
    if is_python:
        return running
    found = shutil.which("python3")
    return Path(found) if found else Path("/usr/bin/python3")
