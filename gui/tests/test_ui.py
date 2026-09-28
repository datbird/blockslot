#!/usr/bin/env python3
"""Smoke tests for the window itself.

These need a display, and they skip themselves when there is not one, so the
suite still passes over ssh and in CI. Under a virtual display they are worth
having: a screen that raises on build, a focus order naming a control that was
renamed, or a row renderer reading a field the model stopped setting are all
mistakes no core test can see.

    xvfb-run -a python3 gui/tests/test_ui.py
"""

import gc
import os
import shutil
import sys
import tempfile
import unittest
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A test run on a Mac must never write the login Keychain (slotd.protect).
os.environ["BLOCKSLOT_NO_KEYCHAIN"] = "1"
from gui.core import catalog, model, settings, uiscale  # noqa: E402

REAL_FOR_WINDOW = uiscale.for_window

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
    del root
    gc.collect()
    return True


def open_window(test, controller, size=(1280, 800)):
    """An App for one test, destroyed and FREED on this thread when it ends.

    A destroyed window is left as garbage in reference cycles, and Python
    frees cycles on whichever thread happens to trigger a collection. When
    that is a worker (the Games screen's job, the service's daemon thread),
    every tkinter Variable and Image in the pile calls Tcl from the wrong
    thread, and _tkinter makes that call wait one second for a main loop
    the tests never run before it gives up. Dozens of them stalled a later
    test's thread for longer than its whole deadline (the service reload
    test on Ubuntu, a busy panel on Windows), and on python 3.12 freeing the
    interpreter itself off its thread aborts the run. So the window's
    garbage is collected here, on the Tk thread, as each test ends.
    """
    from gui.ui import app as app_mod
    window = app_mod.App(controller, size=size)
    test.addCleanup(_close_window, test, weakref.ref(window))
    return window


def _close_window(test, ref):
    window = ref()
    if window is not None:
        try:
            window.destroy()
        except tk.TclError:
            pass
        # The test still holds it as self.window; let go so it is garbage now.
        for name, value in list(vars(test).items()):
            if value is window:
                delattr(test, name)
    del window
    gc.collect()


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
        self.window = open_window(self, StubController())

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

    def test_a_desktop_ludusavi_with_no_game_list_offers_one(self):
        """A fresh ludusavi knows no game until its manifest is down."""
        from gui.core import engine, paths
        folder = Path(tempfile.mkdtemp(prefix="blockslot-ludusavi-"))
        self.addCleanup(shutil.rmtree, str(folder), True)
        binary = folder / "ludusavi"
        binary.write_bytes(b"")
        (folder / "ludusavi.portable").write_text("")
        self.addCleanup(setattr, paths, "ludusavi_path", paths.ludusavi_path)
        paths.ludusavi_path = lambda: binary
        self.addCleanup(setattr, engine, "ludusavi_asset", engine.ludusavi_asset)
        engine.ludusavi_asset = lambda kind=None: ("https://example.invalid/", "0" * 64)
        self.build()
        self.window.show("settings")
        self.window.update()
        screen = self.window.screens["settings"]
        rows = {row.key: row for row in screen.items.rows}
        screen.items.move_to(screen.items.rows.index(rows["ludusavi"]))
        self.window.update()
        self.assertEqual([button.text for button in screen.detail.buttons],
                         ["Download its game list"])
        (folder / "manifest.yaml").write_text("{}")
        binary.unlink()

        def no_release(kind=None):
            raise engine.NoRelease("ludusavi publishes its Mac build for "
                                   "Apple silicon only")
        engine.ludusavi_asset = no_release
        screen.items.move_to(screen.items.rows.index(rows["ludusavi"]))
        screen._ludusavi_detail()
        self.window.update()
        self.assertEqual(screen.detail.buttons, [])

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
        from gui.ui import store
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-ui-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.controller = StubController()
        self.controller.settings.path = self.dir / "savepick.json"
        self.controller.settings.set_store(
            type="s3", endpoint="https://s3.example", bucket="saves",
            access_key="AK", secret_key="the-secret", device="deck")
        self.window = open_window(self, self.controller)
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


@unittest.skipUnless(can_open_a_window(), "no display")
class HiDpi(unittest.TestCase):
    """At 200 percent the window must look as it does at 100, only bigger.

    A Linux laptop (GNOME at 200 percent, Xwayland) drew double-size text in a
    100 percent layout: "BlockS" in the nav rail, "rn on sy" on a button.
    Here the display's scale is faked the way Tk sees it, by doubling
    `tk scaling` before the window measures anything: the text really is
    drawn twice as big, and the real uiscale.for_window has to notice.
    """

    def setUp(self):
        # macOS scales for Retina itself and uiscale answers 1 there on
        # purpose, so a doubled Tk scaling proves nothing on aqua.
        if sys.platform == "darwin":
            self.skipTest("macOS scales the window itself")

    def open(self, times):
        from gui.core import uiscale
        from gui.ui import activity, games, savesets, setup, store
        real = REAL_FOR_WINDOW

        def at_scale(root):
            if times != 1:
                now = float(root.tk.call("tk", "scaling"))
                root.tk.call("tk", "scaling", now * times)
            return real(root, environ={}, xft_reader=lambda display: None)
        self.addCleanup(setattr, uiscale, "for_window", real)
        uiscale.for_window = at_scale
        window = open_window(self, StubController())
        window.add_screens([
            ("games", "Games", games.GamesScreen),
            ("store", "Store", store.StoreScreen),
            ("sets", "Emulator games", savesets.SaveSetsScreen),
            ("settings", "Settings", setup.SetupScreen),
            ("activity", "Activity", activity.ActivityScreen),
        ])
        window.update()
        return window

    @staticmethod
    def buttons(widget):
        from gui.ui import widgets
        found = [widget] if isinstance(widget, widgets.Button) else []
        for child in widget.winfo_children():
            found.extend(HiDpi.buttons(child))
        return found

    def measures(self, window):
        from gui.ui import widgets
        metrics = window.metrics
        games = window.screens["games"]
        return {
            "text": widgets.width_of("Emulator games", metrics.font(bold=True)),
            "nav": int(window.nav.cget("width")),
            "button": int(games.add_button.cget("width")),
            "button_height": metrics.button_height,
            "row": metrics.row_height,
            "two_lines": metrics.px(metrics.small * 1.8),
            "pad": metrics.pad,
        }

    def test_every_label_fits_at_200_percent(self):
        from gui.ui import widgets
        window = self.open(2)
        self.assertGreater(window.ui_scale, 1.9)
        metrics = window.metrics
        widest = max(widgets.width_of(label, metrics.font(bold=True))
                     for _key, label in window.nav.items)
        self.assertGreaterEqual(int(window.nav.cget("width")),
                                widest + metrics.pad * 2.2)
        for button in self.buttons(window):
            text = widgets.width_of(button.text, metrics.font(bold=True))
            self.assertGreaterEqual(int(button.cget("width")), text + metrics.pad,
                                    button.text)

    def test_200_percent_is_the_100_percent_layout_twice_over(self):
        one = self.measures(self.open(1))
        two = self.measures(self.open(2))
        grew = two["text"] / float(one["text"])
        self.assertGreater(grew, 1.7)
        for name in ("nav", "button", "button_height", "row", "two_lines", "pad"):
            ratio = two[name] / float(one[name])
            self.assertAlmostEqual(ratio / grew, 1.0, delta=0.2,
                                   msg="%s grew %.2f, text %.2f" % (name, ratio, grew))

    def test_100_percent_is_unchanged(self):
        window = self.open(1)
        self.assertEqual(window.ui_scale, 1.0)
        self.assertEqual(window.metrics.nav_width, 190)
        self.assertEqual(window.metrics.row_height, 44)

class StoreController(StubController):
    """A device set up with a store and no Syncthing, as the macOS VM is."""

    def __init__(self):
        StubController.__init__(self)
        self.settings.data = {
            "store": {"type": "s3", "endpoint": "https://s3.example.net",
                      "bucket": "saves", "access_key": "a", "secret_key": "s",
                      "device": "imac"},
            "trees": {},
        }

    def steam_shortcut(self):
        return None


@unittest.skipUnless(can_open_a_window(), "no display")
class StoreSettings(unittest.TestCase):
    """Settings on a store device: the store's words, and an honest header."""

    def setUp(self):
        from gui.core import paths, storecheck
        self.answer = {"device": "imac", "queued": []}
        self.addCleanup(setattr, storecheck, "daemon_status",
                        storecheck.daemon_status)
        storecheck.daemon_status = lambda conf: self.answer
        folder = Path(tempfile.mkdtemp(prefix="blockslot-ready-"))
        self.addCleanup(shutil.rmtree, str(folder), True)
        (folder / "savepick.py").write_text("")
        (folder / "ludusavi").write_text("")
        for name, value in (("engine_path", folder / "savepick.py"),
                            ("ludusavi_path", folder / "ludusavi"),
                            ("engine_in_exe", False)):
            self.addCleanup(setattr, paths, name, getattr(paths, name))
            setattr(paths, name, lambda value=value: value)
        self.window = open_window(self, StoreController())

    def screen(self):
        from gui.ui import setup
        self.window.add_screens([("settings", "Settings", setup.SetupScreen)])
        screen = self.window.screens["settings"]
        import time
        deadline = time.monotonic() + 5
        while screen._asking and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.window.update()
        return screen, {row.key: row for row in screen.items.rows}

    def test_the_device_and_sync_rows_describe_the_store(self):
        screen, rows = self.screen()
        self.assertEqual(rows["device"].state, "imac")
        self.assertTrue(rows["device"].ok)
        self.assertEqual(rows["sync"].label, "Store")
        self.assertIn("s3.example.net", rows["sync"].state)
        self.assertTrue(rows["sync"].ok, rows["sync"].state)
        screen.items.move_to(screen.items.rows.index(rows["device"]))
        self.window.update()
        self.assertEqual([button.text for button in screen.detail.buttons],
                         ["Open Store"])

    def test_the_steam_entry_is_optional_and_not_a_warning(self):
        from gui.ui import theme
        screen, rows = self.screen()
        steam = rows["steam"]
        self.assertFalse(steam.required)
        self.assertIn("optional", steam.state)
        self.assertNotEqual(steam.colour(), theme.WARN)
        self.assertEqual(screen.banner.kind, "good")
        self.assertEqual(screen.banner.text, "Everything BlockSlot needs is set.")

    def test_the_header_is_never_all_set_over_an_orange_row(self):
        from gui.ui import theme
        self.answer = None
        screen, rows = self.screen()
        self.assertFalse(rows["sync"].ok)
        self.assertEqual(rows["sync"].colour(), theme.WARN)
        self.assertEqual(screen.banner.kind, "warn")
        self.assertNotIn("Everything", screen.banner.text)
        for row in screen.items.rows:
            if row.required and row.colour() != theme.GOOD:
                self.assertNotEqual(screen.banner.kind, "good")


@unittest.skipUnless(can_open_a_window(), "no display")
class Workers(unittest.TestCase):
    """A job on a worker thread must end on screen without anyone clicking.

    On a Mac, a worker that called Tk (even after()) waited for the next mouse
    event, so "Close Steam and do it" finished its work and left its panel up
    until Cancel was clicked. These run the real worker on a real thread, drive
    the window only with update(), and fail if the worker thread touches Tk.
    """

    def setUp(self):
        import threading
        self.window = open_window(self, StubController())
        self.main = threading.get_ident()
        self.off_thread = []
        for name in ("after", "after_idle", "update", "update_idletasks"):
            real = getattr(self.window, name)

            def guard(*args, _real=real, _name=name, **kwargs):
                if threading.get_ident() != self.main:
                    self.off_thread.append(_name)
                return _real(*args, **kwargs)
            setattr(self.window, name, guard)

    def pump_until(self, done, seconds=6.0):
        import time
        deadline = time.monotonic() + seconds
        while not done() and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        return done()

    def test_a_panel_closes_itself_when_the_job_finishes(self):
        import threading
        from gui.ui import app as app_mod
        self.addCleanup(setattr, app_mod.BusyPanel, "LINGER_MS",
                        app_mod.BusyPanel.LINGER_MS)
        app_mod.BusyPanel.LINGER_MS = 50
        panel = self.window.busy("Add to BlockSlot", "Working ...")
        after = []

        def job():
            panel.say("Closing Steam ...")
            panel.finish("Changed 1 game.", True, then=lambda: after.append(1))
        threading.Thread(target=job, daemon=True).start()
        self.assertTrue(self.pump_until(lambda: panel.closed))
        self.assertFalse(panel.frame.winfo_exists())
        self.assertTrue(self.pump_until(lambda: after))
        self.assertEqual(self.off_thread, [])

    def test_a_failed_job_waits_to_be_read(self):
        import threading
        from gui.ui import app as app_mod
        panel = self.window.busy("Add to BlockSlot", "Working ...")
        threading.Thread(target=panel.finish, args=("Stopped: no", False),
                         daemon=True).start()
        self.assertTrue(self.pump_until(
            lambda: any(isinstance(w, tk.Frame) and w.winfo_children()
                        and isinstance(w.winfo_children()[0],
                                       app_mod.widgets.Button)
                        for w in panel.panel.winfo_children())))
        self.assertFalse(panel.closed)
        self.assertEqual(panel.body.cget("text"), "Stopped: no")
        # Press Close. A generated <Escape> only reaches bind_all when the
        # window holds the keyboard focus, which a CI desktop on Windows
        # never gives it.
        close = [b for w in panel.panel.winfo_children() if isinstance(w, tk.Frame)
                 for b in w.winfo_children() if isinstance(b, app_mod.widgets.Button)]
        close[0].command()
        self.assertTrue(self.pump_until(lambda: panel.closed))
        self.assertEqual(self.off_thread, [])

    def test_turning_on_sync_ends_with_the_panel_gone_and_the_result_shown(self):
        """The Games screen's real worker, on a real thread, as in the app."""
        import threading
        from gui.ui import app as app_mod, games
        self.addCleanup(setattr, app_mod.BusyPanel, "LINGER_MS",
                        app_mod.BusyPanel.LINGER_MS)
        app_mod.BusyPanel.LINGER_MS = 50
        self.window.add_screens([("games", "Games", games.GamesScreen)])
        self.window.update()
        screen = self.window.screens["games"]

        def with_steam_closed(root, settle, work, say):
            say("Closing Steam ...")
            say("Starting Steam again ...")
            return 1
        self.addCleanup(setattr, games.launchopts, "with_steam_closed",
                        games.launchopts.with_steam_closed)
        games.launchopts.with_steam_closed = with_steam_closed
        self.addCleanup(setattr, games.steamdir, "localconfig_path",
                        games.steamdir.localconfig_path)
        games.steamdir.localconfig_path = lambda root, user: None
        screen.library.apply = lambda steam, shortcuts: 1
        self.addCleanup(setattr, self.window, "confirm", self.window.confirm)
        self.window.confirm = lambda *args, **kwargs: True
        real_thread = threading.Thread
        started = []

        def spy(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            started.append(thread)
            return thread
        self.addCleanup(setattr, games.threading, "Thread", real_thread)
        games.threading.Thread = spy
        screen._confirm_and_write(screen.library.rows[:1], "Add to BlockSlot",
                                  {10: "on"}, {})
        games.threading.Thread = real_thread
        self.assertEqual(len(started), 1)
        overlays = [w for w in self.window.place_slaves()]
        self.assertTrue(overlays)
        self.assertTrue(self.pump_until(
            lambda: not any(w.winfo_exists() for w in overlays)))
        self.assertTrue(self.pump_until(
            lambda: screen.status.cget("text") == "Changed 1 game."))
        self.assertEqual(self.off_thread, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
