#!/usr/bin/env python3
"""Tests for the Emulator games screen (gui/ui/savesets.py).

The pure parts (row text, the filter, the choice list) need no display. The
screen itself is built under a virtual display with settings in a temp dir,
and with the daemon replaced, so no real savepick.json or store is touched.

    xvfb-run -a python3 gui/tests/test_savesets.py
"""

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from gui.ui import games, savesets  # noqa: E402
    HAVE_TK = True
except ImportError:
    HAVE_TK = False

from gui.tests.test_ui import StubController, can_open_a_window  # noqa: E402

NOW = 1790000000.0


def iso(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


ISO_2D = iso(2 * 86400)
ISO_1H = iso(3600)


def label(device):
    return {"deck": "Deck", "pc": "PC"}.get(device, device)


def game(**values):
    entry = {"name": "Retro|snes/Mario", "title": "Mario (SNES)",
             "label": "RetroArch (SNES)", "when": ISO_2D, "device": "deck",
             "heads": 1}
    entry.update(values)
    return savesets.GameRow(entry)


@unittest.skipUnless(HAVE_TK, "no tkinter")
class RowText(unittest.TestCase):
    def test_a_row_says_title_label_when_and_who(self):
        self.assertEqual(savesets.row_texts(game(), label, now=NOW),
                         ("Mario (SNES)", "RetroArch (SNES)",
                          "saved 2d ago from Deck", ""))

    def test_two_heads_are_marked(self):
        self.assertEqual(savesets.row_texts(game(heads=2), label, now=NOW)[3],
                         "two saves")
        self.assertEqual(savesets.fork_marker(3), "3 saves")

    def test_a_missing_time_or_device_says_what_is_known(self):
        self.assertEqual(savesets.saved_text(None, "pc", label), "saved from PC")
        self.assertEqual(savesets.saved_text(ISO_1H, "", label, now=NOW),
                         "saved 1h ago")
        self.assertEqual(savesets.saved_text(None, "", label), "")

    def test_a_row_with_no_title_falls_back_to_its_name(self):
        row = savesets.GameRow({"name": "Retro|psx/Xenogears"})
        self.assertEqual((row.title, row.appid, row.heads, row.choices),
                         ("Retro|psx/Xenogears", "Retro|psx/Xenogears", 1, []))

    def test_the_kind_of_tree(self):
        self.assertEqual(savesets.kind_word({"one_game": "Bloodborne"}),
                         "Emulator game")
        self.assertEqual(savesets.kind_word({"roots": {}}), "Emulator library")


@unittest.skipUnless(HAVE_TK, "no tkinter")
class Filtering(unittest.TestCase):
    rows = [game(title="Super Mario World (SNES)"), game(title="Xenogears (PS1)"),
            game(title="Mario Kart 64 (N64)")]

    def titles(self, search):
        return [row.title for row in savesets.filter_games(self.rows, search)]

    def test_nothing_typed_shows_everything(self):
        self.assertEqual(len(self.titles("")), 3)
        self.assertEqual(len(self.titles("   ")), 3)

    def test_every_word_must_match_in_any_case(self):
        self.assertEqual(self.titles("mario"),
                         ["Super Mario World (SNES)", "Mario Kart 64 (N64)"])
        self.assertEqual(self.titles("MARIO n64"), ["Mario Kart 64 (N64)"])
        self.assertEqual(self.titles("zelda"), [])


@unittest.skipUnless(HAVE_TK, "no tkinter")
class Choices(unittest.TestCase):
    def test_newest_first_with_device_and_age(self):
        options = savesets.choice_options(
            [{"id": "a", "device": "deck", "when": ISO_2D},
             {"id": "b", "device": "pc", "when": ISO_1H}], label, now=NOW)
        self.assertEqual(options, [("The PC save, 1h ago", "b"),
                                   ("The Deck save, 2d ago", "a")])


@unittest.skipUnless(HAVE_TK, "no tkinter")
class GamesColumn(unittest.TestCase):
    def test_a_shortcut_names_its_library_or_its_game(self):
        self.assertEqual(games.tree_text("RetroBat", {"roots": {}}),
                         "library: RetroBat")
        self.assertEqual(games.tree_text("shadPS4", {"one_game": "Bloodborne"}),
                         "game: Bloodborne")
        self.assertEqual(games.tree_text("A Very Long Library Name Here", {}),
                         "library: A Very Long Libra...")


class FakeWorker(object):
    """Daemon.library_list and choose, recorded."""

    def __init__(self):
        self.chosen = []

    def library_list(self, library):
        return {"library": library, "games": [
            {"name": "RetroFrontend|snes/Mario", "title": "Mario (SNES)",
             "label": "RetroArch (SNES)", "when": ISO_2D, "device": "deck",
             "heads": 2, "choices": [{"id": "d1", "device": "deck", "when": ISO_2D},
                                     {"id": "p1", "device": "pc", "when": ISO_1H}]},
            {"name": "RetroFrontend|psx/Xenogears", "title": "Xenogears (PS1)",
             "label": "RetroArch (PS1)", "when": ISO_1H, "device": "pc",
             "heads": 1}]}

    def choose(self, game_name, snap_id):
        self.chosen.append((game_name, snap_id))
        return {"ok": True}


class InPlace(object):
    """A thread that runs where it is started. A test has no mainloop for a
    real worker to hand back to; after() then delivers exactly as it does in
    the real window."""

    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@unittest.skipUnless(HAVE_TK and can_open_a_window(), "no display")
class TheScreen(unittest.TestCase):
    def setUp(self):
        from gui.ui import app as app_mod
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-sets-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.controller = StubController()
        self.controller.settings.path = self.dir / "savepick.json"
        self.window = app_mod.App(self.controller, size=(1280, 800))
        self.addCleanup(self.window.destroy)

    def build(self):
        self.window.add_screens([("sets", savesets.TITLE, savesets.SaveSetsScreen)])
        self.window.update()
        return self.window.screens["sets"]

    def test_it_builds_and_shows_under_its_new_name(self):
        screen = self.build()
        self.assertIs(self.window.current, screen)
        self.assertEqual(screen.title, "Emulator games")
        self.assertTrue(screen.focus_order())
        self.assertEqual([row.name for row in screen.list.rows], ["RetroFrontend"])
        # No store: Syncthing has no games to list, so no games list.
        self.assertFalse(screen.games_panel.winfo_manager())

    def test_with_a_store_it_lists_the_games_and_settles_a_fork(self):
        worker = FakeWorker()
        real_open, real_thread = savesets.open_worker, savesets.threading.Thread
        savesets.open_worker = lambda settings: (worker, True)
        savesets.threading.Thread = InPlace
        self.addCleanup(setattr, savesets, "open_worker", real_open)
        self.addCleanup(setattr, savesets.threading, "Thread", real_thread)
        self.controller.settings.set_store(type="local", root=str(self.dir / "store"))
        screen = self.build()
        self.window.update()
        self.assertTrue(screen.games_panel.winfo_manager())
        self.assertEqual([row.title for row in screen.games.rows],
                         ["Mario (SNES)", "Xenogears (PS1)"])
        self.assertIn("1 with two saves", screen.status.cget("text"))
        self.assertIn(screen.games, screen.focus_order())

        screen.search.set("xeno")
        self.window.update()
        self.assertEqual([row.title for row in screen.games.rows], ["Xenogears (PS1)"])
        screen.search.set("")

        asked = []

        def choose(title, message, options, cancel_label="Cancel"):
            asked.append(options)
            return options[0][1]

        self.window.choose = choose
        screen._settle(screen.games.rows[0])
        self.window.update()
        self.assertEqual(asked[0][0], ("The pc save, %s" % savesets.backups.when_text(ISO_1H),
                                       "p1"))
        self.assertEqual(worker.chosen, [("RetroFrontend|snes/Mario", "p1")])

    def test_adding_one_game_stores_its_game_system_and_label(self):
        screen = self.build()
        answers = iter(["Bloodborne", "PS4", "shadPS4", None])
        self.window.choose = lambda *a, **k: savesets.KIND_GAME
        self.window.ask_text = lambda *a, **k: next(answers)
        screen.add()
        tree = self.controller.settings.tree("Bloodborne")
        self.assertEqual((tree["one_game"], tree["system"], tree["label"],
                          tree["extensions"]),
                         ("Bloodborne", "ps4", "shadPS4", "*"))
        self.assertTrue((self.dir / "savepick.json").is_file())

    def test_adding_a_library_asks_only_its_name(self):
        screen = self.build()
        answers = iter(["RetroBat", None])
        self.window.choose = lambda *a, **k: savesets.KIND_LIBRARY
        self.window.ask_text = lambda *a, **k: next(answers)
        screen.add()
        tree = self.controller.settings.tree("RetroBat")
        self.assertNotIn("one_game", tree)
        self.assertNotIn("extensions", tree)


if __name__ == "__main__":
    unittest.main(verbosity=1)
