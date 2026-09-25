"""The save browser, restore and settle, the zip, and the settings documents,
all on a slotstore.LocalStore with snapshots made the way devices make them."""

import io
import json
import zipfile

import helpers
from helpers import GAME, ss
from blockslot_server import saves


class Catalog(helpers.StoreCase):
    def test_lists_games_and_library_games(self):
        self.play("deck", b"one")
        unit = {"library": "RetroFrontend", "id": "snes/Super Metroid (USA)",
                "title": "Super Metroid (USA) (SNES)", "system": "snes",
                "label": "RetroArch (SNES)"}
        name = ss.library_unit_name("RetroFrontend", unit["id"])
        self.play("desktop", b"sram", game=name, unit=unit)
        unlabeled = dict(unit, id="gc/GM4E", title="GM4E (GC)", system="gc", label=None)
        self.play("deck", b"gci", game=ss.library_unit_name("RetroFrontend", "gc/GM4E"),
                  unit=unlabeled)
        rows = {row["title"]: row for row in saves.Catalog(self.store).games()}
        self.assertEqual(rows[GAME]["library"], None)
        self.assertEqual(rows[GAME]["device"], "deck")
        metroid = rows["Super Metroid (USA) (SNES)"]
        self.assertEqual((metroid["library"], metroid["label"]), ("RetroFrontend", "RetroArch (SNES)"))
        self.assertEqual(metroid["key"], name)
        self.assertEqual(rows["GM4E (GC)"]["label"], "Dolphin")      # from saveunits

    def test_two_saves_marker(self):
        base = self.play("deck", b"one")
        self.state("desktop").set_base(GAME, base["id"])
        self.play("deck", b"deck-two")
        self.play("desktop", b"desktop-two")
        row = saves.Catalog(self.store).games()[0]
        self.assertEqual(row["heads"], 2)
        self.assertEqual(row["snapshots"], 3)

    def test_manifests_are_cached_on_disk(self):
        self.play("deck", b"one")
        cache = self.dir + "/cache"
        saves.Catalog(self.store, cache).games()
        calls = []
        original = self.store.get
        self.store.get = lambda key: calls.append(key) or original(key)
        saves.Catalog(self.store, cache).games()
        self.assertEqual(calls, [])

    def test_history(self):
        first = self.play("deck", b"one", played={"start": "2026-09-24T01:00:00Z",
                                                  "end": "2026-09-24T02:00:00Z"})
        second = self.play("deck", b"two")
        view = saves.Catalog(self.store).view(ss.game_key(GAME))
        rows = saves.history(view)
        self.assertEqual([r["id"] for r in rows], [second["id"], first["id"]])
        self.assertTrue(rows[0]["head"])
        self.assertFalse(rows[1]["head"])
        self.assertEqual(rows[1]["played_start"], "2026-09-24T01:00:00Z")
        self.assertGreater(rows[1]["bytes"], 0)

    def test_bad_game_dir_is_no_view(self):
        self.assertIsNone(saves.Catalog(self.store).view("../etc"))


class Restore(helpers.StoreCase):
    def test_restore_an_older_save(self):
        old = self.play("deck", b"old")
        new = self.play("deck", b"new")
        blobs_before = self.store.list(ss.PREFIX + "blobs/")
        catalog = saves.Catalog(self.store)
        view = catalog.view(ss.game_key(GAME))
        made = saves.restore(self.store, view, old["id"])

        self.assertEqual(self.store.list(ss.PREFIX + "blobs/"), blobs_before)  # no upload
        self.assertEqual(made["parents"], [new["id"]])
        self.assertEqual(made["files"], old["files"])
        self.assertTrue(made["merge_only"])
        self.assertEqual(made["restored_from"], old["id"])
        self.assertEqual(made["device"], saves.SERVER_DEVICE)
        self.assertEqual(self.store.list(ss.game_prefix(GAME) + "pending/"), [])
        after = ss.read_game(self.store, GAME)
        self.assertEqual(after.heads, [made["id"]])

        # A device whose base is the old head restores the new one at launch.
        action, head = ss.decide(after, new["id"], ss.save_hashes(new), "deck")
        self.assertEqual((action, head), (ss.RESTORE, made["id"]))

    def test_restore_names_every_head(self):
        base = self.play("deck", b"one")
        self.state("desktop").set_base(GAME, base["id"])
        deck = self.play("deck", b"deck-two")
        desktop = self.play("desktop", b"desktop-two")
        view = saves.Catalog(self.store).view(ss.game_key(GAME))
        made = saves.restore(self.store, view, base["id"])
        self.assertEqual(made["parents"], sorted([deck["id"], desktop["id"]]))
        self.assertEqual(ss.read_game(self.store, GAME).heads, [made["id"]])

    def test_settle_two_saves(self):
        base = self.play("deck", b"one")
        self.state("desktop").set_base(GAME, base["id"])
        deck = self.play("deck", b"deck-two")
        desktop = self.play("desktop", b"desktop-two")
        view = saves.Catalog(self.store).view(ss.game_key(GAME))
        with self.assertRaises(saves.SavesError):
            saves.restore(self.store, view, base["id"], settle=True)   # not a head
        made = saves.restore(self.store, view, deck["id"], settle=True)
        self.assertEqual(made["parents"], sorted([deck["id"], desktop["id"]]))
        self.assertEqual(ss.save_hashes(made), ss.save_hashes(deck))
        # The deck already has this save: it adopts the new head without a restore.
        after = ss.read_game(self.store, GAME)
        self.assertEqual(ss.decide(after, deck["id"], ss.save_hashes(deck), "deck"),
                         (ss.LAUNCH, made["id"]))
        # The desktop, which played the other save, restores the kept one.
        self.assertEqual(ss.decide(after, desktop["id"], ss.save_hashes(desktop), "desktop"),
                         (ss.RESTORE, made["id"]))

    def test_settle_needs_two_saves(self):
        only = self.play("deck", b"one")
        view = saves.Catalog(self.store).view(ss.game_key(GAME))
        with self.assertRaises(saves.SavesError):
            saves.restore(self.store, view, only["id"], settle=True)
        with self.assertRaises(saves.SavesError):
            saves.restore(self.store, view, only["id"])                # already current

    def test_library_unit_keeps_its_label(self):
        unit = {"library": "RetroFrontend", "id": "snes/X", "title": "X (SNES)", "system": "snes"}
        name = ss.library_unit_name("RetroFrontend", "snes/X")
        old = self.play("deck", b"a", game=name, unit=unit)
        self.play("deck", b"b", game=name, unit=unit)
        view = saves.Catalog(self.store).view(name)
        made = saves.restore(self.store, view, old["id"])
        self.assertEqual(made["unit"], unit)
        self.assertEqual(made["game"], name)

    def test_missing_blob_refuses(self):
        old = self.play("deck", b"old")
        self.play("deck", b"new")
        view = saves.Catalog(self.store).view(ss.game_key(GAME))
        record = [r for r in old["files"] if r["path"].endswith("save.sl2")][0]
        self.store.delete(ss.blob_key(record["sha256"]))
        with self.assertRaises(ss.StoreRefused):
            saves.restore(self.store, view, old["id"])


class Zip(helpers.StoreCase):
    def test_zip_holds_the_snapshot(self):
        manifest = self.play("deck", b"the save bytes")
        out = io.BytesIO()
        saves.write_zip(self.store, manifest, out)
        with zipfile.ZipFile(io.BytesIO(out.getvalue())) as archive:
            names = sorted(archive.namelist())
            self.assertEqual(names, sorted(r["path"] for r in manifest["files"]))
            self.assertEqual(archive.read("g/backup-1/drive-C/save.sl2"), b"the save bytes")


class Settings(helpers.StoreCase):
    TREES = {"trees": {
        "RetroFrontend": {
            "roots": {"deck": "/run/media/deck/x/retrodeck/saves",
                      "desktop": "C:/Users/p/Apps/RetroBat/saves"},
            "system_aliases": [["gc", "gamecube"], ["mame", "mame-sa"]],
            "extensions": ["srm", ".SAV"], "always_dirs": ["mame"]},
        "Bloodborne": {
            "roots": {"deck": "/home/deck/.local/share/shadPS4/savedata/CUSA00900"},
            "extensions": "*", "one_game": "Bloodborne", "system": "ps4", "label": "shadPS4"}}}

    def test_shared_round_trip(self):
        doc = {"libraries": {"Bloodborne": {"one_game": "Bloodborne", "system": "ps4",
                                            "label": "shadPS4", "extensions": "*"}},
               "retention": {"per_device": 5, "days": 14}}
        written = saves.write_shared(self.store, doc)
        read = saves.read_shared(self.store)
        self.assertEqual(read, written)
        self.assertEqual(read["version"], 1)
        self.assertEqual(read["retention"], {"per_device": 5, "days": 14})
        self.assertEqual(read["libraries"]["Bloodborne"]["extensions"], "*")
        raw = json.loads(self.store.get(ss.PREFIX + "config/shared.json"))
        self.assertIn("updated", raw)

    def test_shared_rejects_bad_values(self):
        for bad in ({"libraries": []}, {"libraries": {"a/b": {}}},
                    {"retention": {"days": -1}}, {"retention": {"per_device": True}},
                    {"libraries": {"x": {"extensions": "srm"}}},
                    {"libraries": {"x": {"system_aliases": ["gc"]}}}):
            with self.subTest(bad=bad):
                with self.assertRaises(saves.SavesError):
                    saves.clean_shared(bad)

    def test_device_round_trip(self):
        saves.write_device(self.store, "Deck", name="Steam Deck",
                           roots={"RetroFrontend": "/saves", "Empty": " "})
        doc = saves.read_device(self.store, "deck")
        self.assertEqual(doc["device"], "deck")
        self.assertEqual(doc["name"], "Steam Deck")
        self.assertEqual(doc["roots"], {"RetroFrontend": "/saves"})
        self.assertEqual(doc["set_by"], "web")
        self.assertEqual(doc["version"], 1)
        # A name-only change keeps the folders.
        saves.write_device(self.store, "deck", name="Deck")
        self.assertEqual(saves.read_device(self.store, "deck")["roots"], {"RetroFrontend": "/saves"})
        self.assertEqual(set(saves.device_docs(self.store)), {"deck"})

    def test_import_trees(self):
        shared, devices = saves.import_trees(self.store, json.dumps(self.TREES))
        self.assertEqual(devices, ["deck", "desktop"])
        retro = shared["libraries"]["RetroFrontend"]
        self.assertEqual(retro["extensions"], ["srm", "sav"])
        self.assertEqual(retro["system_aliases"], [["gc", "gamecube"], ["mame", "mame-sa"]])
        self.assertNotIn("roots", retro)
        self.assertEqual(shared["libraries"]["Bloodborne"]["one_game"], "Bloodborne")
        deck = saves.read_device(self.store, "deck")
        self.assertEqual(set(deck["roots"]), {"RetroFrontend", "Bloodborne"})
        self.assertEqual(saves.read_device(self.store, "desktop")["roots"],
                         {"RetroFrontend": "C:/Users/p/Apps/RetroBat/saves"})
        # The trees object alone works too.
        libraries, _roots = saves.parse_trees(json.dumps(self.TREES["trees"]))
        self.assertEqual(set(libraries), {"RetroFrontend", "Bloodborne"})

    def test_import_rejects_nonsense(self):
        for text in ("not json", "[]", "{}", '{"trees": {"x": 3}}'):
            with self.subTest(text=text):
                with self.assertRaises(saves.SavesError):
                    saves.parse_trees(text)


class Devices(helpers.StoreCase):
    def test_devices_on_store_leave_out_the_server(self):
        old = self.play("deck", b"a")
        self.play("deck", b"b")
        self.play("desktop", b"c")
        view = saves.Catalog(self.store).view(ss.game_key(GAME))
        saves.restore(self.store, view, old["id"])
        found = saves.devices_on_store(self.store.list_times(ss.PREFIX + "games/"))
        self.assertEqual(set(found), {"deck", "desktop"})


class Clean(helpers.StoreCase):
    def test_retention_applies_and_is_put_back(self):
        import datetime
        for n in range(4):
            # Distinct seconds: clean() ranks a device's saves by id.
            self.play("deck", b"save %d" % n,
                      created=ss.utc_now() - datetime.timedelta(minutes=10 - n))
        before = (ss.KEEP_PER_DEVICE, ss.KEEP_DAYS)
        later = ss.utc_now() + datetime.timedelta(days=60)
        preview = saves.run_clean(self.store, {"per_device": 2, "days": 30}, dry_run=True, now=later)
        self.assertEqual(preview["removed_snapshots"], 2)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(len(self.store.list(ss.game_prefix(GAME) + "snapshots/")), 4)
        done = saves.run_clean(self.store, {"per_device": 2, "days": 30}, now=later)
        self.assertEqual(done["removed_snapshots"], 2)
        self.assertEqual(len(self.store.list(ss.game_prefix(GAME) + "snapshots/")), 2)
        self.assertEqual((ss.KEEP_PER_DEVICE, ss.KEEP_DAYS), before)


if __name__ == "__main__":
    import unittest
    unittest.main()
