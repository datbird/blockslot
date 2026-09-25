"""The Decky panel's store helpers: the one-line summary, the fork list, and
hosting the daemon in the plugin's process.

decky/py_modules/blockslot_store.py imports nothing from Decky, so it runs
here against a LocalStore in a temporary directory.

    python3 gui/tests/run.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "decky" / "py_modules", ROOT / "engine"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import blockslot_store as bs  # noqa: E402
import slotd  # noqa: E402
import slotstore as ss  # noqa: E402

GAME = "Dark Souls II: Scholar of the First Sin"


def backup(root, save):
    """A folder shaped like `ludusavi backup --path` output."""
    game = os.path.join(root, "DS2")
    os.makedirs(os.path.join(game, "b1", "drive-C"), exist_ok=True)
    with open(os.path.join(game, "mapping.yaml"), "w") as handle:
        handle.write("name: DS2\nbackups: [b1]\n")
    with open(os.path.join(game, "b1", "drive-C", "save.sl2"), "wb") as handle:
        handle.write(save)
    return root


class Summary(unittest.TestCase):
    def test_all_uploaded(self):
        got = bs.summary({"store": "nas", "queued": [], "error": None})
        self.assertEqual(got, {"line": "All saves uploaded to nas", "tone": "good"})

    def test_offline_counts_the_queue(self):
        got = bs.summary({"store": "nas", "queued": [{"id": "a"}, {"id": "b"}],
                          "error": {"kind": "offline", "message": "no route"}})
        self.assertEqual(got["line"], "2 saves waiting, offline")
        self.assertEqual(got["tone"], "warn")

    def test_one_is_singular(self):
        got = bs.summary({"store": "nas", "queued": [{"id": "a"}], "error": None})
        self.assertEqual(got["line"], "1 save waiting to upload to nas")

    def test_refused_outranks_everything(self):
        got = bs.summary({"store": "nas", "queued": [{"id": "a"}],
                          "error": {"kind": "refused",
                                    "message": "the CF token has expired"}})
        self.assertEqual(got, {"line": "Refused: the CF token has expired", "tone": "bad"})

    def test_progress_says_uploading(self):
        got = bs.summary({"store": "nas", "error": None,
                          "queued": [{"id": "a", "progress": [10, 100]}]})
        self.assertEqual(got["line"], "Uploading to nas, 1 save waiting")

    def test_panel_status_survives_json(self):
        """Over the HTTP face a progress tuple arrives as a list."""
        status = json.loads(json.dumps({
            "store": "nas", "device": "deck", "last_ok": None, "error": None,
            "queued": [{"id": "x", "game": GAME, "bytes": 5, "progress": (3, 9)}]}))
        got = bs.panel_status(status)
        self.assertTrue(got["configured"])
        self.assertEqual(got["queue"][0]["progress"], {"done": 3, "total": 9})
        self.assertEqual(got["queue"][0]["game"], GAME)

    def test_no_dashes_or_odd_characters_in_lines(self):
        for status in ({"queued": []},
                       {"queued": [{"id": "a"}], "error": {"kind": "offline"}},
                       {"queued": [], "error": {"kind": "refused", "message": "x"}}):
            line = bs.summary(status)["line"]
            self.assertTrue(all(ord(c) < 128 for c in line), line)


def lib_row(title, when, heads=1, device="deck", **extra):
    row = {"name": "Retro/" + title, "title": title, "system": "snes",
           "label": "RetroArch (SNES)", "when": when, "device": device,
           "heads": heads, "base": None}
    row.update(extra)
    return row


class EmulatorGames(unittest.TestCase):
    """library_games: what the Emulator games page draws of one library."""

    ROWS = [lib_row("Super Metroid", "2026-09-20T10:00:00Z"),
            lib_row("Chrono Trigger", "2026-09-23T10:00:00Z"),
            lib_row("EarthBound", "2026-09-01T10:00:00Z", heads=2),
            lib_row("Super Mario World", "2026-09-22T10:00:00Z", heads=2,
                    choices=[{"id": "s1", "device": "deck", "when": "2026-09-22T10:00:00Z"},
                             {"id": "s2", "device": "desktop", "when": "2026-09-21T09:00:00Z"}])]

    def titles(self, answer):
        return [row["title"] for row in answer["games"]]

    def test_two_saves_first_then_newest(self):
        got = bs.library_games(self.ROWS)
        self.assertEqual(self.titles(got), ["Super Mario World", "EarthBound",
                                            "Chrono Trigger", "Super Metroid"])
        self.assertEqual((got["total"], got["all"]), (4, 4))

    def test_search_ignores_case_and_counts_matches(self):
        got = bs.library_games(self.ROWS, "  SUPER ")
        self.assertEqual(self.titles(got), ["Super Mario World", "Super Metroid"])
        self.assertEqual((got["total"], got["all"]), (2, 4))
        self.assertEqual(bs.library_games(self.ROWS, "zelda")["games"], [])

    def test_limit_caps_rows_not_the_total(self):
        many = [lib_row("Game %04d" % i, "2026-09-%02dT00:00:00Z" % (1 + i % 28))
                for i in range(1900)]
        got = bs.library_games(many, "", 200)
        self.assertEqual(len(got["games"]), 200)
        self.assertEqual(got["total"], 1900)
        self.assertEqual(bs.library_games(many, "", "junk")["total"], 1900)

    def test_saved_line(self):
        def ago(stamp):
            return "3h ago"
        self.assertEqual(bs.saved_line({"when": "x", "device": "deck"}, ago),
                         "saved 3h ago from deck")
        self.assertEqual(bs.saved_line({"when": "x"}, ago), "saved 3h ago")
        self.assertEqual(bs.saved_line({"device": "deck"}, ago), "saved from deck")
        self.assertEqual(bs.saved_line({}, ago), "not saved yet")

    def test_saved_line_with_the_real_clock_text(self):
        # The source, not blockslot_core: that copy exists only once the
        # plugin is staged, which a CI runner never does.
        from gui.core import backups
        row = {"when": ss.iso(), "device": "deck"}
        self.assertEqual(bs.saved_line(row, backups.when_text), "saved 1m ago from deck")

    def test_choices_pass_through_when_the_store_names_them(self):
        got = bs.library_games(self.ROWS)["games"]
        mario, earthbound, chrono = got[0], got[1], got[2]
        self.assertTrue(mario["two"])
        self.assertEqual([c["id"] for c in mario["choices"]], ["s1", "s2"])
        self.assertEqual(mario["choices"][1]["device"], "desktop")
        # No choices from an older daemon: the page shows no buttons.
        self.assertTrue(earthbound["two"])
        self.assertIsNone(earthbound["choices"])
        self.assertFalse(chrono["two"])
        self.assertIsNone(chrono["choices"])
        self.assertEqual(mario["label"], "RetroArch (SNES)")

    def test_tree_kind_and_caption(self):
        bloodborne = {"roots": {}, "one_game": "Bloodborne", "system": "ps4"}
        self.assertEqual(bs.tree_kind(bloodborne, lambda system: "shadPS4"),
                         {"kind": "game", "one_game": "Bloodborne", "label": "shadPS4"})
        self.assertEqual(bs.tree_kind({"roots": {}}),
                         {"kind": "library", "one_game": "", "label": ""})
        self.assertEqual(bs.tree_kind(dict(bloodborne, label="shadPS4 (own)"))["label"],
                         "shadPS4 (own)")
        self.assertEqual(bs.tree_caption("PS4 Bloodborne", bloodborne), "game: Bloodborne")
        self.assertEqual(bs.tree_caption("RetroBat", {"roots": {}}), "library: RetroBat")
        self.assertEqual(bs.tree_caption("RetroBat", None), "library: RetroBat")

    def test_label_from_the_real_saveunits(self):
        import saveunits
        got = bs.tree_kind({"one_game": "Bloodborne", "system": "ps4"}, saveunits.label_for)
        self.assertEqual(got["label"], "shadPS4")

    def test_text_is_plain_ascii(self):
        for row in bs.library_games(self.ROWS, when_text=lambda s: "2d ago")["games"]:
            self.assertTrue(all(ord(c) < 128 for c in row["line"]), row["line"])


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="deckystore-")
        self.store = ss.LocalStore(os.path.join(self.dir, "store"))
        os.makedirs(self.store.root)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def daemon(self, device):
        return slotd.Daemon(self.store, device,
                            state_dir=os.path.join(self.dir, "state-" + device))

    def save(self, d, data):
        source = backup(tempfile.mkdtemp(dir=self.dir), data)
        staged = d.stage(GAME, source, played={"start": ss.iso(), "end": ss.iso()})
        self.assertEqual(d.wait(staged["snap"], 10)["state"], "committed")
        return staged["snap"]


class Forks(Temp):
    def fork(self):
        """Both devices save on top of nothing: two heads."""
        deck, pc = self.daemon("deck"), self.daemon("desktop")
        a = self.save(deck, b"deck save")
        b = self.save(pc, b"pc save")
        return deck, a, b

    def test_no_fork_no_rows(self):
        deck = self.daemon("deck")
        self.save(deck, b"one")
        got = bs.list_forks(self.store, deck.state, ss)
        self.assertEqual(got, {"forks": [], "error": None})

    def test_fork_from_bases_json_uses_the_real_name(self):
        deck, a, b = self.fork()
        # bases.json knows the game only by its key.
        self.assertEqual(list(deck.state.bases()), [ss.game_key(GAME)])
        got = bs.list_forks(self.store, deck.state, ss)
        self.assertIsNone(got["error"])
        self.assertEqual(len(got["forks"]), 1)
        fork = got["forks"][0]
        self.assertEqual(fork["game"], GAME)
        self.assertEqual(fork["label"], GAME + ": two different saves")
        self.assertEqual({h["id"] for h in fork["heads"]}, {a, b})
        self.assertEqual({h["device"] for h in fork["heads"]}, {"deck", "desktop"})
        for head in fork["heads"]:
            self.assertTrue(head["created"])
            self.assertTrue(head["played_end"])

    def test_extra_names_are_asked_too(self):
        deck, _a, _b = self.fork()
        empty = ss.LocalState(os.path.join(self.dir, "state-fresh"))
        self.assertEqual(bs.list_forks(self.store, empty, ss)["forks"], [])
        got = bs.list_forks(self.store, empty, ss, extra=[GAME])
        self.assertEqual(len(got["forks"]), 1)

    def test_choose_closes_the_fork(self):
        deck, a, _b = self.fork()
        deck.choose(GAME, a)
        deck.upload_now()
        self.assertEqual(bs.list_forks(self.store, deck.state, ss)["forks"], [])

    def test_offline_store_stops_at_once(self):
        deck, _a, _b = self.fork()

        class Down(ss.Store):
            calls = 0

            def list(self, prefix):
                Down.calls += 1
                raise ss.StoreOffline("no route to nas")

        deck.state.set_base("Another Game", "x")
        got = bs.list_forks(Down(), deck.state, ss)
        self.assertEqual(got["forks"], [])
        self.assertIn("no route", got["error"])
        self.assertEqual(Down.calls, 1)


class Hosting(Temp):
    """The plugin's Host against a real config file and a real serve()."""

    def config(self, store_section):
        home = os.path.join(self.dir, "home")
        path = os.path.join(home, ".config", "savepick.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            json.dump({"store": store_section} if store_section else {}, handle)
        return path, home

    def host(self, store_section):
        path, home = self.config(store_section)
        host = bs.Host(path, home, ROOT / "engine")
        self.addCleanup(host.stop)
        return host, home

    def test_state_dir_is_the_deck_users(self):
        self.assertEqual(bs.state_dir_for({}, "/home/deck"),
                         "/home/deck/.local/state/blockslot/store")
        self.assertEqual(bs.state_dir_for({"state_dir": "/x"}, "/home/deck"), "/x")

    def test_no_store_section_is_not_configured(self):
        host, _home = self.host(None)
        host.start()
        self.assertEqual(host.status()["configured"], False)
        self.assertEqual(host.forks(), {"forks": [], "error": None})

    def test_hosts_serves_and_stops(self):
        host, home = self.host({"type": "local", "root": self.store.root,
                                "device": "deck", "name": "nas"})
        host.start()
        info = os.path.join(bs.state_dir_for({}, home), "daemon.json")
        deadline = time.monotonic() + 5
        while not os.path.isfile(info) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.isfile(info), "serve() never wrote daemon.json")
        # The picker finds it the way it always does.
        client = slotd._client_from_info(bs.state_dir_for({}, home))
        self.assertIsNotNone(client)
        status = host.status()
        self.assertEqual(status["line"], "All saves uploaded to nas")
        self.assertEqual(status["device"], "deck")
        self.assertTrue(host.upload_now()["ok"])

        started = time.monotonic()
        host.stop()
        self.assertLess(time.monotonic() - started, bs.STOP_WAIT + 0.5)
        self.assertFalse(host.thread.is_alive())
        self.assertFalse(os.path.isfile(info), "daemon.json was left behind")

    def test_restart_takes_a_new_store_section(self):
        """What pairing does: rewrite the store section, then restart."""
        host, home = self.host(None)
        host.start()
        self.assertFalse(host.status()["configured"])
        path = os.path.join(home, ".config", "savepick.json")
        with open(path, "w") as handle:
            json.dump({"store": {"type": "local", "root": self.store.root,
                                 "device": "steam-deck", "name": "paired"}}, handle)
        host.restart()
        status = host.status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["device"], "steam-deck")
        # Again, onto another store: the old daemon goes, a new one serves.
        other = os.path.join(self.dir, "other")
        os.makedirs(other)
        with open(path, "w") as handle:
            json.dump({"store": {"type": "local", "root": other,
                                 "device": "steam-deck", "name": "moved"}}, handle)
        old = host.daemon
        host.restart()
        self.assertIsNot(host.daemon, old)
        self.assertEqual(host.status()["line"], "All saves uploaded to moved")

    def test_uses_a_daemon_that_already_answers(self):
        first, home = self.host({"type": "local", "root": self.store.root,
                                 "device": "deck"})
        first.start()
        info = os.path.join(bs.state_dir_for({}, home), "daemon.json")
        deadline = time.monotonic() + 5
        while not os.path.isfile(info) and time.monotonic() < deadline:
            time.sleep(0.05)
        second = bs.Host(os.path.join(home, ".config", "savepick.json"), home,
                         ROOT / "engine")
        second.start()
        self.assertIsNone(second.daemon)
        self.assertIsNotNone(second.client)
        self.assertTrue(second.status()["running"])
        self.assertTrue(second.upload_now()["ok"])


class DeviceNames(unittest.TestCase):
    def test_the_page_shows_the_names_a_person_gave(self):
        import blockslot_store as bs
        names = {"deck": "Steam Deck", "desktop": "Windows PC"}.get
        rows = [{"name": "u", "title": "Mario", "device": "desktop", "heads": 2,
                 "choices": [{"id": "a", "device": "deck"}, {"id": "b", "device": "desktop"}]}]
        game = bs.library_games(rows, device_label=names)["games"][0]
        self.assertEqual(game["device"], "Windows PC")
        self.assertIn("from Windows PC", game["line"])
        self.assertEqual([c["device"] for c in game["choices"]], ["Steam Deck", "Windows PC"])
        self.assertEqual([c["id"] for c in game["choices"]], ["a", "b"])

    def test_an_unnamed_device_keeps_its_id(self):
        import blockslot_store as bs
        game = bs.library_games([{"title": "X", "device": "laptop"}],
                                device_label=lambda d: None)["games"][0]
        self.assertEqual(game["device"], "laptop")


if __name__ == "__main__":
    unittest.main()
