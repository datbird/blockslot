"""Linux and Mac desktops: ludusavi's release per machine, closing and
starting Steam, and the Steam snap's own home.

    python3 gui/tests/run.py
"""

import hashlib
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

from gui.core import engine, launchopts, paths  # noqa: E402


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-desk-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)


class LudusaviForThisMachine(unittest.TestCase):
    """The official v0.31.0 file each kind of machine downloads."""

    def test_each_machine_is_named_the_way_the_release_is(self):
        self.assertEqual(engine.this_machine("win32", "AMD64"), ("windows", "x64"))
        self.assertEqual(engine.this_machine("win32", "ARM64"), ("windows", "x64"))
        self.assertEqual(engine.this_machine("linux", "x86_64"), ("linux", "x64"))
        self.assertEqual(engine.this_machine("linux", "aarch64"), ("linux", "arm64"))
        self.assertEqual(engine.this_machine("darwin", "arm64"), ("mac", "arm64"))
        self.assertEqual(engine.this_machine("darwin", "x86_64", translated=False),
                         ("mac", "x64"))

    def test_an_intel_python_on_apple_silicon_gets_the_arm64_build(self):
        self.assertEqual(engine.this_machine("darwin", "x86_64", translated=True),
                         ("mac", "arm64"))

    def test_the_pinned_files_are_the_official_v0_31_0_assets(self):
        release = ("https://github.com/mtkennerly/ludusavi/releases/download/"
                   "v%s/ludusavi-v%s-" % ((engine.LUDUSAVI_VERSION,) * 2))
        for kind, name in ((("windows", "x64"), "win64.zip"),
                           (("linux", "x64"), "linux.tar.gz"),
                           (("mac", "arm64"), "mac.tar.gz")):
            url, sha = engine.ludusavi_asset(kind)
            self.assertEqual(url, release + name)
            self.assertEqual(len(sha), 64)
            int(sha, 16)
        # Three different files, so no two machines share a hash by mistake.
        self.assertEqual(len({sha for _url, sha in engine.LUDUSAVI_ASSETS.values()}), 3)

    def test_an_intel_mac_is_told_there_is_no_official_build(self):
        with self.assertRaises(engine.NoRelease) as caught:
            engine.ludusavi_asset(("mac", "x64"))
        self.assertIn("Apple silicon", str(caught.exception))

    def test_arm_linux_is_told_there_is_no_official_build(self):
        with self.assertRaises(engine.NoRelease) as caught:
            engine.ludusavi_asset(("linux", "arm64"))
        self.assertIn("arm64", str(caught.exception))


class DownloadingTheRightFile(Temp):
    PAYLOAD = b"\x7fELF fake ludusavi"

    def setUp(self):
        Temp.setUp(self)
        import tarfile
        payload = self.dir / "ludusavi"
        payload.write_bytes(self.PAYLOAD)
        self.archive = self.dir / "ludusavi-v0.31.0-linux.tar.gz"
        with tarfile.open(str(self.archive), "w:gz") as bundle:
            bundle.add(str(payload), arcname="ludusavi")
        payload.unlink()
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.target = self.dir / "bin" / "ludusavi"
        for name, value in (("ludusavi_path", lambda: self.target),):
            self.addCleanup(setattr, paths, name, getattr(paths, name))
            setattr(paths, name, value)

    def test_no_url_downloads_this_machines_asset(self):
        asked = []

        def asset(kind=None):
            return "https://example.invalid/linux.tar.gz", self.sha

        def opener(url):
            asked.append(url)
            return open(str(self.archive), "rb")

        with mock.patch.object(engine, "ludusavi_asset", asset):
            engine.download_ludusavi(opener=opener)
        self.assertEqual(asked, ["https://example.invalid/linux.tar.gz"])
        self.assertEqual(self.target.read_bytes(), self.PAYLOAD)

    def test_a_machine_with_no_release_downloads_nothing(self):
        asked = []

        def asset(kind=None):
            raise engine.NoRelease("no build")

        with mock.patch.object(engine, "ludusavi_asset", asset):
            with self.assertRaises(engine.NoRelease):
                engine.download_ludusavi(opener=asked.append)
        self.assertEqual(asked, [])
        self.assertFalse(self.target.exists())

    def test_a_url_without_its_hash_is_refused(self):
        with self.assertRaises(ValueError):
            engine.download_ludusavi("https://example.invalid/l.tar.gz",
                                     opener=lambda url: open(str(self.archive), "rb"))
        self.assertFalse(self.target.exists())


class LudusavisGameList(Temp):
    """A fresh ludusavi knows no game until its manifest is downloaded."""

    def setUp(self):
        Temp.setUp(self)
        self.binary = self.dir / "bin" / "ludusavi"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"")
        # Portable mode puts the config beside the binary on every OS, so
        # the test never reads a real config folder.
        (self.binary.parent / "ludusavi.portable").write_text("")
        self.addCleanup(setattr, paths, "ludusavi_path", paths.ludusavi_path)
        paths.ludusavi_path = lambda: self.binary
        self.addCleanup(setattr, paths, "steam_sandbox_home", paths.steam_sandbox_home)
        paths.steam_sandbox_home = lambda base=None: None

    def _run(self, writes, code=0):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            if writes:
                (self.binary.parent / "manifest.yaml").write_text("games: {}")
            return mock.Mock(returncode=code, stdout="", stderr="")
        return calls, run

    def test_the_manifest_is_forced_down(self):
        calls, run = self._run(writes=True)
        said = []
        self.assertTrue(engine.update_ludusavi_manifest(say=said.append, run=run))
        self.assertEqual(calls, [[str(self.binary), "manifest", "update", "--force"]])
        self.assertTrue(said)

    def test_no_manifest_afterwards_is_a_failure_that_says_so(self):
        calls, run = self._run(writes=False, code=1)
        said = []
        self.assertFalse(engine.update_ludusavi_manifest(say=said.append, run=run))
        self.assertIn("try again", said[-1])

    def test_ludusavi_that_cannot_run_is_a_failure_not_a_crash(self):
        def run(command, **_kwargs):
            raise OSError("exec format error")
        self.assertFalse(engine.update_ludusavi_manifest(run=run))

    def test_beside_a_snap_steam_it_writes_the_snaps_copy(self):
        (self.binary.parent / "ludusavi.portable").unlink()
        snap = self.dir / "snap" / "steam" / "common"
        paths.steam_sandbox_home = lambda base=None: snap
        with mock.patch.object(paths, "is_windows", lambda: False), \
                mock.patch.object(paths, "is_mac", lambda: False):
            folder = snap / ".config" / "ludusavi"
            self.assertEqual(engine.ludusavi_config_dir(self.binary), folder)
            calls, run = self._run(writes=False)
            engine.update_ludusavi_manifest(run=run)
        self.assertEqual(calls[0][1:3], ["--config", str(folder)])


class ClosingAndStartingSteam(unittest.TestCase):
    """How each desktop asks Steam to go and brings it back."""

    def test_a_mac_looks_for_steam_osx(self):
        self.assertEqual(launchopts.steam_process_name("darwin"), "steam_osx")
        self.assertEqual(launchopts.steam_process_name("linux"), "steam")

    @unittest.skipUnless(hasattr(os, "getuid"), "pgrep is POSIX")
    def test_only_this_users_steam_counts(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            return mock.Mock(stdout="885\n")
        self.assertEqual(launchopts._steam_pids(run=run, system="darwin"), ["885"])
        self.assertEqual(calls, [["pgrep", "-x", "-u", str(os.getuid()), "steam_osx"]])

    def test_a_mac_quits_and_opens_steam_through_launch_services(self):
        self.assertEqual(launchopts.shutdown_command(system="darwin"),
                         ["/usr/bin/open", "steam://exit"])
        with mock.patch.object(launchopts, "mac_steam_app",
                               lambda: "/Applications/Steam.app"):
            self.assertEqual(launchopts.steam_command(system="darwin"),
                             ["/usr/bin/open", "-a", "/Applications/Steam.app"])
        with mock.patch.object(launchopts, "mac_steam_app", lambda: None):
            self.assertIsNone(launchopts.steam_command(system="darwin"))

    def test_the_snaps_steam_is_run_through_the_snap(self):
        root = "/home/me/snap/steam/common/.local/share/Steam"
        found = dict(which=lambda name: "/usr/games/steam",
                     exists=lambda path: path == launchopts.SNAP_STEAM)
        self.assertEqual(launchopts.steam_command(root, system="linux", **found),
                         ["/snap/bin/steam"])
        self.assertEqual(launchopts.shutdown_command(root, system="linux", **found),
                         ["/snap/bin/steam", "-shutdown"])

    def test_the_flatpaks_steam_is_run_through_flatpak(self):
        root = "/home/me/.var/app/com.valvesoftware.Steam/.local/share/Steam"
        command = launchopts.steam_command(
            root, system="linux", which=lambda name: "/usr/bin/" + name,
            exists=lambda path: False)
        self.assertEqual(command, ["flatpak", "run", "com.valvesoftware.Steam"])

    def test_a_native_steam_is_the_one_on_path(self):
        command = launchopts.steam_command(
            "/home/me/.local/share/Steam", system="linux",
            which=lambda name: "/usr/games/steam", exists=lambda path: True)
        self.assertEqual(command, ["/usr/games/steam"])

    def test_no_path_steam_falls_back_to_the_snap(self):
        command = launchopts.steam_command(
            None, system="linux", which=lambda name: None,
            exists=lambda path: path == launchopts.SNAP_STEAM)
        self.assertEqual(command, ["/snap/bin/steam"])

    def test_nothing_found_is_none(self):
        self.assertIsNone(launchopts.steam_command(
            None, system="linux", which=lambda name: None,
            exists=lambda path: False))
        self.assertIsNone(launchopts.shutdown_command(
            None, system="linux", which=lambda name: None,
            exists=lambda path: False))


@unittest.skipUnless(sys.platform.startswith("linux"), "the Steam snap is Linux")
class TheSteamSnapsHome(Temp):
    """Beside a snap Steam, Blockslot's files stay home; ludusavi's are the snap's.

    The snap's steam-support interface lets a game read the real home's
    hidden folders (probed on an Ubuntu laptop, docs/linux-desktop.md), and the engine
    finds them through SNAP_REAL_HOME. ludusavi runs with the snap's HOME,
    so its config and manifest are inside the snap.
    """

    def setUp(self):
        Temp.setUp(self)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.dir)})
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("SNAP", "SNAP_NAME", "SNAP_REAL_HOME", "XDG_STATE_HOME"):
            os.environ.pop(name, None)
        self.snap = self.dir / "snap" / "steam" / "common"

    def _steam(self, base):
        (base / ".local" / "share" / "Steam" / "userdata").mkdir(parents=True)

    def test_no_snap_steam_changes_nothing(self):
        self.assertIsNone(paths.steam_sandbox_home())
        self.assertEqual(paths.config_path(), self.dir / ".config" / "savepick.json")
        self.assertEqual(paths.engine_path(), self.dir / ".local" / "bin" / "savepick.py")

    def test_a_snap_steam_keeps_the_engine_home_and_ludusavis_config_in_the_snap(self):
        self._steam(self.snap)
        self.assertEqual(paths.steam_sandbox_home(), self.snap)
        self.assertEqual(paths.engine_path(), self.dir / ".local" / "bin" / "savepick.py")
        self.assertEqual(paths.ludusavi_path(), self.dir / ".local" / "bin" / "ludusavi")
        self.assertEqual(paths.config_path(), self.dir / ".config" / "savepick.json")
        self.assertEqual(paths.log_path(),
                         self.dir / ".local" / "state" / "blockslot" / "savepick.log")
        self.assertEqual(engine.ludusavi_config_dir(paths.ludusavi_path()),
                         self.snap / ".config" / "ludusavi")

    def test_a_native_steam_in_use_wins(self):
        self._steam(self.snap)
        self._steam(self.dir)
        self.assertIsNone(paths.steam_sandbox_home())

    def test_inside_the_snap_home_already_is_the_snaps(self):
        self._steam(self.snap)
        os.environ["SNAP"] = "/snap/steam/271"
        self.assertIsNone(paths.steam_sandbox_home())

    def test_the_engine_and_the_daemon_agree_with_the_window(self):
        self._steam(self.snap)
        engine_dir = str(ROOT / "engine")
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)
        import savepick
        import slotd
        self.assertEqual(Path(slotd.default_config_path()), paths.config_path())
        self.assertEqual(savepick.config_path(), paths.config_path())
        self.assertEqual(Path(savepick.ludusavi_binary()), paths.ludusavi_path())
        self.assertEqual(savepick._log_path(), paths.log_path())
        self.assertEqual(Path(slotd.default_state_dir()),
                         self.dir / ".local" / "state" / "blockslot" / "store")


if __name__ == "__main__":
    unittest.main()
