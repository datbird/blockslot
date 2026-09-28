"""The Mac: the LaunchAgent, its python, and Steam's own process and links.

launchd only exists on a Mac; everything here that reaches it goes through a
fake launchctl, so these run on every platform. Paths are built with Path and
compared as Path, never joined as POSIX strings, so they hold on Windows too.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from gui import launchagent, winservice  # noqa: E402
from gui.core import launchopts, paths  # noqa: E402
import slotd  # noqa: E402


class FakeLaunchctl(object):
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        verb = argv[1]
        code = self.fail.get(verb, 0)
        return subprocess.CompletedProcess(argv, code, "", "boom" if code else "")

    def verbs(self):
        return [call[1] for call in self.calls]


class FakeSlotd(object):
    """What install needs of slotd: a queue folder, a Keychain, no daemon."""

    def __init__(self, state, keychain=True):
        self.state = state
        self.keychain = keychain
        self.sealed = {}

    def default_state_dir(self):
        return self.state

    def is_sealed(self, text):
        return slotd.is_sealed(text)

    def protect(self, text, machine=False, name=None):
        if not self.keychain:
            return text
        self.sealed[name] = text
        return "keychain:" + name

    def _client_from_info(self, _state):
        return None


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="agent-"))
        self.config = self.home / "savepick.json"
        self.state = str(self.home / "state")

    def tearDown(self):
        shutil.rmtree(str(self.home), ignore_errors=True)

    def write_config(self, store):
        with open(str(self.config), "w", encoding="utf-8") as handle:
            json.dump({"store": store, "trees": {}}, handle)

    def read_config(self):
        with open(str(self.config), "r", encoding="utf-8") as handle:
            return json.load(handle)


class ThePlist(Base):
    def test_it_keeps_alive_on_a_crash_and_runs_at_load(self):
        data = launchagent.plist_data("c.json", home=self.home, frozen=False,
                                      python="/opt/homebrew/bin/python3",
                                      script="/x/blockslot.py")
        self.assertEqual(data["Label"], launchagent.LABEL)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(data["ProgramArguments"],
                         ["/opt/homebrew/bin/python3", "/x/blockslot.py",
                          "--service", "--config", "c.json"])

    def test_logs_go_under_library_logs(self):
        data = launchagent.plist_data("c.json", home=self.home, frozen=True,
                                      executable="/A/BlockSlot")
        self.assertEqual(Path(data["StandardOutPath"]),
                         self.home / "Library" / "Logs" / "BlockSlot" / "agent.log")
        self.assertEqual(data["StandardErrorPath"], data["StandardOutPath"])
        self.assertEqual(data["ProgramArguments"][:2], ["/A/BlockSlot", "--service"])

    def test_it_lives_in_the_users_launch_agents(self):
        self.assertEqual(launchagent.agent_path(self.home),
                         self.home / "Library" / "LaunchAgents"
                         / (launchagent.LABEL + ".plist"))

    def test_what_is_written_is_a_plist_launchd_can_read(self):
        path = launchagent.agent_path(self.home)
        launchagent.write_plist(path, launchagent.plist_data(
            "c.json", home=self.home, frozen=True, executable="/A/B"))
        with open(str(path), "rb") as handle:
            self.assertEqual(plistlib.load(handle)["Label"], launchagent.LABEL)


class Install(Base):
    def install(self, runner, keychain=True, python="/opt/homebrew/bin/python3"):
        self.slotd = FakeSlotd(self.state, keychain)
        return launchagent.install(str(self.config), runner=runner, uid=501,
                                   home=self.home, slotd_module=self.slotd,
                                   frozen=False, python=python,
                                   script="/x/blockslot.py")

    def test_it_boots_out_enables_and_bootstraps_the_gui_domain(self):
        self.write_config({"type": "ssh", "host": "h", "root": "/r"})
        fake = FakeLaunchctl()
        lines = self.install(fake)
        self.assertEqual(fake.verbs(), ["bootout", "enable", "bootstrap"])
        self.assertEqual(fake.calls[2][2:],
                         ["gui/501", str(launchagent.agent_path(self.home))])
        self.assertTrue(launchagent.agent_path(self.home).is_file())
        self.assertTrue(any("Started" in line for line in lines))

    def test_secrets_move_into_the_keychain_and_the_store_is_marked(self):
        self.write_config({"type": "s3", "secret_key": "SK",
                           "cf_client_secret": "keychain:cf_client_secret"})
        self.install(FakeLaunchctl())
        store = self.read_config()["store"]
        self.assertEqual(store["secret_key"], "keychain:secret_key")
        self.assertEqual(self.slotd.sealed, {"secret_key": "SK"})
        self.assertTrue(store["service"])
        self.assertEqual(store["state_dir"], self.state)

    def test_a_queue_folder_already_named_is_kept(self):
        self.write_config({"type": "ssh", "host": "h", "root": "/r",
                           "state_dir": str(self.home / "old-queue")})
        self.install(FakeLaunchctl())
        self.assertEqual(self.read_config()["store"]["state_dir"],
                         str(self.home / "old-queue"))

    def test_no_keychain_leaves_the_key_in_the_private_file_and_says_so(self):
        self.write_config({"type": "s3", "secret_key": "SK"})
        lines = self.install(FakeLaunchctl(), keychain=False)
        self.assertEqual(self.read_config()["store"]["secret_key"], "SK")
        self.assertTrue(any("would not take" in line for line in lines))

    def test_no_store_is_refused(self):
        self.write_config({})
        with self.assertRaises(RuntimeError):
            self.install(FakeLaunchctl())

    def test_the_python_stub_is_refused_before_anything_is_written(self):
        self.write_config({"type": "ssh", "host": "h", "root": "/r"})
        if paths.mac_stub_works():
            self.skipTest("this Mac has the developer tools, so the stub works")
        with self.assertRaises(RuntimeError) as caught:
            self.install(FakeLaunchctl(), python="/usr/bin/python3")
        self.assertIn("xcode-select --install", str(caught.exception))
        self.assertFalse(launchagent.agent_path(self.home).exists())

    def _as_the_app(self):
        self.addCleanup(setattr, paths, "is_mac", paths.is_mac)
        paths.is_mac = lambda: True
        self.slotd = FakeSlotd(self.state)

    def test_a_translocated_app_is_refused_before_anything_is_written(self):
        self.write_config({"type": "ssh", "host": "h", "root": "/r"})
        self._as_the_app()
        moved = ("/private/var/folders/ab/T/AppTranslocation/0A1B/d/"
                 "Blockslot.app/Contents/MacOS/Blockslot")
        for where in (moved, "/Volumes/BlockSlot/Blockslot.app/Contents/MacOS/Blockslot"):
            fake = FakeLaunchctl()
            with self.assertRaises(RuntimeError) as caught:
                launchagent.install(str(self.config), runner=fake, uid=501,
                                    home=self.home, slotd_module=self.slotd,
                                    frozen=True, executable=where)
            self.assertIn("Applications", str(caught.exception))
            self.assertEqual(fake.calls, [])
            self.assertFalse(launchagent.agent_path(self.home).exists())

    def test_the_app_in_applications_names_itself(self):
        self.write_config({"type": "ssh", "host": "h", "root": "/r"})
        self._as_the_app()
        app = "/Applications/Blockslot.app/Contents/MacOS/Blockslot"
        launchagent.install(str(self.config), runner=FakeLaunchctl(), uid=501,
                            home=self.home, slotd_module=self.slotd,
                            frozen=True, executable=app)
        with open(str(launchagent.agent_path(self.home)), "rb") as handle:
            argv = plistlib.load(handle)["ProgramArguments"]
        self.assertEqual(argv[:2], [app, "--service"])

    def test_launchctl_saying_no_is_an_error_in_its_words(self):
        self.write_config({"type": "ssh", "host": "h", "root": "/r"})
        with self.assertRaises(RuntimeError) as caught:
            self.install(FakeLaunchctl(fail={"bootstrap": 5}))
        self.assertIn("boom", str(caught.exception))


class Uninstall(Base):
    def test_it_boots_out_removes_the_plist_and_clears_the_flag(self):
        self.write_config({"type": "ssh", "service": True, "state_dir": "/q"})
        path = launchagent.agent_path(self.home)
        launchagent.write_plist(path, {"Label": launchagent.LABEL})
        fake = FakeLaunchctl()
        lines = launchagent.uninstall(str(self.config), runner=fake, uid=501,
                                      home=self.home)
        self.assertEqual(fake.calls[0][1:], ["bootout", "gui/501/" + launchagent.LABEL])
        self.assertFalse(path.exists())
        self.assertNotIn("service", self.read_config()["store"])
        self.assertEqual(self.read_config()["store"]["state_dir"], "/q")
        self.assertTrue(lines)

    def test_nothing_installed_says_so(self):
        fake = FakeLaunchctl(fail={"bootout": 3})
        self.assertEqual(launchagent.uninstall(None, runner=fake, uid=501,
                                               home=self.home),
                         ["The agent is not installed"])


class TheHost(Base):
    def test_the_mac_host_logs_to_its_own_place_and_queue(self):
        said = []
        host = winservice.Host(str(self.config), slotd, say=said.append,
                               default_state=lambda: self.state)
        self.assertEqual(host.default_state(), self.state)
        host.log("hello")
        self.assertEqual(said, ["hello"])

    def test_a_secret_that_will_not_open_is_waited_out(self):
        said = []

        class Refusing(object):
            ss = slotd.ss

            @staticmethod
            def load_settings(_config):
                raise slotd.ss.StoreRefused("the Keychain is locked")

        host = winservice.Host(str(self.config), Refusing, say=said.append)
        host.stop_event.wait = lambda _timeout: host.stop_event.set()
        host.run()
        self.assertTrue(any("Keychain is locked" in line for line in said))


class MacPython(unittest.TestCase):
    """Which python a Mac launch option and the agent name."""

    def only(self, *present):
        return lambda path: path in present

    def test_the_stub_without_developer_tools_is_not_python(self):
        nothing = self.only()
        self.assertFalse(paths.mac_python_is_real("/usr/bin/python3", nothing))
        self.assertTrue(paths.mac_python_is_real("/opt/homebrew/bin/python3", nothing))

    def test_with_developer_tools_the_stub_is_the_stable_answer(self):
        tools = self.only("/Library/Developer/CommandLineTools/usr/bin/python3")
        running = "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3"
        self.assertEqual(paths.mac_python(running, tools), Path("/usr/bin/python3"))
        self.assertTrue(paths.mac_python_is_real("/usr/bin/python3", tools))

    def test_homebrew_then_python_org_when_there_are_no_tools(self):
        self.assertEqual(paths.mac_python(None, self.only(
            "/usr/local/bin/python3", "/opt/homebrew/bin/python3")),
            Path("/opt/homebrew/bin/python3"))
        framework = "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
        self.assertEqual(paths.mac_python(None, self.only(framework)), Path(framework))

    def test_a_running_python_outside_the_tools_is_kept(self):
        self.assertEqual(paths.mac_python("/opt/homebrew/bin/python3.12",
                                          self.only()),
                         Path("/opt/homebrew/bin/python3.12"))

    def test_nothing_at_all_falls_back_to_the_stub(self):
        self.assertEqual(paths.mac_python(None, self.only()), Path("/usr/bin/python3"))

    def test_logs_are_under_library_logs(self):
        self.assertEqual(paths.mac_logs_dir(),
                         Path.home() / "Library" / "Logs" / "BlockSlot")


class MacSteam(unittest.TestCase):
    def test_steam_is_asked_to_exit_by_its_own_link(self):
        self.assertEqual(launchopts.stop_command(platform="darwin"),
                         ["/usr/bin/open", "steam://exit"])

    def test_steam_starts_through_launch_services(self):
        real = launchopts.mac_steam_app
        launchopts.mac_steam_app = lambda: "/Applications/Steam.app"
        try:
            self.assertEqual(launchopts.start_command(platform="darwin"),
                             ["/usr/bin/open", "-a", "/Applications/Steam.app"])
        finally:
            launchopts.mac_steam_app = real

    def test_steam_app_is_looked_for_in_both_applications_folders(self):
        mine = str(Path.home() / "Applications" / "Steam.app")
        self.assertEqual(launchopts.mac_steam_app(lambda path: path == mine), mine)
        self.assertIsNone(launchopts.mac_steam_app(lambda _path: False))

    @unittest.skipUnless(hasattr(os, "getuid"), "pgrep is POSIX")
    def test_the_mac_client_process_is_steam_osx(self):
        seen = []
        real_run, real_platform = launchopts.subprocess.run, launchopts.sys.platform

        def fake(argv, **_kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "885\n", "")

        launchopts.subprocess.run = fake
        launchopts.sys.platform = "darwin"
        try:
            self.assertTrue(launchopts.steam_running())
        finally:
            launchopts.subprocess.run = real_run
            launchopts.sys.platform = real_platform
        # Only this user's (gui/tests/test_desktops.py has why).
        self.assertEqual(seen[0], ["pgrep", "-x", "-u", str(os.getuid()), "steam_osx"])


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class OnAMac(unittest.TestCase):
    """Reads only: nothing here installs, starts or writes anything."""

    def test_launchctl_is_where_install_calls_it(self):
        self.assertTrue(os.path.isfile(launchagent.LAUNCHCTL))
        code, _out = launchagent._launchctl("print", launchagent.domain())
        self.assertIsNotNone(code)

    def test_the_named_python_is_real_or_install_would_say_so(self):
        try:
            python = paths.python_for_launch()
        except paths.NoRealPython:
            self.skipTest("no python on this Mac beyond the stub")
        self.assertTrue(os.path.isfile(str(python)))


if __name__ == "__main__":
    unittest.main()
