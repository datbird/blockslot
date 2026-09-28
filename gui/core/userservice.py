"""Blockslot's daemon as a systemd user unit: `blockslot.py --install-service`.

The Linux desktop's answer to the Windows service (gui/winservice.py). A user
unit runs as the person, needs no root, starts with their session and is
restarted by systemd when it crashes, so a save queued while a laptop was
offline goes up once it is back on a network without anyone opening a window.

Why a user unit and not a system one: the daemon's settings, secrets and queue
are this person's, in their home, and the picker that talks to it runs as them
from Steam. A system unit would have to be told whose home to use and would
need root to install. And why not an XDG autostart entry: nothing restarts
one after a crash, and it only starts with a graphical login.

Where things are (the XDG base directories, under the real home even when this
runs inside the Steam snap, see paths.home):

    ~/.config/systemd/user/blockslot.service   the unit
    ~/.local/state/blockslot/store/            the queue and daemon.json
    ~/.local/state/blockslot/daemon.log        what the daemon prints
    ~/.config/savepick.json                    settings and store secrets, 0600

The unit is written with absolute paths resolved at install time, so it runs
the same python and the same Blockslot that installed it. Moving the checkout
means installing again, which --check points out.

Standard library only; Python 3.9.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from . import autostart, paths

UNIT = "blockslot.service"
DESCRIPTION = "BlockSlot save sync"
# Exit 2 is the daemon's "the store settings are wrong" (tray.run_daemon, and
# slotd --serve with no store). Restarting cannot fix that, so systemd is told
# not to try; a person fixing the settings starts it again from the window.
SETTINGS_EXIT = 2


def unit_dir():
    return paths.xdg_dir("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def unit_path():
    return unit_dir() / UNIT


def state_root():
    return paths.xdg_dir("XDG_STATE_HOME", ".local/state") / "blockslot"


def log_path():
    return state_root() / "daemon.log"


def is_supported():
    """True on Linux, where there is a systemd to ask."""
    return sys.platform.startswith("linux")


# ------------------------------------------------------------------ the unit


def _quote(word):
    """One ExecStart word, quoted for systemd.

    systemd reads % as a specifier, $ as a variable and backslash and double
    quote as escapes, so each is escaped; the word is always quoted, which
    keeps a path with spaces one argument.
    """
    text = (str(word).replace("\\", "\\\\").replace('"', '\\"')
            .replace("%", "%%").replace("$", "$$"))
    return '"%s"' % text


def _plain(path):
    """A path in a setting that is not a command line: only % is special."""
    return str(path).replace("%", "%%")


def exec_argv(config=None, frozen=None, executable=None, script=None, python=None):
    """What the unit runs: the same --daemon the Windows Run value starts.

    A built program is its own launcher; a checkout needs python named in
    front of it. --config pins the settings file, so the daemon reads the
    one the window writes whatever environment systemd gives it.
    """
    argv = autostart.command_argv(frozen=frozen, executable=executable,
                                  script=script, python=python)
    if config:
        argv += ["--config", str(config)]
    return argv


def unit_text(argv, log=None):
    """The unit file for `argv`.

    Restart=on-failure with a 10 s pause, and StartLimit* so a daemon that
    cannot stay up stops being restarted after five tries in five minutes
    instead of looping all day. UMask 077 makes everything it writes (the
    queue, daemon.json with its token) this person's alone.
    """
    log = log or log_path()
    lines = [
        "[Unit]",
        "Description=%s" % DESCRIPTION,
        "Documentation=https://github.com/datbird/blockslot",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=%s" % " ".join(_quote(word) for word in argv),
        "Restart=on-failure",
        "RestartSec=10",
        "RestartPreventExitStatus=%d" % SETTINGS_EXIT,
        "UMask=0077",
        "Environment=PYTHONUNBUFFERED=1",
        "StandardOutput=append:%s" % _plain(log),
        "StandardError=inherit",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ systemctl


def _systemctl(*args, runner=None):
    run = runner or subprocess.run
    try:
        done = run(["systemctl", "--user"] + list(args), capture_output=True,
                   text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def is_installed():
    return unit_path().is_file()


def is_active(runner=None):
    code, _out = _systemctl("is-active", UNIT, runner=runner)
    return code == 0


def is_current(config=None):
    """True when the installed unit runs this Blockslot, from this place."""
    try:
        have = unit_path().read_text(encoding="utf-8")
    except OSError:
        return False
    return have == unit_text(exec_argv(config))


# ------------------------------------------------------------------ install


def make_private(path):
    """Mode 0600 on a file that holds secrets, when it exists. True if it is.

    savepick.json carries the store's keys in plain text off Windows, as it
    does on the Deck. The window already writes it 0600; a copy made by hand
    or restored from a backup may not be.
    """
    path = Path(path)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return False
    if mode & 0o077:
        os.chmod(str(path), 0o600)
        return True
    return False


def _stop_stray(config, runner=None, wait=10.0, slotd=None):
    """Stop a daemon running outside the unit. True if one was stopped.

    A picker with no unit to start forks its own daemon. Left running, the
    unit's --daemon finds it answering and exits 0 at once, systemd counts
    that a clean stop, and nothing restarts the forked one when it dies.
    """
    try:
        if slotd is None:
            from . import settings as settings_mod
            slotd = settings_mod.engine_module("slotd")
        loaded, _device = slotd.load_settings(str(config))
    except Exception:
        return False
    if not loaded:
        return False
    state_dir = loaded.get("state_dir") or slotd.default_state_dir()
    client = slotd._client_from_info(state_dir)
    if client is None or is_active(runner=runner):
        return False
    try:
        client.stop()
    except Exception:
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and slotd._client_from_info(state_dir):
        time.sleep(0.2)
    return True


def install(config=None, runner=None):
    """Write the unit, enable it for every login and (re)start it.

    Returns plain lines saying what was done. Raises RuntimeError with
    systemctl's own words when a step fails; the unit file is left in place
    then, so `systemctl --user status blockslot` can say more.
    """
    if not is_supported():
        raise RuntimeError("a systemd user unit is for Linux only")
    config = Path(config) if config else paths.config_path()
    lines = []
    state = state_root()
    (state / "store").mkdir(parents=True, exist_ok=True)
    for folder in (state, state / "store"):
        os.chmod(str(folder), 0o700)
    lines.append("Queue: %s" % (state / "store"))
    lines.append("Log: %s" % log_path())
    if make_private(config):
        lines.append("Made %s private (0600): it holds the store keys" % config)

    text = unit_text(exec_argv(config))
    unit_dir().mkdir(parents=True, exist_ok=True)
    fresh = not is_installed()
    temp = unit_path().with_name(UNIT + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(str(temp), str(unit_path()))
    lines.append("%s %s" % ("Wrote" if fresh else "Updated", unit_path()))

    for step in (("daemon-reload",), ("enable", UNIT)):
        code, out = _systemctl(*step, runner=runner)
        if code != 0:
            raise RuntimeError("systemctl --user %s failed: %s"
                               % (" ".join(step), out or "no reason given"))
    lines.append("Enabled: it starts at login and restarts if it fails")
    if _stop_stray(config, runner=runner):
        lines.append("Stopped the daemon a game had started, so the unit runs it")
    # restart, not start: a daemon already running holds the old unit's
    # command line, and a changed unit is the reason to install again.
    code, out = _systemctl("restart", UNIT, runner=runner)
    if code != 0:
        raise RuntimeError("the daemon did not start: %s" % (out or "no reason given"))
    lines.append("Started it")
    return lines


def uninstall(runner=None):
    """Stop the daemon, disable it and remove the unit. Returns plain lines.

    The queue is left alone: a save in it has not reached the store yet, and
    the picker still uploads it on the next game exit.
    """
    if not is_supported():
        raise RuntimeError("a systemd user unit is for Linux only")
    lines = []
    if not is_installed():
        lines.append("No unit at %s; nothing to remove" % unit_path())
        return lines
    code, out = _systemctl("disable", "--now", UNIT, runner=runner)
    if code != 0:
        raise RuntimeError("systemctl --user disable --now %s failed: %s"
                           % (UNIT, out or "no reason given"))
    lines.append("Stopped and disabled %s" % UNIT)
    unit_path().unlink()
    lines.append("Removed %s" % unit_path())
    _systemctl("daemon-reload", runner=runner)
    lines.append("The queue in %s is kept" % (state_root() / "store"))
    return lines
