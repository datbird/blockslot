"""Tests for savepick's restore decision logic."""
import contextlib
import json
import os
import shutil
import time
import unittest
from pathlib import Path
import struct
import tempfile

import savepick


def setUpModule():
    """Never write test lines into the real savepick.log.

    On 2026-09-23 a test run on DESKTOP put thirty test lines into the log a
    person reads when a launch goes wrong.
    """
    global _REAL_LOG, _TEST_LOG_DIR
    _REAL_LOG = savepick.LOG_PATH
    _TEST_LOG_DIR = tempfile.mkdtemp(prefix="savepick-test-log-")
    savepick.LOG_PATH = Path(_TEST_LOG_DIR) / "savepick.log"


def tearDownModule():
    savepick.LOG_PATH = _REAL_LOG
    shutil.rmtree(_TEST_LOG_DIR, ignore_errors=True)



class _NullSpinner:
    """Stands in for the real Spinner in tests. Draws nothing, spawns nothing.

    It keeps every update so a test can assert on what the window said and on
    where the bar was, which is the only way to check either without a screen.
    """

    def __init__(self):
        self.updates = []

    def update(self, text, percent=None):
        self.updates.append((text, percent))

    def close(self):
        pass


@contextlib.contextmanager
def _config(data):
    """Point savepick.config_path at a temporary savepick.json holding `data`."""
    original = savepick.config_path
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "savepick.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        savepick.config_path = lambda: path
        try:
            yield path
        finally:
            savepick.config_path = original


class TestDecide(unittest.TestCase):
    """decide() is a pure function of two mtimes. It must never pick a rollback silently."""

    def test_no_backup_means_skip(self):
        self.assertEqual(savepick.decide(live=100.0, backup=None), savepick.SKIP)

    def test_no_live_save_means_restore(self):
        # Fresh install on this machine. Pull the backup, nothing to lose.
        self.assertEqual(savepick.decide(live=None, backup=100.0), savepick.RESTORE)

    def test_neither_side_has_anything(self):
        self.assertEqual(savepick.decide(live=None, backup=None), savepick.SKIP)

    def test_backup_newer_restores_without_asking(self):
        # The everyday case: you played on the other machine.
        self.assertEqual(savepick.decide(live=100.0, backup=500.0), savepick.RESTORE)

    def test_live_newer_asks(self):
        # Today's bug. Must never restore silently.
        self.assertEqual(savepick.decide(live=500.0, backup=100.0), savepick.ASK)

    def test_equal_mtimes_skip_silently(self):
        # Both sides in sync after a clean round trip. Do not nag.
        self.assertEqual(savepick.decide(live=500.0, backup=500.0), savepick.SKIP)

    def test_within_tolerance_counts_as_equal(self):
        self.assertEqual(savepick.decide(live=500.0, backup=501.0), savepick.SKIP)
        self.assertEqual(savepick.decide(live=501.0, backup=500.0), savepick.SKIP)

    def test_just_outside_tolerance_is_a_real_difference(self):
        self.assertEqual(savepick.decide(live=500.0, backup=510.0), savepick.RESTORE)
        self.assertEqual(savepick.decide(live=510.0, backup=500.0), savepick.ASK)


class TestNewestMtime(unittest.TestCase):
    """newest_mtime_of() picks the latest mtime, filtered by basename when asked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, rel, mtime):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        os.utime(p, (mtime, mtime))
        return p

    def test_returns_none_for_missing_dir(self):
        self.assertIsNone(savepick.newest_mtime_of(self.root / "nope", None))

    def test_picks_the_latest(self):
        self._touch("drive-C/a.sl2", 100)
        self._touch("drive-C/b.sl2", 900)
        self.assertEqual(savepick.newest_mtime_of(self.root, None), 900)

    def test_ignores_ludusavi_metadata(self):
        # mapping.yaml is written at backup time, so it would always look newest.
        self._touch("drive-C/a.sl2", 100)
        self._touch("mapping.yaml", 9999)
        self.assertEqual(savepick.newest_mtime_of(self.root, None), 100)

    def test_basename_filter_wins(self):
        self._touch("drive-C/DS2SOFS0000.sl2", 100)
        self._touch("drive-C/GraphicsConfig.xml", 900)
        got = savepick.newest_mtime_of(self.root, {"DS2SOFS0000.sl2"})
        self.assertEqual(got, 100)

    def test_falls_back_when_filter_matches_nothing(self):
        # Never return None just because our name guess was wrong.
        self._touch("drive-C/something-else.sl2", 400)
        got = savepick.newest_mtime_of(self.root, {"DS2SOFS0000.sl2"})
        self.assertEqual(got, 400)


class TestPickNewestBackup(unittest.TestCase):
    def test_sorts_by_when_not_by_list_order(self):
        backups = [
            {"name": "b-mid", "when": "2026-09-02T03:41:04Z"},
            {"name": "b-new", "when": "2026-09-02T15:24:46Z"},
            {"name": "b-old", "when": "2026-08-31T19:31:53Z"},
        ]
        self.assertEqual(savepick.pick_newest_backup(backups)["name"], "b-new")

    def test_empty_list_is_none(self):
        self.assertIsNone(savepick.pick_newest_backup([]))


class TestFailSafe(unittest.TestCase):
    """Any failure must resolve to 'do not restore'."""

    def test_ask_result_of_none_means_skip(self):
        # Dialog unavailable, timed out with no answer, or crashed.
        self.assertEqual(savepick.resolve_ask(None), savepick.SKIP)

    def test_ask_declined_means_skip(self):
        self.assertEqual(savepick.resolve_ask(False), savepick.SKIP)

    def test_ask_accepted_means_restore(self):
        self.assertEqual(savepick.resolve_ask(True), savepick.RESTORE)


class TestKdialogReading(unittest.TestCase):
    """Regression: on 2026-09-03 a kdialog error exit was read as "restore" and
    savepick restored an older save with nobody asked."""

    def test_yes_button_keeps(self):
        self.assertIs(savepick.read_kdialog(0, ""), False)

    def test_no_button_restores(self):
        self.assertIs(savepick.read_kdialog(1, ""), True)

    def test_error_exit_is_not_an_answer(self):
        self.assertIsNone(savepick.read_kdialog(255, ""))
        self.assertIsNone(savepick.read_kdialog(2, ""))

    def test_stderr_means_it_failed_even_on_exit_1(self):
        # kdialog exits 1 both for "no" and for an unknown flag. Anything on
        # stderr means we did not get a real answer.
        self.assertIsNone(savepick.read_kdialog(1, "Unknown option --yes-label"))

    def test_error_resolves_to_skip_not_restore(self):
        self.assertEqual(savepick.resolve_ask(savepick.read_kdialog(255, "")), savepick.SKIP)
        self.assertEqual(
            savepick.resolve_ask(savepick.read_kdialog(1, "boom")), savepick.SKIP)


class TestHostEnv(unittest.TestCase):
    """Steam's runtime vars break host GTK binaries like zenity (exit 255)."""

    def test_strips_steam_runtime_vars(self):
        import os
        saved = dict(os.environ)
        try:
            os.environ["LD_PRELOAD"] = "/steam/lib/thing.so"
            os.environ["LD_LIBRARY_PATH"] = "/steam/lib"
            os.environ["SAVEPICK_KEEPME"] = "yes"
            env = savepick.host_env()
            self.assertNotIn("LD_PRELOAD", env)
            self.assertNotIn("LD_LIBRARY_PATH", env)
            self.assertEqual(env.get("SAVEPICK_KEEPME"), "yes")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_supplies_a_display_when_none_is_set(self):
        import os
        saved = dict(os.environ)
        try:
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            self.assertEqual(savepick.host_env().get("DISPLAY"), ":0")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_keeps_an_existing_display(self):
        import os
        saved = dict(os.environ)
        try:
            os.environ["DISPLAY"] = ":3"
            self.assertEqual(savepick.host_env().get("DISPLAY"), ":3")
        finally:
            os.environ.clear()
            os.environ.update(saved)


class TestHostTool(unittest.TestCase):
    """Regression: Steam's runtime PATH gave us a GTK2 zenity that could not start."""

    def test_prefers_the_absolute_system_path(self):
        got = savepick.host_tool("sh")
        self.assertTrue(got.startswith("/"), got)
        self.assertTrue(os.access(got, os.X_OK))

    def test_falls_back_to_the_bare_name_when_absent(self):
        self.assertEqual(savepick.host_tool("definitely-not-a-real-tool"),
                         "definitely-not-a-real-tool")


class TestZenityCandidates(unittest.TestCase):
    """Steam's zenity is the one proven to render under gamescope. Try it first,
    and give it Steam's own environment, or it dies on libgtk-x11-2.0.so.0."""

    def test_startup_failures_are_retryable_not_answers(self):
        for code in savepick.STARTUP_FAILURES:
            self.assertIn(code, (126, 127, 255))

    def test_first_candidate_keeps_the_untouched_environment(self):
        import os
        saved = dict(os.environ)
        try:
            os.environ["LD_LIBRARY_PATH"] = "/steam/lib"
            got = savepick.zenity_candidates()
            if not got:
                self.skipTest("no zenity on this machine")
            _binary, env = got[0]
            # The first candidate must NOT have Steam's paths stripped: that is
            # the only combination ever seen to render on the Deck.
            self.assertEqual(env.get("LD_LIBRARY_PATH"), "/steam/lib")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_uses_the_host_binary_not_steams(self):
        for binary, _env in savepick.zenity_candidates():
            self.assertNotIn("ubuntu12_32", binary)
            self.assertNotIn("steam-runtime", binary)


def js_bytes(etype, number, value):
    return struct.pack("IhBB", 0, value, etype, number)


class TestGamepad(unittest.TestCase):
    """In Game Mode the controller drives Steam, not the dialog, so savepick
    reads the pad itself. A keeps this machine, B restores the backup."""

    def test_a_press_keeps(self):
        ev = savepick.parse_js_event(js_bytes(savepick.JS_EVENT_BUTTON, savepick.BUTTON_A, 1))
        self.assertIs(savepick.gamepad_choice(ev), False)

    def test_b_press_restores(self):
        ev = savepick.parse_js_event(js_bytes(savepick.JS_EVENT_BUTTON, savepick.BUTTON_B, 1))
        self.assertIs(savepick.gamepad_choice(ev), True)

    def test_button_release_is_ignored(self):
        ev = savepick.parse_js_event(js_bytes(savepick.JS_EVENT_BUTTON, savepick.BUTTON_A, 0))
        self.assertIsNone(savepick.gamepad_choice(ev))

    def test_startup_state_is_not_a_press(self):
        # The kernel replays every button's state when the device is opened.
        # Reading those as presses would make the dialog answer itself.
        etype = savepick.JS_EVENT_BUTTON | savepick.JS_EVENT_INIT
        for button in (savepick.BUTTON_A, savepick.BUTTON_B):
            ev = savepick.parse_js_event(js_bytes(etype, button, 1))
            self.assertIsNone(savepick.gamepad_choice(ev))

    def test_axis_movement_is_ignored(self):
        ev = savepick.parse_js_event(js_bytes(0x02, 0, 32767))
        self.assertIsNone(savepick.gamepad_choice(ev))

    def test_other_buttons_are_ignored(self):
        ev = savepick.parse_js_event(js_bytes(savepick.JS_EVENT_BUTTON, 7, 1))
        self.assertIsNone(savepick.gamepad_choice(ev))

    def test_short_or_empty_read_is_not_an_answer(self):
        self.assertIsNone(savepick.parse_js_event(b""))
        self.assertIsNone(savepick.parse_js_event(b"abc"))
        self.assertIsNone(savepick.gamepad_choice(None))

    def test_no_answer_resolves_to_skip(self):
        self.assertEqual(savepick.resolve_ask(savepick.gamepad_choice(None)), savepick.SKIP)


class TestAlwaysBacksUp(unittest.TestCase):
    """There is no "skip the backup" path any more. Declining was the one action
    that could leave the other machine stale, which is the bug this tool exists
    to prevent, so the exit backup is unconditional."""

    def test_no_backup_prompt_exists(self):
        for gone in ("ask_backup", "ask_linux_backup", "ask_windows_backup",
                     "backup_prompt_text", "BACKUP_YES", "BACKUP_NO"):
            self.assertFalse(hasattr(savepick, gone),
                             "%s should have been removed" % gone)

    def test_a_warning_dialog_exists_for_the_unsynced_case(self):
        self.assertTrue(callable(savepick.show_warning))
        self.assertGreaterEqual(savepick.WARNING_TIMEOUT_SECONDS, 30)


class TestConfig(unittest.TestCase):
    def test_missing_config_is_empty_not_an_error(self):
        import tempfile as tf
        from pathlib import Path as P
        with tf.TemporaryDirectory() as d:
            original = savepick.config_path
            savepick.config_path = lambda: P(d) / "nope.json"
            try:
                self.assertEqual(savepick.load_config(), {})
            finally:
                savepick.config_path = original

    def test_reads_syncthing_settings(self):
        import json as j, tempfile as tf
        from pathlib import Path as P
        with tf.TemporaryDirectory() as d:
            p = P(d) / "savepick.json"
            p.write_text(j.dumps({"syncthing": {"url": "http://x", "apikey": "k",
                                                "folder": "f", "peer_id": "P"}}))
            original = savepick.config_path
            savepick.config_path = lambda: p
            try:
                self.assertEqual(savepick.load_config()["syncthing"]["folder"], "f")
            finally:
                savepick.config_path = original


class TestSyncSettings(unittest.TestCase):
    NEW = {"syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                         "folder": "gamesaves",
                         "hub_id": "HUB", "hub_name": "nas",
                         "device_dir": "deck",
                         "device_names": {"deck": "Steam Deck",
                                          "desktop": "Windows PC"}}}
    OLD = {"syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                         "folder": "ludusavi-deck",
                         "incoming_folder": "ludusavi-windows",
                         "peer_id": "PEER", "peer_name": "Windows PC"}}

    def setUp(self):
        self.real = savepick.load_config

    def tearDown(self):
        savepick.load_config = self.real

    def test_new_shape_is_accepted(self):
        savepick.load_config = lambda: self.NEW
        cfg = savepick.sync_settings()
        self.assertEqual(cfg["device_dir"], "deck")

    def test_old_shape_reads_as_not_configured(self):
        savepick.load_config = lambda: self.OLD
        self.assertIsNone(savepick.sync_settings())

    def test_no_config_reads_as_not_configured(self):
        savepick.load_config = lambda: {}
        self.assertIsNone(savepick.sync_settings())

    def test_a_missing_key_reads_as_not_configured(self):
        for key in savepick.SYNC_KEYS:
            broken = {"syncthing": dict(self.NEW["syncthing"])}
            del broken["syncthing"][key]
            savepick.load_config = lambda b=broken: b
            self.assertIsNone(savepick.sync_settings(),
                              "missing %s should not be configured" % key)


class TestDeviceLabel(unittest.TestCase):
    CFG = {"device_names": {"deck": "Steam Deck", "desktop": "Windows PC"},
           "hub_name": "nas"}

    def test_known_directory_gets_its_name(self):
        self.assertEqual(savepick.device_label(self.CFG, "desktop"), "Windows PC")

    def test_unknown_directory_falls_back_to_the_directory_name(self):
        self.assertEqual(savepick.device_label(self.CFG, "ally"), "ally")

    def test_no_name_map_falls_back_to_the_directory_name(self):
        self.assertEqual(savepick.device_label({}, "ally"), "ally")

    def test_a_linux_peer_is_never_guessed_to_be_a_steam_deck(self):
        # Regression: the label used to come from the backup's os field, so any
        # Linux peer was called a Steam Deck and anything else a Windows PC.
        self.assertEqual(savepick.device_label({}, "some-linux-box"),
                         "some-linux-box")

    def test_hub_label_falls_back_to_the_server(self):
        self.assertEqual(savepick.hub_label(self.CFG), "nas")
        self.assertEqual(savepick.hub_label({}), "the server")


class TestSpinnerIsSafe(unittest.TestCase):
    """The spinner must never break a launch, even with no display at all."""

    def test_update_and_close_are_safe_when_it_never_opened(self):
        spinner = savepick.Spinner.__new__(savepick.Spinner)
        spinner._proc = None
        spinner._status = None
        spinner._script = None
        spinner.update("anything")
        spinner.close()


class TestGamepadIdentification(unittest.TestCase):
    """Regression: js0 on the Steam Deck is "Mouse passthrough (absolute)" and
    the real pad is js1, "Microsoft X-Box 360 pad 0". Taking the first js*
    device meant savepick watched a mouse and never saw a button press."""

    def test_recognises_the_deck_pad(self):
        self.assertTrue(savepick.looks_like_a_gamepad("Microsoft X-Box 360 pad 0"))
        self.assertTrue(savepick.looks_like_a_gamepad("Valve Software Steam Controller"))

    def test_rejects_the_passthrough_devices(self):
        self.assertFalse(savepick.looks_like_a_gamepad("Mouse passthrough (absolute)"))
        self.assertFalse(savepick.looks_like_a_gamepad("Mouse passthrough"))
        self.assertFalse(savepick.looks_like_a_gamepad("Keyboard passthrough"))

    def test_rejects_the_touchscreen(self):
        self.assertFalse(savepick.looks_like_a_gamepad("FTS3528:00 2808:1015 touchscreen"))

    def test_empty_name_is_not_a_gamepad(self):
        self.assertFalse(savepick.looks_like_a_gamepad(""))
        self.assertFalse(savepick.looks_like_a_gamepad(None))

    def test_ioctl_number_is_the_documented_one(self):
        # JSIOCGNAME(128) == 0x80806a13
        self.assertEqual(savepick._jsiocgname(128), 0x80806A13)


class TestXInput(unittest.TestCase):
    """Windows has no /dev/input, so the PC dialog reads XInput instead.
    Steam Input re-presents any pad as an Xbox one, so A and B stay put."""

    def test_a_keeps(self):
        self.assertIs(savepick.xinput_choice(savepick.XINPUT_GAMEPAD_A), False)

    def test_b_restores(self):
        self.assertIs(savepick.xinput_choice(savepick.XINPUT_GAMEPAD_B), True)

    def test_nothing_held_is_not_an_answer(self):
        self.assertIsNone(savepick.xinput_choice(0))
        self.assertIsNone(savepick.xinput_choice(None))

    def test_other_buttons_are_ignored(self):
        self.assertIsNone(savepick.xinput_choice(0x0010))   # Start
        self.assertIsNone(savepick.xinput_choice(0x0001))   # D-pad up

    def test_a_wins_when_both_are_held(self):
        # A is the safe choice, so an ambiguous grip keeps the newest save.
        both = savepick.XINPUT_GAMEPAD_A | savepick.XINPUT_GAMEPAD_B
        self.assertIs(savepick.xinput_choice(both), False)


class TestCrossPlatformImports(unittest.TestCase):
    """Regression: a top-level `import fcntl` broke savepick on Windows
    completely, so Steam would have failed to launch the game at all."""

    LINUX_ONLY = {"fcntl", "termios", "pwd", "grp", "posix", "resource"}

    def test_no_platform_specific_module_at_import_time(self):
        import ast as a
        tree = a.parse(open(savepick.__file__).read())
        top = []
        for node in tree.body:
            if isinstance(node, a.Import):
                top += [n.name.split(".")[0] for n in node.names]
            elif isinstance(node, a.ImportFrom) and node.module:
                top.append(node.module.split(".")[0])
        clash = self.LINUX_ONLY & set(top)
        self.assertEqual(clash, set(), "Linux-only import at module scope: %s" % clash)


class TestVaultPath(unittest.TestCase):
    """The vault must never live inside a Syncthing folder."""

    def test_vault_root_is_outside_gamesaves(self):
        root = savepick.vault_root()
        self.assertNotIn("GameSaves", str(root))

    def test_game_directory_is_slugged(self):
        # ludusavi names games with characters no filesystem wants.
        got = savepick.vault_dir_for("Dark Souls II: Scholar of the First Sin")
        self.assertNotIn(":", got.name)
        self.assertNotIn("/", got.name)
        self.assertNotIn("\\", got.name)
        self.assertTrue(got.name)

    def test_different_games_get_different_directories(self):
        a = savepick.vault_dir_for("Dark Souls II")
        b = savepick.vault_dir_for("Dark Souls III")
        self.assertNotEqual(a, b)

    def test_a_game_that_slugs_to_nothing_still_gets_a_name(self):
        got = savepick.vault_dir_for("///:::")
        self.assertTrue(got.name)


class TestSnapshotLiveSaves(unittest.TestCase):
    """The copy taken right before a restore overwrites the live save."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.live = self.root / "live"
        self.live.mkdir()
        self.vault = self.root / "vault"

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, name, text="progress", mtime=None):
        path = self.live / name
        path.write_text(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return str(path)

    def test_copies_the_file_and_reports_success(self):
        path = self._save("DS2SOFS0000.sl2")
        ok = savepick.snapshot_live_saves("Dark Souls II", [path], vault=self.vault)
        self.assertTrue(ok)
        copies = list(self.vault.rglob("DS2SOFS0000.sl2"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "progress")

    def test_preserves_mtime(self):
        # mtime is how this whole system tells saves apart. copy2, not copy.
        stamp = 1600000000.0
        path = self._save("save.sl2", mtime=stamp)
        savepick.snapshot_live_saves("g", [path], vault=self.vault)
        copy = next(self.vault.rglob("save.sl2"))
        self.assertAlmostEqual(copy.stat().st_mtime, stamp, places=0)

    def test_writes_a_manifest_mapping_back_to_the_original_path(self):
        path = self._save("save.sl2")
        savepick.snapshot_live_saves("g", [path], vault=self.vault)
        manifest = next(self.vault.rglob("manifest.json"))
        data = json.loads(manifest.read_text())
        self.assertEqual(data["game"], "g")
        originals = [entry["original"] for entry in data["files"]]
        self.assertIn(str(Path(path)), originals)

    def test_two_saves_with_the_same_basename_do_not_collide(self):
        one = self.live / "a"
        two = self.live / "b"
        one.mkdir()
        two.mkdir()
        (one / "save.sl2").write_text("first")
        (two / "save.sl2").write_text("second")
        ok = savepick.snapshot_live_saves(
            "g", [str(one / "save.sl2"), str(two / "save.sl2")], vault=self.vault)
        self.assertTrue(ok)
        manifest = next(self.vault.rglob("manifest.json"))
        data = json.loads(manifest.read_text())
        self.assertEqual(len(data["files"]), 2)
        stored = {entry["stored"] for entry in data["files"]}
        self.assertEqual(len(stored), 2)
        bodies = sorted(p.read_text() for p in self.vault.rglob("*.sl2"))
        self.assertEqual(bodies, ["first", "second"])

    def test_no_live_paths_is_a_failure_not_an_empty_snapshot(self):
        # Nothing to protect means we cannot promise protection. Do not restore.
        self.assertFalse(savepick.snapshot_live_saves("g", [], vault=self.vault))

    def test_a_missing_live_file_fails(self):
        missing = str(self.live / "gone.sl2")
        self.assertFalse(savepick.snapshot_live_saves("g", [missing], vault=self.vault))

    def test_an_unwritable_vault_fails_rather_than_raising(self):
        blocker = self.root / "blocked"
        blocker.write_text("i am a file, not a directory")
        path = self._save("save.sl2")
        self.assertFalse(savepick.snapshot_live_saves("g", [path], vault=blocker))

    def test_a_partial_snapshot_leaves_nothing_behind(self):
        # Half a snapshot is worse than none: it looks like a recovery point.
        good = self._save("save.sl2")
        missing = str(self.live / "gone.sl2")
        self.assertFalse(
            savepick.snapshot_live_saves("g", [good, missing], vault=self.vault))
        self.assertEqual(list(self.vault.rglob("*.sl2")), [])

    def test_two_snapshots_in_the_same_second_do_not_overwrite_each_other(self):
        path = self._save("save.sl2")
        self.assertTrue(savepick.snapshot_live_saves("g", [path], vault=self.vault))
        self.assertTrue(savepick.snapshot_live_saves("g", [path], vault=self.vault))
        self.assertEqual(len(savepick.vault_snapshots("g", vault=self.vault)), 2)


class TestVaultPruning(unittest.TestCase):
    """Keep the newest N snapshots, drop the rest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.game = savepick.vault_dir_for("g", vault=self.vault)
        self.game.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self, name):
        directory = self.game / name
        directory.mkdir()
        (directory / "manifest.json").write_text("{}")
        return directory

    def test_keeps_the_newest_and_deletes_the_oldest(self):
        names = ["20260901T000000Z-0", "20260902T000000Z-0", "20260903T000000Z-0"]
        for name in names:
            self._snapshot(name)
        savepick.prune_vault("g", keep=2, vault=self.vault)
        left = sorted(p.name for p in self.game.iterdir())
        self.assertEqual(left, names[1:])

    def test_under_the_limit_deletes_nothing(self):
        self._snapshot("20260901T000000Z-0")
        savepick.prune_vault("g", keep=10, vault=self.vault)
        self.assertEqual(len(list(self.game.iterdir())), 1)

    def test_pruning_a_missing_game_directory_is_quiet(self):
        savepick.prune_vault("never-played", keep=10, vault=self.vault)

    def test_stray_files_are_left_alone(self):
        self._snapshot("20260901T000000Z-0")
        (self.game / "notes.txt").write_text("hi")
        savepick.prune_vault("g", keep=0, vault=self.vault)
        self.assertTrue((self.game / "notes.txt").exists())

    def test_default_keep_is_ten(self):
        self.assertEqual(savepick.VAULT_KEEP, 10)


class TestSnapshotGatesTheRestore(unittest.TestCase):
    """A failed snapshot must turn a restore into a skip."""

    def test_restore_survives_a_good_snapshot(self):
        self.assertEqual(
            savepick.gate_restore_on_snapshot(savepick.RESTORE, ok=True),
            savepick.RESTORE)

    def test_restore_becomes_skip_when_the_snapshot_fails(self):
        self.assertEqual(
            savepick.gate_restore_on_snapshot(savepick.RESTORE, ok=False),
            savepick.SKIP)

    def test_skip_is_untouched(self):
        self.assertEqual(
            savepick.gate_restore_on_snapshot(savepick.SKIP, ok=False),
            savepick.SKIP)


class TestNeedsSnapshot(unittest.TestCase):
    """A fresh install has nothing to lose and must not be blocked."""

    def test_restore_over_an_existing_save_needs_one(self):
        self.assertTrue(savepick.needs_snapshot(savepick.RESTORE, live_mtime=500.0))

    def test_restore_with_no_live_save_does_not(self):
        self.assertFalse(savepick.needs_snapshot(savepick.RESTORE, live_mtime=None))

    def test_a_skip_never_needs_one(self):
        self.assertFalse(savepick.needs_snapshot(savepick.SKIP, live_mtime=500.0))


class TestSnapshotLeavesTheLiveSaveAlone(unittest.TestCase):
    """Taking a snapshot must not touch the original. mtime is load-bearing."""

    def test_source_content_and_mtime_survive(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            live = root / "save.sl2"
            live.write_text("progress")
            stamp = 1600000000.0
            os.utime(live, (stamp, stamp))
            before = live.stat()
            self.assertTrue(
                savepick.snapshot_live_saves("g", [str(live)], vault=root / "v"))
            after = live.stat()
            self.assertEqual(live.read_text(), "progress")
            self.assertAlmostEqual(after.st_mtime, before.st_mtime, places=0)


class TestConflictTextAlignment(unittest.TestCase):
    """The two dates must line up. Comparing them at a glance is the whole job."""

    def _rows(self, live, backup_label, backup):
        text = savepick.conflict_text("Dark Souls II", live, backup_label, backup)
        lines = text.splitlines()
        this = next(l for l in lines if l.startswith("This device"))
        other = next(l for l in lines if l.startswith(backup_label))
        return this, other

    def test_dates_start_in_the_same_column(self):
        this, other = self._rows("Sep 3, 2026  6:21 PM", "Windows PC backup",
                                 "Sep 2, 2026  3:36 PM")
        self.assertEqual(this.index("Sep 3"), other.index("Sep 2"))

    def test_alignment_holds_when_the_labels_differ_in_length(self):
        # "Steam Deck backup" and "Windows PC backup" happen to match, but a
        # shorter label must not shift the column either.
        this, other = self._rows("Sep 3, 2026  6:21 PM", "PC", "Sep 2, 2026  3:36 PM")
        self.assertEqual(this.index("Sep 3"), other.index("Sep 2"))

    def test_alignment_holds_when_the_dates_differ_in_length(self):
        this, other = self._rows("Sep 3, 2026  6:21 PM", "Windows PC backup",
                                 "Dec 25, 2026  11:05 AM")
        self.assertEqual(this.index("Sep 3"), other.index("Dec 25"))

    def test_the_newest_marker_sits_on_this_machine(self):
        this, other = self._rows("Sep 3, 2026  6:21 PM", "Windows PC backup",
                                 "Sep 2, 2026  3:36 PM")
        self.assertIn("(newest)", this)
        self.assertNotIn("(newest)", other)

    def test_no_trailing_whitespace_on_any_line(self):
        text = savepick.conflict_text("g", "Sep 3, 2026  6:21 PM", "PC",
                                      "Dec 25, 2026  11:05 AM")
        for line in text.splitlines():
            self.assertEqual(line, line.rstrip(), repr(line))

    def test_an_unknown_date_still_aligns(self):
        this, other = self._rows("unknown", "Windows PC backup", "Sep 2, 2026  3:36 PM")
        self.assertEqual(this.index("unknown"), other.index("Sep 2"))


class TestPangoEscape(unittest.TestCase):
    """zenity renders Pango markup, so the game name must not be able to break it."""

    def test_ampersand_and_angle_brackets(self):
        self.assertEqual(savepick.pango_escape("Command & Conquer"),
                         "Command &amp; Conquer")
        self.assertEqual(savepick.pango_escape("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;")

    def test_ampersand_is_escaped_before_the_others(self):
        # Escaping < first would turn &lt; into &amp;lt;.
        self.assertEqual(savepick.pango_escape("<"), "&lt;")

    def test_plain_text_is_untouched(self):
        self.assertEqual(savepick.pango_escape("Dark Souls II: Scholar"),
                         "Dark Souls II: Scholar")


class TestZenityMarkup(unittest.TestCase):
    """The Deck's dialog needs a monospace span or the padding does nothing."""

    def test_body_is_wrapped_in_a_monospace_span(self):
        body = savepick.zenity_markup("Command & Conquer", "Sep 3, 2026  6:21 PM",
                                      "Windows PC backup", "Sep 2, 2026  3:36 PM")
        self.assertTrue(body.startswith("<tt>"))
        self.assertTrue(body.endswith("</tt>"))

    def test_the_game_name_cannot_inject_markup(self):
        body = savepick.zenity_markup("<b>evil</b> & co", "a", "PC", "b")
        self.assertNotIn("<b>", body)
        self.assertIn("&amp;", body)

    def test_it_carries_the_same_words_as_the_windows_dialog(self):
        plain = savepick.conflict_text("Game", "a", "PC", "b")
        body = savepick.zenity_markup("Game", "a", "PC", "b")
        self.assertIn("The backup is OLDER than the save on this device.", plain)
        self.assertIn("The backup is OLDER than the save on this device.", body)


class TestSavePathsFromPreview(unittest.TestCase):
    """Only files ludusavi tags as saves may set the timestamps we compare.

    2026-09-05: three Steam screenshots taken at 1:56 PM made the Deck's live
    save look 20 hours newer than it was, and the dialog offered to keep it.
    Ludusavi lists screenshots for DS2 and gives them no tags at all.
    """

    SAVE = ("/home/deck/.local/share/Steam/steamapps/compatdata/335300/pfx/"
            "drive_c/users/steamuser/AppData/Roaming/DarkSoulsII/"
            "011000010101e6e8/DS2SOFS0000.sl2")
    SHOT = ("/home/deck/.local/share/Steam/userdata/16901864/760/remote/"
            "335300/screenshots/20260905135624_1.jpg")

    def test_untagged_screenshots_are_not_saves(self):
        entry = {"files": {self.SAVE: {"tags": ["save"]}, self.SHOT: {}}}
        self.assertEqual(savepick.save_paths_from_preview(entry), [self.SAVE])

    def test_config_files_are_not_saves(self):
        entry = {"files": {self.SAVE: {"tags": ["save"]},
                           "/x/GraphicsConfig.xml": {"tags": ["config"]}}}
        self.assertEqual(savepick.save_paths_from_preview(entry), [self.SAVE])

    def test_a_file_tagged_both_still_counts(self):
        entry = {"files": {self.SAVE: {"tags": ["config", "save"]}}}
        self.assertEqual(savepick.save_paths_from_preview(entry), [self.SAVE])

    def test_nothing_tagged_anywhere_falls_back_to_every_file(self):
        # An older ludusavi that never emits tags. Losing every path would read
        # as "fresh install", and decide() restores on that. Keep the old shape.
        entry = {"files": {self.SAVE: {}, "/x/other.dat": {}}}
        self.assertEqual(sorted(savepick.save_paths_from_preview(entry)),
                         sorted([self.SAVE, "/x/other.dat"]))

    def test_tags_present_but_no_save_returns_nothing(self):
        # Ludusavi answered, and its answer is that this game has no save here.
        entry = {"files": {"/x/GraphicsConfig.xml": {"tags": ["config"]}}}
        self.assertEqual(savepick.save_paths_from_preview(entry), [])

    def test_no_files_at_all(self):
        self.assertEqual(savepick.save_paths_from_preview({}), [])

    def test_a_screenshot_cannot_win_the_live_mtime(self):
        # End to end over the real bug, with real files on disk.
        root = Path(tempfile.mkdtemp())
        save = root / "DS2SOFS0000.sl2"
        shot = root / "20260905135624_1.jpg"
        save.write_text("save")
        shot.write_text("shot")
        os.utime(save, (1000, 1000))
        os.utime(shot, (99000, 99000))
        entry = {"files": {str(save): {"tags": ["save"]}, str(shot): {}}}
        paths = savepick.save_paths_from_preview(entry)
        self.assertEqual(savepick.newest_live_mtime(paths), 1000)


class TestIncomingReady(unittest.TestCase):
    """Syncthing's own answer decides. savepick must never guess it.

    2026-09-05: Syncthing finished pulling the Windows backup at 13:57:15.
    savepick had already compared timestamps at 13:56:04 and offered to keep a
    save that was 20 hours older. 71 seconds.
    """

    IDLE = {"state": "idle", "needBytes": 0, "needTotalItems": 0,
            "errors": 0, "error": "", "globalBytes": 100, "localBytes": 100}

    def test_idle_with_nothing_needed_is_ready(self):
        self.assertTrue(savepick.incoming_ready(self.IDLE))

    def test_syncing_is_not_ready(self):
        status = dict(self.IDLE, state="syncing")
        self.assertFalse(savepick.incoming_ready(status))

    def test_bytes_still_needed_is_not_ready(self):
        # The exact 13:56:04 shape: it had not pulled last night's backup yet.
        self.assertFalse(savepick.incoming_ready(dict(self.IDLE, needBytes=8251680)))

    def test_items_still_needed_is_not_ready(self):
        self.assertFalse(savepick.incoming_ready(dict(self.IDLE, needTotalItems=1)))

    def test_a_folder_error_is_not_ready(self):
        self.assertFalse(savepick.incoming_ready(dict(self.IDLE, errors=2)))
        self.assertFalse(savepick.incoming_ready(dict(self.IDLE, error="stopped")))

    def test_no_answer_is_not_ready(self):
        # Fail-safe: an unreadable Syncthing is not a green light.
        self.assertFalse(savepick.incoming_ready(None))
        self.assertFalse(savepick.incoming_ready({}))

    def test_scanning_is_not_ready(self):
        self.assertFalse(savepick.incoming_ready(dict(self.IDLE, state="scanning")))


class TestSyncPercent(unittest.TestCase):
    """Only for the spinner text. It never decides anything."""

    def test_nothing_left_to_pull_is_a_hundred(self):
        self.assertEqual(savepick.sync_percent(
            {"globalBytes": 200, "needBytes": 0}), 100)

    def test_half_way(self):
        self.assertEqual(savepick.sync_percent(
            {"globalBytes": 200, "needBytes": 100}), 50)

    def test_an_empty_folder_does_not_divide_by_zero(self):
        self.assertEqual(savepick.sync_percent({"globalBytes": 0, "needBytes": 0}), 100)
        self.assertEqual(savepick.sync_percent({"globalBytes": 0, "needBytes": 5}), 0)

    def test_no_status_reads_as_nothing_here(self):
        self.assertEqual(savepick.sync_percent(None), 0)


class FakeSyncthing:
    """Stands in for the REST API. Returns one status per poll, in order."""

    def __init__(self, statuses, connections=None):
        self.statuses = list(statuses)
        self.connections = connections
        self.posted = []
        self.polls = 0
        self.post_result = True

    def get(self, _cfg, path):
        if path.startswith("/rest/config/"):
            return {"paused": False, "path": "/saves/gamesaves"}
        if path.startswith("/rest/system/connections"):
            if self.connections is None:
                return {"connections": {"HUB": {"connected": True, "paused": False}}}
            return self.connections.pop(0) if self.connections else None
        self.polls += 1
        return self.statuses.pop(0) if self.statuses else self.statuses_last()

    def statuses_last(self):
        return TestIncomingReady.IDLE

    def post(self, _cfg, path, payload=None, timeout=None):
        self.posted.append(path)
        return self.post_result


class TestWaitForIncoming(unittest.TestCase):

    CFG = {"syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                         "folder": "gamesaves",
                         "hub_id": "HUB", "hub_name": "nas",
                         "device_dir": "deck",
                         "device_names": {"desktop": "Windows PC"}}}

    def setUp(self):
        self.real_get = savepick.syncthing_get
        self.real_post = savepick.syncthing_post
        self.real_cfg = savepick.load_config

    def tearDown(self):
        savepick.syncthing_get = self.real_get
        savepick.syncthing_post = self.real_post
        savepick.load_config = self.real_cfg

    def _wire(self, fake, cfg=None):
        savepick.syncthing_get = fake.get
        savepick.syncthing_post = fake.post
        savepick.load_config = lambda: (self.CFG if cfg is None else cfg)

    def test_it_waits_until_syncthing_says_idle(self):
        busy = dict(TestIncomingReady.IDLE, state="syncing", needBytes=800)
        fake = FakeSyncthing([busy, busy, TestIncomingReady.IDLE])
        self._wire(fake)
        self.assertIs(savepick.wait_for_incoming(poll=0, settle=0), True)
        self.assertGreaterEqual(fake.polls, 3)

    def test_it_asks_for_no_scan_before_a_launch(self):
        """The scan WAS the wait.

        Measured on the Windows PC 2026-09-21: 51.6 seconds to scan the whole
        folder with nothing to transfer and nothing changed, and the wait
        cannot end while the folder is scanning. Nothing on this device writes
        to the shared folder except the exit backup, which scans its own
        directory itself, so there was never anything here for a scan to find.
        """
        fake = FakeSyncthing([TestIncomingReady.IDLE])
        self._wire(fake)
        savepick.wait_for_incoming(poll=0, settle=0)
        self.assertEqual([p for p in fake.posted if "db/scan" in p], [])

    def test_it_still_resumes_a_paused_hub_and_folder(self):
        # The poke does more than scan, and the rest of it is cheap and still
        # wanted: a paused device or folder can never reach 100 percent.
        fake = FakeSyncthing([TestIncomingReady.IDLE])
        self._wire(fake)
        savepick.wait_for_incoming(poll=0, settle=0)
        self.assertIn("/rest/system/resume?device=HUB", fake.posted)


    def test_the_window_is_told_what_the_wait_is_blocked_on(self):
        scanning = dict(TestIncomingReady.IDLE, state="scanning")
        fake = FakeSyncthing([scanning, TestIncomingReady.IDLE])
        self._wire(fake)
        spinner = _NullSpinner()
        savepick.wait_for_incoming(spinner, poll=0, settle=0)
        said = [text for text, _pct in spinner.updates]
        self.assertTrue(any("Checking this device's files" in t for t in said),
                        said)
        self.assertFalse(any("100%" in t for t in said), said)

    def test_the_settle_window_fills_the_bar(self):
        """The one wait whose length is known, so the bar shows it filling."""

        class Clock:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += max(seconds, 1.0)

        fake = FakeSyncthing([TestIncomingReady.IDLE] * 20)
        self._wire(fake)
        spinner = _NullSpinner()
        real_time = savepick.time
        savepick.time = Clock()
        try:
            self.assertIs(savepick.wait_for_incoming(
                spinner, poll=0, settle=4.0), True)
        finally:
            savepick.time = real_time
        bars = [pct for _text, pct in spinner.updates if pct is not None]
        self.assertEqual(bars[-1], 100)
        self.assertEqual(bars, sorted(bars))
        self.assertGreater(len(bars), 2)

    def test_missing_incoming_folder_setting_returns_none(self):
        # None, not True. Not knowing is never a green light.
        fake = FakeSyncthing([TestIncomingReady.IDLE])
        self._wire(fake, cfg={"syncthing": {"url": "u", "apikey": "k",
                                            "hub_id": "HUB"}})
        self.assertIsNone(savepick.wait_for_incoming(poll=0, settle=0))

    def test_no_syncthing_config_at_all_returns_none(self):
        fake = FakeSyncthing([TestIncomingReady.IDLE])
        self._wire(fake, cfg={})
        self.assertIsNone(savepick.wait_for_incoming(poll=0, settle=0))

    def test_a_folder_error_stops_the_wait(self):
        bad = dict(TestIncomingReady.IDLE, error="folder marked stopped")
        fake = FakeSyncthing([bad])
        self._wire(fake)
        self.assertIsNone(savepick.wait_for_incoming(poll=0, settle=0))

    def test_pull_errors_are_waited_out_not_given_up_on(self):
        """2026-09-10 22:13:59: the Deck gave up on a prune that healed itself.

        DESKTOP's ludusavi retention deleted two old backups. Syncthing carried
        the deletes to the Deck and could not remove the directories on its
        first pass, because it tried the parents before the children. It logged
        9 pull errors and "will be retried (wait=1m1s)". savepick sampled 20
        seconds into that minute, read errors=9, gave up, and told him it could
        not get an answer. Syncthing finished the deletes at 22:14:39.

        A pull-error COUNT is retryable and is not the folder failing. Only the
        folder-level error STRING means the folder itself has stopped.
        """
        retrying = dict(TestIncomingReady.IDLE, errors=9, pullErrors=9)
        fake = FakeSyncthing([retrying, retrying, TestIncomingReady.IDLE])
        self._wire(fake)
        self.assertIs(savepick.wait_for_incoming(poll=0, settle=0), True)

    def test_pull_errors_never_count_as_synced(self):
        # Waiting them out must not mean accepting them. Only a clean status
        # is a green light, so a run of pull errors can only ever end in a
        # cancel or a real recovery.
        retrying = dict(TestIncomingReady.IDLE, errors=9, pullErrors=9)
        fake = FakeSyncthing([retrying] * 50)
        self._wire(fake)

        class Cancel:
            def __init__(self):
                self.calls = 0

            def pressed(self):
                self.calls += 1
                return self.calls > 3

        self.assertIs(savepick.wait_for_incoming(
            cancel=Cancel(), poll=0, settle=0), False)

    def test_a_dead_api_gives_up_rather_than_spinning_forever(self):
        fake = FakeSyncthing([None] * 20)
        self._wire(fake)
        self.assertIsNone(savepick.wait_for_incoming(poll=0, settle=0))

    def test_the_user_can_cancel(self):
        busy = dict(TestIncomingReady.IDLE, state="syncing", needBytes=800)
        fake = FakeSyncthing([busy] * 50)
        self._wire(fake)

        class Cancel:
            def __init__(self):
                self.calls = 0
            def pressed(self):
                self.calls += 1
                return self.calls > 2

        self.assertIs(savepick.wait_for_incoming(cancel=Cancel(), poll=0, settle=0),
                      False)

    def test_it_waits_for_the_peer_to_connect(self):
        offline = {"connections": {"HUB": {"connected": False}}}
        online = {"connections": {"HUB": {"connected": True}}}
        fake = FakeSyncthing([TestIncomingReady.IDLE],
                             connections=[offline, offline, online])
        self._wire(fake)
        self.assertIs(savepick.wait_for_incoming(poll=0, settle=0), True)

    def test_idle_before_the_peer_connects_does_not_count(self):
        # The trap: right after a wake the folder reads idle with nothing
        # needed, because Windows has not sent its index yet.
        offline = {"connections": {"HUB": {"connected": False}}}
        online = {"connections": {"HUB": {"connected": True}}}
        fake = FakeSyncthing([TestIncomingReady.IDLE] * 5,
                             connections=[offline, online, online])
        self._wire(fake)
        self.assertIs(savepick.wait_for_incoming(poll=0, settle=0), True)
        # It never polled the folder while the peer was offline.
        self.assertLessEqual(fake.polls, 2)


class TestWaitGivesUpOnAStuckFolder(unittest.TestCase):
    CFG = TestWaitForIncoming.CFG

    def setUp(self):
        self.real_get = savepick.syncthing_get
        self.real_post = savepick.syncthing_post
        self.real_cfg = savepick.load_config

    def tearDown(self):
        savepick.syncthing_get = self.real_get
        savepick.syncthing_post = self.real_post
        savepick.load_config = self.real_cfg

    def _wire(self, fake):
        savepick.syncthing_get = fake.get
        savepick.syncthing_post = fake.post
        savepick.load_config = lambda: self.CFG

    ERRORS = {"state": "syncing", "needBytes": 1, "needTotalItems": 1,
              "errors": 9, "error": "", "globalBytes": 100}

    def test_pull_errors_keep_it_waiting_at_first(self):
        # The 2026-09-10 shape: Syncthing retries these and they clear.
        statuses = [self.ERRORS, self.ERRORS, TestIncomingReady.IDLE,
                    TestIncomingReady.IDLE]
        fake = FakeSyncthing(statuses)
        self._wire(fake)
        result = savepick.wait_for_incoming(poll=0, settle=0, heal_after=999)
        self.assertIs(result, True)

    def test_a_folder_stuck_past_the_heal_window_gives_up(self):
        fake = FakeSyncthing([self.ERRORS] * 50)
        self._wire(fake)
        result = savepick.wait_for_incoming(poll=0, settle=0, heal_after=0)
        self.assertIsNone(result)

    def test_it_never_reverts_the_folder(self):
        # The shared folder holds this device's own backups now, so revert
        # could throw away a session that has not reached the hub yet.
        fake = FakeSyncthing([self.ERRORS] * 50)
        self._wire(fake)
        savepick.wait_for_incoming(poll=0, settle=0, heal_after=0)
        self.assertFalse([p for p in fake.posted if "revert" in p])

    def test_heal_incoming_is_gone(self):
        self.assertFalse(hasattr(savepick, "heal_incoming"))

    def test_an_old_shaped_config_is_not_configured(self):
        savepick.load_config = lambda: {
            "syncthing": {"url": "http://x", "apikey": "k",
                          "incoming_folder": "ludusavi-windows",
                          "folder": "ludusavi-deck", "peer_id": "PEER"}}
        self.assertIsNone(savepick.wait_for_incoming(poll=0, settle=0))

    def test_errors_that_clear_reset_the_give_up_timer(self):
        # A folder that recovers must not carry its old error clock into the
        # next run of errors and give up on a wait it never needed. The two
        # error runs must be separated by a status that is NOT ready, or the
        # wait returns True on the first idle and never reaches a second run
        # of errors at all.
        BUSY = {"state": "syncing", "needBytes": 50, "needTotalItems": 1,
                "errors": 0, "error": "", "globalBytes": 100}
        statuses = [self.ERRORS, BUSY, self.ERRORS, BUSY,
                    TestIncomingReady.IDLE]
        fake = FakeSyncthing(statuses)
        self._wire(fake)

        class FakeClock:
            """A deterministic stand-in for the time module.

            poll=0 makes real elapsed time far too small to cross any
            heal_after worth testing, so this steps a fixed amount on every
            monotonic() call instead.
            """

            def __init__(self, step):
                self.step = step
                self.now = 0

            def monotonic(self):
                self.now += self.step
                return self.now

            def sleep(self, _seconds):
                pass

        real_time = savepick.time
        savepick.time = FakeClock(step=10)
        try:
            self.assertIs(savepick.wait_for_incoming(
                poll=0, settle=0, heal_after=20), True)
        finally:
            savepick.time = real_time


class TestTargetedScan(unittest.TestCase):
    """The scan covers one game, not the whole hub.

    2026-09-21: the pre-launch window took 58 to 126 seconds on the Windows PC
    with nothing to transfer. savepick was asking Syncthing to rescan the whole
    shared folder and then refusing to finish while the folder said "scanning".
    Measured on the real folder: 51.6 seconds for 28,889 files, and 0.26
    seconds for one game's directories.
    """

    CFG = {"url": "http://127.0.0.1:8384", "apikey": "k", "folder": "gamesaves",
           "hub_id": "HUB", "hub_name": "nas", "device_dir": "desktop"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        self.real_root = savepick.folder_root
        self.real_json = savepick.run_json
        savepick.folder_root = lambda _cfg: self.root
        for name in ("desktop", "deck", ".stversions"):
            (self.root / name / "Dark Souls II_ Scholar of the First Sin").mkdir(
                parents=True)
        (self.root / "deck" / "Balatro").mkdir()

    def tearDown(self):
        savepick.folder_root = self.real_root
        savepick.run_json = self.real_json
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _names(self, backup_path):
        def run_json(args, timeout=120):
            return {"games": {args[-1]: {"backupPath": backup_path}}}
        savepick.run_json = run_json

    def test_only_this_devices_own_directory_is_scanned(self):
        # A peer's directory is written by Syncthing and indexed by Syncthing.
        # Scanning the Deck's copy of the RetroBat set cost 24 of the 41
        # seconds measured on 2026-09-21 and could not learn anything.
        self._names("C:/Users/player/GameSaves/gamesaves/desktop/"
                    "Dark Souls II_ Scholar of the First Sin")
        self.assertEqual(
            savepick.scan_subs(self.CFG, "Dark Souls II: Scholar of the First Sin"),
            ["desktop/Dark Souls II_ Scholar of the First Sin"])

    def test_named_devices_are_covered_when_asked_for(self):
        self._names("/saves/gamesaves/deck/Dark Souls II_ Scholar of the First Sin")
        self.assertEqual(
            savepick.scan_subs(self.CFG,
                               "Dark Souls II: Scholar of the First Sin",
                               devices=["deck", "desktop", ".stversions"]),
            ["deck/Dark Souls II_ Scholar of the First Sin",
             "desktop/Dark Souls II_ Scholar of the First Sin"])

    def test_a_device_without_the_game_is_left_out(self):
        self._names("/saves/gamesaves/deck/Balatro")
        self.assertEqual(savepick.scan_subs(self.CFG, "Balatro",
                                            devices=["deck", "desktop"]),
                         ["deck/Balatro"])

    def test_a_game_this_device_does_not_hold_scans_nothing(self):
        # Empty, never the whole folder. There is nothing here a scan could
        # correct, so a minute of scanning would buy exactly nothing.
        self._names("/saves/gamesaves/desktop/Hollow Knight")
        self.assertEqual(savepick.scan_subs(self.CFG, "Hollow Knight"), [])

    def test_the_directory_name_comes_from_ludusavi_never_from_the_game_name(self):
        # ludusavi rewrites the characters a filesystem refuses. Building the
        # name here would guess wrong on the first colon.
        self._names("/saves/gamesaves/desktop/"
                    "Dark Souls II_ Scholar of the First Sin")
        self.assertEqual(
            savepick.backup_dir_name(self.CFG,
                                     "Dark Souls II: Scholar of the First Sin"),
            "Dark Souls II_ Scholar of the First Sin")

    def test_a_windows_backup_path_is_read_as_a_path(self):
        self._names(r"C:\Users\player\GameSaves\gamesaves\desktop\Balatro")
        self.assertEqual(savepick.backup_dir_name(self.CFG, "Balatro"), "Balatro")

    def test_a_game_this_device_has_never_backed_up_is_asked_of_the_peers(self):
        seen = []

        def run_json(args, timeout=120):
            seen.append(args)
            if "--path" not in args:
                return {"games": {}}
            return {"games": {args[-1]: {"backupPath":
                                         "/saves/gamesaves/deck/Balatro"}}}
        savepick.run_json = run_json
        self.assertEqual(savepick.backup_dir_name(self.CFG, "Balatro"), "Balatro")
        self.assertTrue(any("--path" in args for args in seen))

    def test_no_backup_directory_anywhere_scans_nothing(self):
        savepick.run_json = lambda args, timeout=120: {"games": {}}
        self.assertEqual(savepick.scan_subs(self.CFG, "Balatro"), [])


class TestPokeSyncthing(unittest.TestCase):

    CFG = {"url": "http://127.0.0.1:8384", "apikey": "k", "folder": "gamesaves",
           "hub_id": "HUB", "device_dir": "desktop"}

    def setUp(self):
        self.posted = []
        self.real_post = savepick.syncthing_post
        self.real_get = savepick.syncthing_get
        self.real_patch = savepick.syncthing_patch
        savepick.syncthing_post = lambda _cfg, path, payload=None, timeout=None: (
            self.posted.append(path) or True)
        savepick.syncthing_get = lambda _cfg, _path: {"paused": False}
        savepick.syncthing_patch = lambda _cfg, _path, _payload: True

    def tearDown(self):
        savepick.syncthing_post = self.real_post
        savepick.syncthing_get = self.real_get
        savepick.syncthing_patch = self.real_patch

    def _scans(self):
        return [p for p in self.posted if "/rest/db/scan" in p]

    def test_subs_are_url_encoded_into_one_request(self):
        # Syncthing takes every sub in one call. Measured 2026-09-21: two
        # game directories in 0.53s, against 51.6s for the folder.
        savepick.poke_syncthing(self.CFG, ["deck/Dark Souls II_ Scholar",
                                           "desktop/Dark Souls II_ Scholar"])
        self.assertEqual(self._scans(), [
            "/rest/db/scan?folder=gamesaves"
            "&sub=deck/Dark%20Souls%20II_%20Scholar"
            "&sub=desktop/Dark%20Souls%20II_%20Scholar"])

    def test_an_empty_sub_list_scans_nothing_at_all(self):
        savepick.poke_syncthing(self.CFG, [])
        self.assertEqual(self._scans(), [])

    def test_no_subs_still_scans_the_whole_folder(self):
        savepick.poke_syncthing(self.CFG)
        self.assertEqual(self._scans(), ["/rest/db/scan?folder=gamesaves"])

    def test_the_hub_is_resumed_before_any_scan(self):
        savepick.poke_syncthing(self.CFG, [])
        self.assertEqual(self.posted[0], "/rest/system/resume?device=HUB")


class _FastClock:
    """A clock that advances one second per look and per sleep."""

    def __init__(self, step=1.0):
        self.now = 0.0
        self.step = step

    def monotonic(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, self.step)


class _NoSleep:
    """The time module with the waiting taken out."""

    monotonic = staticmethod(time.monotonic)

    @staticmethod
    def sleep(_seconds):
        pass


class TestExitScan(unittest.TestCase):
    """The exit backup scans what it just wrote, then waits for the hub.

    /rest/db/completion answers from Syncthing's index. A backup written
    seconds ago is not in it, so the hub needs nothing of it and the old exit
    wait could call that "nas is up to date" while the save had not been
    looked at, let alone sent.
    """

    CFG = {"syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                         "folder": "gamesaves", "hub_id": "HUB",
                         "hub_name": "nas", "device_dir": "desktop"}}

    def setUp(self):
        self.posted = []
        self.real = {name: getattr(savepick, name) for name in
                     ("run_backup", "scan_subs", "wait_for_sync", "show_warning",
                      "syncthing_post", "syncthing_get", "syncthing_patch",
                      "load_config", "Spinner")}
        savepick.run_backup = lambda _game: (False, "")
        savepick.scan_subs = lambda _cfg, game: ["desktop/%s" % game]
        savepick.wait_for_sync = lambda _spinner, _game=None: True
        savepick.show_warning = lambda *_a: None
        savepick.syncthing_post = lambda _cfg, path, payload=None, timeout=None: (
            self.posted.append(path) or True)
        savepick.syncthing_patch = lambda *_a: True
        savepick.load_config = lambda: self.CFG
        savepick.Spinner = lambda *_a, **_k: _NullSpinner()
        # backup_on_exit pauses so a player can read the last line. A test
        # does not read it.
        self.real_time = savepick.time
        savepick.time = _NoSleep()

    def tearDown(self):
        savepick.time = self.real_time
        for name, value in self.real.items():
            setattr(savepick, name, value)

    def _states(self, states):
        """Feed one state per poll. The last one repeats for ever."""
        queue = list(states)

        def get(_cfg, path):
            if path.startswith("/rest/db/status"):
                return {"state": queue.pop(0) if len(queue) > 1 else queue[0]}
            return {"paused": False}
        savepick.syncthing_get = get

    def test_it_scans_the_directory_it_just_wrote_to(self):
        self._states(["idle"])
        savepick.backup_on_exit("Balatro")
        self.assertIn("/rest/db/scan?folder=gamesaves&sub=desktop/Balatro",
                      self.posted)

    def test_it_waits_for_that_scan_to_finish(self):
        self._states(["scanning", "scanning", "idle"])
        self.assertIs(savepick.wait_for_scan(self.CFG["syncthing"], poll=0), True)

    def test_a_scan_that_never_ends_does_not_hold_the_exit_forever(self):
        self._states(["scanning"] * 50)
        self.assertIs(savepick.wait_for_scan(self.CFG["syncthing"], poll=0,
                                             limit=0.01), False)

    def test_an_unreadable_folder_ends_the_scan_wait(self):
        savepick.syncthing_get = lambda _cfg, _path: None
        self.assertIs(savepick.wait_for_scan(self.CFG["syncthing"], poll=0), False)

    def test_a_game_with_nothing_here_skips_the_scan(self):
        self._states(["idle"])
        savepick.scan_subs = lambda _cfg, _game: []
        savepick.backup_on_exit("Balatro")
        self.assertEqual([p for p in self.posted if "db/scan" in p], [])


class TestTheGameGetsTheForeground(unittest.TestCase):
    """Windows opens the game behind Steam, because savepick was not clicked.

    He alt-tabbed to the game on every launch. The ctypes calls cannot run
    here, so what is tested is the two decisions they depend on: which
    processes count as the game, and which window is the game's.
    """

    def test_a_wrapper_counts_as_the_game(self):
        # RetroBat starts its own frontend and shadPS4 is launched by python,
        # so the window belongs to a grandchild, not to the child.
        parents = {100: 1, 200: 100, 300: 200, 400: 1}
        self.assertEqual(savepick.process_tree(100, parents), {100, 200, 300})

    def test_an_unrelated_process_is_not_the_game(self):
        parents = {100: 1, 999: 1}
        self.assertEqual(savepick.process_tree(100, parents), {100})

    def test_a_parent_map_with_a_cycle_cannot_hang(self):
        # pids are reused, so a snapshot can contradict itself.
        parents = {100: 200, 200: 100, 300: 100}
        self.assertIn(300, savepick.process_tree(100, parents))

    def test_it_picks_the_games_own_visible_window(self):
        windows = [
            (11, 999, 0, "Steam", True),
            (12, 300, 0, "", True),
            (13, 300, 0, "Dark Souls II", True),
        ]
        self.assertEqual(savepick.choose_window(windows, {100, 300}), 13)

    def test_a_dialog_is_not_the_game(self):
        # An owned window is a dialog or a tooltip. Raising it would put a
        # splash in front of the game savepick just started.
        windows = [(21, 300, 13, "Loading", True),
                   (22, 300, 0, "Dark Souls II", True)]
        self.assertEqual(savepick.choose_window(windows, {300}), 22)

    def test_a_hidden_window_is_not_the_game(self):
        windows = [(31, 300, 0, "Dark Souls II", False)]
        self.assertIsNone(savepick.choose_window(windows, {300}))

    def test_no_window_yet_is_not_an_error(self):
        self.assertIsNone(savepick.choose_window([], {300}))

    @unittest.skipIf(os.name == "nt", "Windows needs no DISPLAY")
    def test_no_display_means_no_watch(self):
        # A headless run has no window to raise and nothing to raise it with.
        old = os.environ.pop("DISPLAY", None)
        try:
            self.assertIsNone(savepick.watch_for_the_game_window(
                type("P", (), {"pid": 1234})()))
        finally:
            if old is not None:
                os.environ["DISPLAY"] = old

    def test_the_linux_side_asks_xdotool_for_each_process(self):
        seen = []

        class Done:
            stdout = "12345\n"

        def run(cmd):
            seen.append(cmd)
            return Done()
        self.assertEqual(savepick.linux_window_ids({7, 9}, run=run),
                         ["12345", "12345"])
        self.assertTrue(all("--pid" in cmd for cmd in seen))

    def test_a_game_that_sets_no_pid_on_its_window_is_not_an_error(self):
        # Wine and Proton do not always set _NET_WM_PID, so an empty answer
        # is ordinary. It must never read as a failure.
        class Done:
            stdout = ""
        self.assertEqual(savepick.linux_window_ids({7}, run=lambda _c: Done()), [])

    def test_a_compositor_that_names_no_active_window_is_left_alone(self):
        # gamescope. It picks the focused window itself and publishes no
        # _NET_ACTIVE_WINDOW, so there is nothing here to reinforce.
        class Done:
            stdout = "XGetWindowProperty[_NET_ACTIVE_WINDOW] failed (code=1)"
        self.assertFalse(savepick.linux_focus_is_ours(run=lambda _c: Done()))

    def test_a_desktop_session_is_ours_to_drive(self):
        class Done:
            stdout = "50331651\n"
        self.assertTrue(savepick.linux_focus_is_ours(run=lambda _c: Done()))

    def test_the_linux_raise_believes_the_window_manager_not_itself(self):
        # gamescope picks the focused window itself and may ignore this
        # outright. Asking which window is active afterwards is the only
        # honest answer.
        class Done:
            def __init__(self, out): self.stdout = out

        answers = [Done(""), Done("999\n")]
        self.assertFalse(savepick.linux_raise("12345",
                                              run=lambda _c: answers.pop(0)))
        answers = [Done(""), Done("12345\n")]
        self.assertTrue(savepick.linux_raise("12345",
                                             run=lambda _c: answers.pop(0)))

    @unittest.skipIf(os.name == "nt", "/proc is the Linux way to read this")
    def test_a_process_name_with_spaces_does_not_break_the_parent_map(self):
        # /proc/<pid>/stat puts the command in brackets and it can hold
        # spaces and brackets, which is why the fields are counted from the
        # last one.
        parents = savepick.linux_process_parents()
        self.assertIn(os.getpid(), parents)
        self.assertEqual(parents[os.getpid()], os.getppid())

    def test_a_process_with_no_pid_is_not_watched(self):
        self.assertIsNone(savepick.watch_for_the_game_window(object()))

    def test_the_watch_never_holds_the_game_up(self):
        # A game that never opens a window must not delay the launch or the
        # exit backup, so the watch runs on a daemon thread nobody joins.
        real_windows = savepick.is_windows
        real_focus = savepick.focus_the_game
        savepick.is_windows = lambda: True
        savepick.focus_the_game = lambda *_a, **_k: False
        try:
            thread = savepick.watch_for_the_game_window(
                type("P", (), {"pid": 4321})())
            self.assertTrue(thread.daemon)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        finally:
            savepick.is_windows = real_windows
            savepick.focus_the_game = real_focus


class TestRemoteNeed(unittest.TestCase):
    """What the hub still needs, asked of the list rather than the summary."""

    CFG = {"url": "http://127.0.0.1:8384", "apikey": "k", "folder": "gamesaves",
           "hub_id": "HUB", "device_dir": "deck"}

    def setUp(self):
        self.real = savepick.syncthing_get
        self.asked = []

    def tearDown(self):
        savepick.syncthing_get = self.real

    def _pages(self, pages):
        def get(_cfg, path):
            self.asked.append(path)
            page = int(path.split("page=")[1].split("&")[0])
            return {"files": pages[page - 1] if page <= len(pages) else []}
        savepick.syncthing_get = get

    def test_it_counts_only_the_directory_asked_about(self):
        # A backlog elsewhere in the folder is not this game's problem and
        # must never hold up this game's confirmation.
        self._pages([[{"name": "deck/Balatro/backup-1/save", "size": 100},
                      {"name": "desktop/Other Game/backup-9/save", "size": 999},
                      {"name": "deck/Balatro/backup-1/more", "size": 50}]])
        self.assertEqual(savepick.remote_need(self.CFG, "deck/Balatro/"),
                         (2, 150))

    def test_no_prefix_counts_everything(self):
        self._pages([[{"name": "deck/Balatro/x", "size": 100},
                      {"name": "desktop/Other/y", "size": 900}]])
        self.assertEqual(savepick.remote_need(self.CFG), (2, 1000))

    def test_a_windows_path_separator_still_matches(self):
        self._pages([[{"name": "deck\\Balatro\\x", "size": 7}]])
        self.assertEqual(savepick.remote_need(self.CFG, "deck/Balatro/"), (1, 7))

    def test_it_reads_further_pages(self):
        full = [{"name": "deck/Balatro/f%d" % i, "size": 1}
                for i in range(savepick.REMOTE_NEED_PER_PAGE)]
        self._pages([full, [{"name": "deck/Balatro/last", "size": 5}]])
        self.assertEqual(savepick.remote_need(self.CFG, "deck/Balatro/"),
                         (savepick.REMOTE_NEED_PER_PAGE + 1,
                          savepick.REMOTE_NEED_PER_PAGE + 5))

    def test_more_pages_than_it_will_read_is_never_reported_as_nothing_left(self):
        full = [{"name": "desktop/Other/f%d" % i, "size": 1}
                for i in range(savepick.REMOTE_NEED_PER_PAGE)]
        self._pages([full] * (savepick.REMOTE_NEED_PAGES + 2))
        items, _bytes = savepick.remote_need(self.CFG, "deck/Balatro/")
        self.assertGreater(items, 0)

    def test_an_endpoint_that_does_not_answer_gives_none(self):
        savepick.syncthing_get = lambda _cfg, _path: None
        self.assertIsNone(savepick.remote_need(self.CFG))

    def test_an_endpoint_without_a_file_list_gives_none(self):
        # An older Syncthing answers something else entirely. None means
        # "use the fallback", never "nothing left".
        savepick.syncthing_get = lambda _cfg, _path: {"error": "no such endpoint"}
        self.assertIsNone(savepick.remote_need(self.CFG))

    def test_hub_holds_reads_the_availability_list(self):
        savepick.syncthing_get = lambda _cfg, _path: {
            "availability": [{"id": "OTHER"}, {"id": "HUB"}]}
        self.assertIs(savepick.hub_holds(self.CFG, "deck/x/y"), True)

    def test_hub_holds_is_false_when_the_hub_is_not_listed(self):
        savepick.syncthing_get = lambda _cfg, _path: {"availability": [{"id": "OTHER"}]}
        self.assertIs(savepick.hub_holds(self.CFG, "deck/x/y"), False)


class TestWaitForSync(unittest.TestCase):
    """The exit wait. Two yes answers, or it keeps waiting."""

    CFG = {"syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                         "folder": "gamesaves", "hub_id": "HUB",
                         "hub_name": "nas", "device_dir": "deck"}}

    def setUp(self):
        self.real = {name: getattr(savepick, name) for name in
                     ("syncthing_get", "load_config", "backup_dir_name",
                      "newest_backup_file", "time")}
        savepick.load_config = lambda: self.CFG
        savepick.backup_dir_name = lambda _cfg, game: game
        savepick.newest_backup_file = lambda _cfg, game: "deck/%s/backup-1/save" % game
        # A clock that moves on its own, so a wait meant to run out runs out
        # in a test instead of in three real minutes.
        savepick.time = _FastClock()

    def tearDown(self):
        for name, value in self.real.items():
            setattr(savepick, name, value)

    def _wire(self, needs, holds=True, connected=True):
        """needs: one remoteneed file list per poll, last repeating."""
        queue = list(needs)

        def get(_cfg, path):
            if path.startswith("/rest/db/remoteneed"):
                page = int(path.split("page=")[1].split("&")[0])
                if page > 1:
                    return {"files": []}
                return {"files": queue.pop(0) if len(queue) > 1 else queue[0]}
            if path.startswith("/rest/db/file"):
                return {"availability": [{"id": "HUB"}] if holds else []}
            if path.startswith("/rest/system/connections"):
                return {"connections": {"HUB": {"connected": connected}}}
            return None
        savepick.syncthing_get = get

    def test_nothing_needed_and_the_hub_holds_it_is_a_yes(self):
        self._wire([[]], holds=True)
        spinner = _NullSpinner()
        self.assertIs(savepick.wait_for_sync(spinner, "Balatro"), True)
        self.assertEqual(spinner.updates[-1][1], 100)

    def test_nothing_needed_but_the_hub_has_not_heard_yet_is_not_a_yes(self):
        """The hub needs nothing it does not know about.

        This is the whole reason the wait asks two questions. A backup written
        one second ago is not in anybody's need list yet.
        """
        self._wire([[]], holds=False)
        self.assertIs(savepick.wait_for_sync(_NullSpinner(), "Balatro"), False)

    def test_it_waits_while_this_game_is_still_queued(self):
        self._wire([[{"name": "deck/Balatro/backup-1/save", "size": 100}],
                    []], holds=True)
        self.assertIs(savepick.wait_for_sync(_NullSpinner(), "Balatro"), True)

    def test_a_backlog_elsewhere_does_not_hold_up_this_game(self):
        # 2026-09-21: completion counted 8.2 MB of somebody else's business
        # and the window sat at 99% until it timed out.
        self._wire([[{"name": "desktop/Some Other Game/backup-3/save",
                      "size": 8259533}]], holds=True)
        self.assertIs(savepick.wait_for_sync(_NullSpinner(), "Balatro"), True)

    def test_the_bar_measures_this_backup_not_the_folder(self):
        self._wire([[{"name": "deck/Balatro/backup-1/a", "size": 100},
                     {"name": "deck/Balatro/backup-1/b", "size": 100}],
                    [{"name": "deck/Balatro/backup-1/b", "size": 100}],
                    []], holds=True)
        spinner = _NullSpinner()
        savepick.wait_for_sync(spinner, "Balatro")
        bars = [pct for _text, pct in spinner.updates if pct is not None]
        self.assertEqual(bars, [0, 50, 100])

    def test_a_hub_that_goes_offline_is_a_no(self):
        self._wire([[{"name": "deck/Balatro/backup-1/save", "size": 10}]],
                   holds=False, connected=False)
        self.assertIs(savepick.wait_for_sync(_NullSpinner(), "Balatro"), False)

    def test_no_remoteneed_endpoint_falls_back_to_completion(self):
        def get(_cfg, path):
            if path.startswith("/rest/db/remoteneed"):
                return {"error": "unknown endpoint"}
            if path.startswith("/rest/db/completion"):
                return {"completion": 100, "needBytes": 0}
            if path.startswith("/rest/system/connections"):
                return {"connections": {"HUB": {"connected": True}}}
            return None
        savepick.syncthing_get = get
        self.assertIs(savepick.wait_for_sync(_NullSpinner(), "Balatro"), True)

    def test_the_deck_regression(self):
        """2026-09-21, Syncthing v2.1.2 on the Deck.

        completion said 99.85% with 8.2 MB, 20 items and 28 deletes
        outstanding. remoteneed said the hub needed nothing, and the hub had
        the save on disk. savepick ran its full 180 seconds on the completion
        number and then showed NOT SYNCED for a save that had arrived.
        """
        def get(_cfg, path):
            if path.startswith("/rest/db/remoteneed"):
                return {"files": []}
            if path.startswith("/rest/db/completion"):
                return {"completion": 99.85, "needBytes": 8259533,
                        "needItems": 20, "needDeletes": 28}
            if path.startswith("/rest/db/file"):
                return {"availability": [{"id": "HUB"}]}
            if path.startswith("/rest/system/connections"):
                return {"connections": {"HUB": {"connected": True}}}
            return None
        savepick.syncthing_get = get
        self.assertIs(savepick.wait_for_sync(_NullSpinner(),
                                             "Dark Souls II"), True)


class TestWaitMessage(unittest.TestCase):
    """What the window says, and where the bar sits, for one status."""

    IDLE = {"state": "idle", "needBytes": 0, "needTotalItems": 0,
            "errors": 0, "error": "", "globalBytes": 100}

    def test_a_transfer_shows_its_percentage_and_what_is_left(self):
        text, percent = savepick.wait_message(
            dict(self.IDLE, state="syncing", globalBytes=1000, needBytes=250),
            "nas")
        self.assertEqual(percent, 75)
        self.assertIn("75%", text)
        self.assertIn("250 B", text)

    def test_a_transfer_still_running_never_says_100(self):
        # 7.9 MB left of 5.6 GB, seen on the Deck 2026-09-21. It rounded to
        # 100 and the same line said 7.9 MB to go.
        text, percent = savepick.wait_message(
            dict(self.IDLE, state="syncing",
                 globalBytes=5600000000, needBytes=7900000), "nas")
        self.assertEqual(percent, 99)
        self.assertIn("99%", text)

    def test_a_scan_never_claims_a_percentage(self):
        # THE BUG. needBytes was 0 all through a 51 second scan, so the old
        # line read "Getting saves from nas ... 100%" and sat there. A
        # scan has no number to show, so the bar pulses and the text says so.
        text, percent = savepick.wait_message(
            dict(self.IDLE, state="scanning"), "nas")
        self.assertIsNone(percent)
        self.assertNotIn("100%", text)
        self.assertIn("this device", text)

    def test_weightless_changes_say_how_many_not_how_full(self):
        # Deletes and directories have no bytes. 2026-09-10 was exactly this.
        text, percent = savepick.wait_message(
            dict(self.IDLE, state="syncing", needTotalItems=9), "nas")
        self.assertIsNone(percent)
        self.assertIn("9", text)

    def test_the_hub_is_named_in_every_message(self):
        for status in (dict(self.IDLE, needBytes=5),
                       dict(self.IDLE, needTotalItems=1),
                       dict(self.IDLE, state="scanning"),
                       dict(self.IDLE, state="syncing"),
                       self.IDLE):
            self.assertIn("nas", savepick.wait_message(status, "nas")[0])

    def test_human_bytes_reads_like_a_size(self):
        self.assertEqual(savepick.human_bytes(512), "512 B")
        self.assertEqual(savepick.human_bytes(1536), "1.5 KB")
        self.assertEqual(savepick.human_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(savepick.human_bytes(0), "0 B")

    def test_the_settle_bar_fills_across_its_own_window(self):
        self.assertEqual(savepick.settle_percent(0, 6), 0)
        self.assertEqual(savepick.settle_percent(3, 6), 50)
        self.assertEqual(savepick.settle_percent(6, 6), 100)
        self.assertEqual(savepick.settle_percent(99, 6), 100)
        self.assertEqual(savepick.settle_percent(1, 0), 100)


class TestStatusLine(unittest.TestCase):
    """The one line the window parses: text, then where the bar sits."""

    def test_no_percent_means_a_pulsing_bar(self):
        self.assertEqual(savepick.status_line("Checking ...", None),
                         "Checking ...|-")

    def test_a_percent_fills_the_bar(self):
        self.assertEqual(savepick.status_line("Copying", 42), "Copying|42")

    def test_a_percent_outside_the_bar_is_clamped(self):
        self.assertEqual(savepick.status_line("x", 140), "x|100")
        self.assertEqual(savepick.status_line("x", -5), "x|0")

    def test_the_line_never_carries_a_newline(self):
        self.assertEqual(savepick.status_line("one\ntwo", None), "one two|-")

    def test_the_window_script_reads_that_shape(self):
        self.assertIn('-match "^(.*)\\|(-|\\d{1,3})\\s*$"',
                      savepick.WINDOWS_SPINNER)

    def test_the_bar_moves_even_where_the_marquee_cannot(self):
        """A Marquee bar with visual styles off is an empty box that never
        moves. RenderWithVisualStyles is False on the Windows PC, measured
        2026-09-21, so the bar is swept by hand there instead."""
        script = savepick.WINDOWS_SPINNER
        self.assertIn("$script:canMarquee", script)
        self.assertIn("$script:pulseValue = ($script:pulseValue + 8) % 104",
                      script)

    def test_the_window_turns_visual_styles_on(self):
        # Without this a Marquee bar is drawn by the classic control, which
        # is an empty box that never moves. It looked like a hang.
        self.assertIn("EnableVisualStyles", savepick.WINDOWS_SPINNER)
        self.assertLess(savepick.WINDOWS_SPINNER.index("EnableVisualStyles"),
                        savepick.WINDOWS_SPINNER.index("New-Object System.Windows.Forms.Form"))


class TestExitBackupWaitsForTheHub(unittest.TestCase):
    CFG = {"syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                         "folder": "gamesaves", "hub_id": "HUB",
                         "hub_name": "nas", "device_dir": "deck"}}

    def setUp(self):
        self.real_get = savepick.syncthing_get
        self.real_cfg = savepick.load_config
        self.asked = []

    def tearDown(self):
        savepick.syncthing_get = self.real_get
        savepick.load_config = self.real_cfg

    def _wire(self, completion):
        def get(_cfg, path):
            self.asked.append(path)
            if path.startswith("/rest/db/completion"):
                return completion
            return {"connections": {"HUB": {"connected": True}}}
        savepick.syncthing_get = get
        savepick.load_config = lambda: self.CFG

    def test_completion_is_asked_about_the_hub_and_the_shared_folder(self):
        self._wire({"completion": 100, "needBytes": 0})
        result = savepick.wait_for_sync(_NullSpinner())
        self.assertIs(result, True)
        self.assertIn("/rest/db/completion?folder=gamesaves&device=HUB",
                      self.asked)

    def test_an_old_shaped_config_is_not_configured(self):
        # The early return must fire on the config shape alone. Reaching
        # Syncthing at all would mean the shape check was bypassed, so make
        # that loud rather than letting a network failure fake the answer.
        def never(_cfg, path):
            raise AssertionError("syncthing_get called for an old-shaped config: %s" % path)
        savepick.syncthing_get = never
        savepick.load_config = lambda: {
            "syncthing": {"url": "http://x", "apikey": "k",
                          "folder": "ludusavi-deck", "peer_id": "PEER"}}
        self.assertIsNone(savepick.wait_for_sync(_NullSpinner()))


class TestMainGatesOnSync(unittest.TestCase):
    """Only a confirmed sync may lead to a restore. Everything else launches as is."""

    def setUp(self):
        self.saved = {name: getattr(savepick, name) for name in
                      ("confirm_incoming_sync", "warn_unconfirmed", "launch",
                       "backup_on_exit", "game_name_for_appid",
                       "live_save_files", "newest_backup_info", "ask_user",
                       "snapshot_live_saves", "restore_now",
                       "warn_restore_incomplete", "newest_live_mtime",
                       "sync_settings")}
        self.launched = []
        self.warned = []
        self.restored = []
        self.incomplete = []
        savepick.launch = lambda cmd, do_restore=False: self.launched.append(do_restore) or 0
        savepick.warn_unconfirmed = lambda synced: self.warned.append(synced)
        savepick.restore_now = (
            lambda game, mtime, name, peer_dir: self.restored.append(
                (game, mtime, name)) or True)
        savepick.warn_restore_incomplete = lambda game: self.incomplete.append(game)
        self.real_spinner = savepick.Spinner
        savepick.Spinner = lambda *a, **k: _NullSpinner()
        savepick.backup_on_exit = lambda game: None
        savepick.game_name_for_appid = lambda appid: "Dark Souls II"
        savepick.live_save_files = lambda game: ["/x/save.sl2"]
        savepick.newest_backup_info = lambda game, names, cfg: (
            "Windows PC backup", 9000.0, "b1", Path("/peer"))
        savepick.snapshot_live_saves = lambda *a, **k: True
        savepick.ask_user = lambda *a: None
        # A confirmed sync implies sync_settings() held a real config a
        # moment ago. Stub it truthy by default so these tests exercise the
        # confirmed-sync path regardless of what savepick.json happens to
        # hold on the machine running the suite. Tests of the config
        # vanishing between the two reads override this themselves.
        savepick.sync_settings = lambda: {
            "url": "http://hub", "apikey": "k", "folder": "gamesaves",
            "hub_id": "nas", "device_dir": "windows-pc"}
        os.environ["SteamAppId"] = "335300"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(savepick, name, value)
        savepick.Spinner = self.real_spinner
        os.environ.pop("SteamAppId", None)

    def _run(self):
        return savepick.main(["--", "/bin/true"])

    def test_cancelled_sync_never_restores(self):
        savepick.confirm_incoming_sync = lambda: False
        self._run()
        self.assertEqual(self.launched, [False])
        self.assertEqual(self.warned, [False])

    def test_unreadable_syncthing_never_restores(self):
        savepick.confirm_incoming_sync = lambda: None
        self._run()
        self.assertEqual(self.launched, [False])
        self.assertEqual(self.warned, [None])

    def test_config_vanishing_between_the_two_reads_still_launches(self):
        """confirm_incoming_sync() and main() read sync_settings() seconds to
        minutes apart, across the whole sync wait. load_config() swallows
        OSError and ValueError, so a savepick.json that becomes unreadable or
        half-written in between reads back as {}, and sync_settings() then
        returns None even though confirm_incoming_sync() already returned
        True. main() must treat that exactly like an unconfirmed sync: warn,
        launch without a restore, and still back up on exit. It must NOT
        raise trying to use an empty config, and the game must still start.
        """
        savepick.confirm_incoming_sync = lambda: True
        savepick.sync_settings = lambda: None
        backed = []
        savepick.backup_on_exit = lambda game: backed.append(game)
        code = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [False])
        self.assertEqual(self.warned, [True])
        self.assertEqual(self.restored, [])
        self.assertEqual(backed, ["Dark Souls II"])

    def test_a_confirmed_sync_lets_a_newer_backup_restore(self):
        # live is missing, so decide() restores. savepick does that restore
        # itself and then launches with no restore, because wrap returning
        # nonzero over a file it does not care about must not stop the game.
        savepick.confirm_incoming_sync = lambda: True
        savepick.live_save_files = lambda game: []
        self._run()
        self.assertEqual(self.restored, [("Dark Souls II", 9000.0, "b1")])
        self.assertEqual(self.launched, [False])
        self.assertEqual(self.warned, [])

    def test_a_restore_that_never_landed_says_so(self):
        savepick.confirm_incoming_sync = lambda: True
        savepick.live_save_files = lambda game: []
        savepick.restore_now = lambda game, mtime, name, peer_dir: False
        self._run()
        self.assertEqual(self.incomplete, ["Dark Souls II"])
        self.assertEqual(self.launched, [False])

    def test_pressing_B_restores_through_savepick_too(self):
        # Every restore goes through savepick now, the B path included, so
        # wrap can never hold a veto over the game starting.
        savepick.confirm_incoming_sync = lambda: True
        savepick.newest_backup_info = lambda game, names, cfg: (
            "Windows PC backup", 100.0, "b1", Path("/peer"))
        savepick.newest_live_mtime = lambda paths: 9000.0
        savepick.ask_user = lambda *a: True
        self._run()
        self.assertEqual(self.restored, [("Dark Souls II", 100.0, "b1")])
        self.assertEqual(self.launched, [False])

    def test_keeping_the_newest_never_restores(self):
        savepick.confirm_incoming_sync = lambda: True
        savepick.newest_backup_info = lambda game, names, cfg: (
            "Windows PC backup", 100.0, "b1", Path("/peer"))
        savepick.newest_live_mtime = lambda paths: 9000.0
        savepick.ask_user = lambda *a: False
        self._run()
        self.assertEqual(self.restored, [])
        self.assertEqual(self.launched, [False])

    def test_it_still_backs_up_on_exit_after_an_unconfirmed_launch(self):
        savepick.confirm_incoming_sync = lambda: None
        backed = []
        savepick.backup_on_exit = lambda game: backed.append(game)
        self._run()
        self.assertEqual(backed, ["Dark Souls II"])

    def test_main_forwards_the_real_config_and_the_chosen_peer_dir(self):
        """A stub that only accepts the new arity proves nothing.

        Both wires matter: newest_backup_info must receive the config main()
        actually read (not None, not {}), and restore_now must receive the
        exact peer directory newest_backup_info picked, not a different one
        and not None. This is the test Task 3 should have broken and did not.
        """
        sentinel_cfg = {"url": "http://hub", "apikey": "k", "folder": "gamesaves",
                         "hub_id": "nas", "device_dir": "windows-pc"}
        savepick.sync_settings = lambda: sentinel_cfg
        savepick.confirm_incoming_sync = lambda: True
        savepick.live_save_files = lambda game: []  # live missing -> restore

        chosen_peer = Path("/mnt/gamesaves/steam-deck")
        seen_cfg = []

        def fake_newest_backup_info(game, names, cfg):
            seen_cfg.append(cfg)
            return "Steam Deck backup", 9000.0, "b1", chosen_peer

        savepick.newest_backup_info = fake_newest_backup_info

        seen_peer = []

        def fake_restore_now(game, mtime, name, peer_dir):
            seen_peer.append(peer_dir)
            return True

        savepick.restore_now = fake_restore_now

        self._run()

        self.assertEqual(seen_cfg, [sentinel_cfg])
        self.assertTrue(seen_cfg[0])
        self.assertEqual(seen_peer, [chosen_peer])
        self.assertIsNotNone(seen_peer[0])


class TestSaveLanded(unittest.TestCase):
    """After a restore, one question matters: is the save the one we meant?

    ludusavi returns nonzero when ANY entry fails. On 2026-09-11 at 00:27 the
    Deck's backup carried three Steam screenshots at Deck-only paths
    (/home/deck/...), which have no home on Windows. The .sl2 restored
    perfectly. wrap still exited 1, showed "Failed to restore save data", and
    never started the game.
    """

    def setUp(self):
        self.real_live = savepick.live_save_files
        self.real_mtime = savepick.newest_live_mtime

    def tearDown(self):
        savepick.live_save_files = self.real_live
        savepick.newest_live_mtime = self.real_mtime

    def _live(self, mtime):
        savepick.live_save_files = lambda game: ["/x/save.sl2"]
        savepick.newest_live_mtime = lambda paths: mtime

    def test_a_matching_save_landed(self):
        self._live(9000.0)
        self.assertTrue(savepick.save_landed("G", 9000.0))

    def test_within_the_tolerance_still_landed(self):
        self._live(9001.0)
        self.assertTrue(savepick.save_landed("G", 9000.0))

    def test_an_unchanged_live_save_did_not_land(self):
        self._live(100.0)
        self.assertFalse(savepick.save_landed("G", 9000.0))

    def test_no_backup_time_is_never_a_pass(self):
        self._live(9000.0)
        self.assertFalse(savepick.save_landed("G", None))

    def test_no_live_save_is_never_a_pass(self):
        self._live(None)
        self.assertFalse(savepick.save_landed("G", 9000.0))


class TestRestoreNow(unittest.TestCase):
    """savepick runs the restore itself so it can see what actually failed."""

    def setUp(self):
        self.real_run = savepick.run_json
        self.real_landed = savepick.save_landed
        self.calls = []

    def tearDown(self):
        savepick.run_json = self.real_run
        savepick.save_landed = self.real_landed

    def _wire(self, data, landed):
        def run_json(args, **kwargs):
            self.calls.append(args)
            return data
        savepick.run_json = run_json
        savepick.save_landed = lambda game, mtime: landed

    def test_cosmetic_failures_do_not_stop_the_game(self):
        data = {"games": {"G": {"files": {
            "/home/deck/.local/share/Steam/userdata/1/760/remote/1/screenshots/a.jpg":
                {"failed": True, "error": {"message": "no such path"}},
            "C:/Users/player/AppData/Roaming/DarkSoulsII/x/DS2SOFS0000.sl2":
                {"change": "Different"}}}}}
        self._wire(data, landed=True)
        self.assertTrue(savepick.restore_now("G", 9000.0, "backup-1", Path("/peer")))

    def test_a_save_that_never_landed_is_reported(self):
        self._wire({"games": {"G": {"files": {}}}}, landed=False)
        self.assertFalse(savepick.restore_now("G", 9000.0, "backup-1", Path("/peer")))

    def test_it_restores_the_exact_backup_it_compared(self):
        # wrap always takes the newest. If a sync lands between the comparison
        # and the launch, that is a different backup than the one savepick
        # showed him.
        self._wire({"games": {"G": {"files": {}}}}, landed=True)
        savepick.restore_now("G", 9000.0, "backup-20260911T044403Z", Path("/peer"))
        args = self.calls[0]
        self.assertIn("--backup", args)
        self.assertEqual(args[args.index("--backup") + 1],
                         "backup-20260911T044403Z")
        self.assertIn("--force", args)
        self.assertIn("--api", args)

    def test_a_single_backup_directory_needs_no_backup_flag(self):
        self._wire({"games": {"G": {"files": {}}}}, landed=True)
        savepick.restore_now("G", 9000.0, ".", Path("/peer"))
        self.assertNotIn("--backup", self.calls[0])

    def test_no_answer_from_ludusavi_still_checks_the_save(self):
        # Fail-safe: a crashed ludusavi must not be read as a restore.
        self._wire(None, landed=False)
        self.assertFalse(savepick.restore_now("G", 9000.0, "backup-1", Path("/peer")))


class TestRestoreNowPinsThePath(unittest.TestCase):
    def setUp(self):
        self.real_run = savepick.run_json
        self.real_landed = savepick.save_landed
        self.seen = []

        def run_json(args, timeout=None):
            self.seen.append(list(args))
            return {"games": {}}
        savepick.run_json = run_json
        savepick.save_landed = lambda *_a, **_k: True

    def tearDown(self):
        savepick.run_json = self.real_run
        savepick.save_landed = self.real_landed

    def test_passes_both_the_peer_path_and_the_backup_id(self):
        savepick.restore_now("DS2", 100.0, "backup-A", Path("/saves/desktop"))
        args = self.seen[0]
        self.assertEqual(args[args.index("--path") + 1], "/saves/desktop")
        self.assertEqual(args[args.index("--backup") + 1], "backup-A")
        self.assertIn("--force", args)
        self.assertIn("--no-manifest-update", args)

    def test_a_dot_backup_name_sends_no_backup_flag(self):
        savepick.restore_now("DS2", 100.0, ".", Path("/saves/desktop"))
        self.assertNotIn("--backup", self.seen[0])
        self.assertEqual(self.seen[0][self.seen[0].index("--path") + 1],
                         "/saves/desktop")

    def test_no_peer_dir_means_no_restore(self):
        landed = savepick.restore_now("DS2", 100.0, "backup-A", None)
        self.assertFalse(landed)
        self.assertEqual(self.seen, [])


class TestLaunchRunsTheGameDirectly(unittest.TestCase):
    """savepick starts the game itself. ludusavi is not in the launch path.

    wrap had nothing left to do once savepick took over the restore and the
    backup, and it carried two ways to stop the game starting:

      It returns nonzero when ANY entry of a save job fails. On 2026-09-11
      three Steam screenshots in the Deck's backup had no home on Windows, so
      wrap exited 1 on a restore that had actually worked, and DS2 never ran.

      With `--infer steam` and no SteamAppId in the environment it cannot work
      out the game, and it blocks on a GUI prompt FOREVER. Proven on the Deck
      2026-09-11: the command never ran and it had to be killed at 20 s.
      savepick's own no-SteamAppId path went straight into that.
    """

    def setUp(self):
        self.real_popen = savepick.subprocess.Popen
        self.cmds = []
        self.code = 0
        test = self

        class FakeProc(object):
            def __init__(self, cmd):
                test.cmds.append(cmd)
                self.signals = []
                self.killed = False

            def wait(self, timeout=None):
                return test.code

            def poll(self):
                return test.code

            def send_signal(self, signum):
                self.signals.append(signum)

            def kill(self):
                self.killed = True

        self.FakeProc = FakeProc
        savepick.subprocess.Popen = lambda cmd, *a, **k: FakeProc(cmd)
        savepick.STOP_DEADLINE = []
        del savepick.SIGNALS_SEEN[:]

    def tearDown(self):
        savepick.subprocess.Popen = self.real_popen
        savepick.CHILD = None
        savepick.STOP_DEADLINE = []
        del savepick.SIGNALS_SEEN[:]

    def test_it_runs_the_game_command_and_nothing_else(self):
        savepick.launch(["/game.exe", "--windowed"])
        self.assertEqual(self.cmds, [["/game.exe", "--windowed"]])

    def test_ludusavi_is_not_in_the_launch_path(self):
        savepick.launch(["/game.exe"])
        flat = " ".join(self.cmds[0])
        self.assertNotIn("ludusavi", flat)
        self.assertNotIn("wrap", flat)

    def test_it_returns_the_games_own_exit_code(self):
        self.code = 3
        self.assertEqual(savepick.launch(["/game.exe"]), 3)

    def test_a_game_that_will_not_start_is_reported_not_swallowed(self):
        def popen(cmd, *a, **k):
            raise OSError("no such file")
        savepick.subprocess.Popen = popen
        self.assertNotEqual(savepick.launch(["/game.exe"]), 0)

    def test_the_running_game_is_reachable_while_it_runs(self):
        """A signal handler has to be able to reach the game, so launch holds it."""
        seen = []

        class Watcher(self.FakeProc):
            def wait(self, timeout=None):
                seen.append(savepick.CHILD)
                return 0

        savepick.subprocess.Popen = lambda cmd, *a, **k: Watcher(cmd)
        savepick.launch(["/game.exe"])
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertIsNone(savepick.CHILD)


class TestDowngradeNotice(unittest.TestCase):
    """Answering B restores a save older than the live one. Say where the old one went."""

    def setUp(self):
        self.saved = {name: getattr(savepick, name) for name in
                      ("confirm_incoming_sync", "launch", "backup_on_exit",
                       "game_name_for_appid", "live_save_files",
                       "newest_backup_info", "ask_user", "snapshot_live_saves",
                       "restore_now", "warn_restore_incomplete",
                       "newest_live_mtime", "warn_downgraded", "sync_settings")}
        self.launched = []
        self.notices = []
        savepick.launch = lambda cmd, do_restore=False: self.launched.append(do_restore) or 0
        savepick.backup_on_exit = lambda game: None
        savepick.game_name_for_appid = lambda appid: "Dark Souls II"
        savepick.live_save_files = lambda game: ["/x/save.sl2"]
        savepick.snapshot_live_saves = lambda *a, **k: True
        savepick.restore_now = lambda game, mtime, name, peer_dir: True
        savepick.warn_restore_incomplete = lambda game: None
        savepick.warn_downgraded = lambda game: self.notices.append(game)
        self.real_spinner = savepick.Spinner
        savepick.Spinner = lambda *a, **k: _NullSpinner()
        savepick.confirm_incoming_sync = lambda: True
        # A confirmed sync implies sync_settings() held a real config a
        # moment ago; stub it truthy so this class does not depend on
        # whatever savepick.json happens to hold on the machine running
        # the suite.
        savepick.sync_settings = lambda: {
            "url": "http://hub", "apikey": "k", "folder": "gamesaves",
            "hub_id": "nas", "device_dir": "windows-pc"}
        os.environ["SteamAppId"] = "335300"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(savepick, name, value)
        savepick.Spinner = self.real_spinner
        os.environ.pop("SteamAppId", None)

    def _run(self):
        return savepick.main(["--", "/bin/true"])

    def test_pressing_B_restores_and_says_where_the_newer_save_went(self):
        savepick.newest_backup_info = lambda g, n, cfg: (
            "Steam Deck backup", 100.0, "b1", Path("/peer"))
        savepick.newest_live_mtime = lambda paths: 9000.0
        savepick.ask_user = lambda *a: True
        self._run()
        self.assertEqual(self.notices, ["Dark Souls II"])
        self.assertEqual(self.launched, [False])

    def test_keeping_the_newest_says_nothing_and_does_not_restore(self):
        savepick.newest_backup_info = lambda g, n, cfg: (
            "Steam Deck backup", 100.0, "b1", Path("/peer"))
        savepick.newest_live_mtime = lambda paths: 9000.0
        savepick.ask_user = lambda *a: False
        restored = []
        savepick.restore_now = lambda game, mtime, name, peer_dir: restored.append(game) or True
        self._run()
        self.assertEqual(self.notices, [])
        self.assertEqual(restored, [])

    def test_an_everyday_restore_shows_no_notice(self):
        savepick.newest_backup_info = lambda g, n, cfg: (
            "Steam Deck backup", 9000.0, "b1", Path("/peer"))
        savepick.newest_live_mtime = lambda paths: 100.0
        self._run()
        self.assertEqual(self.notices, [])
        self.assertEqual(self.launched, [False])


class TestTheGameAlwaysStarts(unittest.TestCase):
    """The one promise savepick makes to the launch path.

    Every early exit in main() must still end with the game running. The
    no-SteamAppId path used to hand the command to `wrap --infer steam`, which
    cannot infer without the id and blocks on a GUI prompt with no timeout.
    """

    def setUp(self):
        self.saved = {name: getattr(savepick, name) for name in
                      ("confirm_incoming_sync", "launch", "backup_on_exit",
                       "game_name_for_appid", "warn_unconfirmed")}
        self.launched = []
        savepick.launch = lambda cmd, do_restore=False: self.launched.append(cmd) or 0
        savepick.backup_on_exit = lambda game: None
        savepick.warn_unconfirmed = lambda synced: None
        savepick.confirm_incoming_sync = lambda: None
        os.environ.pop("SteamAppId", None)
        os.environ.pop("SteamGameId", None)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(savepick, name, value)
        os.environ.pop("SteamAppId", None)
        os.environ.pop("SteamGameId", None)

    def test_no_steam_app_id_still_starts_the_game(self):
        savepick.main(["--", "/bin/true", "--windowed"])
        self.assertEqual(self.launched, [["/bin/true", "--windowed"]])

    def test_an_unknown_steam_id_still_starts_the_game(self):
        os.environ["SteamAppId"] = "999999"
        savepick.game_name_for_appid = lambda appid: None
        savepick.main(["--", "/bin/true"])
        self.assertEqual(self.launched, [["/bin/true"]])

    def test_an_unreadable_syncthing_still_starts_the_game(self):
        os.environ["SteamAppId"] = "335300"
        savepick.game_name_for_appid = lambda appid: "Dark Souls II"
        savepick.main(["--", "/bin/true"])
        self.assertEqual(self.launched, [["/bin/true"]])

    def test_no_game_command_is_the_only_case_that_does_not_launch(self):
        self.assertEqual(savepick.main(["--"]), 2)
        self.assertEqual(self.launched, [])


class TestNothingWaitsOnStdin(unittest.TestCase):
    """Every ludusavi call must close its stdin, or it can block forever.

    ludusavi's `find` accepts game names on stdin as an alternative to
    arguments, so it reads stdin when stdin is a pipe. savepick never set it,
    so the child inherited whatever Steam handed savepick. Measured on DESKTOP
    on 2026-09-11: the same `find --steam-id` that takes 1 second from a
    console took the full 120 second timeout from Python, for a known id and an
    unknown one alike, because it sat waiting on an inherited pipe. That is a
    hang in the launch path, before the game starts.

    subprocess.DEVNULL gives it EOF at once.
    """

    def setUp(self):
        self.real_run = savepick.subprocess.run
        self.kwargs = []

        class Done:
            returncode = 0
            stdout = '{"games": {}}'
            stderr = ""

        def run(cmd, **kwargs):
            self.kwargs.append(kwargs)
            return Done()

        savepick.subprocess.run = run

    def tearDown(self):
        savepick.subprocess.run = self.real_run

    def test_run_json_closes_stdin(self):
        savepick.run_json(["backups", "--api", "G"])
        self.assertIs(self.kwargs[0].get("stdin"), savepick.subprocess.DEVNULL)

    def test_the_exit_backup_closes_stdin(self):
        savepick.run_backup("G")
        self.assertIs(self.kwargs[0].get("stdin"), savepick.subprocess.DEVNULL)


class _FakeDialogProc:
    """A PowerShell dialog that stays up until something terminates it."""

    def __init__(self):
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        self.returncode = 0
        return "KEEP", ""


class TestWindowsDialogReadsThePad(unittest.TestCase):
    """2026-09-11: the PC dialog answered only to a mouse. ask_windows ran the
    PowerShell script with a blocking subprocess.run, so nothing ever polled
    XInput while the window was up. The pad watcher existed and was unreachable."""

    def setUp(self):
        self.real_windows = savepick.is_windows
        self.real_popen = savepick.subprocess.Popen
        self.real_buttons = savepick.xinput_buttons
        self.real_winmm = savepick.winmm_buttons
        self.real_sleep = savepick.time.sleep
        self.proc = _FakeDialogProc()
        savepick.is_windows = lambda: True
        savepick.subprocess.Popen = lambda *a, **k: self.proc
        savepick.time.sleep = lambda _s: None

    def tearDown(self):
        savepick.is_windows = self.real_windows
        savepick.subprocess.Popen = self.real_popen
        savepick.xinput_buttons = self.real_buttons
        savepick.winmm_buttons = self.real_winmm
        savepick.time.sleep = self.real_sleep

    def _pad(self, presses):
        seq = list(presses)
        savepick.xinput_buttons = lambda: seq.pop(0) if seq else 0
        savepick.winmm_buttons = lambda: None

    def _winmm_pad(self, presses):
        seq = list(presses)
        savepick.xinput_buttons = lambda: None
        savepick.winmm_buttons = lambda: seq.pop(0) if seq else 0

    def _ask(self):
        return savepick.ask_windows("Dark Souls II", "Sep 11, 2026  2:07 AM",
                                    "Steam Deck backup", "Sep 10, 2026  11:43 PM")

    def test_b_on_the_pad_restores(self):
        self._pad([0, savepick.XINPUT_GAMEPAD_B])
        self.assertIs(self._ask(), True)
        self.assertTrue(self.proc.terminated)

    def test_a_on_the_pad_keeps(self):
        self._pad([0, savepick.XINPUT_GAMEPAD_A])
        self.assertIs(self._ask(), False)
        self.assertTrue(self.proc.terminated)

    def test_a_still_held_from_the_last_game_is_not_an_answer(self):
        # The press that quit the game must not answer the dialog on sight.
        self._pad([savepick.XINPUT_GAMEPAD_A, 0, savepick.XINPUT_GAMEPAD_B])
        self.assertIs(self._ask(), True)

    def test_no_pad_at_all_falls_back_to_the_windows_buttons(self):
        savepick.xinput_buttons = lambda: None
        savepick.winmm_buttons = lambda: None
        self.assertIs(self._ask(), False)   # the fake dialog prints KEEP
        self.assertFalse(self.proc.terminated)

    def test_a_directinput_pad_answers_through_winmm(self):
        # XInput lists nothing. The legacy joystick API still sees the pad.
        self._winmm_pad([0, savepick.WINMM_B])
        self.assertIs(self._ask(), True)
        self.assertTrue(self.proc.terminated)

    def test_winmm_answers_while_xinput_sits_there_connected(self):
        # Steam Input can leave XInput reporting a pad whose buttons never move.
        seq = [0, savepick.WINMM_A]
        savepick.xinput_buttons = lambda: 0
        savepick.winmm_buttons = lambda: seq.pop(0) if seq else 0
        self.assertIs(self._ask(), False)
        self.assertTrue(self.proc.terminated)


class TestFolderRoot(unittest.TestCase):
    CFG = {"url": "http://x", "apikey": "k", "folder": "gamesaves",
           "hub_id": "HUB", "device_dir": "deck"}

    def setUp(self):
        self.real = savepick.syncthing_get

    def tearDown(self):
        savepick.syncthing_get = self.real

    def test_reads_the_path_from_syncthing(self):
        savepick.syncthing_get = lambda _c, _p: {"path": "/home/deck/GameSaves/gamesaves"}
        self.assertEqual(savepick.folder_root(self.CFG),
                         Path("/home/deck/GameSaves/gamesaves"))

    def test_expands_a_tilde(self):
        savepick.syncthing_get = lambda _c, _p: {"path": "~/GameSaves/gamesaves"}
        self.assertEqual(savepick.folder_root(self.CFG),
                         Path.home() / "GameSaves" / "gamesaves")

    def test_unreadable_syncthing_is_none_not_a_guess(self):
        savepick.syncthing_get = lambda _c, _p: None
        self.assertIsNone(savepick.folder_root(self.CFG))

    def test_a_folder_with_no_path_is_none(self):
        savepick.syncthing_get = lambda _c, _p: {"id": "gamesaves"}
        self.assertIsNone(savepick.folder_root(self.CFG))

    def test_a_config_with_no_folder_key_is_none_not_a_crash(self):
        # Defence in depth: a caller that reaches here with an empty or
        # half-read config must fail safe, never raise KeyError.
        called = []
        savepick.syncthing_get = lambda c, p: called.append(p) or None
        self.assertIsNone(savepick.folder_root({}))
        self.assertEqual(called, [])


class TestSyncthingGetMissingConfig(unittest.TestCase):
    """A missing url or apikey must resolve to None like any other failure."""

    def test_no_url_is_none_not_a_crash(self):
        self.assertIsNone(savepick.syncthing_get({}, "/rest/system/connections"))

    def test_no_url_never_opens_a_connection(self):
        real_urlopen = savepick.urllib.request.urlopen
        called = []
        savepick.urllib.request.urlopen = lambda *a, **k: called.append(1) or (_ for _ in ()).throw(
            AssertionError("urlopen should not be called with no url"))
        try:
            self.assertIsNone(savepick.syncthing_get({"apikey": "k"}, "/x"))
        finally:
            savepick.urllib.request.urlopen = real_urlopen
        self.assertEqual(called, [])


class TestPeerDirs(unittest.TestCase):
    def setUp(self):
        self.real = savepick.folder_root
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        savepick.folder_root = self.real
        self.tmp.cleanup()

    def _wire(self):
        savepick.folder_root = lambda _cfg: self.root

    def test_skips_this_devices_own_directory(self):
        for name in ("deck", "desktop"):
            (self.root / name).mkdir()
        self._wire()
        names = [p.name for p in savepick.peer_dirs({"device_dir": "deck"})]
        self.assertEqual(names, ["desktop"])

    def test_skips_its_own_directory_case_insensitively(self):
        # NTFS folds case. device_dir "Desktop" against an on-disk "desktop"
        # must still read as this device's own directory, not a peer.
        for name in ("desktop", "deck"):
            (self.root / name).mkdir()
        self._wire()
        names = [p.name for p in savepick.peer_dirs({"device_dir": "Desktop"})]
        self.assertEqual(names, ["deck"])

    def test_finds_every_other_device(self):
        for name in ("deck", "desktop", "ally"):
            (self.root / name).mkdir()
        self._wire()
        names = [p.name for p in savepick.peer_dirs({"device_dir": "deck"})]
        self.assertEqual(names, ["ally", "desktop"])

    def test_skips_syncthing_and_versioning_directories(self):
        for name in ("desktop", ".stfolder", ".stversions"):
            (self.root / name).mkdir()
        self._wire()
        names = [p.name for p in savepick.peer_dirs({"device_dir": "deck"})]
        self.assertEqual(names, ["desktop"])

    def test_skips_loose_files(self):
        (self.root / "desktop").mkdir()
        (self.root / "README.txt").write_text("hi")
        self._wire()
        names = [p.name for p in savepick.peer_dirs({"device_dir": "deck"})]
        self.assertEqual(names, ["desktop"])

    def test_no_folder_root_is_an_empty_list_not_a_crash(self):
        savepick.folder_root = lambda _cfg: None
        self.assertEqual(savepick.peer_dirs({"device_dir": "deck"}), [])

    def test_a_missing_directory_on_disk_is_an_empty_list(self):
        savepick.folder_root = lambda _cfg: self.root / "gone"
        self.assertEqual(savepick.peer_dirs({"device_dir": "deck"}), [])


class TestNewestBackupInfo(unittest.TestCase):
    CFG = {"device_dir": "deck",
           "device_names": {"deck": "Steam Deck", "desktop": "Windows PC"}}

    def setUp(self):
        self.real_peers = savepick.peer_dirs
        self.real_run = savepick.run_json
        self.real_mtime = savepick.newest_mtime_of
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        savepick.peer_dirs = self.real_peers
        savepick.run_json = self.real_run
        savepick.newest_mtime_of = self.real_mtime
        self.tmp.cleanup()

    def _peers(self, *names):
        dirs = []
        for name in names:
            d = self.root / name
            d.mkdir()
            dirs.append(d)
        savepick.peer_dirs = lambda _cfg: dirs
        return dirs

    def _answers(self, by_path):
        """by_path maps a peer directory name to its ludusavi backups payload."""
        def run_json(args, timeout=None):
            path = args[args.index("--path") + 1]
            return by_path.get(Path(path).name)
        savepick.run_json = run_json

    @staticmethod
    def _payload(game, backup_path, name):
        return {"games": {game: {"backupPath": backup_path,
                                 "backups": [{"name": name}]}}}

    def test_one_peer_behaves_as_before(self):
        (desktop,) = self._peers("desktop")
        self._answers({"desktop": self._payload("DS2", str(desktop), "backup-A")})
        savepick.newest_mtime_of = lambda _d, _b: 100.0
        label, mtime, name, peer = savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertEqual(label, "Windows PC backup")
        self.assertEqual(mtime, 100.0)
        self.assertEqual(name, "backup-A")
        self.assertEqual(peer, desktop)

    def test_takes_the_newest_across_three_peers(self):
        ally, desktop, tower = self._peers("ally", "desktop", "tower")
        self._answers({
            "ally": self._payload("DS2", str(ally), "backup-ally"),
            "desktop": self._payload("DS2", str(desktop), "backup-desktop"),
            "tower": self._payload("DS2", str(tower), "backup-tower"),
        })
        times = {"backup-ally": 100.0, "backup-desktop": 300.0,
                 "backup-tower": 200.0}
        savepick.newest_mtime_of = lambda d, _b: times[Path(d).name]
        label, mtime, name, peer = savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertEqual(mtime, 300.0)
        self.assertEqual(name, "backup-desktop")
        self.assertEqual(peer, desktop)
        self.assertEqual(label, "Windows PC backup")

    def test_an_unnamed_peer_is_labelled_by_its_directory(self):
        (ally,) = self._peers("ally")
        self._answers({"ally": self._payload("DS2", str(ally), "backup-A")})
        savepick.newest_mtime_of = lambda _d, _b: 100.0
        label, _mtime, _name, _peer = savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertEqual(label, "ally backup")

    def test_a_peer_with_no_backup_for_this_game_is_skipped(self):
        ally, desktop = self._peers("ally", "desktop")
        self._answers({"ally": {"games": {}},
                       "desktop": self._payload("DS2", str(desktop), "backup-A")})
        savepick.newest_mtime_of = lambda _d, _b: 100.0
        _label, mtime, name, peer = savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertEqual((mtime, name, peer), (100.0, "backup-A", desktop))

    def test_an_unreadable_peer_is_skipped_not_fatal(self):
        ally, desktop = self._peers("ally", "desktop")
        self._answers({"ally": None,
                       "desktop": self._payload("DS2", str(desktop), "backup-A")})
        savepick.newest_mtime_of = lambda _d, _b: 100.0
        _label, mtime, _name, peer = savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertEqual((mtime, peer), (100.0, desktop))

    def test_an_unreadable_peer_is_named_in_the_log(self):
        # The dialog can end up naming a different device with no hint that a
        # newer save on the skipped one went unread. Say so in the log.
        real_log = savepick.log
        logged = []
        savepick.log = logged.append
        try:
            ally, desktop = self._peers("ally", "desktop")
            self._answers({"ally": None,
                           "desktop": self._payload("DS2", str(desktop), "backup-A")})
            savepick.newest_mtime_of = lambda _d, _b: 100.0
            savepick.newest_backup_info("DS2", set(), self.CFG)
        finally:
            savepick.log = real_log
        self.assertTrue(any("ally" in msg for msg in logged),
                        "expected a log line naming the skipped peer 'ally': %r" % logged)

    def test_a_backup_with_no_readable_save_time_is_skipped(self):
        ally, desktop = self._peers("ally", "desktop")
        self._answers({
            "ally": self._payload("DS2", str(ally), "backup-ally"),
            "desktop": self._payload("DS2", str(desktop), "backup-desktop"),
        })
        times = {"backup-ally": None, "backup-desktop": 100.0}
        savepick.newest_mtime_of = lambda d, _b: times[Path(d).name]
        _label, mtime, name, _peer = savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertEqual((mtime, name), (100.0, "backup-desktop"))

    def test_no_peers_at_all_is_all_none(self):
        savepick.peer_dirs = lambda _cfg: []
        self.assertEqual(savepick.newest_backup_info("DS2", set(), self.CFG),
                         (None, None, None, None))

    def test_the_backups_call_is_pinned_to_the_peer_path(self):
        (desktop,) = self._peers("desktop")
        seen = []

        def run_json(args, timeout=None):
            seen.append(list(args))
            return self._payload("DS2", str(desktop), "backup-A")
        savepick.run_json = run_json
        savepick.newest_mtime_of = lambda _d, _b: 100.0
        savepick.newest_backup_info("DS2", set(), self.CFG)
        self.assertIn("--path", seen[0])
        self.assertEqual(seen[0][seen[0].index("--path") + 1], str(desktop))
        self.assertIn("--no-manifest-update", seen[0])


class TestUniversalWording(unittest.TestCase):
    def test_the_conflict_dialog_says_device(self):
        text = savepick.conflict_text("DS2", "Sep 3, 2026  4:13 PM",
                                      "Windows PC backup",
                                      "Sep 2, 2026  3:36 PM")
        self.assertIn("This device:", text)
        self.assertIn("the save on this device", text)
        self.assertIn("A = keep this device.", text)
        self.assertNotIn("machine", text)

    def test_the_peer_row_keeps_its_real_name(self):
        text = savepick.conflict_text("DS2", "a", "Windows PC backup", "b")
        self.assertIn("Windows PC backup:", text)

    def test_the_keep_label_says_device(self):
        self.assertIn("device", savepick.KEEP_LABEL)
        self.assertNotIn("machine", savepick.KEEP_LABEL)

    def test_no_user_visible_string_hardcodes_a_product_name(self):
        """Docstrings and comments keep their history. Live strings do not.

        The spec is explicit: a comment recording that the Deck's backup held
        three Steam screenshots is evidence of a real failure and stays. A
        string savepick can print does not.
        """
        import ast
        source = Path(savepick.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)

        banned = ("Steam Deck", "Windows PC", "the other machine",
                  "this machine")
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if text in docstrings or text in savepick.GAMEPAD_HINTS:
                continue
            if any(name in text for name in banned):
                offenders.append((node.lineno, text[:60]))
        self.assertEqual(offenders, [])


class TestTreeConfig(unittest.TestCase):
    """Tree roots are read from config, never guessed from the operating system."""

    def test_backup_prefix_for_a_windows_root(self):
        self.assertEqual(
            savepick.backup_prefix("C:/Users/player/Apps/RetroBat/saves"),
            "drive-C/Users/player/Apps/RetroBat/saves")

    def test_backup_prefix_accepts_backslashes(self):
        self.assertEqual(
            savepick.backup_prefix(r"C:\Users\player\Apps\RetroBat\saves"),
            "drive-C/Users/player/Apps/RetroBat/saves")

    def test_backup_prefix_uppercases_the_drive_letter(self):
        self.assertEqual(savepick.backup_prefix("d:/games/saves"),
                         "drive-D/games/saves")

    def test_backup_prefix_for_a_posix_root(self):
        self.assertEqual(
            savepick.backup_prefix("/run/media/mmcblk0p1/retrodeck/saves"),
            "drive-0/run/media/mmcblk0p1/retrodeck/saves")

    def test_backup_prefix_ignores_a_trailing_slash(self):
        self.assertEqual(savepick.backup_prefix("/a/b/"), "drive-0/a/b")

    def test_tree_roots_missing_is_empty(self):
        with _config({}):
            self.assertEqual(savepick.tree_roots("RetroFrontend"), {})

    def test_tree_roots_reads_the_named_set(self):
        cfg = {"trees": {"RetroFrontend": {"roots": {"deck": "/a/saves"}}}}
        with _config(cfg):
            self.assertEqual(savepick.tree_roots("RetroFrontend"),
                             {"deck": "/a/saves"})

    def test_local_tree_root_uses_this_device_directory(self):
        cfg = {"trees": {"RetroFrontend": {"roots": {"deck": "/a/saves",
                                                     "desktop": "C:/b/saves"}}}}
        with _config(cfg):
            root = savepick.local_tree_root("RetroFrontend", {"device_dir": "deck"})
        self.assertEqual(root, Path("/a/saves"))

    def test_local_tree_root_is_none_when_this_device_has_no_entry(self):
        cfg = {"trees": {"RetroFrontend": {"roots": {"desktop": "C:/b/saves"}}}}
        with _config(cfg):
            self.assertIsNone(
                savepick.local_tree_root("RetroFrontend", {"device_dir": "deck"}))


class TestTreeIndex(unittest.TestCase):
    """index_tree walks a directory. It reports relative paths, never absolute ones."""

    def test_index_is_empty_for_a_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(savepick.index_tree(Path(tmp) / "nope"), {})

    def test_index_reports_forward_slash_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "psx").mkdir()
            (root / "psx" / "Alundra.srm").write_text("x")
            self.assertEqual(list(savepick.index_tree(root)), ["psx/Alundra.srm"])

    def test_index_records_the_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a.srm"
            target.write_text("x")
            os.utime(target, (500.0, 500.0))
            self.assertEqual(savepick.index_tree(root)["a.srm"], 500.0)

    def test_index_skips_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "psx").mkdir()
            self.assertEqual(savepick.index_tree(root), {})


class TestRestorable(unittest.TestCase):
    """Only battery saves and memory cards cross devices. Save states never do."""

    def test_a_battery_save_is_restorable(self):
        self.assertTrue(savepick.is_restorable("snes/ActRaiser (U) [!].srm"))

    def test_a_memory_card_is_restorable(self):
        self.assertTrue(savepick.is_restorable("psx/Alundra.mcr"))

    def test_every_allowed_extension_is_restorable(self):
        for ext in savepick.TREE_RESTORE_EXTENSIONS:
            self.assertTrue(savepick.is_restorable("psx/game.%s" % ext), ext)

    def test_an_auto_save_state_is_not_restorable(self):
        self.assertFalse(savepick.is_restorable("snes/game.state.auto"))

    def test_a_numbered_save_state_is_not_restorable(self):
        self.assertFalse(savepick.is_restorable("snes/game.state1"))

    def test_a_screenshot_is_not_restorable(self):
        self.assertFalse(savepick.is_restorable("snes/game.png"))

    def test_a_freefilesync_database_is_not_restorable(self):
        self.assertFalse(savepick.is_restorable("snes/sync.ffs_db"))

    def test_an_unknown_extension_is_not_restorable(self):
        # bin, dat and metadata are deliberately off the list until someone
        # checks which emulator writes each one. They are still backed up.
        for name in ("psx/card.bin", "psx/card.dat", "psx/card.metadata"):
            self.assertFalse(savepick.is_restorable(name), name)

    def test_a_file_with_no_extension_is_not_restorable(self):
        self.assertFalse(savepick.is_restorable("snes/README"))

    def test_the_extension_check_ignores_case(self):
        self.assertTrue(savepick.is_restorable("snes/game.SRM"))


class TestPeerTreeIndex(unittest.TestCase):
    """Every peer is read. The newest copy of a path anywhere wins."""

    def _peer(self, tmp, name, backup, files):
        """Build one peer directory holding one ludusavi backup."""
        peer = Path(tmp) / name
        base = peer / "RetroFrontend" / backup / "drive-0" / "a" / "saves"
        base.mkdir(parents=True)
        for rel, mtime in files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")
            os.utime(target, (mtime, mtime))
        return peer

    def _patch(self, peers, backups_by_peer):
        """Stand in for peer_dirs and the ludusavi backups call."""
        savepick.peer_dirs = lambda _cfg: peers
        savepick.run_json = lambda args, timeout=120: backups_by_peer.get(
            Path(args[args.index("--path") + 1]).name)

    def setUp(self):
        self._saved = (savepick.peer_dirs, savepick.run_json)

    def tearDown(self):
        savepick.peer_dirs, savepick.run_json = self._saved

    def test_a_peer_without_a_recorded_root_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = self._peer(tmp, "desktop", "backup-1", {"psx/a.srm": 100.0})
            self._patch([peer], {"desktop": {"games": {"RetroFrontend": {
                "backupPath": str(peer / "RetroFrontend"),
                "backups": [{"name": "backup-1"}]}}}})
            with _config({"trees": {"RetroFrontend": {"roots": {}}}}):
                self.assertEqual(
                    savepick.peer_tree_index("RetroFrontend", {"device_dir": "deck"}),
                    {})

    def test_files_are_reported_relative_to_the_peer_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = self._peer(tmp, "desktop", "backup-1", {"psx/a.srm": 100.0})
            self._patch([peer], {"desktop": {"games": {"RetroFrontend": {
                "backupPath": str(peer / "RetroFrontend"),
                "backups": [{"name": "backup-1"}]}}}})
            with _config({"trees": {"RetroFrontend": {
                    "roots": {"desktop": "/a/saves"}}}}):
                index = savepick.peer_tree_index("RetroFrontend",
                                                 {"device_dir": "deck"})
            self.assertEqual(list(index), ["psx/a.srm"])
            self.assertEqual(index["psx/a.srm"][0], 100.0)
            self.assertEqual(index["psx/a.srm"][2], "desktop")

    def test_the_newest_copy_across_two_peers_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = self._peer(tmp, "desktop", "backup-1", {"psx/a.srm": 100.0})
            new = self._peer(tmp, "w541", "backup-1", {"psx/a.srm": 900.0})
            self._patch([old, new], {
                name: {"games": {"RetroFrontend": {
                    "backupPath": str(Path(tmp) / name / "RetroFrontend"),
                    "backups": [{"name": "backup-1"}]}}}
                for name in ("desktop", "w541")})
            with _config({"trees": {"RetroFrontend": {"roots": {
                    "desktop": "/a/saves", "w541": "/a/saves"}}}}):
                index = savepick.peer_tree_index("RetroFrontend",
                                                 {"device_dir": "deck"})
            self.assertEqual(index["psx/a.srm"][0], 900.0)
            self.assertEqual(index["psx/a.srm"][2], "w541")

    def test_an_unreadable_peer_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            peer = self._peer(tmp, "desktop", "backup-1", {"psx/a.srm": 100.0})
            self._patch([peer], {})          # run_json returns None
            with _config({"trees": {"RetroFrontend": {
                    "roots": {"desktop": "/a/saves"}}}}):
                self.assertEqual(
                    savepick.peer_tree_index("RetroFrontend",
                                             {"device_dir": "deck"}),
                    {})


class TestMergePlan(unittest.TestCase):
    """merge_plan is pure. Given two indexes it says exactly what to copy."""

    def _peer_index(self, mapping):
        return {rel: (mtime, Path("/backup") / rel, "desktop")
                for rel, mtime in mapping.items()}

    def test_nothing_on_the_peers_means_nothing_to_copy(self):
        self.assertEqual(savepick.merge_plan({"psx/a.srm": 100.0}, {}), [])

    def test_a_newer_peer_file_is_copied(self):
        plan = savepick.merge_plan({"psx/a.srm": 100.0},
                                   self._peer_index({"psx/a.srm": 500.0}))
        self.assertEqual([row[0] for row in plan], ["psx/a.srm"])
        self.assertEqual(plan[0][2], "desktop")
        self.assertEqual(plan[0][3], 500.0)
        self.assertEqual(plan[0][4], 100.0)

    def test_a_newer_local_file_is_left_alone(self):
        # Castlevania played here, Suikoden played elsewhere. Neither may
        # overwrite the other.
        plan = savepick.merge_plan({"psx/a.srm": 900.0},
                                   self._peer_index({"psx/a.srm": 100.0}))
        self.assertEqual(plan, [])

    def test_a_file_missing_locally_is_copied(self):
        plan = savepick.merge_plan({}, self._peer_index({"psx/a.srm": 100.0}))
        self.assertEqual([row[0] for row in plan], ["psx/a.srm"])
        self.assertIsNone(plan[0][4])

    def test_equal_within_the_tolerance_is_left_alone(self):
        plan = savepick.merge_plan({"psx/a.srm": 100.0},
                                   self._peer_index({"psx/a.srm": 101.5}))
        self.assertEqual(plan, [])

    def test_newer_by_more_than_the_tolerance_is_copied(self):
        plan = savepick.merge_plan({"psx/a.srm": 100.0},
                                   self._peer_index({"psx/a.srm": 103.0}))
        self.assertEqual(len(plan), 1)

    def test_a_save_state_is_never_copied_even_when_newer(self):
        plan = savepick.merge_plan({"snes/g.state.auto": 100.0},
                                   self._peer_index({"snes/g.state.auto": 900.0}))
        self.assertEqual(plan, [])

    def test_a_save_state_missing_locally_is_never_copied(self):
        plan = savepick.merge_plan({}, self._peer_index({"snes/g.state1": 900.0}))
        self.assertEqual(plan, [])

    def test_one_stale_game_does_not_block_a_fresh_one(self):
        # The case whole-tree restore gets wrong.
        local = {"psx/castlevania.srm": 900.0, "psx/suikoden.srm": 100.0}
        peers = self._peer_index({"psx/castlevania.srm": 100.0,
                                  "psx/suikoden.srm": 900.0})
        plan = savepick.merge_plan(local, peers)
        self.assertEqual([row[0] for row in plan], ["psx/suikoden.srm"])

    def test_the_plan_is_sorted_by_path(self):
        peers = self._peer_index({"psx/b.srm": 500.0, "psx/a.srm": 500.0})
        plan = savepick.merge_plan({}, peers)
        self.assertEqual([row[0] for row in plan], ["psx/a.srm", "psx/b.srm"])


class TestOverwritePaths(unittest.TestCase):
    """Only files the merge replaces need protecting. A new file has nothing to lose."""

    def test_a_file_missing_locally_needs_no_snapshot(self):
        plan = [("psx/a.srm", Path("/backup/psx/a.srm"), "desktop", 500.0, None)]
        self.assertEqual(savepick.overwrite_paths(plan, Path("/saves")), [])

    def test_a_file_being_replaced_is_listed(self):
        plan = [("psx/a.srm", Path("/backup/psx/a.srm"), "desktop", 500.0, 100.0)]
        self.assertEqual(savepick.overwrite_paths(plan, Path("/saves")),
                         [Path("/saves/psx/a.srm")])

    def test_only_the_replaced_files_are_listed(self):
        plan = [
            ("psx/a.srm", Path("/backup/psx/a.srm"), "desktop", 500.0, 100.0),
            ("psx/b.srm", Path("/backup/psx/b.srm"), "desktop", 500.0, None),
        ]
        self.assertEqual(savepick.overwrite_paths(plan, Path("/saves")),
                         [Path("/saves/psx/a.srm")])

    def test_an_empty_plan_lists_nothing(self):
        self.assertEqual(savepick.overwrite_paths([], Path("/saves")), [])


class TestApplyMerge(unittest.TestCase):
    """apply_merge copies and never deletes. A dry run touches nothing."""

    def _plan(self, tmp, rel, content, local_mtime=100.0):
        source = Path(tmp) / "backup" / rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content)
        os.utime(source, (500.0, 500.0))
        return [(rel, source, "desktop", 500.0, local_mtime)]

    def test_a_file_is_copied_into_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saves"
            root.mkdir()
            plan = self._plan(tmp, "psx/a.srm", "new")
            copied, failed = savepick.apply_merge(plan, root)
            self.assertEqual((copied, failed), (1, 0))
            self.assertEqual((root / "psx" / "a.srm").read_text(), "new")

    def test_a_missing_parent_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saves"
            root.mkdir()
            plan = self._plan(tmp, "deep/er/a.srm", "new", local_mtime=None)
            savepick.apply_merge(plan, root)
            self.assertTrue((root / "deep" / "er" / "a.srm").is_file())

    def test_the_copy_keeps_the_source_mtime(self):
        # Timestamps must survive the trip or the next comparison is wrong.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saves"
            root.mkdir()
            plan = self._plan(tmp, "psx/a.srm", "new")
            savepick.apply_merge(plan, root)
            self.assertEqual((root / "psx" / "a.srm").stat().st_mtime, 500.0)

    def test_a_dry_run_copies_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saves"
            root.mkdir()
            plan = self._plan(tmp, "psx/a.srm", "new")
            copied, failed = savepick.apply_merge(plan, root, dry_run=True)
            self.assertEqual((copied, failed), (1, 0))
            self.assertFalse((root / "psx" / "a.srm").exists())

    def test_an_untouched_local_file_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saves"
            (root / "psx").mkdir(parents=True)
            keep = root / "psx" / "keep.srm"
            keep.write_text("mine")
            savepick.apply_merge(self._plan(tmp, "psx/a.srm", "new"), root)
            self.assertEqual(keep.read_text(), "mine")

    def test_a_failed_copy_is_counted_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saves"
            root.mkdir()
            plan = [("psx/a.srm", Path(tmp) / "gone.srm", "desktop", 500.0, 100.0)]
            copied, failed = savepick.apply_merge(plan, root)
            self.assertEqual((copied, failed), (0, 1))

    def test_an_empty_plan_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(savepick.apply_merge([], Path(tmp)), (0, 0))


class TestTreeName(unittest.TestCase):
    """--tree selects a save set by name. Nothing is inferred from a Steam id."""

    def test_no_flag_is_none(self):
        self.assertIsNone(savepick.tree_name(["--", "retrobat.exe"]))

    def test_the_flag_gives_the_name(self):
        self.assertEqual(
            savepick.tree_name(["--tree", "RetroFrontend", "--", "x"]),
            "RetroFrontend")

    def test_a_flag_with_no_value_is_none(self):
        self.assertIsNone(savepick.tree_name(["--tree"]))

    def test_a_flag_followed_by_the_separator_is_none(self):
        self.assertIsNone(savepick.tree_name(["--tree", "--", "x"]))


class TestMainTree(unittest.TestCase):
    """main_tree always launches. Every failure resolves to no restore."""

    def setUp(self):
        self._saved = {name: getattr(savepick, name) for name in (
            "confirm_incoming_sync", "sync_settings", "local_tree_root",
            "index_tree", "peer_tree_index", "apply_merge", "launch",
            "backup_on_exit", "warn_unconfirmed", "snapshot_live_saves",
            "Spinner")}
        self.launched = []
        savepick.launch = lambda command, do_restore=False: self.launched.append(
            (command, do_restore)) or 0
        savepick.backup_on_exit = lambda _game: None
        savepick.warn_unconfirmed = lambda _synced: None
        savepick.snapshot_live_saves = lambda *a, **k: True
        savepick.Spinner = lambda *a, **k: _NullSpinner()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(savepick, name, value)

    def test_an_unconfigured_root_launches_without_a_restore(self):
        savepick.confirm_incoming_sync = lambda: True
        savepick.sync_settings = lambda: {"device_dir": "deck"}
        savepick.local_tree_root = lambda _game, _cfg: None
        self.assertEqual(savepick.main_tree("RetroFrontend", ["x"]), 0)
        self.assertEqual(self.launched, [(["x"], False)])

    def test_an_unconfirmed_sync_launches_without_a_restore(self):
        savepick.confirm_incoming_sync = lambda: False
        savepick.sync_settings = lambda: {"device_dir": "deck"}
        savepick.local_tree_root = lambda _game, _cfg: Path("/saves")
        called = []
        savepick.apply_merge = lambda *a, **k: called.append(a) or (0, 0)
        self.assertEqual(savepick.main_tree("RetroFrontend", ["x"]), 0)
        self.assertEqual(called, [])
        self.assertEqual(self.launched, [(["x"], False)])

    def test_a_failed_snapshot_cancels_the_copy_but_still_launches(self):
        savepick.confirm_incoming_sync = lambda: True
        savepick.sync_settings = lambda: {"device_dir": "deck"}
        savepick.local_tree_root = lambda _game, _cfg: Path("/saves")
        savepick.index_tree = lambda _root: {"psx/a.srm": 100.0}
        savepick.peer_tree_index = lambda _game, _cfg: {
            "psx/a.srm": (900.0, Path("/backup/psx/a.srm"), "desktop")}
        savepick.snapshot_live_saves = lambda *a, **k: False
        called = []
        savepick.apply_merge = lambda *a, **k: called.append(a) or (0, 0)
        savepick.main_tree("RetroFrontend", ["x"])
        self.assertEqual(called, [])
        self.assertEqual(self.launched, [(["x"], False)])

    def test_a_plan_that_only_adds_files_needs_no_snapshot(self):
        savepick.confirm_incoming_sync = lambda: True
        savepick.sync_settings = lambda: {"device_dir": "deck"}
        savepick.local_tree_root = lambda _game, _cfg: Path("/saves")
        savepick.index_tree = lambda _root: {}
        savepick.peer_tree_index = lambda _game, _cfg: {
            "psx/a.srm": (900.0, Path("/backup/psx/a.srm"), "desktop")}
        savepick.snapshot_live_saves = lambda *a, **k: self.fail("snapshotted")
        copied = []
        savepick.apply_merge = lambda plan, root, dry_run=False: copied.append(
            plan) or (1, 0)
        savepick.main_tree("RetroFrontend", ["x"])
        self.assertEqual(len(copied[0]), 1)

    def test_the_frontend_launches_after_a_merge(self):
        savepick.confirm_incoming_sync = lambda: True
        savepick.sync_settings = lambda: {"device_dir": "deck"}
        savepick.local_tree_root = lambda _game, _cfg: Path("/saves")
        savepick.index_tree = lambda _root: {}
        savepick.peer_tree_index = lambda _game, _cfg: {}
        savepick.apply_merge = lambda *a, **k: (0, 0)
        self.assertEqual(savepick.main_tree("RetroFrontend", ["x"]), 0)
        self.assertEqual(self.launched, [(["x"], False)])


class TestPathShape(unittest.TestCase):
    """A peer path only lands here when its shape means something here.

    The two frontends nest some cores differently. RetroBat writes
    3do/opera/per_game/<rom>.0.srm and 3ds/Citra/sdmc/..., which nothing on a
    RetroDECK tree ever reads.
    """

    def test_a_system_level_save_always_fits(self):
        # An unplayed system has no directory yet. That is not a layout clash.
        self.assertTrue(savepick.path_fits("pcengine/Ys.srm", set()))

    def test_a_system_name_with_a_hyphen_still_fits(self):
        self.assertTrue(savepick.path_fits("sg-1000/Ys.srm", set()))
        self.assertTrue(savepick.path_fits("mame-sa/Ys.srm", set()))

    def test_a_dotted_top_directory_needs_to_be_here_already(self):
        # RetroArch's sort-by-content writes saves/<game>.m3u/<game>.srm.
        # RetroBat has no such directory and never reads one.
        self.assertFalse(savepick.path_fits("Alundra.m3u/Alundra.srm", set()))

    def test_a_dotted_top_directory_fits_when_this_device_uses_it(self):
        self.assertTrue(savepick.path_fits("Alundra.m3u/Alundra.srm",
                                           {"Alundra.m3u"}))

    def test_a_file_at_the_root_fits(self):
        self.assertTrue(savepick.path_fits("Ys.srm", set()))

    def test_a_nested_path_needs_its_parent_here(self):
        self.assertFalse(
            savepick.path_fits("3do/opera/per_game/Doom (USA).0.srm", {"3do"}))

    def test_a_nested_path_fits_when_the_parent_is_here(self):
        self.assertTrue(
            savepick.path_fits("3do/opera/per_game/Doom (USA).0.srm",
                               {"3do", "3do/opera", "3do/opera/per_game"}))

    def test_tree_dirs_lists_every_parent(self):
        self.assertEqual(savepick.tree_dirs({"a/b/c.srm": 1.0, "d.srm": 2.0}),
                         {"a", "a/b"})

    def test_tree_dirs_of_an_empty_index_is_empty(self):
        self.assertEqual(savepick.tree_dirs({}), set())


class TestMergePlanShape(unittest.TestCase):
    """The shape rule applies only to files this device does not have."""

    def _peer_index(self, mapping):
        return {rel: (mtime, Path("/backup") / rel, "desktop")
                for rel, mtime in mapping.items()}

    def test_a_foreign_nested_path_is_not_copied(self):
        local = {"psx/a.srm": 100.0}
        peers = self._peer_index({"3do/opera/per_game/Doom.0.srm": 900.0})
        self.assertEqual(savepick.merge_plan(local, peers), [])

    def test_a_new_system_directory_is_copied(self):
        local = {"psx/a.srm": 100.0}
        peers = self._peer_index({"pcengine/Ys.srm": 900.0})
        plan = savepick.merge_plan(local, peers)
        self.assertEqual([row[0] for row in plan], ["pcengine/Ys.srm"])

    def test_a_nested_path_is_copied_when_this_device_uses_it(self):
        local = {"3do/opera/per_game/Gex.0.srm": 100.0}
        peers = self._peer_index({"3do/opera/per_game/Doom.0.srm": 900.0})
        plan = savepick.merge_plan(local, peers)
        self.assertEqual([row[0] for row in plan], ["3do/opera/per_game/Doom.0.srm"])

    def test_the_shape_rule_never_blocks_an_overwrite(self):
        # This device already holds the file, so the shape is proven.
        local = {"3do/opera/per_game/Doom.0.srm": 100.0}
        peers = self._peer_index({"3do/opera/per_game/Doom.0.srm": 900.0})
        self.assertEqual(len(savepick.merge_plan(local, peers)), 1)


    def test_a_sort_by_content_directory_is_not_copied(self):
        local = {"psx/a.srm": 100.0}
        peers = self._peer_index({"Alundra.m3u/Alundra.srm": 900.0})
        self.assertEqual(savepick.merge_plan(local, peers), [])


class TestTreeExtensions(unittest.TestCase):
    """A save set can opt out of the allow list when every file in it is a save."""

    def test_the_default_is_the_battery_save_list(self):
        with _config({"trees": {"RetroFrontend": {"roots": {}}}}):
            self.assertEqual(savepick.tree_allowed("RetroFrontend"),
                             savepick.TREE_RESTORE_EXTENSIONS)

    def test_a_missing_tree_gets_the_default(self):
        with _config({}):
            self.assertEqual(savepick.tree_allowed("Nope"),
                             savepick.TREE_RESTORE_EXTENSIONS)

    def test_a_star_means_every_file(self):
        with _config({"trees": {"Bloodborne": {"roots": {}, "extensions": "*"}}}):
            self.assertIsNone(savepick.tree_allowed("Bloodborne"))

    def test_an_explicit_list_is_used(self):
        with _config({"trees": {"X": {"roots": {}, "extensions": ["srm", "ZIP"]}}}):
            self.assertEqual(savepick.tree_allowed("X"), frozenset({"srm", "zip"}))

    def test_none_allows_a_file_with_no_extension(self):
        # shadPS4 writes userdata0000 and backup0000. No dots anywhere.
        self.assertTrue(savepick.is_restorable("SPRJ0005/userdata0000", None))

    def test_none_allows_any_extension(self):
        self.assertTrue(savepick.is_restorable("SPRJ0005/thing.anything", None))

    def test_the_default_still_blocks_an_extensionless_file(self):
        self.assertFalse(savepick.is_restorable("SPRJ0005/userdata0000"))

    def test_merge_plan_honours_an_open_allow_list(self):
        local = {"SPRJ0005/userdata0000": 100.0}
        peers = {"SPRJ0005/userdata0000": (900.0, Path("/b/u"), "desktop")}
        self.assertEqual(savepick.merge_plan(local, peers), [])
        plan = savepick.merge_plan(local, peers, allowed=None)
        self.assertEqual([row[0] for row in plan], ["SPRJ0005/userdata0000"])


class TestSystemAliases(unittest.TestCase):
    """Frontends disagree about system names. sg-1000 and sg1000 are one system."""

    GROUPS = [["gc", "gamecube"], ["sg-1000", "sg1000"], ["mame", "mame-sa"]]

    def test_no_groups_leaves_a_path_alone(self):
        self.assertEqual(savepick.alias_path("gamecube/a.srm", [], {"gc"}),
                         "gamecube/a.srm")

    def test_a_peer_name_is_rewritten_to_the_local_one(self):
        self.assertEqual(
            savepick.alias_path("gamecube/Metroid.s01", self.GROUPS, {"gc"}),
            "gc/Metroid.s01")

    def test_a_name_this_device_already_uses_is_left_alone(self):
        self.assertEqual(
            savepick.alias_path("gc/Metroid.s01", self.GROUPS, {"gc"}),
            "gc/Metroid.s01")

    def test_a_group_no_local_directory_matches_is_refused(self):
        # Copying into a directory the frontend never reads helps nobody.
        self.assertIsNone(
            savepick.alias_path("gamecube/a.srm", self.GROUPS, {"psx"}))

    def test_a_system_outside_every_group_is_untouched(self):
        self.assertEqual(savepick.alias_path("psx/a.srm", self.GROUPS, {"psx"}),
                         "psx/a.srm")

    def test_a_deeper_path_keeps_its_tail(self):
        self.assertEqual(
            savepick.alias_path("gamecube/dolphin/a.srm", self.GROUPS, {"gc"}),
            "gc/dolphin/a.srm")

    def test_a_file_at_the_root_is_untouched(self):
        self.assertEqual(savepick.alias_path("a.srm", self.GROUPS, {"gc"}),
                         "a.srm")

    def test_tree_aliases_reads_the_config(self):
        cfg = {"trees": {"X": {"roots": {}, "system_aliases": [["gc", "gamecube"]]}}}
        with _config(cfg):
            self.assertEqual(savepick.tree_aliases("X"), [["gc", "gamecube"]])

    def test_tree_aliases_defaults_to_empty(self):
        with _config({"trees": {"X": {"roots": {}}}}):
            self.assertEqual(savepick.tree_aliases("X"), [])

    def test_top_dirs_lists_only_the_first_level(self):
        self.assertEqual(savepick.top_dirs({"gc/a.srm": 1.0, "psx/b/c.srm": 2.0,
                                            "loose.srm": 3.0}),
                         {"gc", "psx"})


class TestMergePlanAliases(unittest.TestCase):
    """An aliased file lands under the name this device actually uses."""

    GROUPS = [["gc", "gamecube"], ["sg-1000", "sg1000"]]

    def _peer_index(self, mapping):
        return {rel: (mtime, Path("/backup") / rel, "desktop")
                for rel, mtime in mapping.items()}

    def test_an_aliased_new_file_is_copied_under_the_local_name(self):
        local = {"gc/Zelda.srm": 100.0}
        peers = self._peer_index({"gamecube/Metroid.srm": 900.0})
        plan = savepick.merge_plan(local, peers, aliases=self.GROUPS)
        self.assertEqual([row[0] for row in plan], ["gc/Metroid.srm"])

    def test_an_aliased_file_compares_against_the_local_copy(self):
        # The Ys save: sg1000 here, sg-1000 there, same game.
        name = "Ys - The Vanished Omens (UE) [!].srm"
        local = {"sg1000/%s" % name: 900.0}
        peers = self._peer_index({"sg-1000/%s" % name: 100.0})
        self.assertEqual(savepick.merge_plan(local, peers, aliases=self.GROUPS), [])

    def test_an_aliased_peer_file_that_is_newer_wins(self):
        name = "Ys - The Vanished Omens (UE) [!].srm"
        local = {"sg1000/%s" % name: 100.0}
        peers = self._peer_index({"sg-1000/%s" % name: 900.0})
        plan = savepick.merge_plan(local, peers, aliases=self.GROUPS)
        self.assertEqual([row[0] for row in plan], ["sg1000/%s" % name])
        self.assertEqual(plan[0][4], 100.0)

    def test_an_alias_with_no_local_directory_copies_nothing(self):
        local = {"psx/a.srm": 100.0}
        peers = self._peer_index({"gamecube/Metroid.srm": 900.0})
        self.assertEqual(savepick.merge_plan(local, peers, aliases=self.GROUPS), [])

    def test_without_aliases_the_old_behaviour_holds(self):
        local = {"gc/Zelda.srm": 100.0}
        peers = self._peer_index({"gamecube/Metroid.srm": 900.0})
        # gamecube is a dotless system directory, so it would be created.
        plan = savepick.merge_plan(local, peers)
        self.assertEqual([row[0] for row in plan], ["gamecube/Metroid.srm"])


class TestAlwaysDirs(unittest.TestCase):
    """Some directories hold nothing but save data, whatever the file is called."""

    def test_a_file_inside_an_always_dir_passes(self):
        # MAME names nvram dumps after the chip: at28c16, ioasic, nov0,
        # 0_eagle1_bram. The list is unbounded, so no extension list covers it.
        self.assertTrue(savepick.is_restorable("mame-sa/kof98/at28c16",
                                               always_dirs={"mame-sa"}))

    def test_a_deep_file_inside_an_always_dir_passes(self):
        self.assertTrue(savepick.is_restorable("mame-sa/a/b/nov0",
                                               always_dirs={"mame-sa"}))

    def test_a_file_outside_it_still_needs_the_allow_list(self):
        self.assertFalse(savepick.is_restorable("snes/game.state1",
                                                always_dirs={"mame-sa"}))

    def test_an_always_dir_does_not_match_a_prefix(self):
        self.assertFalse(savepick.is_restorable("mame-saved/x/nov0",
                                                always_dirs={"mame-sa"}))

    def test_a_bare_file_is_unaffected(self):
        self.assertFalse(savepick.is_restorable("nov0", always_dirs={"mame-sa"}))

    def test_tree_always_dirs_reads_the_config(self):
        cfg = {"trees": {"X": {"roots": {}, "always_dirs": ["mame", "mame-sa"]}}}
        with _config(cfg):
            self.assertEqual(savepick.tree_always_dirs("X"), {"mame", "mame-sa"})

    def test_tree_always_dirs_defaults_to_empty(self):
        with _config({"trees": {"X": {"roots": {}}}}):
            self.assertEqual(savepick.tree_always_dirs("X"), set())

    def test_merge_plan_honours_always_dirs(self):
        local = {"mame-sa/kof98/nvram": 100.0}
        peers = {"mame-sa/kof98/nvram": (900.0, Path("/b/n"), "desktop")}
        self.assertEqual(savepick.merge_plan(local, peers), [])
        plan = savepick.merge_plan(local, peers, always_dirs={"mame-sa"})
        self.assertEqual([row[0] for row in plan], ["mame-sa/kof98/nvram"])

    def test_always_dirs_is_checked_after_aliasing(self):
        # The peer calls it mame, this device calls it mame-sa.
        local = {"mame-sa/kof98/nvram": 100.0}
        peers = {"mame/kof98/eeprom": (900.0, Path("/b/e"), "desktop")}
        plan = savepick.merge_plan(local, peers, aliases=[["mame", "mame-sa"]],
                                   always_dirs={"mame", "mame-sa"})
        self.assertEqual([row[0] for row in plan], ["mame-sa/kof98/eeprom"])


class TestOpenSetShape(unittest.TestCase):
    """A set that trusts every file must trust every path shape too.

    shadPS4 keeps a save's metadata one level deeper, at
    SPRJ0005/sce_sys/param.sfo. On a device that has never run the game the
    parent directory does not exist, and the shape rule refused it, so the
    save arrived without the file that describes it.
    """

    def _peer_index(self, mapping):
        return {rel: (mtime, Path("/backup") / rel, "desktop")
                for rel, mtime in mapping.items()}

    def test_a_deep_new_path_is_taken_when_every_file_is_trusted(self):
        local = {"SPRJ0005/userdata0000": 100.0}
        peers = self._peer_index({"SPRJ0005/sce_sys/param.sfo": 900.0})
        plan = savepick.merge_plan(local, peers, allowed=None)
        self.assertEqual([row[0] for row in plan], ["SPRJ0005/sce_sys/param.sfo"])

    def test_a_deep_new_path_into_an_empty_tree_is_taken(self):
        peers = self._peer_index({"SPRJ0005/sce_sys/icon0.png": 900.0})
        plan = savepick.merge_plan({}, peers, allowed=None)
        self.assertEqual(len(plan), 1)

    def test_the_shape_rule_still_applies_to_a_frontend_tree(self):
        local = {"psx/a.srm": 100.0}
        peers = self._peer_index({"3do/opera/per_game/Doom.0.srm": 900.0})
        self.assertEqual(savepick.merge_plan(local, peers), [])

    def test_newest_wins_still_applies_to_a_trusted_set(self):
        local = {"SPRJ0005/userdata0000": 900.0}
        peers = self._peer_index({"SPRJ0005/userdata0000": 100.0})
        self.assertEqual(savepick.merge_plan(local, peers, allowed=None), [])


class TestAStopSignalStillBacksUp(unittest.TestCase):
    """A quit must not cost the session.

    On 2026-09-17 two Bloodborne sessions on the Deck were played and lost.
    Steam signalled the process group on quit, savepick's handler raised
    SystemExit, subprocess.call killed shadPS4 on the way out, and the exit
    backup never ran. The saves stayed on the Deck and DESKTOP never saw them.
    """

    def setUp(self):
        import signal
        self.signal = signal
        self.real_handlers = {}
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                self.real_handlers[sig] = signal.getsignal(sig)
        self.real_popen = savepick.subprocess.Popen
        savepick.CHILD = None
        savepick.STOP_DEADLINE = []
        del savepick.SIGNALS_SEEN[:]

    def tearDown(self):
        for sig, handler in self.real_handlers.items():
            self.signal.signal(sig, handler)
        savepick.subprocess.Popen = self.real_popen
        savepick.CHILD = None
        savepick.STOP_DEADLINE = []
        del savepick.SIGNALS_SEEN[:]

    def _handler(self):
        savepick.trace_signals()
        return self.signal.getsignal(self.signal.SIGTERM)

    def _child(self, exits_on_signal=True):
        test = self

        class FakeProc(object):
            def __init__(self):
                self.signals = []
                self.killed = False
                self.running = True

            def wait(self, timeout=None):
                if self.running and timeout is not None:
                    raise savepick.subprocess.TimeoutExpired("game", timeout)
                return 0

            def poll(self):
                return None if self.running else 0

            def send_signal(self, signum):
                self.signals.append(signum)
                if exits_on_signal:
                    self.running = False

            def kill(self):
                self.killed = True
                self.running = False

        proc = FakeProc()
        savepick.subprocess.Popen = lambda cmd, *a, **k: proc
        return proc

    def test_the_first_signal_does_not_raise(self):
        """Raising here is what skipped the backup. The handler must return."""
        handler = self._handler()
        handler(self.signal.SIGTERM, None)

    def test_the_signal_is_passed_to_the_game(self):
        proc = self._child()
        savepick.CHILD = proc
        handler = self._handler()
        handler(self.signal.SIGTERM, None)
        self.assertEqual(proc.signals, [self.signal.SIGTERM])

    def test_a_second_signal_of_the_same_kind_is_a_force_quit(self):
        handler = self._handler()
        handler(self.signal.SIGTERM, None)
        with self.assertRaises(SystemExit):
            handler(self.signal.SIGTERM, None)

    def test_launch_returns_after_a_signal_so_the_backup_can_run(self):
        proc = self._child()
        handler = self._handler()
        started = []

        real_wait = savepick.wait_for_child

        def wait(p):
            started.append(p)
            handler(self.signal.SIGTERM, None)
            return real_wait(p)

        savepick.wait_for_child = wait
        try:
            code = savepick.launch(["/game.exe"])
        finally:
            savepick.wait_for_child = real_wait
        self.assertEqual(code, 0)
        self.assertEqual(proc.signals, [self.signal.SIGTERM])
        self.assertFalse(proc.killed)

    def test_a_game_that_ignores_the_signal_is_killed_not_waited_on_forever(self):
        proc = self._child(exits_on_signal=False)
        savepick.CHILD = proc
        handler = self._handler()
        handler(self.signal.SIGTERM, None)
        savepick.STOP_DEADLINE = [savepick.time.monotonic() - 1]
        self.assertEqual(savepick.wait_for_child(proc), 0)
        self.assertTrue(proc.killed)

    def test_no_signal_means_no_deadline_and_no_kill(self):
        """A normal session runs as long as it likes."""
        proc = self._child()
        proc.running = False
        self.assertEqual(savepick.wait_for_child(proc), 0)
        self.assertFalse(proc.killed)
        self.assertEqual(savepick.STOP_DEADLINE, [])


class TestBorderless(unittest.TestCase):
    """--borderless takes a windowed game's frame off and fits it to the screen.

    Dark Souls II offers exclusive fullscreen or a window with a title bar. The
    ctypes calls cannot run here, so what is tested is every decision around
    them: the style bits, when a window counts as done, which window is the
    game's, that the watch outlives a game putting its frame back, and that
    --no-sync never goes near a save.
    """

    FRAMED = 0x16CF0000   # WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_CLIPSIBLINGS...
    MONITOR = (0, 0, 2560, 1440)

    def test_every_part_of_the_frame_comes_off(self):
        style, ex = savepick.borderless_style(self.FRAMED, 0x00000300)
        self.assertEqual(style & savepick.WS_FRAME_BITS, 0)
        self.assertEqual(ex & savepick.WS_EX_FRAME_BITS, 0)

    def test_the_rest_of_the_style_is_kept(self):
        # WS_VISIBLE must survive, or the game's window disappears.
        style, _ex = savepick.borderless_style(self.FRAMED, 0)
        self.assertTrue(style & 0x10000000)

    def test_framed_is_not_done(self):
        self.assertFalse(savepick.is_borderless(
            self.FRAMED, 0, self.MONITOR, self.MONITOR))

    def test_frameless_but_small_is_not_done(self):
        style, ex = savepick.borderless_style(self.FRAMED, 0)
        self.assertFalse(savepick.is_borderless(
            style, ex, (0, 0, 1920, 1080), self.MONITOR))

    def test_frameless_and_covering_the_monitor_is_done(self):
        style, ex = savepick.borderless_style(self.FRAMED, 0)
        self.assertTrue(savepick.is_borderless(
            style, ex, self.MONITOR, self.MONITOR))

    def test_the_biggest_game_window_wins(self):
        windows = [(1, 300, 0, "Crash Reporter", True),
                   (2, 300, 0, "DARK SOULS II", True),
                   (3, 999, 0, "Steam", True)]
        sizes = {1: 400 * 300, 2: 1920 * 1080, 3: 2560 * 1440}
        self.assertEqual(
            savepick.choose_game_window(windows, {300}, sizes.get), 2)

    def test_dialogs_hidden_and_untitled_windows_are_never_chosen(self):
        windows = [(1, 300, 9, "Loading", True),
                   (2, 300, 0, "DARK SOULS II", False),
                   (3, 300, 0, "", True)]
        self.assertIsNone(
            savepick.choose_game_window(windows, {300}, lambda _h: 10))

    def test_a_frame_put_back_is_taken_off_again(self):
        class Proc(object):
            pid = 1
            polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls <= 4 else 0

        made = []
        answers = iter(["done", "already", "done", "already"])

        def make(hwnd):
            made.append(hwnd)
            return next(answers)

        result = savepick.keep_the_game_borderless(
            Proc(), poll=0, settle=0, make=make, find=lambda: 7)
        self.assertTrue(result)
        self.assertEqual(made, [7, 7, 7, 7])

    def test_no_window_yet_is_not_an_error(self):
        class Proc(object):
            pid = 1
            polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls <= 2 else 0

        result = savepick.keep_the_game_borderless(
            Proc(), poll=0, settle=0, make=lambda _h: self.fail("no window"),
            find=lambda: None)
        self.assertFalse(result)

    def test_a_failure_stops_the_watch_and_never_raises(self):
        class Proc(object):
            pid = 1

            def poll(self):
                return None

        def make(_hwnd):
            raise OSError("access denied")

        self.assertFalse(savepick.keep_the_game_borderless(
            Proc(), poll=0, settle=0, make=make, find=lambda: 7))

    def test_not_asked_for_means_no_watch(self):
        real = savepick.BORDERLESS
        savepick.BORDERLESS = False
        try:
            self.assertIsNone(savepick.watch_to_keep_borderless(
                type("P", (), {"pid": 4321})()))
        finally:
            savepick.BORDERLESS = real

    @unittest.skipIf(os.name == "nt", "this is the not-Windows case")
    def test_off_windows_there_is_nothing_to_do(self):
        real = savepick.BORDERLESS
        savepick.BORDERLESS = True
        try:
            self.assertIsNone(savepick.watch_to_keep_borderless(
                type("P", (), {"pid": 4321})()))
        finally:
            savepick.BORDERLESS = real

    def test_the_switches_are_read_before_the_separator_only(self):
        self.assertEqual(savepick.head_of(["--borderless", "--", "game.exe",
                                           "--no-sync"]), ["--borderless"])
        self.assertEqual(savepick.head_of(["--", "game.exe"]), [])


class TestNoSync(unittest.TestCase):
    """--no-sync starts the game and touches nothing else."""

    def setUp(self):
        self.saved = {name: getattr(savepick, name) for name in
                      ("confirm_incoming_sync", "launch", "backup_on_exit",
                       "game_name_for_appid", "warn_unconfirmed",
                       "BORDERLESS")}
        self.launched = []
        savepick.launch = lambda cmd, do_restore=False: self.launched.append(cmd) or 0

        def touched(*_a, **_k):
            raise AssertionError("--no-sync went near the saves")

        savepick.confirm_incoming_sync = touched
        savepick.backup_on_exit = touched
        savepick.game_name_for_appid = touched
        os.environ["SteamAppId"] = "335300"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(savepick, name, value)
        os.environ.pop("SteamAppId", None)

    def test_it_launches_and_never_syncs(self):
        code = savepick.main(["--borderless", "--no-sync", "--", "game.exe",
                              "-windowed"])
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [["game.exe", "-windowed"]])
        self.assertTrue(savepick.BORDERLESS)

    def test_a_game_argument_named_like_a_switch_is_the_games(self):
        # The game is synced as normal: --no-sync after the separator is the
        # game's own argument, not ours.
        savepick.confirm_incoming_sync = lambda: None
        savepick.warn_unconfirmed = lambda synced: None
        savepick.backup_on_exit = lambda game: None
        savepick.game_name_for_appid = lambda appid: "Dark Souls II"
        savepick.main(["--", "game.exe", "--no-sync"])
        self.assertEqual(self.launched, [["game.exe", "--no-sync"]])
        self.assertFalse(savepick.BORDERLESS)


class TestStoreMode(unittest.TestCase):
    """The picker's store path, against a fake daemon.

    Every dialog and ludusavi call is replaced, so what is tested is the
    decision each daemon answer leads to.
    """

    class Worker(object):
        def __init__(self, answer, waits=None):
            self.answer = answer
            self.waits = list(waits or [{"state": "committed"}])
            self.calls = []

        def decide(self, game, hashes):
            self.calls.append(("decide", game))
            return self.answer

        def fetch(self, game, snap, into):
            self.calls.append(("fetch", snap))
            os.makedirs(os.path.join(into, "G", "b"))
            with open(os.path.join(into, "G", "b", "save.sl2"), "wb") as handle:
                handle.write(b"restored")
            with open(os.path.join(into, "G", "mapping.yaml"), "w") as handle:
                handle.write("x")

        def set_base(self, game, snap, merge=None):
            self.calls.append(("base", snap, tuple(merge or ())))

        def choose(self, game, snap):
            self.calls.append(("choose", snap))

        def stage(self, game, source, played=None, mode="game"):
            self.calls.append(("stage", sorted(os.listdir(source))))
            return {"snap": "S1"}

        def wait(self, snap, timeout):
            self.calls.append(("wait", snap))
            return self.waits.pop(0) if len(self.waits) > 1 else self.waits[0]

    class Quiet(object):
        def __init__(self, *a, **k):
            pass

        def update(self, *a, **k):
            pass

        def close(self):
            pass

        def cancelled(self):
            return False

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.live = os.path.join(self.dir, "save.sl2")
        with open(self.live, "wb") as handle:
            handle.write(b"local")
        names = ("live_save_files", "snapshot_live_saves", "run_json", "ask_user",
                 "show_warning", "Spinner", "SpinnerOrPadCancel", "warn_restore_incomplete",
                 "STORE_EXIT_NOTE_SECONDS", "ludusavi_binary")
        self.saved = {n: getattr(savepick, n) for n in names}
        self.warnings = []
        self.asked = []
        savepick.live_save_files = lambda game: [self.live]
        savepick.snapshot_live_saves = lambda game, paths: True
        savepick.show_warning = lambda title, text: self.warnings.append(text)
        savepick.warn_restore_incomplete = lambda game: self.warnings.append("INCOMPLETE")
        savepick.Spinner = self.Quiet
        savepick.SpinnerOrPadCancel = lambda spinner: type(
            "C", (), {"pressed": lambda s: False, "close": lambda s: None})()
        savepick.STORE_EXIT_NOTE_SECONDS = 0

        def restore(args, timeout=None):
            with open(self.live, "wb") as handle:
                handle.write(b"restored")
            return {}

        savepick.run_json = restore

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(savepick, name, value)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_newer_save_is_restored_and_becomes_the_base(self):
        worker = self.Worker({"action": "restore",
                              "restore": {"id": "R", "device": "deck"}})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertIn(("fetch", "R"), worker.calls)
        self.assertIn(("base", "R", ()), worker.calls)
        self.assertEqual(self.warnings, [])

    def test_a_restore_that_did_not_land_says_so(self):
        savepick.run_json = lambda args, timeout=None: {}
        worker = self.Worker({"action": "restore",
                              "restore": {"id": "R", "device": "deck"}})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertEqual(self.warnings, ["INCOMPLETE"])
        self.assertNotIn(("base", "R", ()), worker.calls)

    def test_no_vault_snapshot_means_no_restore(self):
        savepick.snapshot_live_saves = lambda game, paths: False
        worker = self.Worker({"action": "restore",
                              "restore": {"id": "R", "device": "deck"}})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertNotIn(("fetch", "R"), worker.calls)

    def test_an_unreadable_store_warns_and_launches(self):
        worker = self.Worker({"action": "unknown", "error": "no network"})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertIn("Could not check nas", self.warnings[0])

    def test_a_decide_that_raises_is_unknown(self):
        worker = self.Worker(None)
        worker.decide = lambda game, hashes: (_ for _ in ()).throw(OSError("gone"))
        savepick.store_before_launch(worker, "nas", "G")
        self.assertIn("Could not check", self.warnings[0])

    def test_adopting_a_matching_save_sets_the_base_only(self):
        worker = self.Worker({"action": "launch", "adopt": "A"})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertEqual([c for c in worker.calls if c[0] != "decide"], [("base", "A", ())])

    def test_another_device_still_uploading_is_named(self):
        worker = self.Worker({"action": "wait", "pending": [{"device": "desktop"}]})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertIn("desktop has a newer save", self.warnings[0])

    def test_choosing_the_other_save_restores_it(self):
        savepick.ask_user = lambda *a, **k: True
        worker = self.Worker({"action": "ask", "base": "B", "choices": [
            {"id": "D", "device": "deck", "played_end": "2026-09-24T01:00:00Z"}]})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertIn(("choose", "D"), worker.calls)
        self.assertIn(("fetch", "D"), worker.calls)

    def test_keeping_this_device_closes_the_fork_on_the_next_save(self):
        savepick.ask_user = lambda *a, **k: False
        worker = self.Worker({"action": "ask", "base": "B", "choices": [
            {"id": "D", "device": "deck", "played_end": "2026-09-24T01:00:00Z"}]})
        savepick.store_before_launch(worker, "nas", "G")
        self.assertIn(("base", "B", ("D",)), worker.calls)
        self.assertNotIn(("fetch", "D"), worker.calls)

    def fake_backup(self, ok=True):
        real_run = savepick.subprocess.run

        def run(cmd, **kw):
            if "--path" in cmd and ok:
                where = cmd[cmd.index("--path") + 1]
                os.makedirs(os.path.join(where, "G"))
                with open(os.path.join(where, "G", "mapping.yaml"), "w") as handle:
                    handle.write("x")
            return type("Done", (), {"returncode": 0 if ok else 1})()
        savepick.subprocess.run = run
        self.addCleanup(setattr, savepick.subprocess, "run", real_run)
        savepick.ludusavi_binary = lambda: "ludusavi"

    def test_an_upload_that_lands_says_nothing_more(self):
        self.fake_backup()
        worker = self.Worker(None, waits=[{"state": "uploading", "done": 1, "total": 2},
                                          {"state": "committed"}])
        savepick.store_after_exit(worker, "nas", "G", {})
        self.assertEqual(worker.calls[0], ("stage", ["G"]))
        self.assertEqual(self.warnings, [])

    def test_offline_says_not_uploaded_and_does_not_wait(self):
        self.fake_backup()
        worker = self.Worker(None, waits=[{"state": "offline", "message": "x"}])
        savepick.store_after_exit(worker, "nas", "G", {})
        self.assertIn("NOT UPLOADED YET", self.warnings[0])
        self.assertEqual(len([c for c in worker.calls if c[0] == "wait"]), 1)

    def test_a_refusal_gives_the_reason(self):
        self.fake_backup()
        worker = self.Worker(None, waits=[{"state": "refused",
                                           "message": "the CF token has expired"}])
        savepick.store_after_exit(worker, "nas", "G", {})
        self.assertIn("the CF token has expired", self.warnings[0])

    def test_an_unchanged_save_is_not_uploaded(self):
        self.fake_backup()
        worker = self.Worker(None)
        # fake_backup writes only mapping.yaml, which has no save hashes.
        savepick.store_after_exit(worker, "nas", "G", {}, started_on=set())
        self.assertEqual(worker.calls, [])

    def test_a_restore_that_does_not_land_is_tried_again_and_explained(self):
        attempts = []

        def run_json(args, timeout=None):
            attempts.append(args)
            if len(attempts) == 2:
                with open(self.live, "wb") as handle:
                    handle.write(b"restored")
            return {"games": {"G": {"decision": "Processed", "change": "Different",
                                    "files": {self.live: {"change": "Different"}}}}}

        savepick.run_json = run_json
        real_sleep = savepick.time.sleep
        savepick.time.sleep = lambda s: None
        try:
            worker = self.Worker({"action": "restore",
                                  "restore": {"id": "R", "device": "deck"}})
            savepick.store_before_launch(worker, "nas", "G")
        finally:
            savepick.time.sleep = real_sleep
        self.assertEqual(len(attempts), 2)
        self.assertIn(("base", "R", ()), worker.calls)
        self.assertEqual(self.warnings, [])

    def test_a_failed_backup_stages_nothing(self):
        self.fake_backup(ok=False)
        worker = self.Worker(None)
        savepick.store_after_exit(worker, "nas", "G", {})
        self.assertIn("Backup FAILED", self.warnings[0])
        self.assertEqual(worker.calls, [])

    def test_the_conflict_dialog_says_both_were_played(self):
        text = savepick.conflict_text("G", "Sep 24", "deck save", "Sep 23",
                                      headline="These two saves are different, "
                                               "and both were played.")
        self.assertIn("both were played", text)
        self.assertNotIn("OLDER", text)
        self.assertNotIn("(newest)", text)

    def test_the_old_dialog_wording_is_unchanged(self):
        text = savepick.conflict_text("G", "Sep 24", "deck backup", "Sep 23")
        self.assertIn("The backup is OLDER", text)
        self.assertIn("(newest)", text)


class TestStoreLibraries(unittest.TestCase):
    """An emulator saves folder, split into one save per game, on two devices."""

    def setUp(self):
        import slotd
        import slotstore
        self.slotd, self.ss = slotd, slotstore
        self.dir = tempfile.mkdtemp()
        self.store = slotstore.LocalStore(os.path.join(self.dir, "store"))
        os.makedirs(self.store.root)
        self.roots = {"deck": os.path.join(self.dir, "deck"), "pc": os.path.join(self.dir, "pc")}
        self.config = {"trees": {
            "Retro": {"roots": self.roots, "system_aliases": [["sg-1000", "sg1000"]]},
            "Bloodborne": {"roots": {"deck": os.path.join(self.dir, "bb-deck"),
                                     "pc": os.path.join(self.dir, "bb-pc")},
                           "extensions": "*", "one_game": "Bloodborne", "system": "ps4",
                           "label": "shadPS4"}}}
        names = ("load_config", "Spinner", "SpinnerOrPadCancel", "show_warning",
                 "snapshot_live_saves", "warn_restore_incomplete", "ask_user",
                 "STORE_EXIT_NOTE_SECONDS")
        self.saved = {n: getattr(savepick, n) for n in names}
        savepick.load_config = lambda: self.config
        savepick.Spinner = TestStoreMode.Quiet
        savepick.SpinnerOrPadCancel = lambda spinner: type(
            "C", (), {"pressed": lambda s: False, "close": lambda s: None})()
        savepick.STORE_EXIT_NOTE_SECONDS = 0
        self.warnings, self.vaulted, self.asked = [], [], []
        savepick.show_warning = lambda t, x: self.warnings.append(x)
        savepick.warn_restore_incomplete = lambda g: self.warnings.append("INCOMPLETE")
        savepick.snapshot_live_saves = lambda g, p: self.vaulted.extend(p) or True
        savepick.ask_user = lambda *a, **k: self.asked.append(a) or False
        self.d = {dev: slotd.Daemon(self.store, dev, os.path.join(self.dir, "st-" + dev))
                  for dev in ("deck", "pc")}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(savepick, name, value)
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, dev, rel, data, when, library="Retro"):
        root = self.config["trees"][library]["roots"][dev]
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        os.utime(path, (when, when))

    def read(self, dev, rel, library="Retro"):
        root = self.config["trees"][library]["roots"][dev]
        with open(os.path.join(root, *rel.split("/")), "rb") as handle:
            return handle.read()

    def session(self, dev, library="Retro", play=None):
        """One launch and exit on a device; `play` changes saves in between."""
        root = Path(self.config["trees"][library]["roots"][dev])
        answer = savepick.store_library_before_launch(self.d[dev], "store", library, root)
        before = savepick.library_units(library, root)
        before["__new_here__"] = (answer or {}).get("new_here") or set()
        if play:
            play()
        savepick.store_library_after_exit(self.d[dev], "store", library, root, before, {})

    def test_each_game_travels_on_its_own(self):
        self.put("deck", "snes/Mario.srm", b"deck mario", 1700000000)
        self.put("deck", "snes/Zelda.srm", b"deck zelda", 1600000000)
        self.session("deck")
        self.put("pc", "snes/Zelda.srm", b"pc zelda old", 1500000000)
        self.put("pc", "gba/Metroid.srm", b"pc metroid", 1600000000)
        self.session("pc")
        # The pc had never synced: its Zelda is a save the store never saw,
        # so it is kept and asked about, not overwritten. Mario was only on
        # the deck, so it arrives.
        self.assertEqual(self.read("pc", "snes/Mario.srm"), b"deck mario")
        self.assertEqual(self.read("pc", "snes/Zelda.srm"), b"pc zelda old")
        self.assertIn("Zelda", self.warnings[-1])
        self.assertEqual(self.asked, [])      # a library never opens a dialog
        # And the pc's own Metroid went up for the deck.
        self.session("deck")
        self.assertEqual(self.read("deck", "gba/Metroid.srm"), b"pc metroid")

    def test_a_newer_save_elsewhere_is_restored_and_vaulted(self):
        self.put("deck", "snes/Mario.srm", b"v1", 1600000000)
        self.session("deck")
        self.session("pc")
        self.assertEqual(self.read("pc", "snes/Mario.srm"), b"v1")
        self.session("deck", play=lambda: self.put("deck", "snes/Mario.srm", b"v2", 1700000000))
        self.session("pc")
        self.assertEqual(self.read("pc", "snes/Mario.srm"), b"v2")
        self.assertTrue(any(p.endswith("Mario.srm") for p in self.vaulted))

    def test_only_changed_games_go_up(self):
        self.put("deck", "snes/Mario.srm", b"a", 1600000000)
        self.put("deck", "snes/Zelda.srm", b"b", 1600000000)
        self.session("deck")
        before = len(self.store.list(self.ss.PREFIX + "games/"))
        self.session("deck", play=lambda: self.put("deck", "snes/Mario.srm", b"a2", 1700000000))
        added = len(self.store.list(self.ss.PREFIX + "games/")) - before
        self.assertEqual(added, 1)

    def test_an_aliased_system_lands_under_this_devices_name(self):
        self.put("deck", "sg-1000/Ys.srm", b"ys", 1600000000)
        self.session("deck")
        self.put("pc", "sg1000/Other.srm", b"x", 1500000000)
        self.session("pc")
        self.assertEqual(self.read("pc", "sg1000/Ys.srm"), b"ys")

    def test_save_states_are_not_part_of_a_game(self):
        self.put("deck", "snes/Mario.srm", b"s", 1600000000)
        self.put("deck", "snes/Mario.state1", b"state", 1600000000)
        self.session("deck")
        self.session("pc")
        self.assertFalse(os.path.exists(os.path.join(self.roots["pc"], "snes", "Mario.state1")))

    def test_a_one_game_library_asks_like_any_game(self):
        self.put("deck", "SPRJ0005/userdata0000", b"deck", 1600000000, "Bloodborne")
        self.session("deck", "Bloodborne")
        self.put("pc", "SPRJ0005/userdata0000", b"pc", 1700000000, "Bloodborne")
        self.session("pc", "Bloodborne")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.read("pc", "SPRJ0005/userdata0000", "Bloodborne"), b"pc")

    def test_units_carry_their_label(self):
        self.put("deck", "snes/Mario.srm", b"a", 1600000000)
        units = savepick.library_units("Retro", Path(self.roots["deck"]))
        info = list(units.values())[0]["unit"]
        self.assertEqual(info["label"], "RetroArch (SNES)")
        self.assertEqual(info["title"], "Mario (SNES)")
        bb = savepick.library_units("Bloodborne", Path(os.path.join(self.dir, "bb-deck")))
        self.assertEqual(bb, {})
        self.put("deck", "SPRJ0005/userdata0000", b"x", 1600000000, "Bloodborne")
        bb = savepick.library_units("Bloodborne", Path(os.path.join(self.dir, "bb-deck")))
        self.assertEqual(list(bb.values())[0]["unit"]["label"], "shadPS4")


class TestHandoffFolders(unittest.TestCase):
    def test_a_handoff_folder_is_a_plain_folder_in_temp(self):
        path = savepick.handoff_dir("blockslot-test-")
        try:
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(os.path.dirname(path), tempfile.gettempdir())
        finally:
            os.rmdir(path)


class TestTheLogSurvivesARestart(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows keeps %TEMP%")
    def test_the_log_is_not_in_tmp(self):
        self.assertNotIn("/tmp/", str(savepick._log_path()) + "/")
        self.assertTrue(str(savepick._log_path()).endswith("blockslot/savepick.log"))

    def test_a_big_log_rotates(self):
        old = savepick.LOG_PATH
        where = Path(tempfile.mkdtemp()) / "sub" / "savepick.log"
        savepick.LOG_PATH = where
        try:
            where.parent.mkdir(parents=True)
            where.write_bytes(b"x" * (savepick.LOG_ROTATE_BYTES + 1))
            savepick.log("after the rotation")
            self.assertTrue(Path(str(where) + ".1").exists())
            self.assertIn("after the rotation", where.read_text())
        finally:
            savepick.LOG_PATH = old
            shutil.rmtree(str(where.parent.parent), ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
