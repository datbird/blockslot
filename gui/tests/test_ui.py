#!/usr/bin/env python3
"""Smoke tests for the window itself.

These need a display, and they skip themselves when there is not one, so the
suite still passes over ssh and in CI. Under a virtual display they are worth
having: a screen that raises on build, a focus order naming a control that was
renamed, or a row renderer reading a field the model stopped setting are all
mistakes no core test can see.

    xvfb-run -a python3 gui/tests/test_ui.py
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.core import catalog, model, settings  # noqa: E402

try:
    import tkinter as tk
    HAVE_TK = True
except ImportError:
    HAVE_TK = False


def can_open_a_window():
    """Whether a window can be opened here, WITHOUT hanging to find out.

    On macOS over ssh, tk.Tk() blocks forever waiting for a WindowServer that
    an ssh session is not allowed to talk to. It does not raise, so a plain
    try/except never returns. The environment has to be checked first.
    """
    if not HAVE_TK:
        return False
    if sys.platform == "darwin" and os.environ.get("SSH_CONNECTION"):
        return False
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        root = tk.Tk()
    except Exception:
        return False
    root.destroy()
    return True


class StubController(object):
    """Everything the screens ask the controller for, and nothing else."""

    def __init__(self):
        self.settings = settings.Settings(data={
            "syncthing": {"url": "http://127.0.0.1:8384", "apikey": "k",
                          "folder": "gamesaves", "device_dir": "here",
                          "device_names": {"here": "This PC"}},
            "trees": {"RetroFrontend": {"roots": {"here": "/tmp"}}},
        })
        self.library = model.Library(root=None, user_id=None,
                                     settings=self.settings,
                                     catalog=catalog.Catalog())
        self.library.rows = [
            model.Row(10, "A Game", model.KIND_STEAM, installed=True,
                      cloud=False, saves=True, last_played=1700000000),
            model.Row(20, "Cloudy", model.KIND_STEAM, installed=True,
                      cloud=True, saves=True),
            model.Row(30, "Retro", model.KIND_SHORTCUT, installed=True,
                      cloud=False, saves=None, index=0,
                      launch_options="/sp.py --tree RetroFrontend -- /a.exe"),
        ]

    def engine_source(self):
        return None

    def prepare(self):
        return self.library


@unittest.skipUnless(can_open_a_window(), "no display")
class Screens(unittest.TestCase):
    def setUp(self):
        from gui.ui import app as app_mod
        self.window = app_mod.App(StubController(), size=(1280, 800))
        self.addCleanup(self.window.destroy)

    def build(self):
        from gui.ui import activity, games, savesets, setup, store, sync
        self.window.add_screens([
            ("games", "Games", games.GamesScreen),
            ("sync", "Sync", sync.SyncScreen),
            ("store", "Store", store.StoreScreen),
            ("sets", "Emulator games", savesets.SaveSetsScreen),
            ("settings", "Settings", setup.SetupScreen),
            ("activity", "Activity", activity.ActivityScreen),
        ])
        self.window.update()

    def test_every_screen_builds_and_can_be_shown(self):
        self.build()
        for key in ("games", "sync", "store", "sets", "settings", "activity"):
            self.window.show(key)
            self.window.update()
            self.assertIs(self.window.current, self.window.screens[key])

    def test_every_screen_offers_somewhere_to_put_focus(self):
        self.build()
        for key, screen in self.window.screens.items():
            self.assertTrue(screen.focus_order(), key)

    def test_the_games_list_draws_every_kind_of_row(self):
        self.build()
        screen = self.window.screens["games"]
        screen.hide_cloud.set(False)
        screen.apply_filter()
        self.window.update()
        self.assertEqual(len(screen.list.rows), 3)

    def test_picking_a_row_enables_the_action(self):
        self.build()
        screen = self.window.screens["games"]
        self.window.show("games")
        screen.list.move_to(0)
        screen.list.toggle_current()
        self.window.update()
        self.assertTrue(screen.add_button.enabled)
        self.assertEqual(len(screen.list.selected_rows()), 1)

    def test_clearing_disables_it_again(self):
        self.build()
        screen = self.window.screens["games"]
        screen.list.select_all(screen.rows)
        screen.list.clear_selection()
        self.window.update()
        self.assertFalse(screen.add_button.enabled)

    def test_the_shoulder_buttons_change_screen(self):
        self.build()
        self.window.show("games")
        self.window.step_screen(1)
        self.window.update()
        self.assertIs(self.window.current, self.window.screens["sync"])
        self.window.step_screen(-1)
        self.window.update()
        self.assertIs(self.window.current, self.window.screens["games"])

    def test_the_cursor_stops_at_the_ends_of_the_list(self):
        self.build()
        screen = self.window.screens["games"]
        screen.list.move_to(0)
        screen.list.move(-1)
        self.assertEqual(screen.list.cursor, 0)
        screen.list.move_to(len(screen.list.rows) - 1)
        screen.list.move(1)
        self.assertEqual(screen.list.cursor, len(screen.list.rows) - 1)

    def test_the_hub_column_waits_before_it_says_nothing(self):
        self.build()
        screen = self.window.screens["games"]
        # Until ludusavi has answered, an empty cell would read as "never
        # synced", which is a different and much worse claim.
        screen._backups_read = False
        self.assertEqual(screen._synced_text(screen.library.rows[0])[0], "...")
        screen._backups_ready({"A Game": ("2026-09-01T10:00:00Z", "deck")})
        self.window.update()
        self.assertIn("deck", screen._synced_text(screen.library.rows[0])[0])
        self.assertEqual(screen._synced_text(screen.library.rows[1])[0], "")

    def test_the_emulator_games_screen_lists_them_with_this_devices_folder(self):
        self.build()
        self.window.show("sets")
        self.window.update()
        rows = self.window.screens["sets"].list.rows
        self.assertEqual([row.name for row in rows], ["RetroFrontend"])
        self.assertEqual(rows[0].root, "/tmp")

    def test_picking_a_settings_row_fills_the_detail_pane(self):
        """The left list is a menu, so selecting must change the right side."""
        self.build()
        self.window.show("settings")
        self.window.update()
        screen = self.window.screens["settings"]
        self.assertTrue(screen.items.rows)
        seen = []
        for index in range(len(screen.items.rows)):
            screen.items.move_to(index)
            self.window.update()
            seen.append(screen.detail._body.winfo_children()[1].cget("text"))
        # Every row shows something different from its neighbours.
        self.assertEqual(len(seen), len(set(seen)), seen)

    def test_the_windows_exe_says_the_engine_is_built_in(self):
        """No savepick.py to install, and ludusavi is one click away."""
        from gui.core import paths
        for name in ("is_frozen", "is_windows"):
            self.addCleanup(setattr, paths, name, getattr(paths, name))
            setattr(paths, name, lambda: True)
        self.addCleanup(setattr, sys, "executable", sys.executable)
        sys.executable = "C:/Apps/BlockSlot/Blockslot.exe"
        missing = Path("/nonexistent-blockslot-test/ludusavi.exe")
        self.addCleanup(setattr, paths, "ludusavi_path", paths.ludusavi_path)
        paths.ludusavi_path = lambda: missing
        self.build()
        self.window.show("settings")
        self.window.update()
        screen = self.window.screens["settings"]
        rows = {row.key: row for row in screen.items.rows}
        self.assertEqual(rows["engine"].state, "built in")
        self.assertTrue(rows["engine"].ok)
        screen.items.move_to(screen.items.rows.index(rows["engine"]))
        self.window.update()
        self.assertEqual(screen.detail.buttons, [])
        screen.items.move_to(screen.items.rows.index(rows["ludusavi"]))
        self.window.update()
        self.assertEqual([button.text for button in screen.detail.buttons],
                         ["Install ludusavi"])

    def test_a_readout_takes_no_focus(self):
        """A list you cannot act on must not look or behave like a menu."""
        self.build()
        self.window.show("sync")
        self.window.update()
        checks = self.window.screens["sync"].checks
        self.assertFalse(checks.interactive)
        self.assertEqual(str(checks.canvas.cget("takefocus")), "0")

    def test_the_buttons_say_what_they_will_do(self):
        self.build()
        screen = self.window.screens["games"]
        self.window.show("games")
        screen.hide_cloud.set(False)
        screen.apply_filter()
        screen.list.select_all(screen.rows)
        self.window.update()
        self.assertIn("2", screen.add_button.text)  # one of the three is already on


@unittest.skipUnless(can_open_a_window(), "no display")
class TheStoreScreen(unittest.TestCase):
    """The store screen on its own, with a settings file in a temp dir.

    The stub's settings would otherwise save to the real savepick.json.
    """

    def setUp(self):
        import shutil
        import tempfile
        from gui.ui import app as app_mod, store
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-ui-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.controller = StubController()
        self.controller.settings.path = self.dir / "savepick.json"
        self.controller.settings.set_store(
            type="s3", endpoint="https://s3.example", bucket="saves",
            access_key="AK", secret_key="the-secret", device="deck")
        self.window = app_mod.App(self.controller, size=(1280, 800))
        self.addCleanup(self.window.destroy)
        self.window.add_screens([("store", "Store", store.StoreScreen)])
        self.window.update()
        self.screen = self.window.screens["store"]

    def test_it_builds_and_shows(self):
        self.assertIs(self.window.current, self.screen)
        self.assertTrue(self.screen.focus_order())

    def test_a_saved_secret_is_never_put_back_in_a_box(self):
        self.assertEqual(self.screen.secret_key.get(), "")
        self.assertIn("saved", self.screen.secret_key.label.cget("text"))
        self.assertNotIn("saved", self.screen.cf_secret.label.cget("text"))

    def test_an_empty_secret_box_keeps_the_saved_one(self):
        values = self.screen.form_values()
        self.assertNotIn("secret_key", values)
        self.assertEqual(values["bucket"], "saves")
        self.assertEqual(values["device"], "deck")

    def test_switching_kind_changes_the_fields_and_drops_the_old_ones(self):
        self.screen.pick_kind("local")
        self.window.update()
        self.assertIn(self.screen.local_root, self.screen.focus_order())
        self.assertNotIn(self.screen.endpoint, self.screen.focus_order())
        self.screen.local_root.set(str(self.dir / "store"))
        values = self.screen.form_values()
        self.assertEqual(values["type"], "local")
        self.assertIsNone(values["endpoint"])
        self.assertIsNone(values["secret_key"])

    def test_save_writes_the_file_and_shows_the_test(self):
        # A worker thread cannot reach Tk without a running mainloop, which a
        # test does not have. Run the worker in place instead; after() then
        # hands the answer over exactly as it does in the real window.
        from gui.ui import store

        class InPlace(object):
            def __init__(self, target, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        real = store.threading.Thread
        store.threading.Thread = InPlace
        self.addCleanup(setattr, store.threading, "Thread", real)
        (self.dir / "store").mkdir()
        self.screen.pick_kind("local")
        self.screen.local_root.set(str(self.dir / "store"))
        self.screen.save()
        self.assertTrue((self.dir / "savepick.json").is_file())
        self.assertEqual(self.controller.settings.store()["type"], "local")
        self.assertNotIn("secret_key", self.controller.settings.store())
        self.window.update()
        self.assertTrue(self.screen.checks.rows)
        self.assertTrue(all(row.ok for row in self.screen.checks.rows),
                        [(r.label, r.detail) for r in self.screen.checks.rows])


if __name__ == "__main__":
    unittest.main(verbosity=1)
