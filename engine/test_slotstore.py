"""Tests for slotstore: the store, the commit order, the queue and the lineage.

    python3 -m unittest test_slotstore

The S3 tests run against a real MinIO when BLOCKSLOT_TEST_S3 names one
(endpoint,bucket,access,secret[,region]) and skip otherwise.
"""

import json
import os
import shutil
import tempfile
import unittest

import slotstore as ss


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode())


def backup_dir(root, save=b"save-1", name="backup-20260924T000000Z"):
    """A folder shaped like `ludusavi backup --path` output."""
    game = os.path.join(root, "Dark Souls II_ Scholar of the First Sin")
    write(os.path.join(game, "mapping.yaml"), "name: DS2\nbackups: [%s]\n" % name)
    write(os.path.join(game, name, "drive-C", "Users", "p", "DS2SOFS0000.sl2"), save)
    return root


GAME = "Dark Souls II: Scholar of the First Sin"


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slotstore-")
        self.store = ss.LocalStore(os.path.join(self.dir, "store"))
        os.makedirs(self.store.root)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def state(self, device):
        return ss.LocalState(os.path.join(self.dir, "state-" + device))

    def source(self, save, label="src"):
        return backup_dir(tempfile.mkdtemp(prefix=label + "-", dir=self.dir), save)

    def play(self, device, save, upload=True):
        """One session on a device: stage its save and, optionally, upload."""
        state = self.state(device)
        manifest = state.stage(GAME, device, self.source(save))
        if upload:
            committed, error = ss.drain(self.store, state)
            self.assertIsNone(error)
        return manifest


class Names(unittest.TestCase):
    def test_a_game_key_survives_any_filesystem(self):
        self.assertEqual(ss.game_key(GAME), "Dark_Souls_II_Scholar_of_the_First_Sin")
        self.assertEqual(ss.game_key("../.."), "game")

    def test_ids_sort_by_time_and_carry_the_device(self):
        a = ss.make_manifest(GAME, "Deck", [], [],
                             created=ss.parse_iso("2026-09-24T01:00:00Z"))
        b = ss.make_manifest(GAME, "desktop", [], [],
                             created=ss.parse_iso("2026-09-24T02:00:00Z"))
        self.assertLess(a["id"], b["id"])
        self.assertEqual(ss.snap_device(a["id"]), "deck")

    def test_the_same_content_gets_the_same_id(self):
        when = ss.parse_iso("2026-09-24T01:00:00Z")
        a = ss.make_manifest(GAME, "deck", [], ["x"], created=when)
        b = ss.make_manifest(GAME, "deck", [], ["x"], created=when)
        self.assertEqual(a["id"], b["id"])

    def test_metadata_is_not_part_of_the_save(self):
        manifest = {"files": [{"path": "G/mapping.yaml", "sha256": "m"},
                              {"path": "G/b/save.sl2", "sha256": "s"}]}
        self.assertEqual(ss.save_hashes(manifest), {"s"})


class TheLocalStore(Temp):
    def test_round_trip(self):
        self.store.put("a/b.txt", b"hi")
        self.assertEqual(self.store.get("a/b.txt"), b"hi")
        self.assertTrue(self.store.exists("a/b.txt"))
        self.assertEqual(self.store.list("a/"), ["a/b.txt"])
        self.store.delete("a/b.txt")
        self.assertFalse(self.store.exists("a/b.txt"))

    def test_a_missing_key_is_not_found(self):
        with self.assertRaises(ss.NotFound):
            self.store.get("nope")

    def test_a_key_may_not_climb_out(self):
        with self.assertRaises(ss.StoreRefused):
            self.store.put("../escape", b"x")

    def test_a_missing_store_folder_is_offline_not_empty(self):
        # An unmounted share must never read as "no saves on the store".
        gone = ss.LocalStore(os.path.join(self.dir, "unplugged"))
        with self.assertRaises(ss.StoreOffline):
            gone.list(ss.game_prefix(GAME))


class Committing(Temp):
    def test_a_snapshot_round_trips_byte_for_byte(self):
        manifest = self.play("deck", b"the save")
        out = os.path.join(self.dir, "out")
        ss.fetch(self.store, manifest, out)
        self.assertEqual(ss.scan_dir(out), manifest["files"])

    def test_an_unchanged_file_is_not_uploaded_twice(self):
        self.play("deck", b"same")
        state = self.state("deck")
        manifest = state.stage(GAME, "deck", self.source(b"same"))
        uploaded = ss.commit(self.store, manifest, state.queued_data(manifest["id"]))
        # mapping.yaml differs by nothing here either, so nothing new at all.
        self.assertEqual(uploaded, 0)

    def test_the_intent_marker_is_cleared_after_commit(self):
        manifest = self.play("deck", b"x")
        self.assertFalse(self.store.exists(ss.pending_key(GAME, manifest["id"])))
        self.assertTrue(self.store.exists(ss.manifest_key(GAME, manifest["id"])))

    def test_every_interruption_point_recovers(self):
        """Cut the commit off after each write and prove the retry lands it."""
        for stop_after in range(0, 4):
            store = ss.LocalStore(os.path.join(self.dir, "s%d" % stop_after))
            os.makedirs(store.root)
            state = ss.LocalState(os.path.join(self.dir, "st%d" % stop_after))
            manifest = state.stage(GAME, "deck", self.source(b"v%d" % stop_after))

            class Cut(Exception):
                pass

            writes = [0]
            real_put = store.put

            def put(key, data, real_put=real_put, writes=writes, stop=stop_after):
                if writes[0] >= stop:
                    raise ss.StoreOffline("lid closed")
                writes[0] += 1
                return real_put(key, data)

            store.put = put
            committed, error = ss.drain(store, state)
            self.assertIsInstance(error, ss.StoreOffline)
            self.assertEqual(state.queued(), [manifest["id"]])
            store.put = real_put
            committed, error = ss.drain(store, state)
            self.assertIsNone(error)
            self.assertEqual(committed, [manifest["id"]])
            view = ss.read_game(store, GAME)
            self.assertEqual(view.heads, [manifest["id"]])
            self.assertEqual(view.pending, {})

    def test_a_staged_copy_that_changed_is_refused(self):
        state = self.state("deck")
        manifest = state.stage(GAME, "deck", self.source(b"x"))
        data = state.queued_data(manifest["id"])
        for folder, _d, files in os.walk(data):
            for name in files:
                if name.endswith(".sl2"):
                    write(os.path.join(folder, name), b"tampered")
        committed, error = ss.drain(self.store, state)
        self.assertEqual(committed, [])
        self.assertIsInstance(error, ss.StoreError)

    def test_a_corrupt_blob_is_refused_on_fetch(self):
        manifest = self.play("deck", b"x")
        record = [r for r in manifest["files"] if r["path"].endswith(".sl2")][0]
        import gzip
        self.store.put(ss.blob_key(record["sha256"]), gzip.compress(b"not it"))
        with self.assertRaises(ss.StoreError):
            ss.fetch(self.store, manifest, os.path.join(self.dir, "out"))


class TheQueue(Temp):
    def test_offline_sessions_form_a_chain(self):
        state = self.state("laptop")
        first = self.play("laptop", b"base")
        l1 = state.stage(GAME, "laptop", self.source(b"L1"))
        l2 = state.stage(GAME, "laptop", self.source(b"L2"))
        l3 = state.stage(GAME, "laptop", self.source(b"L3"))
        self.assertEqual(l1["parents"], [first["id"]])
        self.assertEqual(l2["parents"], [l1["id"]])
        self.assertEqual(l3["parents"], [l2["id"]])
        committed, error = ss.drain(self.store, state)
        self.assertIsNone(error)
        self.assertEqual(committed, [l1["id"], l2["id"], l3["id"]])
        self.assertEqual(ss.read_game(self.store, GAME).heads, [l3["id"]])
        self.assertEqual(state.base(GAME), l3["id"])

    def test_the_base_moves_only_once_the_chain_is_up(self):
        state = self.state("laptop")
        l1 = state.stage(GAME, "laptop", self.source(b"L1"))
        l2 = state.stage(GAME, "laptop", self.source(b"L2"))
        committed, error = ss.drain(self.store, state, only=l1["id"])
        self.assertEqual(committed, [l1["id"]])
        self.assertIsNone(state.base(GAME))
        ss.drain(self.store, state)
        self.assertEqual(state.base(GAME), l2["id"])

    def test_a_second_uploader_steps_aside(self):
        state = self.state("deck")
        state.stage(GAME, "deck", self.source(b"x"))
        lock = ss.QueueLock(os.path.join(state.root, "upload.lock"))
        self.assertTrue(lock.acquire())
        try:
            committed, error = ss.drain(self.store, state)
            self.assertEqual((committed, error), ([], None))
        finally:
            lock.release()

    def test_a_dead_uploaders_lock_is_taken_over(self):
        state = self.state("deck")
        path = os.path.join(state.root, "upload.lock")
        write(path, "99999")
        os.utime(path, (1, 1))
        self.assertTrue(ss.QueueLock(path).acquire())


class Forks(Temp):
    def test_another_device_playing_meanwhile_makes_a_fork(self):
        a = self.play("deck", b"A")
        laptop = self.state("laptop")
        laptop.set_base(GAME, a["id"])
        self.state("deck").set_base(GAME, a["id"])
        d1 = self.play("deck", b"D1")
        l1 = laptop.stage(GAME, "laptop", self.source(b"L1"))
        ss.drain(self.store, laptop)
        view = ss.read_game(self.store, GAME)
        self.assertEqual(sorted(view.heads), sorted([d1["id"], l1["id"]]))
        # Nothing was dropped: both saves are on the store.
        self.assertIn(d1["id"], view.manifests)
        self.assertIn(l1["id"], view.manifests)

    def test_choosing_closes_the_fork_with_no_upload(self):
        a = self.play("deck", b"A")
        self.state("deck").set_base(GAME, a["id"])
        laptop = self.state("laptop")
        laptop.set_base(GAME, a["id"])
        d1 = self.play("deck", b"D1")
        l1 = laptop.stage(GAME, "laptop", self.source(b"L1"))
        ss.drain(self.store, laptop)
        view = ss.read_game(self.store, GAME)
        blobs_before = len(self.store.list(ss.PREFIX + "blobs/"))
        merge = laptop.stage_merge(GAME, "laptop", view.manifests[d1["id"]], view.heads)
        ss.drain(self.store, laptop)
        view = ss.read_game(self.store, GAME)
        self.assertEqual(view.heads, [merge["id"]])
        self.assertEqual(len(self.store.list(ss.PREFIX + "blobs/")), blobs_before)
        self.assertEqual(ss.save_hashes(view.manifests[merge["id"]]),
                         ss.save_hashes(view.manifests[d1["id"]]))

    def test_simultaneous_commits_both_land(self):
        deck = self.state("deck")
        pc = self.state("pc")
        m1 = deck.stage(GAME, "deck", self.source(b"1"), parents=[])
        m2 = pc.stage(GAME, "pc", self.source(b"2"), parents=[])
        ss.drain(self.store, deck)
        ss.drain(self.store, pc)
        self.assertEqual(sorted(ss.read_game(self.store, GAME).heads),
                         sorted([m1["id"], m2["id"]]))


class Deciding(Temp):
    """Every row of the launch table in the spec."""

    def hashes(self, save):
        manifest = self.state("probe").stage(GAME, "probe", self.source(save), parents=[])
        return ss.save_hashes(manifest)

    def test_nothing_on_the_store_launches(self):
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, None, set(), "pc"), (ss.LAUNCH, None))

    def test_head_is_base_and_unchanged_launches(self):
        a = self.play("deck", b"A")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, a["id"], self.hashes(b"A"), "deck"),
                         (ss.LAUNCH, None))

    def test_a_newer_head_restores(self):
        a = self.play("deck", b"A")
        self.state("deck").set_base(GAME, a["id"])
        b = self.play("deck", b"B")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, a["id"], self.hashes(b"A"), "pc"),
                         (ss.RESTORE, b["id"]))

    def test_played_here_since_the_head_launches(self):
        a = self.play("deck", b"A")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, a["id"], self.hashes(b"MINE"), "pc"),
                         (ss.LAUNCH, None))

    def test_played_here_and_a_newer_head_asks(self):
        a = self.play("deck", b"A")
        self.state("deck").set_base(GAME, a["id"])
        b = self.play("deck", b"B")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, a["id"], self.hashes(b"MINE"), "pc"),
                         (ss.ASK, [b["id"]]))

    def test_a_fork_asks(self):
        m1 = self.state("deck").stage(GAME, "deck", self.source(b"1"), parents=[])
        m2 = self.state("pc").stage(GAME, "pc", self.source(b"2"), parents=[])
        ss.drain(self.store, self.state("deck"))
        ss.drain(self.store, self.state("pc"))
        action, heads = ss.decide(ss.read_game(self.store, GAME), m1["id"],
                                  self.hashes(b"1"), "deck")
        self.assertEqual(action, ss.ASK)
        self.assertEqual(sorted(heads), sorted([m1["id"], m2["id"]]))

    def test_another_devices_upload_in_flight_waits(self):
        a = self.play("deck", b"A")
        later = ss.stamp(ss.utc_now().replace(year=2099))
        self.store.put(ss.pending_key(GAME, "%s_desktop_deadbeef" % later),
                       json.dumps({"device": "desktop"}).encode())
        view = ss.read_game(self.store, GAME)
        action, detail = ss.decide(view, a["id"], self.hashes(b"A"), "deck")
        self.assertEqual(action, ss.WAIT)

    def test_my_own_unfinished_upload_does_not_make_me_wait(self):
        a = self.play("deck", b"A")
        later = ss.stamp(ss.utc_now().replace(year=2099))
        self.store.put(ss.pending_key(GAME, "%s_deck_deadbeef" % later), b"{}")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, a["id"], self.hashes(b"A"), "deck")[0],
                         ss.LAUNCH)

    def test_a_new_device_with_no_save_restores(self):
        a = self.play("deck", b"A")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, None, set(), "pc"), (ss.RESTORE, a["id"]))

    def test_a_new_device_with_the_same_save_adopts_it(self):
        a = self.play("deck", b"A")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, None, self.hashes(b"A"), "pc"),
                         (ss.LAUNCH, a["id"]))

    def test_a_device_that_fell_behind_restores_without_asking(self):
        """After the import: the Deck holds an older save the head descends from."""
        a = self.play("deck", b"A")
        self.state("deck").set_base(GAME, a["id"])
        b = self.play("deck", b"B")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, None, self.hashes(b"A"), "pc"),
                         (ss.RESTORE, b["id"]))

    def test_a_new_device_with_a_different_save_asks(self):
        a = self.play("deck", b"A")
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, None, self.hashes(b"OTHER"), "pc"),
                         (ss.ASK, [a["id"]]))

    def test_a_head_on_a_branch_we_never_had_asks(self):
        a = self.state("deck").stage(GAME, "deck", self.source(b"A"), parents=[])
        ss.drain(self.store, self.state("deck"))
        other = self.state("pc").stage(GAME, "pc", self.source(b"P"), parents=[])
        ss.drain(self.store, self.state("pc"))
        # Only `other` is visible to a device whose base is `a`, from a store
        # where `a` was removed.
        self.store.delete(ss.manifest_key(GAME, a["id"]))
        view = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(view, a["id"], self.hashes(b"A"), "deck"),
                         (ss.ASK, [other["id"]]))

    def test_an_unreadable_store_is_unknown(self):
        self.assertEqual(ss.decide(None, "x", set(), "pc"), (ss.UNKNOWN, None))


class Caching(Temp):
    def test_a_manifest_is_read_from_the_store_once(self):
        self.play("deck", b"A")
        cache = os.path.join(self.dir, "cache")
        ss.read_game(self.store, GAME, cache_dir=cache)
        real_get = self.store.get
        asked = []
        self.store.get = lambda key: asked.append(key) or real_get(key)
        ss.read_game(self.store, GAME, cache_dir=cache)
        self.assertEqual([k for k in asked if "/snapshots/" in k], [])


class S3Signing(unittest.TestCase):
    """The request shape, checked against a recorded opener."""

    def make(self, **kw):
        self.sent = []

        class Response(object):
            status = 200
            headers = {}

            def __init__(self, body=b""):
                self.body = body

            def read(self):
                return self.body

        def opener(request, timeout=None):
            self.sent.append(request)
            return Response(b"<ListBucketResult><IsTruncated>false</IsTruncated>"
                            b"<Contents><Key>p/blockslot/v1/x&amp;y</Key></Contents>"
                            b"</ListBucketResult>")

        return ss.S3Store("https://s3.example.com", "saves", "AK", "SK",
                          prefix="p", opener=opener, **kw)

    def test_path_style_url_and_signature(self):
        store = self.make()
        store.put("blockslot/v1/a b", b"x")
        request = self.sent[0]
        self.assertEqual(request.full_url, "https://s3.example.com/saves/p/blockslot/v1/a%20b")
        self.assertTrue(request.get_header("Authorization").startswith(
            "AWS4-HMAC-SHA256 Credential=AK/"))
        self.assertIsNone(request.get_header("Cf-access-client-id"))

    def test_it_names_itself_not_urllib(self):
        store = self.make()
        store.exists("x")
        self.assertEqual(self.sent[0].get_header("User-agent"), ss.USER_AGENT)

    def test_the_cf_token_rides_on_every_request(self):
        store = self.make(cf_token=("id.access", "secret"))
        store.list("blockslot/v1/")
        request = self.sent[0]
        self.assertEqual(request.get_header("Cf-access-client-id"), "id.access")
        self.assertEqual(request.get_header("Cf-access-client-secret"), "secret")

    def test_listing_strips_the_prefix_and_unescapes(self):
        store = self.make()
        self.assertEqual(store.list("blockslot/v1/"), ["blockslot/v1/x&y"])

    def test_a_cloudflare_403_is_refused_in_words(self):
        error = ss._s3_error(403, b"<html>Cloudflare Access</html>", {"cf-ray": "x"})
        self.assertIsInstance(error, ss.StoreRefused)
        self.assertIn("service token", str(error))

    def test_a_gateway_error_is_offline(self):
        self.assertIsInstance(ss._s3_error(502, b"", {}), ss.StoreOffline)


class Certificates(unittest.TestCase):
    def test_an_empty_store_loads_the_system_bundle(self):
        import ssl
        real = ssl.create_default_context

        class Empty(object):
            loaded = None

            def cert_store_stats(self):
                return {"x509_ca": 0}

            def load_verify_locations(self, cafile=None):
                self.loaded = cafile

        ssl.create_default_context = lambda: Empty()
        try:
            bundle = tempfile.NamedTemporaryFile(delete=False)
            bundle.close()
            if ss.sys.platform == "win32":
                self.skipTest("Windows keeps its default context")
            context = ss.tls_context(bundles=("/nope", bundle.name))
            self.assertEqual(context.loaded, bundle.name)
        finally:
            ssl.create_default_context = real
            os.unlink(bundle.name)


class SSHCommands(unittest.TestCase):
    def test_errors_split_into_refused_and_offline(self):
        self.assertIsInstance(ss._ssh_error(b"Permission denied (publickey).", "h"),
                              ss.StoreRefused)
        self.assertIsInstance(ss._ssh_error(b"ssh: connect to host h port 22: "
                                            b"Connection timed out", "h"),
                              ss.StoreOffline)

    def test_the_cf_token_travels_in_the_environment(self):
        seen = {}

        def runner(argv, **kw):
            seen["argv"] = argv
            seen["env"] = kw["env"]

            class Done(object):
                returncode = 0
                stdout = b""
                stderr = b""
            return Done()

        store = ss.SSHStore("saves.example.com", "/data", cf_token=("id", "sec"),
                            runner=runner)
        store.exists("blockslot/v1/x")
        self.assertNotIn("sec", " ".join(seen["argv"]))
        self.assertEqual(seen["env"]["TUNNEL_SERVICE_TOKEN_SECRET"], "sec")
        self.assertIn("ProxyCommand=cloudflared access ssh --hostname %h",
                      seen["argv"])


class AtAGlance(Temp):
    def test_the_newest_snapshot_of_each_game_in_one_listing(self):
        self.play("deck", b"A")
        later = self.state("pc").stage(GAME, "pc", self.source(b"B"), parents=[],
                                       created=ss.utc_now() + ss.datetime.timedelta(hours=1))
        ss.drain(self.store, self.state("pc"))
        calls = []
        real = self.store.list
        self.store.list = lambda prefix: calls.append(prefix) or real(prefix)
        newest = ss.newest_on_store(self.store)
        self.assertEqual(len(calls), 1)
        self.assertEqual(newest[ss.game_key(GAME)][1], "pc")
        self.assertEqual(newest[ss.game_key(GAME)][0], later["created"])


class KnownBlobs(Temp):
    def test_a_known_blob_is_not_asked_about(self):
        state = self.state("deck")
        self.play("deck", b"A")
        asked = []
        real = self.store.exists
        self.store.exists = lambda key: asked.append(key) or real(key)
        state.stage(GAME, "deck", self.source(b"A"))
        ss.drain(self.store, state)
        self.assertEqual(asked, [])

    def test_the_cache_survives_a_restart(self):
        state = self.state("deck")
        self.play("deck", b"A")
        self.assertEqual(len(ss.LocalState(state.root).known_blobs()), 2)


class Trees(Temp):
    def test_each_device_contributes_its_newest_tree(self):
        deck = self.state("deck")
        old = deck.stage(GAME, "deck", self.source(b"old"), mode="tree")
        new = deck.stage(GAME, "deck", self.source(b"new"), mode="tree")
        pc = self.state("pc").stage(GAME, "pc", self.source(b"pc"), mode="tree")
        ss.drain(self.store, deck)
        ss.drain(self.store, self.state("pc"))
        best = ss.newest_per_device(ss.read_game(self.store, GAME))
        self.assertEqual(best["deck"]["id"], new["id"])
        self.assertEqual(best["pc"]["id"], pc["id"])

    def test_single_files_come_back_with_their_time(self):
        manifest = self.play("deck", b"one file")
        record = [r for r in manifest["files"] if r["path"].endswith(".sl2")][0]
        target = os.path.join(self.dir, "tree", "snes", "game.srm")
        written, failed = ss.fetch_blobs(self.store, [dict(record, path=target)])
        self.assertEqual((written, failed), (1, []))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"one file")
        self.assertEqual(ss.iso(ss.datetime.datetime.fromtimestamp(
            os.path.getmtime(target), ss.datetime.timezone.utc)), record["mtime"])

    def test_a_missing_blob_fails_that_file_only(self):
        written, failed = ss.fetch_blobs(self.store, [
            {"sha256": "0" * 64, "path": os.path.join(self.dir, "x"), "mtime": None}])
        self.assertEqual(written, 0)
        self.assertEqual(len(failed), 1)


class Cleaning(Temp):
    def age(self, days):
        """Make every object on the store look `days` old."""
        when = ss.utc_now().timestamp() - days * 86400
        for folder, _d, files in os.walk(self.store.root):
            for name in files:
                os.utime(os.path.join(folder, name), (when, when))

    def history(self, count, device="deck", start_days_ago=100):
        state = self.state(device)
        made = []
        for i in range(count):
            created = ss.utc_now() - ss.datetime.timedelta(days=start_days_ago - i)
            made.append(state.stage(GAME, device, self.source(b"v%d" % i),
                                    created=created))
        ss.drain(self.store, state)
        return made

    def test_old_history_goes_and_the_newest_ten_stay(self):
        made = self.history(14)
        self.age(60)
        result = ss.clean(self.store)
        view = ss.read_game(self.store, GAME)
        self.assertEqual(result["removed_snapshots"], 4)
        self.assertEqual(sorted(view.manifests), sorted(m["id"] for m in made[-10:]))
        # Every kept snapshot can still be fetched.
        for manifest in view.manifests.values():
            ss.fetch(self.store, manifest, tempfile.mkdtemp(dir=self.dir))

    def test_the_last_thirty_days_are_kept_whatever_the_count(self):
        self.history(14, start_days_ago=20)
        self.assertEqual(ss.clean(self.store)["removed_snapshots"], 0)

    def test_a_young_blob_is_never_removed(self):
        self.history(14)
        # Young: written just now. The four old snapshots go, their blobs stay.
        result = ss.clean(self.store)
        self.assertEqual(result["removed_blobs"], 0)

    def test_an_upload_in_progress_stops_the_clean(self):
        self.history(14)
        self.age(60)
        self.store.put(ss.pending_key(GAME, "%s_pc_deadbeef" % ss.stamp()), b"{}")
        self.assertTrue(ss.clean(self.store)["skipped"])

    def test_a_head_is_kept_even_when_old(self):
        made = self.history(1, device="pc", start_days_ago=400)
        self.history(12, device="deck")
        self.age(400)
        ss.clean(self.store)
        self.assertIn(made[0]["id"], ss.read_game(self.store, GAME).manifests)

    def test_an_abandoned_upload_marker_goes_after_a_week(self):
        old = ss.stamp(ss.utc_now() - ss.datetime.timedelta(days=9))
        key = ss.pending_key(GAME, "%s_pc_deadbeef" % old)
        self.store.put(key, b"{}")
        ss.clean(self.store)
        self.assertFalse(self.store.exists(key))

    def test_a_dry_run_removes_nothing(self):
        self.history(14)
        self.age(60)
        before = self.store.list(ss.PREFIX)
        ss.clean(self.store, dry_run=True)
        self.assertEqual(self.store.list(ss.PREFIX), before)


class Importing(Temp):
    """The Syncthing gamesaves folder, moved onto the store."""

    def gamesaves(self):
        root = os.path.join(self.dir, "gamesaves")
        for device, stamps in (("deck", ["20260920T010000Z", "20260922T010000Z"]),
                               ("desktop", ["20260921T010000Z"])):
            game = os.path.join(root, device, "Dark Souls II_ Scholar of the First Sin")
            write(os.path.join(game, "mapping.yaml"),
                  'name: "%s"\nbackups: []\n' % GAME)
            for stamp in stamps:
                write(os.path.join(game, "backup-" + stamp, "drive-C", "s.sl2"),
                      "%s-%s" % (device, stamp))
        return root

    def test_every_backup_arrives_and_the_game_has_one_head(self):
        heads = ss.import_backups(self.store, self.gamesaves())
        view = ss.read_game(self.store, GAME)
        self.assertEqual(len(view.manifests), 4)   # 3 backups and 1 merge
        self.assertEqual(view.heads, [heads[GAME]])
        head = view.manifests[heads[GAME]]
        # The newest backup is the deck's second one; the head holds it.
        self.assertIn("20260922T010000Z", " ".join(r["path"] for r in head["files"]))

    def test_the_newest_save_wins_not_the_newest_backup(self):
        root = self.gamesaves()
        # The desktop backup is older by name than the deck's second one,
        # but its save file is newer.
        save = os.path.join(root, "desktop", "Dark Souls II_ Scholar of the First Sin",
                            "backup-20260921T010000Z", "drive-C", "s.sl2")
        os.utime(save, (2000000000, 2000000000))
        heads = ss.import_backups(self.store, root)
        head = ss.read_game(self.store, GAME).manifests[heads[GAME]]
        self.assertIn("20260921T010000Z", " ".join(r["path"] for r in head["files"]))

    def test_one_devices_backups_chain_in_time_order(self):
        ss.import_backups(self.store, self.gamesaves())
        view = ss.read_game(self.store, GAME)
        deck = sorted(sid for sid in view.manifests
                      if ss.snap_device(sid) == "deck"
                      and not view.manifests[sid].get("merge_only"))
        self.assertEqual(view.manifests[deck[1]]["parents"], [deck[0]])

    def test_importing_twice_changes_nothing(self):
        root = self.gamesaves()
        first = ss.import_backups(self.store, root)
        keys = self.store.list(ss.PREFIX)
        # A later run: every file it writes itself has a later time. On
        # 2026-09-24 that alone doubled every chain a first, cut-off run had
        # reached.
        import time as _time
        _time.sleep(1.1)
        second = ss.import_backups(self.store, root)
        self.assertEqual(first, second)
        self.assertEqual(self.store.list(ss.PREFIX), keys)

    def test_a_mapping_keeps_only_its_own_backup(self):
        text = ('---\nname: "G"\ndrives:\n  drive-0: ""\nbackups:\n'
                '  - name: backup-1\n    when: "a"\n    files:\n      /x:\n'
                '        hash: 1\n    children: []\n'
                '  - name: backup-2\n    when: "b"\n    files: {}\n    children: []\n')
        one = ss.filter_mapping(text, "backup-2")
        self.assertIn("backup-2", one)
        self.assertNotIn("backup-1", one)
        self.assertIn('drive-0: ""', one)
        self.assertTrue(one.startswith('---\nname: "G"'))

    def test_a_dry_run_writes_nothing(self):
        ss.import_backups(self.store, self.gamesaves(), dry_run=True)
        self.assertEqual(self.store.list(ss.PREFIX), [])

    def test_a_device_that_already_has_the_head_adopts_it(self):
        heads = ss.import_backups(self.store, self.gamesaves())
        view = ss.read_game(self.store, GAME)
        hashes = ss.save_hashes(view.manifests[heads[GAME]])
        self.assertEqual(ss.decide(view, None, hashes, "deck"), (ss.LAUNCH, heads[GAME]))


@unittest.skipUnless(os.environ.get("BLOCKSLOT_TEST_S3"), "no test S3 server")
class RealS3(Temp):
    """End to end against a real S3 server (MinIO)."""

    def setUp(self):
        Temp.setUp(self)
        fields = os.environ["BLOCKSLOT_TEST_S3"].split(",")
        endpoint, bucket, access, secret = fields[:4]
        self.region = fields[4] if len(fields) > 4 else "us-east-1"
        self.store = ss.S3Store(endpoint, bucket, access, secret,
                                region=self.region, prefix="t%d" % os.getpid())

    def test_round_trip_list_and_delete(self):
        self.store.put("blockslot/v1/a/b", b"hello")
        self.assertEqual(self.store.get("blockslot/v1/a/b"), b"hello")
        self.assertTrue(self.store.exists("blockslot/v1/a/b"))
        self.assertIn("blockslot/v1/a/b", self.store.list("blockslot/v1/"))
        self.store.delete("blockslot/v1/a/b")
        self.assertFalse(self.store.exists("blockslot/v1/a/b"))

    def test_a_full_snapshot(self):
        manifest = self.play("deck", b"real s3 save")
        out = os.path.join(self.dir, "out")
        ss.fetch(self.store, manifest, out)
        self.assertEqual(ss.scan_dir(out), manifest["files"])
        self.assertEqual(ss.read_game(self.store, GAME).heads, [manifest["id"]])

    def test_a_multipart_blob(self):
        big = os.path.join(self.dir, "big.bin")
        with open(big, "wb") as handle:
            handle.write(os.urandom(ss.MULTIPART_OVER + 1024))
        self.store.put("blockslot/v1/big", big)
        with open(big, "rb") as handle:
            self.assertEqual(self.store.get("blockslot/v1/big"), handle.read())

    def test_listing_carries_the_write_time(self):
        self.store.put("blockslot/v1/t", b"x")
        (key, when), = self.store.list_times("blockslot/v1/t")
        self.assertLess(abs((ss.utc_now() - when).total_seconds()), 300)

    def test_a_wrong_secret_is_refused(self):
        bad = ss.S3Store(self.store.endpoint, self.store.bucket, "nobody", "wrong",
                         region=self.region)
        with self.assertRaises(ss.StoreRefused):
            bad.list("blockslot/")


@unittest.skipUnless(os.environ.get("BLOCKSLOT_TEST_SSH"), "no test SSH server")
class RealSSH(Temp):
    """End to end against a real SSH server. BLOCKSLOT_TEST_SSH=host:/root/dir"""

    def setUp(self):
        Temp.setUp(self)
        host, root = os.environ["BLOCKSLOT_TEST_SSH"].split(":", 1)
        self.store = ss.SSHStore(host, "%s/t%d" % (root.rstrip("/"), os.getpid()))

    def tearDown(self):
        self.store._run("rm -rf %s" % self.store.root)
        Temp.tearDown(self)

    def test_round_trip_list_and_delete(self):
        self.store.put("blockslot/v1/a b/c", b"hello")
        self.assertEqual(self.store.get("blockslot/v1/a b/c"), b"hello")
        self.assertIn("blockslot/v1/a b/c", self.store.list("blockslot/v1/"))
        self.store.delete("blockslot/v1/a b/c")
        self.assertFalse(self.store.exists("blockslot/v1/a b/c"))
        with self.assertRaises(ss.NotFound):
            self.store.get("blockslot/v1/a b/c")

    def test_a_full_snapshot(self):
        manifest = self.play("deck", b"real ssh save")
        out = os.path.join(self.dir, "out")
        ss.fetch(self.store, manifest, out)
        self.assertEqual(ss.scan_dir(out), manifest["files"])

    def test_listing_carries_the_write_time(self):
        self.store.put("blockslot/v1/t", b"x")
        (key, when), = self.store.list_times("blockslot/v1/")
        self.assertEqual(key, "blockslot/v1/t")
        self.assertLess(abs((ss.utc_now() - when).total_seconds()), 300)

    def test_an_unknown_host_is_offline(self):
        gone = ss.SSHStore("no-such-host.invalid", "/tmp/x")
        with self.assertRaises(ss.StoreOffline):
            gone.list("blockslot/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
