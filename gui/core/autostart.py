"""Start the Blockslot daemon when this Windows user logs in.

The daemon has to be running before any game exits, or a save made offline
waits for the next launch to go up. A Run value under HKCU is the lightest way
there: no admin rights, no scheduled task, and it shows in Task Manager's
Startup tab where a person can switch it off without us.

Other platforms get a user service or a login item instead, which is not this
module's job, so everything here is a no-op returning False off Windows.
"""

import sys
from pathlib import Path

from gui.core import paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Blockslot"


def command_line(frozen=None, executable=None, script=None, python=None,
                 flag="--daemon"):
    """What the Run value holds.

    The built exe is its own launcher. The source tree needs pythonw.exe named
    in front of it, never python.exe, which would open a console at every
    login and keep it open for as long as the daemon runs.
    """
    frozen = paths.is_frozen() if frozen is None else frozen
    if frozen:
        return '"%s" %s' % (executable or sys.executable, flag)
    if script is None:
        script = Path(__file__).resolve().parents[1] / "blockslot.py"
    if python is None:
        python = paths.python_for_launch()
    return '"%s" "%s" %s' % (python, script, flag)


def command_argv(frozen=None, executable=None, script=None, python=None):
    """The same command as command_line, as a list to start it with now."""
    frozen = paths.is_frozen() if frozen is None else frozen
    if frozen:
        return [executable or sys.executable, "--daemon"]
    if script is None:
        script = Path(__file__).resolve().parents[1] / "blockslot.py"
    if python is None:
        python = paths.python_for_launch()
    return [str(python), str(script), "--daemon"]


def _registry():
    import winreg
    return winreg


def _open(reg, write):
    access = reg.KEY_SET_VALUE | reg.KEY_QUERY_VALUE if write else reg.KEY_QUERY_VALUE
    return reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, access)


def is_enabled(reg=None, windows=None):
    """True when the Run value is there and names this install.

    A value left behind by a copy that has since moved does not count: it
    would start nothing, or start an old build.
    """
    return current(reg, windows) == command_line()


def current(reg=None, windows=None):
    """The Run value as it stands, or None."""
    if not (paths.is_windows() if windows is None else windows):
        return None
    reg = reg or _registry()
    try:
        with _open(reg, write=False) as key:
            value, _kind = reg.QueryValueEx(key, VALUE_NAME)
            return value
    except OSError:
        return None


def enable(reg=None, windows=None, tray=False):
    """Write the Run value for this install. True when it is written.

    `tray` starts only the icon, for when the Windows service holds the
    daemon; otherwise login starts the daemon with its icon.
    """
    if not (paths.is_windows() if windows is None else windows):
        return False
    reg = reg or _registry()
    try:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0,
                             reg.KEY_SET_VALUE | reg.KEY_QUERY_VALUE) as key:
            reg.SetValueEx(key, VALUE_NAME, 0, reg.REG_SZ,
                           command_line(flag="--tray" if tray else "--daemon"))
        return True
    except OSError:
        return False


def disable(reg=None, windows=None):
    """Remove the Run value. True when it is gone, including already gone."""
    if not (paths.is_windows() if windows is None else windows):
        return False
    reg = reg or _registry()
    try:
        with _open(reg, write=True) as key:
            reg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
