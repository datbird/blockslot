"""The Windows service: the command line, the install steps and the reload loop.

The service manager itself only exists on Windows; everything it depends on is
tested here with fakes.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from gui import winservice  # noqa: E402
from gui.core import autostart  # noqa: E402
import slotd  # noqa: E402
import slotstore as ss  # noqa: E402


class Commands(unittest.TestCase):
    def test_the_exe_runs_itself_with_the_config(self):
        line = winservice.service_command(r"C:\U\savepick.json", frozen=True,
                                          executable=r"C:\A\Blockslot.exe")
        self.assertEqual(line, r'"C:\A\Blockslot.exe" --service --config "C:\U\savepick.json"')

    def test_a_checkout_uses_python_not_pythonw(self):
        line = winservice.service_command("c.json", frozen=False,
                                          python=r"C:\Py\pythonw.exe", script="b.py")
        self.assertTrue(line.startswith(r'"C:\Py\python.exe" "b.py" --service'))

    def test_login_starts_the_tray_when_the_service_holds_the_daemon(self):
        self.assertTrue(autostart.command_line(frozen=True, executable="B.exe",
                                               flag="--tray").endswith("--tray"))


class FakeSC(object):
    """sc.exe, remembering what it was told."""

    def __init__(self, installed=False):
        self.installed = installed
        self.running = False
        self.calls = []

    def __call__(self, argv, **_kw):
        args = argv[1:]
        self.calls.append(args)
        out, code = "", 0
        if args[0] == "query":
            if not self.installed:
                code = 1060
            else:
                out = "STATE : 4 RUNNING" if self.running else "STATE : 1 STOPPED"
        elif args[0] == "create":
            self.installed = True
        elif args[0] == "start":
            self.running = True
        elif args[0] == "stop":
            self.running = False
        elif args[0] == "delete":
            self.installed = False
        return type("Done", (), {"returncode": code, "stdout": out, "stderr": ""})()


class Installing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "savepick.json")
        with open(self.config, "w") as handle:
            json.dump({"syncthing": {"x": 1}, "store": {"type": "local", "root": self.dir,
                                                        "secret_key": "plain"}}, handle)
        self.real_pd = winservice.program_data
        winservice.program_data = lambda: Path(self.dir) / "PD"

    def tearDown(self):
        winservice.program_data = self.real_pd
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_install_registers_starts_and_points_settings_at_the_shared_queue(self):
        sc = FakeSC()
        lines = winservice.install(self.config, runner=sc, grant_user="")
        self.assertTrue(sc.installed and sc.running)
        verbs = [call[0] for call in sc.calls]
        self.assertIn("create", verbs)
        self.assertIn("failure", verbs)
        data = json.load(open(self.config))
        self.assertTrue(data["store"]["service"])
        self.assertEqual(data["store"]["state_dir"], str(Path(self.dir) / "PD" / "store"))
        self.assertEqual(data["syncthing"], {"x": 1})
        self.assertTrue(os.path.exists(self.config + ".before-service.bak"))
        self.assertTrue(any("Started" in line for line in lines))

    def test_a_second_install_updates_instead_of_failing(self):
        sc = FakeSC(installed=True)
        winservice.install(self.config, runner=sc, grant_user="")
        self.assertIn("config", [call[0] for call in sc.calls])
        self.assertNotIn("create", [call[0] for call in sc.calls])

    def test_uninstall_stops_and_removes(self):
        sc = FakeSC(installed=True)
        sc.running = True
        winservice.uninstall(runner=sc)
        self.assertFalse(sc.installed)


class Reloading(unittest.TestCase):
    """The service reruns the daemon when savepick.json changes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "savepick.json")
        os.makedirs(os.path.join(self.dir, "store"))
        self.write("one")
        self.real_poll = winservice.RELOAD_POLL
        winservice.RELOAD_POLL = 0.2
        # The service log goes under PROGRAMDATA; off Windows that would be a
        # folder named "C:\ProgramData" in the working directory.
        self.real_programdata = os.environ.get("PROGRAMDATA")
        os.environ["PROGRAMDATA"] = self.dir

    def tearDown(self):
        winservice.RELOAD_POLL = self.real_poll
        if self.real_programdata is None:
            os.environ.pop("PROGRAMDATA", None)
        else:
            os.environ["PROGRAMDATA"] = self.real_programdata
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, device):
        with open(self.config, "w") as handle:
            json.dump({"store": {"type": "local", "root": os.path.join(self.dir, "store"),
                                 "device": device,
                                 "state_dir": os.path.join(self.dir, "state")}}, handle)

    def device_answering(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            client = slotd._client_from_info(os.path.join(self.dir, "state"))
            if client:
                try:
                    return client.status()["device"]
                except Exception:
                    pass
            time.sleep(0.1)
        return None

    def test_a_settings_change_reloads_the_daemon(self):
        host = winservice.Host(self.config, slotd)
        thread = threading.Thread(target=host.run, daemon=True)
        thread.start()
        try:
            self.assertEqual(self.device_answering(), "one")
            time.sleep(1.1)     # a later mtime on every filesystem
            self.write("two")
            deadline = time.monotonic() + 10
            seen = None
            while time.monotonic() < deadline and seen != "two":
                seen = self.device_answering(1)
            self.assertEqual(seen, "two")
        finally:
            host.stop()
            thread.join(20)
        self.assertFalse(thread.is_alive())


class TheTrayForTheService(unittest.TestCase):
    def test_the_proxy_speaks_to_the_service(self):
        from gui import tray
        calls = []

        class Client(object):
            def status(self):
                return {"paused": True, "queued": []}

            def kick(self):
                calls.append("kick")

            def pause(self, value):
                calls.append(("pause", value))
                return {"paused": value}

        proxy = tray.ServiceProxy(lambda: Client())
        self.assertTrue(proxy.status()["paused"])
        self.assertTrue(proxy.paused)
        proxy.kick.set()
        proxy.paused = False
        self.assertEqual(calls, ["kick", ("pause", False)])

    def test_no_service_reads_as_offline_not_a_crash(self):
        from gui import tray
        proxy = tray.ServiceProxy(lambda: None)
        self.assertEqual(proxy.status()["error"]["kind"], "offline")
        proxy.kick.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
