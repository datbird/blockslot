"""Tests for slotd: the daemon, its HTTP face, and the picker's connect().

    python3 -m unittest test_slotd
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import shlex
import subprocess
import sys
import time
import unittest
from unittest import mock

import slotd
import slotstore as ss
from test_slotstore import GAME, GOI, backup_dir, os_backup


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
        # The pc has never played it; the picker says it runs the Windows
        # build, the family of the Deck's (drive-C) save.
        answer = pc.decide(GAME, [], os_family=ss.WINDOWS)
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


class OperatingSystems(Base):
    """Each OS family keeps its own history; the daemon decides within one.

    The 2026-09-28 bug: a Linux save of Getting Over It was the newest, the
    Mac restored it, ludusavi could not write a Linux path there, and the
    player was told the restore did not finish.
    """

    def play(self, d, kind, save, os_family=None, game=GOI):
        source = os_backup(tempfile.mkdtemp(dir=self.dir), kind, save)
        staged = d.stage(game, source, played={"start": ss.iso(), "end": ss.iso()},
                         os_family=os_family)
        self.assertEqual(d.wait(staged["snap"], 10), {"state": "committed"})
        return staged["snap"]

    def hashes(self, snap, game=GOI):
        return sorted(ss.save_hashes(ss.read_game(self.store, game).manifests[snap]))

    def test_the_mac_ignores_a_newer_linux_save(self):
        mac, ubuntu = self.daemon("imac"), self.daemon("ubuntu")
        m = self.play(mac, "mac", b"mac")
        self.play(ubuntu, "linux", b"linux")
        answer = mac.decide(GOI, self.hashes(m), os_family=ss.MAC)
        self.assertEqual(answer["action"], ss.LAUNCH)
        self.assertEqual(answer["os"], ss.MAC)
        self.assertEqual([h["id"] for h in answer["heads"]], [m])
        # Asked with no word from the picker (an older one), its own history
        # says it plays the Mac build.
        self.assertEqual(mac.decide(GOI, self.hashes(m))["action"], ss.LAUNCH)

    def test_linux_ignores_a_newer_mac_save(self):
        mac, ubuntu = self.daemon("imac"), self.daemon("ubuntu")
        l1 = self.play(ubuntu, "linux", b"linux")
        self.play(mac, "mac", b"mac")
        self.assertEqual(ubuntu.decide(GOI, self.hashes(l1), os_family=ss.LINUX)["action"],
                         ss.LAUNCH)
        laptop = self.daemon("laptop")
        answer = laptop.decide(GOI, [], os_family=ss.LINUX)
        self.assertEqual((answer["action"], answer["restore"]["id"]), (ss.RESTORE, l1))
        self.assertEqual(answer["restore"]["os"], ss.LINUX)

    def test_the_deck_and_a_linux_desktop_share(self):
        deck, ubuntu = self.daemon("deck"), self.daemon("ubuntu")
        d1 = self.play(deck, "deck-native", b"deck")
        answer = ubuntu.decide(GOI, [], os_family=ss.LINUX)
        self.assertEqual((answer["action"], answer["restore"]["id"]), (ss.RESTORE, d1))
        ubuntu.set_base(GOI, d1)
        l1 = self.play(ubuntu, "linux", b"desk")
        answer = deck.decide(GOI, self.hashes(d1), os_family=ss.LINUX)
        self.assertEqual((answer["action"], answer["restore"]["id"]), (ss.RESTORE, l1))

    def test_proton_on_the_deck_and_windows_share(self):
        deck, pc1 = self.daemon("deck"), self.daemon("pc1")
        # The picker on the Deck saw Proton; ludusavi's mapping agrees.
        d1 = self.play(deck, "proton", b"deck", os_family=ss.WINDOWS, game=GAME)
        answer = pc1.decide(GAME, [], os_family=ss.WINDOWS)
        self.assertEqual((answer["action"], answer["restore"]["id"]), (ss.RESTORE, d1))
        pc1.set_base(GAME, d1)
        e1 = self.play(pc1, "windows", b"pc", game=GAME)
        answer = deck.decide(GAME, self.hashes(d1, GAME), os_family=ss.WINDOWS)
        self.assertEqual((answer["action"], answer["restore"]["id"]), (ss.RESTORE, e1))
        # With no word from the picker, the Deck's own Proton snapshot says
        # Windows, not the Deck's own OS.
        self.assertEqual(deck.decide(GAME, self.hashes(d1, GAME))["os"], ss.WINDOWS)

    def test_a_proton_save_with_no_hint_is_still_windows(self):
        deck = self.daemon("deck")
        d1 = self.play(deck, "proton", b"deck", game=GAME)
        manifest = ss.read_game(self.store, GAME).manifests[d1]
        self.assertEqual(manifest["os"], ss.WINDOWS)

    def test_legacy_deck_and_pc_histories_keep_working(self):
        # The existing snapshots: no "os" field, committed straight.
        def legacy(device, kind, save, parents=()):
            source = os_backup(tempfile.mkdtemp(dir=self.dir), kind, save)
            manifest = ss.make_manifest(GAME, device, ss.scan_dir(source), list(parents))
            ss.commit(self.store, manifest, source)
            return manifest["id"]
        d1 = legacy("deck", "proton", b"deck")
        e1 = legacy("pc1", "windows", b"pc", parents=[d1])
        deck = self.daemon("deck")
        deck.set_base(GAME, d1)
        answer = deck.decide(GAME, self.hashes(d1, GAME), os_family=ss.WINDOWS)
        self.assertEqual((answer["action"], answer["restore"]["id"]), (ss.RESTORE, e1))
        # A daemon asked by an older picker, with no family, gets there too.
        self.assertEqual(deck.decide(GAME, self.hashes(d1, GAME))["action"], ss.RESTORE)

    def test_a_save_of_another_os_cannot_be_chosen_here(self):
        mac, ubuntu = self.daemon("imac"), self.daemon("ubuntu")
        self.play(mac, "mac", b"mac")
        l1 = self.play(ubuntu, "linux", b"linux")
        with self.assertRaises(ss.StoreRefused) as caught:
            mac.choose(GOI, l1)
        self.assertIn("Linux", str(caught.exception))

    def test_choosing_settles_only_this_os(self):
        a, b, mac = self.daemon("deck"), self.daemon("ubuntu"), self.daemon("imac")
        d1 = self.play(a, "deck-native", b"d")
        l1 = self.play(b, "linux", b"l")
        m1 = self.play(mac, "mac", b"m")
        chosen = a.choose(GOI, l1)
        a.wait(chosen["merge"], 10)
        view = ss.read_game(self.store, GOI)
        merge = view.manifests[chosen["merge"]]
        self.assertEqual(merge["os"], ss.LINUX)
        self.assertEqual(sorted(merge["parents"]), sorted([d1, l1]))
        families = dict(ss.view_families(view))
        self.assertEqual(families[ss.LINUX].heads, [chosen["merge"]])
        self.assertEqual(families[ss.MAC].heads, [m1])

    def test_a_pending_choice_of_another_os_is_not_restored(self):
        mac, ubuntu = self.daemon("imac"), self.daemon("ubuntu")
        m1 = self.play(mac, "mac", b"mac")
        l1 = self.play(ubuntu, "linux", b"linux")
        mac.state.set_restore_pending(GOI, l1)
        self.assertEqual(mac.decide(GOI, [], os_family=ss.MAC)["action"], ss.LAUNCH)
        self.assertEqual(mac.decide(GOI, self.hashes(m1), os_family=ss.MAC)["action"],
                         ss.LAUNCH)

    def test_status_says_each_queued_saves_os(self):
        deck = self.daemon("deck")
        deck.paused = True
        source = os_backup(tempfile.mkdtemp(dir=self.dir), "proton", b"x")
        deck.stage(GAME, source)
        status = deck.status()
        self.assertEqual([q["os"] for q in status["queued"]], [ss.WINDOWS])
        self.assertIn(status["os"], ss.FAMILIES)


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

    def test_the_os_travels_over_http(self):
        c = self.client()
        staged = c.stage(GOI, os_backup(tempfile.mkdtemp(dir=self.dir), "deck-native", b"A"),
                         os_family=ss.WINDOWS)
        self.assertEqual(c.wait(staged["snap"], 10), {"state": "committed"})
        manifest = ss.read_game(self.store, GOI).manifests[staged["snap"]]
        self.assertEqual(manifest["os"], ss.WINDOWS)
        self.assertEqual(c.decide(GOI, [], os_family=ss.MAC)["os"], ss.MAC)
        self.assertEqual(c.decide(GOI, [], os_family=ss.MAC)["heads"], [])

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
        if os.name == "nt" or sys.platform == "darwin":
            self.skipTest("Windows and macOS seal it")
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


class FakeSecurity(object):
    """/usr/bin/security with a Keychain in a dict. Records every argv and
    every stdin, so a test can prove where a key travelled."""

    def __init__(self, broken=False, silent_failure=False):
        self.items = {}
        self.argvs = []
        self.stdins = []
        self.broken = broken
        self.silent_failure = silent_failure

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None):
        self.argvs.append(list(argv))
        self.stdins.append(input)
        if self.broken:
            raise OSError("no such file")
        args = argv[1:]
        if args == ["-i"]:
            for line in (input or "").splitlines():
                words = shlex.split(line)
                if words[0] == "add-generic-password" and not self.silent_failure:
                    opts = dict(zip(words[1:], words[2:]))
                    self.items[(opts["-s"], opts["-a"])] = opts["-w"]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[0] == "find-generic-password":
            opts = dict(zip(args, args[1:]))
            key = (opts["-s"], opts["-a"])
            if key not in self.items:
                return subprocess.CompletedProcess(argv, 44, "", "not found")
            return subprocess.CompletedProcess(argv, 0, self.items[key] + "\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "unknown")


class Keychain(unittest.TestCase):
    """macOS: secrets go into the login Keychain through /usr/bin/security."""

    def test_a_secret_goes_in_and_the_file_keeps_only_its_name(self):
        fake = FakeSecurity()
        sealed = slotd.protect("s3cr\"et\\x y", name="secret_key",
                               platform="darwin", runner=fake)
        self.assertEqual(sealed, "keychain:secret_key")
        self.assertEqual(fake.items[("BlockSlot", "secret_key")], "s3cr\"et\\x y")
        self.assertEqual(slotd.unprotect(sealed, platform="darwin", runner=fake),
                         "s3cr\"et\\x y")

    def test_the_key_never_reaches_a_command_line(self):
        fake = FakeSecurity()
        slotd.protect("TOPSECRET", name="secret_key", platform="darwin", runner=fake)
        for argv in fake.argvs:
            self.assertNotIn("TOPSECRET", " ".join(argv))
        self.assertTrue(any("TOPSECRET" in (stdin or "") for stdin in fake.stdins))

    def test_no_keychain_keeps_the_key_in_the_private_file(self):
        self.assertEqual(slotd.protect("k", name="secret_key", platform="darwin",
                                       runner=FakeSecurity(broken=True)), "k")

    def test_a_write_that_did_not_land_is_not_trusted(self):
        fake = FakeSecurity(silent_failure=True)
        self.assertEqual(slotd.protect("k", name="secret_key", platform="darwin",
                                       runner=fake), "k")

    def test_a_key_with_a_line_break_stays_in_the_file(self):
        fake = FakeSecurity()
        self.assertEqual(slotd.protect("a\nb", name="x", platform="darwin",
                                       runner=fake), "a\nb")
        self.assertEqual(fake.argvs, [])

    def test_a_missing_item_is_refused_in_words(self):
        with self.assertRaises(ss.StoreRefused) as caught:
            slotd.unprotect("keychain:secret_key", platform="darwin",
                            runner=FakeSecurity())
        self.assertIn("Keychain", str(caught.exception))

    def test_a_keychain_secret_is_refused_off_the_mac(self):
        with self.assertRaises(ss.StoreRefused):
            slotd.unprotect("keychain:secret_key", platform="linux")

    def test_plain_text_passes_through(self):
        self.assertEqual(slotd.unprotect("plain", platform="darwin",
                                         runner=FakeSecurity(broken=True)), "plain")

    def test_sealed_forms_are_recognised(self):
        self.assertTrue(slotd.is_sealed("dpapi:AAAA"))
        self.assertTrue(slotd.is_sealed("keychain:secret_key"))
        self.assertFalse(slotd.is_sealed("plain"))
        self.assertFalse(slotd.is_sealed(None))

    def test_settings_open_a_keychain_secret(self):
        fake = FakeSecurity()
        fake.items[("BlockSlot", "secret_key")] = "SK"
        folder = tempfile.mkdtemp(prefix="slotd-kc-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "savepick.json")
        with open(path, "w") as handle:
            json.dump({"store": {"type": "s3", "secret_key": "keychain:secret_key",
                                 "device": "mac"}}, handle)
        real = slotd.unprotect
        slotd.unprotect = lambda text: real(text, platform="darwin", runner=fake)
        try:
            store, device = slotd.load_settings(path)
        finally:
            slotd.unprotect = real
        self.assertEqual(store["secret_key"], "SK")
        self.assertEqual(device, "mac")

    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_security_is_there_on_a_mac(self):
        # Reads only: a search for an item no one has.
        code, _out = slotd._security(["find-generic-password", "-s",
                                      "BlockSlot-test-none", "-a", "none", "-w"])
        self.assertIsNotNone(code)


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

    def test_a_save_made_during_the_sync_is_never_undone(self):
        """The file changes between the daemon reading it and writing it: the
        daemon must not put the old version back (seen on CI: the service's
        reload test read "one" after "two" had been written)."""
        real = slotd.central_merge

        def merge_while_someone_saves(data, *args):
            answer = real(data, *args)
            changed = self.read()
            changed["trees"]["RetroFrontend"]["roots"]["desktop"] = "G:/moved"
            self.write(changed)
            return answer

        slotd.central_merge = merge_while_someone_saves
        try:
            self.assertFalse(self.d.sync_config(force=True))
        finally:
            slotd.central_merge = real
        self.assertEqual(self.read()["trees"]["RetroFrontend"]["roots"]["desktop"], "G:/moved")
        # The next pass merges the saved version and carries the change on.
        self.assertTrue(self.d.sync_config(now=time.time()))
        self.assertEqual(self.read()["trees"]["RetroFrontend"]["roots"]["desktop"], "G:/moved")
        self.assertEqual(self.own_file()["roots"], {"RetroFrontend": "G:/moved"})

    def test_other_settings_survive(self):
        self.d.sync_config(force=True)
        self.assertEqual(self.read()["store"]["type"], "local")


# ------------------------------------------------------------------ the Linux desktop


def _snap_env(real):
    """What a game started by snap Steam sees (probed on an Ubuntu laptop, 2026-09-28)."""
    inside = os.path.join(real, "snap", "steam", "common")
    return {"SNAP_NAME": "steam", "SNAP_REAL_HOME": real, "HOME": inside,
            "XDG_CONFIG_HOME": os.path.join(inside, ".config"),
            "XDG_DATA_HOME": os.path.join(inside, ".local", "share")}


@unittest.skipIf(sys.platform == "win32", "the snap is Linux; Windows has its own places")
class SnapPlaces(unittest.TestCase):
    """A picker inside snap Steam and a daemon outside it name one queue."""

    def setUp(self):
        self.real = tempfile.mkdtemp(prefix="slotd-home-")
        self.addCleanup(shutil.rmtree, self.real, True)

    def test_settings_come_from_the_real_home(self):
        with mock.patch.dict(os.environ, _snap_env(self.real)):
            self.assertEqual(slotd.default_config_path(),
                             os.path.join(self.real, ".config", "savepick.json"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "XDG state is Linux")
    def test_the_queue_is_the_one_outside_the_snap(self):
        with mock.patch.dict(os.environ, _snap_env(self.real)):
            os.environ["XDG_STATE_HOME"] = os.path.join(self.real, "snap", "x")
            self.assertEqual(slotd.default_state_dir(),
                             os.path.join(self.real, ".local", "state",
                                          "blockslot", "store"))
            self.assertEqual(slotd.systemd_unit_path(),
                             os.path.join(self.real, ".config", "systemd", "user",
                                          "blockslot.service"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "XDG state is Linux")
    def test_outside_a_snap_xdg_is_honoured(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.real}):
            os.environ.pop("SNAP_NAME", None)
            self.assertEqual(slotd.default_state_dir(),
                             os.path.join(self.real, "blockslot", "store"))


class _Ran(object):
    def __init__(self, code):
        self.calls = []
        self.code = code

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        return mock.Mock(returncode=self.code)


@unittest.skipUnless(sys.platform.startswith("linux"), "systemd is Linux")
class StartThroughSystemd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotd-unit-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SNAP_NAME", None)

    def _unit(self):
        folder = os.path.join(self.dir, "systemd", "user")
        os.makedirs(folder)
        with open(os.path.join(folder, "blockslot.service"), "w") as handle:
            handle.write("[Service]\n")

    def test_the_unit_is_started_when_it_is_installed(self):
        self._unit()
        ran = _Ran(0)
        with mock.patch.object(slotd.subprocess, "Popen") as popen:
            self.assertTrue(slotd.start_detached(runner=ran))
        self.assertEqual(ran.calls, [["systemctl", "--user", "--no-block", "start",
                                      "blockslot.service"]])
        popen.assert_not_called()

    def test_without_the_unit_it_forks_as_before(self):
        ran = _Ran(0)
        with mock.patch.object(slotd.subprocess, "Popen") as popen:
            self.assertTrue(slotd.start_detached(runner=ran))
        self.assertEqual(ran.calls, [])
        self.assertIn("--serve", popen.call_args[0][0])

    def test_a_systemctl_that_fails_falls_back_to_a_fork(self):
        self._unit()
        with mock.patch.object(slotd.subprocess, "Popen") as popen:
            self.assertTrue(slotd.start_detached(runner=_Ran(1)))
        self.assertIn("--serve", popen.call_args[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
