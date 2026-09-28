"""Blockslot's daemon as a macOS LaunchAgent: `blockslot --install-service`.

The Mac's answer to the Windows service (gui/winservice.py). A LaunchAgent in
~/Library/LaunchAgents runs as the user, in the user's login session, from
login until logout, and launchd starts it again if it crashes. That is all
the daemon needs: it runs as the user anyway, so unlike the Windows service
there is nothing to re-seal and no shared folder to open up.

    ~/Library/LaunchAgents/com.datbird.blockslot.plist   the agent
    ~/Library/Logs/BlockSlot/daemon.log                  what the daemon says
    ~/Library/Logs/BlockSlot/agent.log                   its stdout and stderr
    ~/Library/Application Support/Blockslot/store        the queue

The agent runs `blockslot.py --service`, which on a Mac is the same Host the
Windows service runs: it waits for a store to be set up, reloads when
savepick.json changes, and steps aside while another daemon answers. So
saving the Store screen needs no restart here either.

KeepAlive restarts it only when it did not exit cleanly. A clean exit is
launchd itself stopping it (logout, or uninstall), and SIGTERM is turned into
that clean exit so the daemon withdraws daemon.json on the way out.

Install and remove go through launchctl's bootstrap and bootout, the forms
Apple has kept since 10.10; the old load and unload still work but say less
when they fail.

Standard library only; Python 3.9.
"""

import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from gui.core import paths  # noqa: E402

LABEL = "com.datbird.blockslot"
LAUNCHCTL = "/bin/launchctl"
THROTTLE_SECONDS = 30
# launchd hands an agent only /usr/bin:/bin:/usr/sbin:/sbin. ssh is in
# /usr/bin, but a cloudflared or a ludusavi from Homebrew is not.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SECRETS = ("secret_key", "cf_client_secret")

NO_PYTHON = (
    "this Mac has no python 3 to run the daemon with: /usr/bin/python3 is "
    "only the installer for Apple's command line tools. Run "
    "`xcode-select --install`, or install python from python.org or "
    "Homebrew, then install again.")

TEMPORARY = (
    "BlockSlot is running from a temporary place (%s): a copy macOS moved "
    "aside because it came from the internet, or one on a disk image. The "
    "agent would name a program that is gone at the next login. Move "
    "BlockSlot.app into your Applications folder, open it from there, then "
    "install again.")


def agent_path(home=None):
    return Path(home or paths.home()) / "Library" / "LaunchAgents" / (LABEL + ".plist")


def logs_dir(home=None):
    if home is None:
        return paths.mac_logs_dir()
    return Path(home) / "Library" / "Logs" / "BlockSlot"


def domain(uid=None):
    """The user's GUI session in launchd, where a LaunchAgent lives."""
    return "gui/%d" % (os.getuid() if uid is None else uid)


def agent_arguments(config, frozen=None, executable=None, script=None,
                    python=None):
    """ProgramArguments: the program, then --service and the settings file."""
    frozen = paths.is_frozen() if frozen is None else frozen
    if frozen:
        head = [str(executable or sys.executable)]
    else:
        head = [str(python or paths.python_for_launch()),
                str(script or HERE / "blockslot.py")]
    return head + ["--service", "--config", str(config)]


def plist_data(config, home=None, **command):
    """The agent's property list, as a dict for plistlib."""
    home = Path(home or paths.home())
    log = str(logs_dir(home) / "agent.log")
    return {
        "Label": LABEL,
        "ProgramArguments": agent_arguments(config, **command),
        "RunAtLoad": True,
        # Only a crash brings it back: a clean exit is launchd's own stop.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {"PATH": AGENT_PATH},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def write_plist(path, data):
    """Written whole through a temporary file, so launchd never reads half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = str(path) + ".tmp"
    with open(temp, "wb") as handle:
        plistlib.dump(data, handle)
    os.chmod(temp, 0o644)
    os.replace(temp, str(path))


def _launchctl(*args, runner=None):
    run = runner or subprocess.run
    try:
        done = run([LAUNCHCTL] + list(args), capture_output=True, text=True,
                   timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def is_loaded(runner=None, uid=None):
    code, _out = _launchctl("print", "%s/%s" % (domain(uid), LABEL), runner=runner)
    return code == 0


# ------------------------------------------------------------------ install


def prepare_settings(config, slotd_module, default_state):
    """Mark the store as held by the agent, pin its queue folder, and move
    any key still written in the file into the Keychain.

    A queue folder already named is kept: saves may be waiting in it.

    "service" is the flag the Windows service sets, and it means the same
    here: something else keeps the daemon running and reloads on its own,
    so the picker does not start one of its own and the Store screen does
    not restart it. Returns (plain lines saying what was done, the queue).
    """
    from gui.core import settings as settings_mod
    conf = settings_mod.Settings.load(config)
    store = conf.store()
    if not store.get("type"):
        raise RuntimeError("no store is set up in %s" % config)
    lines = []
    moved = []
    for field in SECRETS:
        value = store.get(field)
        if isinstance(value, str) and value and not slotd_module.is_sealed(value):
            sealed = slotd_module.protect(value, name=field)
            if sealed != value:
                store[field] = sealed
                moved.append(field)
    if moved:
        lines.append("Moved %s into the login Keychain" % " and ".join(moved))
    elif any(isinstance(store.get(field), str) and store.get(field)
             and not slotd_module.is_sealed(store[field]) for field in SECRETS):
        lines.append("The Keychain would not take the store's secret; it stays "
                     "in %s, which only you can read" % config)
    state_dir = store.get("state_dir") or str(default_state)
    store["state_dir"] = state_dir
    store["service"] = True
    conf.data["store"] = store
    conf.save()
    lines.append("Queue: %s" % state_dir)
    return lines, state_dir


def install(config, runner=None, uid=None, home=None, slotd_module=None,
            **command):
    """Write the agent and start it. Returns plain lines; raises RuntimeError
    with launchctl's own words when a step fails."""
    frozen = command.get("frozen")
    frozen = paths.is_frozen() if frozen is None else frozen
    python = command.get("python")
    if frozen:
        # The agent names this program by its full path at every login. A
        # translocated copy or one on a disk image is gone by then.
        executable = command.get("executable") or sys.executable
        if paths.in_temporary_place(executable):
            raise RuntimeError(TEMPORARY % executable)
    else:
        try:
            python = python or paths.python_for_launch()
        except paths.NoRealPython:
            raise RuntimeError(NO_PYTHON)
        if not paths.mac_python_is_real(python):
            raise RuntimeError(NO_PYTHON)
        command["python"] = python
    if slotd_module is None:
        from gui.tray import load_slotd
        slotd_module = load_slotd()
    lines, state = prepare_settings(config, slotd_module,
                                    slotd_module.default_state_dir())
    Path(state).mkdir(parents=True, exist_ok=True)
    logs_dir(home).mkdir(parents=True, exist_ok=True)

    # A daemon the picker started on its own holds the queue, and the
    # agent's host would only wait behind it. Ask it to go.
    client = slotd_module._client_from_info(state)
    if client is not None:
        try:
            client.stop()
        except Exception:
            pass

    path = agent_path(home)
    write_plist(path, plist_data(config, home=home, **command))
    lines.append("Wrote %s" % path)
    target = "%s/%s" % (domain(uid), LABEL)
    # Replace one that is loaded, and lift a `launchctl disable` left from
    # before: a disabled label refuses bootstrap.
    _launchctl("bootout", target, runner=runner)
    _launchctl("enable", target, runner=runner)
    code, out = _launchctl("bootstrap", domain(uid), str(path), runner=runner)
    if code != 0:
        raise RuntimeError("launchctl could not start the agent: %s" % out)
    lines.append("Started it (at every login, and again if it crashes)")
    lines.append("Logs: %s" % logs_dir(home))
    return lines


def uninstall(config=None, runner=None, uid=None, home=None):
    """Stop the agent and remove it. Secrets stay in the Keychain, where the
    settings still point, and the picker goes back to starting a daemon of
    its own."""
    lines = []
    path = agent_path(home)
    code, out = _launchctl("bootout", "%s/%s" % (domain(uid), LABEL), runner=runner)
    if code == 0:
        lines.append("Stopped the agent")
    if path.is_file():
        path.unlink()
        lines.append("Removed %s" % path)
    if not lines:
        return ["The agent is not installed"]
    if config and os.path.isfile(str(config)):
        from gui.core import settings as settings_mod
        conf = settings_mod.Settings.load(config)
        store = conf.store()
        if store.get("service"):
            store.pop("service", None)
            conf.data["store"] = store
            conf.save()
            lines.append("Games start their own uploader again")
    return lines


# ------------------------------------------------------------------ the host


def log(message, home=None):
    import time
    try:
        folder = logs_dir(home)
        folder.mkdir(parents=True, exist_ok=True)
        with open(str(folder / "daemon.log"), "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def run(config, slotd_module):
    """What launchd runs. Blocks until launchd stops it."""
    from gui.winservice import Host
    host = Host(config, slotd_module, say=log,
                default_state=slotd_module.default_state_dir)

    def stop(_signum, _frame):
        host.stop()

    signal.signal(signal.SIGTERM, stop)
    log("agent started (pid %d)" % os.getpid())
    try:
        host.run()
    except KeyboardInterrupt:
        host.stop()
    return 0
