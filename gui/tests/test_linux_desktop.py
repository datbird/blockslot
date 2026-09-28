#!/usr/bin/env python3
"""Tests for the Linux desktop: snap and flatpak Steam, and the user unit.

What these stand on was probed on an Ubuntu 26.04 laptop (steam snap rev 271) on
2026-09-28: the snap keeps Steam in ~/snap/steam/common/.local/share/Steam,
starts it with HOME=~/snap/steam/common and SNAP_REAL_HOME=<the real home>,
and its client runs as <root>/ubuntu12_32/steam.

No display, no Steam, no systemd: systemctl is a fake that records what it
was asked. Tests that need POSIX file modes or symlinks skip on Windows.

    python3 gui/tests/test_linux_desktop.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.core import (catalog, launchopts, model, paths, settings, steamdir,  # noqa: E402
                      userservice, wrap)
from gui.tests.test_core import LOGINUSERS, build_steam  # noqa: E402

LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-linux-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)


def snap_env(home, real):
    """What a game started by snap Steam sees, as probed on an Ubuntu laptop."""
    return {"SNAP_NAME": "steam", "SNAP_REAL_HOME": str(real),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
            "XDG_DATA_HOME": str(Path(home) / ".local" / "share")}


# ----------------------------------------------------------------- finding it


class Candidates(Temp):
    def test_linux_looks_at_native_snap_and_flatpak(self):
        with mock.patch.object(sys, "platform", "linux"):
            found = steamdir.candidate_roots(self.dir)
        self.assertEqual(found[0], self.dir / ".local" / "share" / "Steam")
        self.assertIn(self.dir / "snap" / "steam" / "common" / ".local" / "share"
                      / "Steam", found)
        self.assertIn(self.dir / ".var" / "app" / "com.valvesoftware.Steam"
                      / ".local" / "share" / "Steam", found)

    def test_macos_looks_in_application_support(self):
        with mock.patch.object(sys, "platform", "darwin"):
            found = steamdir.candidate_roots(self.dir)
        self.assertEqual(found, [self.dir / "Library" / "Application Support"
                                 / "Steam"])

    def test_the_home_is_the_real_one_from_inside_the_snap(self):
        inside = self.dir / "snap" / "steam" / "common"
        with mock.patch.dict(os.environ, snap_env(inside, self.dir)), \
                mock.patch.object(sys, "platform", "linux"):
            found = steamdir.candidate_roots()
        self.assertIn(self.dir / "snap" / "steam" / "common" / ".local" / "share"
                      / "Steam", found)
        self.assertNotIn(inside / "snap" / "steam" / "common" / ".local"
                         / "share" / "Steam", found)


class InstallKind(Temp):
    def test_each_install_is_named_by_where_it_lives(self):
        snap = build_steam(self.dir.joinpath(*steamdir.SNAP_DATA))
        flat = build_steam(self.dir.joinpath(*steamdir.FLATPAK_DATA))
        native = build_steam(self.dir / ".local" / "share" / "Steam")
        self.assertEqual(steamdir.install_kind(snap), steamdir.SNAP)
        self.assertEqual(steamdir.install_kind(flat), steamdir.FLATPAK)
        self.assertEqual(steamdir.install_kind(native), steamdir.NATIVE)

    def test_a_folder_that_only_mentions_snap_is_native(self):
        other = build_steam(self.dir / "snap" / "Steam")
        self.assertEqual(steamdir.install_kind(other), steamdir.NATIVE)

    @unittest.skipIf(WINDOWS, "symlinks need privileges on Windows")
    def test_a_link_into_the_snap_counts_as_the_snap(self):
        snap = build_steam(self.dir.joinpath(*steamdir.SNAP_DATA))
        (self.dir / ".steam").mkdir()
        os.symlink(str(snap), str(self.dir / ".steam" / "steam"))
        self.assertEqual(steamdir.install_kind(self.dir / ".steam" / "steam"),
                         steamdir.SNAP)


class FindRoot(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.native = build_steam(self.dir / ".local" / "share" / "Steam")
        self.snap = build_steam(self.dir.joinpath(*steamdir.SNAP_DATA))
        self.both = [self.native, self.snap]

    def _signed_in(self, root, when):
        path = root / "config" / "loginusers.vdf"
        path.write_text(LOGINUSERS, encoding="utf-8")
        os.utime(str(path), (when, when))

    def test_only_the_snap_is_found(self):
        shutil.rmtree(str(self.native))
        self.assertEqual(steamdir.find_root(self.both, running=[]), self.snap)

    def test_the_running_steam_wins(self):
        self._signed_in(self.native, 2000000000)
        self._signed_in(self.snap, 1000000000)
        self.assertEqual(steamdir.find_root(self.both, running=[self.snap]),
                         self.snap)

    def test_else_the_one_signed_into_last(self):
        self._signed_in(self.native, 1000000000)
        self._signed_in(self.snap, 2000000000)
        self.assertEqual(steamdir.find_root(self.both, running=[]), self.snap)
        self._signed_in(self.native, 2100000000)
        self.assertEqual(steamdir.find_root(self.both, running=[]), self.native)

    def test_a_running_steam_that_is_not_a_candidate_changes_nothing(self):
        self._signed_in(self.native, 2000000000)
        self._signed_in(self.snap, 1000000000)
        self.assertEqual(
            steamdir.find_root(self.both, running=[self.dir / "elsewhere"]),
            self.native)

    @unittest.skipIf(WINDOWS, "symlinks need privileges on Windows")
    def test_a_link_and_its_target_are_one_install(self):
        shutil.rmtree(str(self.native))
        (self.dir / ".steam").mkdir()
        os.symlink(str(self.snap), str(self.dir / ".steam" / "steam"))
        found = steamdir.find_root([self.dir / ".steam" / "steam", self.snap],
                                   running=[self.snap])
        self.assertEqual(steamdir.install_kind(found), steamdir.SNAP)


class RunningRoots(Temp):
    def _process(self, pid, argv, environ=b""):
        folder = self.dir / "proc" / str(pid)
        folder.mkdir(parents=True)
        (folder / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
        (folder / "environ").write_bytes(environ)

    def test_reads_the_client_path_from_cmdline(self):
        client = "/home/u/snap/steam/common/.local/share/Steam/ubuntu12_32/steam"
        self._process(118475, [client.encode(), b"-srt-logger-opened"])
        self._process(118765, [b"./steamwebhelper", b"-nocrashdialog"])
        self._process(1, [b"/sbin/init"])
        (self.dir / "proc" / "self").mkdir()
        found = steamdir.running_roots(str(self.dir / "proc"), home=self.dir)
        self.assertEqual([str(path).replace("\\", "/") for path in found],
                         ["/home/u/snap/steam/common/.local/share/Steam"])

    def test_the_flatpak_is_mapped_back_to_its_real_folder(self):
        # Inside its sandbox ~/.var/app/<id> is mounted over the home, so the
        # path reads like a native install's.
        self._process(4242, [b"/home/u/.local/share/Steam/ubuntu12_32/steam"],
                      environ=b"HOME=/home/u\0FLATPAK_ID=com.valvesoftware.Steam\0")
        found = steamdir.running_roots(str(self.dir / "proc"), home=self.dir)
        self.assertEqual(found, [self.dir.joinpath(*steamdir.FLATPAK_DATA)])

    def test_no_proc_is_no_answer(self):
        self.assertEqual(steamdir.running_roots(str(self.dir / "none")), [])


# ----------------------------------------------------------------- the home


class RealHome(Temp):
    def test_outside_a_snap_nothing_changes(self):
        env = {"XDG_STATE_HOME": str(self.dir / "state")}
        self.assertFalse(paths.in_snap(env))
        self.assertEqual(paths.xdg_dir("XDG_STATE_HOME", ".local/state", env),
                         self.dir / "state")

    def test_inside_the_snap_the_real_home_is_used(self):
        inside = self.dir / "snap" / "steam" / "common"
        env = snap_env(inside, self.dir)
        self.assertTrue(paths.in_snap(env))
        self.assertEqual(paths.home(env), self.dir)
        # The snap's own XDG folders are not Blockslot's.
        self.assertEqual(paths.xdg_dir("XDG_CONFIG_HOME", ".config", env),
                         self.dir / ".config")

    @unittest.skipIf(WINDOWS, "savepick.json is under APPDATA on Windows")
    def test_settings_state_and_engine_are_found_from_inside_the_snap(self):
        inside = self.dir / "snap" / "steam" / "common"
        with mock.patch.dict(os.environ, snap_env(inside, self.dir)):
            os.environ.pop("XDG_STATE_HOME", None)
            self.assertEqual(paths.config_path(),
                             self.dir / ".config" / "savepick.json")
            self.assertEqual(paths.engine_path(),
                             self.dir / ".local" / "bin" / "savepick.py")
            if LINUX:
                self.assertEqual(paths.state_dir(),
                                 self.dir / ".local" / "state" / "blockslot")


# ----------------------------------------------------------------- the launch


class PythonForSteam(unittest.TestCase):
    def test_snap_steam_gets_the_python_every_snap_base_has(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(str(paths.python_for_steam(steamdir.SNAP)).replace("\\", "/"),
                             "/usr/bin/python3")

    def test_native_steam_gets_the_usual_answer(self):
        self.assertEqual(paths.python_for_steam(steamdir.NATIVE),
                         paths.python_for_launch())
        self.assertEqual(paths.python_for_steam(None), paths.python_for_launch())


class PythonOf(unittest.TestCase):
    def test_reads_the_interpreter_of_each_form(self):
        value = wrap.build("/opt/venv/bin/python3", "/home/u/.local/bin/savepick.py",
                           "-novid")
        self.assertEqual(wrap.python_of(value), "/opt/venv/bin/python3")
        self.assertEqual(wrap.python_of('"C:/B/Blockslot.exe" --pick -- %command%'),
                         None)
        self.assertIsNone(wrap.python_of("-novid %command%"))

    def test_a_shortcut_names_it_in_its_exe(self):
        exe, options = wrap.build_shortcut("/usr/bin/python3", "/e/savepick.py",
                                           "/games/x.sh")
        self.assertEqual(wrap.python_of(options, exe), "/usr/bin/python3")


class SnapLaunchOption(Temp):
    """The option the window writes for a game in snap Steam."""

    def setUp(self):
        Temp.setUp(self)
        self.root = build_steam(self.dir.joinpath(*steamdir.SNAP_DATA))
        self.library = model.Library(root=self.root, user_id=40000001,
                                     settings=settings.Settings(data={}))
        self.library.steam_running = lambda: False
        self.library.catalog = catalog.Catalog(
            entries={220: catalog.Entry("Half-Life 2", 220, [{"p": "x"}], [], 0)},
            steam_cloud=set())
        self.library.load()

    def test_names_usr_bin_python3_and_the_deployed_engine(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(paths, "is_frozen", lambda: False):
            changes, _ = self.library.plan([220], True)
        value = changes[220]
        self.assertTrue(value.startswith("/usr/bin/python3 "), value)
        self.assertEqual(wrap.engine_of(value), str(paths.engine_path()))
        self.assertIn("-novid", value)

    def test_a_wrap_with_another_python_is_redone(self):
        old = wrap.build("/opt/venv/bin/python3", paths.engine_path(), "-novid %command%")
        text = launchopts.load(steamdir.localconfig_path(self.root, 40000001))
        text = launchopts.write_all(text, {220: old})
        launchopts.save(steamdir.localconfig_path(self.root, 40000001), text)
        self.library.load()
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(paths, "is_frozen", lambda: False):
            changes, _ = self.library.plan([220], True)
            self.assertTrue(changes[220].startswith("/usr/bin/python3 "))
            self.library.apply(changes, [])
            self.library.load()
            again, _ = self.library.plan([220], True)
        self.assertEqual(again, {})


class SteamCommand(Temp):
    def test_snap_steam_is_driven_through_its_own_launcher(self):
        root = build_steam(self.dir.joinpath(*steamdir.SNAP_DATA))
        launcher = self.dir / "snap-bin-steam"
        launcher.write_text("", encoding="utf-8")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(launchopts, "SNAP_STEAM", str(launcher)), \
                mock.patch.object(launchopts.shutil, "which",
                                  lambda name: "/usr/games/" + name):
            self.assertEqual(launchopts.steam_command(root), [str(launcher)])

    def test_flatpak_steam_is_started_with_flatpak_run(self):
        root = build_steam(self.dir.joinpath(*steamdir.FLATPAK_DATA))
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(launchopts.shutil, "which",
                                  lambda name: "/usr/bin/" + name):
            self.assertEqual(launchopts.steam_command(root),
                             ["flatpak", "run", "com.valvesoftware.Steam"])

    def test_native_steam_is_steam_on_the_path(self):
        root = build_steam(self.dir / ".local" / "share" / "Steam")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(launchopts.shutil, "which",
                                  lambda name: "/usr/games/" + name):
            self.assertEqual(launchopts.steam_command(root), ["/usr/games/steam"])


# ----------------------------------------------------------------- the unit


class FakeSystemctl(object):
    """Records `systemctl --user ...` and answers 0, or `fail` for a verb."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv[2:])
        code = 1 if self.fail and self.fail in argv else 0
        return mock.Mock(returncode=code, stdout="", stderr="boom" if code else "")


class UnitText(unittest.TestCase):
    ARGV = ["/usr/bin/python3", "/home/u/My Games/blockslot/gui/blockslot.py",
            "--daemon", "--config", "/home/u/.config/savepick.json"]

    def test_runs_the_daemon_and_restarts_it_on_failure(self):
        text = userservice.unit_text(self.ARGV, log="/home/u/.local/state/blockslot/daemon.log")
        self.assertIn('ExecStart="/usr/bin/python3" '
                      '"/home/u/My Games/blockslot/gui/blockslot.py" "--daemon" '
                      '"--config" "/home/u/.config/savepick.json"\n', text)
        self.assertIn("Restart=on-failure\n", text)
        self.assertIn("RestartPreventExitStatus=2\n", text)
        self.assertIn("UMask=0077\n", text)
        self.assertIn("StandardOutput=append:/home/u/.local/state/blockslot/daemon.log\n",
                      text)
        self.assertIn("[Install]\nWantedBy=default.target\n", text)

    def test_percent_and_quotes_are_escaped_for_systemd(self):
        text = userservice.unit_text(['/x/100% "fun"/python3'], log="/l/50%.log")
        self.assertIn('ExecStart="/x/100%% \\"fun\\"/python3"\n', text)
        self.assertIn("StandardOutput=append:/l/50%%.log\n", text)

    def test_a_dollar_is_not_a_variable_to_systemd(self):
        text = userservice.unit_text(["/home/u/$HOME/python3"], log="/l.log")
        self.assertIn('ExecStart="/home/u/$$HOME/python3"\n', text)

    def test_the_command_is_the_daemon_with_its_settings(self):
        argv = userservice.exec_argv("/c/savepick.json", frozen=False,
                                     script="/b/gui/blockslot.py", python="/usr/bin/python3")
        self.assertEqual(argv, ["/usr/bin/python3", "/b/gui/blockslot.py", "--daemon",
                                "--config", "/c/savepick.json"])

    def test_the_engine_and_the_unit_agree_on_its_name(self):
        slotd = settings.engine_module("slotd")
        self.assertEqual(slotd.SYSTEMD_UNIT, userservice.UNIT)


@unittest.skipUnless(LINUX, "the systemd user unit is Linux only")
class InstallUnit(Temp):
    def setUp(self):
        Temp.setUp(self)
        env = {"XDG_CONFIG_HOME": str(self.dir / "config"),
               "XDG_STATE_HOME": str(self.dir / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("SNAP_NAME", "SNAP_REAL_HOME"):
            os.environ.pop(name, None)
        self.config = self.dir / "config" / "savepick.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"store": {"type": "local", "root": "/x"}}',
                               encoding="utf-8")
        os.chmod(str(self.config), 0o644)

    def test_install_writes_enables_and_starts(self):
        fake = FakeSystemctl()
        lines = userservice.install(self.config, runner=fake)
        unit = self.dir / "config" / "systemd" / "user" / "blockslot.service"
        self.assertTrue(unit.is_file())
        self.assertIn('"--config" "%s"' % self.config, unit.read_text(encoding="utf-8"))
        self.assertEqual(fake.calls, [["daemon-reload"], ["enable", "blockslot.service"],
                                      ["restart", "blockslot.service"]])
        self.assertTrue(userservice.is_current(self.config))
        self.assertTrue(any("Started" in line for line in lines))

    def test_install_makes_the_secrets_and_the_queue_private(self):
        userservice.install(self.config, runner=FakeSystemctl())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        store = self.dir / "state" / "blockslot" / "store"
        self.assertTrue(store.is_dir())
        self.assertEqual(store.stat().st_mode & 0o777, 0o700)

    def test_the_picker_finds_the_unit_where_it_was_written(self):
        userservice.install(self.config, runner=FakeSystemctl())
        slotd = settings.engine_module("slotd")
        self.assertEqual(Path(slotd.systemd_unit_path()), userservice.unit_path())

    def test_a_daemon_a_game_forked_is_stopped_so_the_unit_runs_it(self):
        stopped = []

        class Client(object):
            def stop(self):
                stopped.append(True)

        class Slotd(object):
            @staticmethod
            def load_settings(_path):
                return {"type": "local"}, "d"

            @staticmethod
            def default_state_dir():
                return "/state"

            @staticmethod
            def _client_from_info(_state_dir):
                return None if stopped else Client()

        self.assertTrue(userservice._stop_stray(
            self.config, runner=FakeSystemctl(fail="is-active"), slotd=Slotd))
        self.assertEqual(stopped, [True])
        # The unit's own daemon is left to `systemctl restart`.
        del stopped[:]
        self.assertFalse(userservice._stop_stray(
            self.config, runner=FakeSystemctl(), slotd=Slotd))
        self.assertEqual(stopped, [])

    def test_a_failed_enable_says_why(self):
        with self.assertRaises(RuntimeError) as caught:
            userservice.install(self.config, runner=FakeSystemctl(fail="enable"))
        self.assertIn("boom", str(caught.exception))

    def test_uninstall_stops_disables_and_removes_but_keeps_the_queue(self):
        userservice.install(self.config, runner=FakeSystemctl())
        queued = self.dir / "state" / "blockslot" / "store" / "queue"
        queued.mkdir()
        fake = FakeSystemctl()
        userservice.uninstall(runner=fake)
        self.assertFalse(userservice.unit_path().exists())
        self.assertEqual(fake.calls[0], ["disable", "--now", "blockslot.service"])
        self.assertTrue(queued.is_dir())

    def test_uninstall_with_nothing_there_is_not_an_error(self):
        lines = userservice.uninstall(runner=FakeSystemctl())
        self.assertIn("nothing to remove", lines[0])

    def test_blockslot_install_service_writes_the_unit_on_linux(self):
        import io
        from contextlib import redirect_stdout
        from gui import blockslot
        fake = FakeSystemctl()
        out = io.StringIO()
        with mock.patch.object(userservice.subprocess, "run", fake), \
                redirect_stdout(out):
            code = blockslot.main(["--install-service", "--config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertTrue(userservice.unit_path().is_file())
        self.assertIn("Started it", out.getvalue())
        with mock.patch.object(userservice.subprocess, "run", fake), \
                redirect_stdout(out):
            code = blockslot.main(["--uninstall-service"])
        self.assertEqual(code, 0)
        self.assertFalse(userservice.unit_path().exists())


@unittest.skipIf(WINDOWS, "file modes are POSIX")
class MakePrivate(Temp):
    def test_tightens_a_readable_file_and_leaves_a_private_one(self):
        path = self.dir / "savepick.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(str(path), 0o664)
        self.assertTrue(userservice.make_private(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(userservice.make_private(path))
        self.assertFalse(userservice.make_private(self.dir / "missing.json"))


if __name__ == "__main__":
    unittest.main()
