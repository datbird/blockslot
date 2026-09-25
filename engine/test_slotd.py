"""Tests for slotd: the daemon, its HTTP face, and the picker's connect().

    python3 -m unittest test_slotd
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import slotd
import slotstore as ss
from test_slotstore import GAME, backup_dir


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotd-")
        self.store = ss.LocalStore(os.path.join(self.dir, "store"))
        os.makedirs(self.store.root)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def daemon(self, device):
        return slotd.Daemon(self.store, device,
                            state_dir=os.path.join(self.dir, "state-" + device))

    def source(self, save):
        return backup_dir(tempfile.mkdtemp(dir=self.dir), save)

    def session(self, d, save):
        staged = d.stage(GAME, self.source(save),
                         played={"start": ss.iso(), "end": ss.iso()})
        return staged["snap"], d.wait(staged["snap"], 10)


class TheDaemon(Base):
    def test_a_save_goes_up_and_the_base_follows(self):
        deck = self.daemon("deck")
        snap, result = self.session(deck, b"A")
        self.assertEqual(result, {"state": "committed"})
        self.assertEqual(deck.state.base(GAME), snap)

    def test_another_device_is_told_to_restore(self):
        deck = self.daemon("deck")
        snap, _ = self.session(deck, b"A")
        pc = self.daemon("pc")
        answer = pc.decide(GAME, [])
        self.assertEqual(answer["action"], ss.RESTORE)
        self.assertEqual(answer["restore"]["id"], snap)
        self.assertEqual(answer["restore"]["device"], "deck")
        out = os.path.join(self.dir, "restore")
        pc.fetch(GAME, snap, out)
        self.assertTrue(os.path.isdir(out))

    def test_offline_is_said_at_once(self):
        deck = self.daemon("deck")
        real = self.store.put

        def put(key, data):
            raise ss.StoreOffline("no network")

        self.store.put = put
        started = time.monotonic()
        snap, result = self.session(deck, b"A")
        self.assertEqual(result["state"], "offline")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(deck.state.queued(), [snap])
        self.store.put = real
        self.assertEqual(deck.wait(snap, 10), {"state": "committed"})

    def test_a_refusal_carries_its_reason(self):
        deck = self.daemon("deck")

        def put(key, data):
            raise ss.StoreRefused("the CF token has expired")

        self.store.put = put
        _snap, result = self.session(deck, b"A")
        self.assertEqual(result["state"], "refused")
        self.assertIn("expired", result["message"])
        self.assertEqual(deck.status()["error"]["kind"], "refused")

    def test_an_unreachable_store_is_unknown_not_a_crash(self):
        deck = self.daemon("deck")

        def listing(prefix):
            raise ss.StoreOffline("no network")

        self.store.list = listing
        answer = deck.decide(GAME, [])
        self.assertEqual(answer["action"], ss.UNKNOWN)
        self.assertFalse(answer["reachable"])

    def test_queued_saves_here_are_never_overwritten(self):
        deck = self.daemon("deck")
        self.session(deck, b"A")
        pc = self.daemon("pc")
        pc.decide(GAME, [])
        # The pc played offline; its save is queued, not uploaded.
        real = self.store.put
        self.store.put = lambda k, d: (_ for _ in ()).throw(ss.StoreOffline("x"))
        self.session(pc, b"PC")
        self.store.put = real
        answer = pc.decide(GAME, [])
        self.assertNotEqual(answer["action"], ss.RESTORE)

    def test_a_choice_made_away_from_a_launch_restores_at_the_next(self):
        """The Deck holds its own save and chooses the other device's."""
        deck = self.daemon("deck")
        pc = self.daemon("pc")
        a, _ = self.session(deck, b"A")
        pc.set_base(GAME, a)
        d1, _ = self.session(deck, b"D1")
        p1, _ = self.session(pc, b"P1")
        chosen = deck.choose(GAME, p1)
        deck.wait(chosen["merge"], 10)
        deck_hashes = ss.save_hashes(ss.read_game(self.store, GAME).manifests[d1])
        answer = deck.decide(GAME, deck_hashes)
        self.assertEqual(answer["action"], ss.RESTORE)
        # Once restored, the next launch is quiet.
        deck.set_base(GAME, answer["restore"]["id"])
        p1_hashes = ss.save_hashes(ss.read_game(self.store, GAME).manifests[p1])
        self.assertEqual(deck.decide(GAME, p1_hashes)["action"], ss.LAUNCH)

    def test_stop_ends_serve(self):
        d = self.daemon("deck")
        thread = threading.Thread(target=slotd.serve, args=(d,), daemon=True)
        thread.start()
        for _ in range(50):
            if d.server is not None:
                break
            time.sleep(0.05)
        info = os.path.join(d.state.root, "daemon.json")
        for _ in range(50):
            if os.path.isfile(info):
                break
            time.sleep(0.05)
        with open(info) as handle:
            data = json.load(handle)
        slotd.Client(data["port"], data["token"]).stop()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(os.path.exists(info))

    def test_choosing_a_head_closes_the_fork(self):
        deck = self.daemon("deck")
        pc = self.daemon("pc")
        a, _ = self.session(deck, b"A")
        pc.set_base(GAME, a)
        d1, _ = self.session(deck, b"D1")
        p1, _ = self.session(pc, b"P1")
        answer = pc.decide(GAME, [])
        self.assertEqual(answer["action"], ss.ASK)
        self.assertEqual(sorted(c["id"] for c in answer["choices"]), sorted([d1, p1]))
        chosen = pc.choose(GAME, d1)
        pc.wait(chosen["merge"], 10)
        view = ss.read_game(self.store, GAME)
        self.assertEqual(view.heads, [chosen["merge"]])


class Pausing(Base):
    def test_a_paused_daemon_uploads_nothing(self):
        deck = self.daemon("deck")
        deck.paused = True
        staged = deck.stage(GAME, self.source(b"A"))
        self.assertEqual(deck.upload_now(), ([], None))
        self.assertEqual(deck.state.queued(), [staged["snap"]])
        self.assertIsNone(deck.status()["error"])
        self.assertTrue(deck.status()["paused"])

    def test_the_picker_is_told_at_once_and_why(self):
        deck = self.daemon("deck")
        deck.paused = True
        staged = deck.stage(GAME, self.source(b"A"))
        started = time.monotonic()
        result = deck.wait(staged["snap"], 30)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["state"], "refused")
        self.assertTrue(result["paused"])
        self.assertIn("paused", result["message"])

    def test_the_uploader_waits_and_resuming_sends_it(self):
        deck = self.daemon("deck")
        deck.paused = True
        staged = deck.stage(GAME, self.source(b"A"))
        thread = threading.Thread(target=deck.run_uploader, daemon=True)
        thread.start()
        try:
            time.sleep(0.5)
            self.assertEqual(deck.state.queued(), [staged["snap"]])
            deck.paused = False
            deck.kick.set()
            for _ in range(100):
                if not deck.state.queued():
                    break
                time.sleep(0.05)
            self.assertEqual(deck.state.queued(), [])
        finally:
            deck.stopping = True
            deck.kick.set()
            thread.join(5)

    def test_status_says_not_paused_by_default(self):
        self.assertFalse(self.daemon("deck").status()["paused"])


class Libraries(Base):
    def test_a_library_lists_its_games_with_labels(self):
        d = self.daemon("deck")
        src = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(src, "Mario.srm"), "wb") as handle:
            handle.write(b"x")
        name = ss.library_unit_name("Retro", "snes/Mario")
        staged = d.stage(name, src, mode="library",
                         unit={"title": "Mario (SNES)", "label": "RetroArch (SNES)",
                               "system": "snes"})
        d.wait(staged["snap"], 10)
        rows = d.library_list("Retro")["games"]
        self.assertEqual([(r["title"], r["label"], r["device"], r["heads"]) for r in rows],
                         [("Mario (SNES)", "RetroArch (SNES)", "deck", 1)])
        self.assertNotIn("choices", rows[0])

    def test_a_forked_game_lists_its_choices(self):
        name = ss.library_unit_name("Retro", "snes/Mario")
        unit = {"title": "Mario (SNES)", "label": "RetroArch (SNES)", "system": "snes"}

        def play(d, save):
            src = tempfile.mkdtemp(dir=self.dir)
            with open(os.path.join(src, "Mario.srm"), "wb") as handle:
                handle.write(save)
            staged = d.stage(name, src, mode="library", unit=unit,
                             played={"start": ss.iso(), "end": ss.iso()})
            d.wait(staged["snap"], 10)
            return staged["snap"]

        deck = self.daemon("deck")
        pc = self.daemon("pc")
        first = play(deck, b"A")
        pc.set_base(name, first)
        d1 = play(deck, b"D1")
        p1 = play(pc, b"P1")
        row = pc.library_list("Retro")["games"][0]
        self.assertEqual(row["heads"], 2)
        self.assertEqual(sorted((c["id"], c["device"]) for c in row["choices"]),
                         sorted([(d1, "deck"), (p1, "pc")]))
        self.assertTrue(all(c["when"] for c in row["choices"]))
        chosen = pc.choose(name, row["choices"][0]["id"])
        pc.wait(chosen["merge"], 10)
        self.assertEqual(pc.library_list("Retro")["games"][0]["heads"], 1)


class Keeping(Base):
    def test_only_the_keeper_cleans_and_only_once_a_day(self):
        d = self.daemon("deck")
        self.assertIsNone(d.maybe_clean())
        d.keeper = True
        self.assertIsNotNone(d.maybe_clean())
        self.assertIsNone(d.maybe_clean())
        self.assertIsNotNone(d.maybe_clean(now=time.time() + slotd.CLEAN_EVERY + 5))


class TheHTTPFace(Base):
    def setUp(self):
        Base.setUp(self)
        self.d = self.daemon("deck")
        self.thread = threading.Thread(target=slotd.serve, args=(self.d,), daemon=True)
        self.thread.start()
        info = os.path.join(self.d.state.root, "daemon.json")
        for _ in range(50):
            if os.path.isfile(info):
                break
            time.sleep(0.05)
        with open(info) as handle:
            self.info = json.load(handle)

    def client(self, token=None):
        return slotd.Client(self.info["port"], token or self.info["token"])

    def test_a_whole_session_through_http(self):
        c = self.client()
        staged = c.stage(GAME, self.source(b"A"))
        self.assertEqual(c.wait(staged["snap"], 10), {"state": "committed"})
        self.assertEqual(c.decide(GAME, [])["action"], ss.LAUNCH)
        self.assertEqual(c.status()["queued"], [])

    def test_pause_and_resume_over_http(self):
        c = self.client()
        self.assertEqual(c.pause(True), {"paused": True})
        self.assertTrue(self.d.paused)
        c.pause(False)
        self.assertFalse(self.d.paused)

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(slotd.DaemonUnavailable):
            self.client("wrong").status()

    def test_connect_finds_the_running_daemon(self):
        settings = {"type": "local", "root": self.store.root,
                    "state_dir": self.d.state.root}
        worker, how = slotd.connect(settings, "deck", start=False)
        self.assertEqual(how, "daemon")
        self.assertIsInstance(worker, slotd.Client)


class Connecting(Base):
    def test_no_daemon_and_no_start_works_in_process(self):
        settings = {"type": "local", "root": self.store.root}
        worker, how = slotd.connect(settings, "deck",
                                    state_dir=os.path.join(self.dir, "st"), start=False)
        self.assertEqual(how, "local")
        staged = worker.stage(GAME, self.source(b"A"))
        self.assertEqual(worker.wait(staged["snap"], 10), {"state": "committed"})

    def test_a_service_install_never_starts_a_user_daemon(self):
        started = []
        real = slotd.start_detached
        slotd.start_detached = lambda *a, **k: started.append(1) or True
        try:
            worker, how = slotd.connect({"type": "local", "root": self.store.root,
                                         "service": True}, "deck",
                                        state_dir=os.path.join(self.dir, "st"))
        finally:
            slotd.start_detached = real
        self.assertEqual((how, started), ("local", []))

    def test_no_store_set_up_says_so(self):
        worker, why = slotd.connect({}, "deck", start=False)
        self.assertIsNone(worker)
        self.assertIn("no store", why)

    def test_a_stale_daemon_file_is_not_trusted(self):
        state = os.path.join(self.dir, "st")
        os.makedirs(state)
        with open(os.path.join(state, "daemon.json"), "w") as handle:
            json.dump({"port": 1, "token": "x"}, handle)
        worker, how = slotd.connect({"type": "local", "root": self.store.root},
                                    "deck", state_dir=state, start=False)
        self.assertEqual(how, "local")


class Settings(unittest.TestCase):
    def test_secrets_are_plain_off_windows(self):
        if os.name == "nt":
            self.skipTest("Windows encrypts")
        self.assertEqual(slotd.protect("s"), "s")
        self.assertEqual(slotd.unprotect("s"), "s")

    def test_a_windows_secret_is_refused_elsewhere_in_words(self):
        if os.name == "nt":
            self.skipTest("Windows can read it")
        with self.assertRaises(ss.StoreRefused):
            slotd.unprotect("dpapi:AAAA")

    @unittest.skipUnless(os.name == "nt", "DPAPI is Windows only")
    def test_dpapi_round_trips(self):
        sealed = slotd.protect("the secret")
        self.assertTrue(sealed.startswith("dpapi:"))
        self.assertNotIn("the secret", sealed)
        self.assertEqual(slotd.unprotect(sealed), "the secret")


class CentralSettings(Base):
    """The server's settings on the store, copied into savepick.json."""

    def setUp(self):
        super().setUp()
        self.config = os.path.join(self.dir, "savepick.json")
        self.write({"store": {"type": "local", "path": self.store.root},
                    "syncthing": {"device_names": {"desktop": "Desktop"}},
                    "trees": {"RetroFrontend": {"roots": {"desktop": "D:/saves", "deck": "/d"},
                                                "extensions": ["srm"]}}})
        self.d = self.daemon("desktop")
        self.d.config_path = self.config

    def write(self, data):
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def read(self):
        with open(self.config, encoding="utf-8") as handle:
            return json.load(handle)

    def put(self, key, doc):
        self.store.put(key, json.dumps(doc).encode("utf-8"))

    def own_file(self):
        return json.loads(self.store.get(slotd.device_config_key("desktop")).decode("utf-8"))

    def test_no_server_settings_publishes_only_this_device(self):
        self.d.sync_config(force=True)
        own = self.own_file()
        self.assertEqual(own["roots"], {"RetroFrontend": "D:/saves"})
        self.assertEqual(own["name"], "Desktop")
        self.assertEqual(own["set_by"], "device")
        trees = self.read()["trees"]
        self.assertEqual(trees["RetroFrontend"]["extensions"], ["srm"])
        self.assertFalse(self.d.sync_config(force=True), "a second sync changes nothing")

    def test_shared_decides_the_libraries(self):
        self.put(slotd.SHARED_KEY, {"version": 1, "updated": "t1", "libraries": {
            "RetroFrontend": {"extensions": "*"},
            "Bloodborne": {"one_game": "Bloodborne", "system": "ps4", "label": "shadPS4"}}})
        self.assertTrue(self.d.sync_config(force=True))
        trees = self.read()["trees"]
        self.assertEqual(trees["RetroFrontend"]["extensions"], "*")
        self.assertEqual(trees["RetroFrontend"]["roots"]["desktop"], "D:/saves")
        self.assertEqual(trees["Bloodborne"]["label"], "shadPS4")
        self.assertEqual(trees["Bloodborne"]["roots"], {})

    def test_a_library_the_server_removed_goes(self):
        self.put(slotd.SHARED_KEY, {"libraries": {"Other": {}}})
        self.d.sync_config(force=True)
        self.assertEqual(sorted(self.read()["trees"]), ["Other"])

    def test_other_devices_folders_and_names_come_from_their_files(self):
        self.put(slotd.device_config_key("deck"),
                 {"device": "deck", "name": "Steam Deck", "roots": {"RetroFrontend": "/new"}})
        self.d.sync_config(force=True)
        data = self.read()
        self.assertEqual(data["trees"]["RetroFrontend"]["roots"]["deck"], "/new")
        self.assertEqual(data["syncthing"]["device_names"]["deck"], "Steam Deck")

    def test_a_web_edit_of_this_device_reaches_it(self):
        self.d.sync_config(force=True)
        self.put(slotd.device_config_key("desktop"),
                 {"device": "desktop", "name": "Big PC", "set_by": "web",
                  "roots": {"RetroFrontend": "E:/saves"}})
        self.assertTrue(self.d.sync_config(force=True))
        data = self.read()
        self.assertEqual(data["trees"]["RetroFrontend"]["roots"]["desktop"], "E:/saves")
        self.assertEqual(data["syncthing"]["device_names"]["desktop"], "Big PC")
        self.assertEqual(self.own_file()["set_by"], "web", "no write back")

    def test_an_edit_on_this_device_reaches_the_store(self):
        self.d.sync_config(force=True)
        data = self.read()
        data["trees"]["RetroFrontend"]["roots"]["desktop"] = "F:/saves"
        self.write(data)
        self.d.sync_config(force=True)
        self.assertEqual(self.own_file()["roots"], {"RetroFrontend": "F:/saves"})
        self.assertEqual(self.read()["trees"]["RetroFrontend"]["roots"]["desktop"], "F:/saves")

    def test_a_file_that_is_not_json_is_skipped(self):
        self.store.put(slotd.SHARED_KEY, b"{half")
        self.d.sync_config(force=True)
        self.assertIn("RetroFrontend", self.read()["trees"])

    def test_it_waits_between_syncs(self):
        self.d.sync_config(now=1000.0)
        self.put(slotd.device_config_key("deck"), {"device": "deck", "name": "Deck2"})
        self.assertFalse(self.d.sync_config(now=1000.0 + 10))
        self.assertTrue(self.d.sync_config(now=1000.0 + slotd.CONFIG_EVERY + 1))

    def test_other_settings_survive(self):
        self.d.sync_config(force=True)
        self.assertEqual(self.read()["store"]["type"], "local")


if __name__ == "__main__":
    unittest.main(verbosity=2)
