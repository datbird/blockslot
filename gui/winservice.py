"""Blockslot's daemon as a native Windows service: `Blockslot.exe --service`.

A service starts before anyone logs in and keeps running across log-offs, so
a save queued while a laptop was offline goes up the moment it is back on a
network, whoever is signed in. It runs as LocalSystem in session 0, which
has two consequences that shape everything here:

- It cannot show a tray icon. The icon is `Blockslot.exe --tray`, a separate
  process in the user's session that talks to the service over the same
  localhost HTTP face the picker uses.
- It cannot read secrets sealed to one user with DPAPI. Installing re-seals
  them with machine scope, which LocalSystem and the user can both open.

Settings live where they always have, in the installing user's savepick.json.
The service watches that file and reloads when it changes, so saving the
Store screen needs no admin rights and no service restart.

Install and remove go through sc.exe, which is on every Windows and says
plainly what failed. The service runtime (the dispatcher and control handler)
is ctypes, because a service process must call into advapi32 itself.

Standard library only; Python 3.9.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from gui.core import paths  # noqa: E402

NAME = "Blockslot"
DISPLAY = "BlockSlot save sync"
DESCRIPTION = ("Uploads game saves to the BlockSlot store and keeps them queued "
               "while this PC is offline.")
RELOAD_POLL = 10.0
RETRY_SECONDS = 30.0


def program_data():
    return Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "Blockslot"


def shared_state_dir():
    """The queue and daemon.json, shared by the service and the picker."""
    return program_data() / "store"


def log_path():
    return program_data() / "service.log"


def log(message):
    try:
        program_data().mkdir(parents=True, exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


# ------------------------------------------------------------------ commands


def service_command(config, frozen=None, executable=None, script=None, python=None):
    """The service's command line, as sc.exe wants it in binPath."""
    frozen = paths.is_frozen() if frozen is None else frozen
    if frozen:
        head = '"%s"' % (executable or sys.executable)
    else:
        # python.exe, not pythonw: a service has no console either way, and
        # python.exe reports a crash to the service log instead of hiding it.
        exe = python or sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-len("pythonw.exe")] + "python.exe"
        head = '"%s" "%s"' % (exe, script or HERE / "blockslot.py")
    return '%s --service --config "%s"' % (head, config)


def _sc(*args, runner=None):
    run = runner or subprocess.run
    done = run(["sc.exe"] + list(args), capture_output=True, text=True)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def is_installed(runner=None):
    code, _out = _sc("query", NAME, runner=runner)
    return code == 0


def is_running(runner=None):
    code, out = _sc("query", NAME, runner=runner)
    return code == 0 and "RUNNING" in out


# ------------------------------------------------------------------ install


def install(config, runner=None, grant_user=None):
    """Install (or update) and start the service. Needs an admin token.

    Returns a list of plain lines saying what was done, and raises
    RuntimeError with sc.exe's own words when a step fails.
    """
    from gui.tray import load_slotd
    load_slotd()
    lines = []
    state = shared_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    user = grant_user or os.environ.get("USERNAME")
    if user:
        # The picker runs as the user and, when the service is down, does the
        # upload itself in this same folder. It needs to write here.
        subprocess.run(["icacls", str(program_data()), "/grant",
                        "%s:(OI)(CI)M" % user, "/T", "/Q"],
                       capture_output=True, text=True)
    lines.append("Shared queue: %s" % state)
    lines += reseal_for_service(config, state)

    command = service_command(config)
    if is_installed(runner):
        code, out = _sc("config", NAME, "binPath=", command, "start=", "auto",
                        runner=runner)
        verb = "Updated"
    else:
        code, out = _sc("create", NAME, "binPath=", command, "start=", "auto",
                        "DisplayName=", DISPLAY, runner=runner)
        verb = "Installed"
    if code != 0:
        raise RuntimeError("sc.exe could not register the service: %s" % out.strip())
    _sc("description", NAME, DESCRIPTION, runner=runner)
    # Restart after a crash: 5 s, 5 s, then a minute; the count resets daily.
    _sc("failure", NAME, "reset=", "86400",
        "actions=", "restart/5000/restart/5000/restart/60000", runner=runner)
    lines.append("%s the %s service (starts with Windows, restarts if it fails)"
                 % (verb, NAME))
    if is_running(runner):
        _sc("stop", NAME, runner=runner)
        _wait_state("STOPPED", runner)
    code, out = _sc("start", NAME, runner=runner)
    if code != 0:
        raise RuntimeError("the service did not start: %s" % out.strip())
    lines.append("Started it")
    return lines


def uninstall(runner=None):
    if not is_installed(runner):
        return ["The service is not installed"]
    _sc("stop", NAME, runner=runner)
    _wait_state("STOPPED", runner)
    code, out = _sc("delete", NAME, runner=runner)
    if code != 0:
        raise RuntimeError("sc.exe could not remove the service: %s" % out.strip())
    return ["Removed the %s service" % NAME]


def _wait_state(state, runner=None, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _code, out = _sc("query", NAME, runner=runner)
        if state in out:
            return True
        time.sleep(0.5)
    return False


def reseal_for_service(config, state_dir):
    """Point the store settings at the shared queue and re-seal the secrets.

    Run as the user who installs: a user-scope DPAPI secret can be opened
    only by that user, and is re-sealed with machine scope so LocalSystem can
    open it too. The file keeps its other settings untouched.
    """
    import shutil
    from gui.tray import load_slotd
    slotd = load_slotd()
    with open(config, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    store = data.get("store")
    if not store:
        raise RuntimeError("no store is set up in %s" % config)
    lines = []
    for field in ("secret_key", "cf_client_secret"):
        value = store.get(field)
        if isinstance(value, str) and value:
            plain = slotd.unprotect(value)
            store[field] = slotd.protect(plain, machine=True)
    store["state_dir"] = str(state_dir)
    store["service"] = True
    backup = config + ".before-service.bak"
    if not os.path.exists(backup):
        shutil.copy2(config, backup)
    tmp = config + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, config)
    lines.append("Settings point at the shared queue; secrets re-sealed for the service")
    return lines


# ------------------------------------------------------------------ the host


class Host(object):
    """Runs the daemon, and runs it again whenever savepick.json changes.

    Also waits, instead of exiting, when another daemon already answers on
    the shared queue (a picker's own worker, say), because a service that
    exits is one the service manager keeps restarting.
    """

    def __init__(self, config, slotd_module):
        self.config = config
        self.slotd = slotd_module
        self.stop_event = threading.Event()
        self.daemon = None

    def _mtime(self):
        try:
            return os.path.getmtime(self.config)
        except OSError:
            return None

    def run(self):
        slotd = self.slotd
        while not self.stop_event.is_set():
            settings, device = slotd.load_settings(self.config)
            if not settings:
                log("no store in %s; checking again in %ds" % (self.config, RETRY_SECONDS))
                self.stop_event.wait(RETRY_SECONDS)
                continue
            state = settings.get("state_dir") or str(shared_state_dir())
            if slotd._client_from_info(state):
                log("another daemon answers on %s; waiting" % state)
                self.stop_event.wait(RETRY_SECONDS)
                continue
            try:
                daemon = slotd.daemon_from_settings(settings, device, state,
                                                    str(self.config))
            except Exception as exc:
                log("the store settings are wrong: %s" % exc)
                self.stop_event.wait(RETRY_SECONDS)
                continue
            self.daemon = daemon
            seen = self._mtime()
            server = threading.Thread(target=slotd.serve, args=(daemon, state),
                                      name="slotd", daemon=True)
            server.start()
            log("serving %s for %s" % (daemon.store_label, device))
            while not self.stop_event.is_set() and server.is_alive():
                self.stop_event.wait(RELOAD_POLL)
                if self._mtime() != seen:
                    log("settings changed; reloading")
                    break
            daemon.paused = True
            if daemon.lock.acquire(timeout=15):
                daemon.lock.release()
            daemon.shutdown()
            server.join(10)
            self.daemon = None
        log("stopped")

    def stop(self):
        self.stop_event.set()


# ------------------------------------------------------------------ the SCM


def run_as_service(config, slotd_module):
    """Hand the process to the service control manager. Blocks until stopped."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    SERVICE_WIN32_OWN_PROCESS = 0x10
    STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
    CONTROL_STOP, CONTROL_INTERROGATE, CONTROL_SHUTDOWN = 1, 4, 5
    ACCEPT_STOP, ACCEPT_SHUTDOWN = 0x1, 0x4
    NO_ERROR = 0

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD),
                    ("dwCheckPoint", wintypes.DWORD), ("dwWaitHint", wintypes.DWORD)]

    MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    HANDLER = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, ctypes.c_void_p)

    class ENTRY(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", MAIN)]

    advapi32.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    advapi32.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER, ctypes.c_void_p]
    advapi32.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
    advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(ENTRY)]

    host = Host(config, slotd_module)
    state = {"handle": None, "status": SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, START_PENDING,
                                                      0, NO_ERROR, 0, 0, 30000)}

    def report(current, accepted=0, wait_hint=0):
        status = state["status"]
        status.dwCurrentState = current
        status.dwControlsAccepted = accepted
        status.dwWaitHint = wait_hint
        status.dwCheckPoint = 0 if current in (RUNNING, STOPPED) else status.dwCheckPoint + 1
        if state["handle"]:
            advapi32.SetServiceStatus(state["handle"], ctypes.byref(status))

    def handler(control, _event_type, _event_data, _context):
        if control in (CONTROL_STOP, CONTROL_SHUTDOWN):
            report(STOP_PENDING, wait_hint=30000)
            host.stop()
            return NO_ERROR
        if control == CONTROL_INTERROGATE:
            return NO_ERROR
        return 120  # ERROR_CALL_NOT_IMPLEMENTED

    handler_ref = HANDLER(handler)

    def service_main(_argc, _argv):
        state["handle"] = advapi32.RegisterServiceCtrlHandlerExW(NAME, handler_ref, None)
        report(RUNNING, ACCEPT_STOP | ACCEPT_SHUTDOWN)
        log("service started (pid %d)" % os.getpid())
        try:
            host.run()
        except Exception as exc:
            log("service failed: %r" % exc)
        report(STOPPED)

    main_ref = MAIN(service_main)
    table = (ENTRY * 2)(ENTRY(NAME, main_ref), ENTRY(None, MAIN()))
    if not advapi32.StartServiceCtrlDispatcherW(table):
        error = ctypes.get_last_error()
        # 1063: not started by the service manager, i.e. run from a console.
        if error == 1063:
            log("--service run outside the service manager; running in the foreground")
            try:
                host.run()
            except KeyboardInterrupt:
                host.stop()
            return 0
        log("StartServiceCtrlDispatcher failed: %d" % error)
        return 1
    return 0
