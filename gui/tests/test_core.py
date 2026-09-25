#!/usr/bin/env python3
"""Tests for everything under gui/core.

No display, no Steam, no network. Each test builds the files it reads, so the
suite says the same thing on a laptop, on the Deck and in CI.

    python3 gui/tests/test_core.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.core import (appinfo, backups, catalog, engine, launchopts, logview,
                      model, paths, settings, shortcuts, steamdir, storecheck,
                      syncthing, vdf, wrap)


# ----------------------------------------------------------------- fixtures


LOCALCONFIG = '''"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"220"
\t\t\t\t\t{
\t\t\t\t\t\t"LastPlayed"\t\t"1600000000"
\t\t\t\t\t\t"LaunchOptions"\t\t"-novid %command%"
\t\t\t\t\t}
\t\t\t\t\t"400"
\t\t\t\t\t{
\t\t\t\t\t\t"LastPlayed"\t\t"1700000000"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\t"LastPlayedTimesSyncTime"\t\t"1789"
\t\t\t}
\t\t}
\t}
}
'''

APPMANIFEST = '''"AppState"
{
\t"appid"\t\t"%(appid)s"
\t"name"\t\t"%(name)s"
\t"installdir"\t\t"%(name)s"
\t"LastUpdated"\t\t"1700000000"
\t"SizeOnDisk"\t\t"123456"
}
'''

LIBRARYFOLDERS = '''"libraryfolders"
{
\t"0"
\t{
\t\t"path"\t\t"%s"
\t}
}
'''

LOGINUSERS = '''"users"
{
\t"76561198000265729"
\t{
\t\t"AccountName"\t\t"tester"
\t\t"PersonaName"\t\t"Tester"
\t\t"MostRecent"\t\t"1"
\t}
}
'''


def build_steam(base, apps=(("220", "Half-Life 2"), ("400", "Portal"))):
    """A Steam directory complete enough for every reader in core."""
    base = Path(base)
    (base / "steamapps").mkdir(parents=True, exist_ok=True)
    (base / "config").mkdir(parents=True, exist_ok=True)
    user = base / "userdata" / "40000001" / "config"
    user.mkdir(parents=True, exist_ok=True)
    (base / "steamapps" / "libraryfolders.vdf").write_text(
        LIBRARYFOLDERS % str(base).replace("\\", "\\\\"), encoding="utf-8")
    for appid, name in apps:
        (base / "steamapps" / ("appmanifest_%s.acf" % appid)).write_text(
            APPMANIFEST % {"appid": appid, "name": name}, encoding="utf-8")
    (base / "config" / "loginusers.vdf").write_text(LOGINUSERS, encoding="utf-8")
    (user / "localconfig.vdf").write_text(LOCALCONFIG, encoding="utf-8")
    return base


class Temp(unittest.TestCase):
    """A test case with a directory of its own."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-test-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)


# ----------------------------------------------------------------- vdf


class TextKeyValues(unittest.TestCase):
    def test_reads_nested_blocks(self):
        tree = vdf.parse_text(LOCALCONFIG)
        apps = tree["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]
        self.assertEqual(apps["220"]["LaunchOptions"], "-novid %command%")

    def test_keeps_a_value_that_looks_like_a_brace(self):
        tree = vdf.parse_text('"a" "{" "b" "}"')
        self.assertEqual(tree, {"a": "{", "b": "}"})

    def test_unescapes_backslashes_and_quotes(self):
        tree = vdf.parse_text(r'"path" "C:\\Games" "say" "a \"quoted\" word"')
        self.assertEqual(tree["path"], r"C:\Games")
        self.assertEqual(tree["say"], 'a "quoted" word')

    def test_skips_comments(self):
        tree = vdf.parse_text('// leading\n"a" "1" // trailing\n')
        self.assertEqual(tree, {"a": "1"})

    def test_an_unclosed_block_is_an_error(self):
        with self.assertRaises(vdf.VdfError):
            vdf.parse_text('"a" {')

    def test_an_extra_brace_is_an_error(self):
        with self.assertRaises(vdf.VdfError):
            vdf.parse_text('"a" { } }')

    def test_escape_round_trips(self):
        for value in (r"C:\Games\x", 'a "quoted" word', "plain"):
            self.assertEqual(vdf.unescape(vdf.escape(value)), value)

    def test_find_block_is_case_insensitive(self):
        text = '"Root"\n{\n\t"APPS"\n\t{\n\t\t"1" "2"\n\t}\n}\n'
        span = vdf.find_block(text, ["root", "apps"])
        self.assertIsNotNone(span)
        self.assertEqual(text[span[0]], "{")
        self.assertEqual(text[span[1]], "}")

    def test_find_block_returns_none_when_missing(self):
        self.assertIsNone(vdf.find_block(LOCALCONFIG, ["Nope", "apps"]))


class BinaryKeyValues(unittest.TestCase):
    def test_round_trips_the_types_steam_writes(self):
        tree = {"shortcuts": {"0": {"appid": 5, "AppName": "Game",
                                    "tags": {}}}}
        data = vdf.dump_binary(tree)
        again, offset = vdf.parse_binary(data, 0)
        self.assertEqual(again, tree)
        self.assertEqual(offset, len(data))

    def test_refuses_a_type_it_cannot_write(self):
        with self.assertRaises(vdf.VdfError):
            vdf.dump_binary({"a": 1.5})

    def test_reads_string_table_keys(self):
        table = ["alpha", "beta"]
        data = bytearray()
        data.append(vdf.BIN_STRING)
        data += (0).to_bytes(4, "little")
        data += b"value\x00"
        data.append(vdf.BIN_INT32)
        data += (1).to_bytes(4, "little")
        data += (7).to_bytes(4, "little", signed=True)
        data.append(vdf.BIN_END)
        tree, _ = vdf.parse_binary(bytes(data), 0, table)
        self.assertEqual(tree, {"alpha": "value", "beta": 7})


# ----------------------------------------------------------------- appinfo


def build_appinfo(entries):
    """A v29 appinfo.vdf holding the given {appid: tree}."""
    import struct
    strings = []

    def index(word):
        if word not in strings:
            strings.append(word)
        return strings.index(word)

    bodies = []
    for appid, tree in entries.items():
        # Two passes: the keys have to be in the table before they are written.
        def walk(node):
            for key, value in node.items():
                index(key)
                if isinstance(value, dict):
                    walk(value)
        walk(tree)
        bodies.append((appid, tree))

    payload = bytearray()
    payload += struct.pack("<II", appinfo.MAGIC_V29, 1)
    table_offset_at = len(payload)
    payload += struct.pack("<q", 0)
    for appid, tree in bodies:
        blob = vdf.dump_binary(tree, strings)
        header = struct.pack("<IIQ", 0, 0, 0) + b"\x00" * 20 + \
            struct.pack("<I", 0) + b"\x00" * 20
        payload += struct.pack("<II", appid, len(header) + len(blob))
        payload += header + blob
    payload += struct.pack("<I", 0)
    table_offset = len(payload)
    payload += struct.pack("<I", len(strings))
    for word in strings:
        payload += word.encode("utf-8") + b"\x00"
    payload[table_offset_at:table_offset_at + 8] = struct.pack("<q", table_offset)
    return bytes(payload)


class AppInfo(unittest.TestCase):
    def setUp(self):
        self.data = build_appinfo({
            10: {"appinfo": {"common": {"name": "Cloudy", "type": "Game"},
                             "ufs": {"savefiles": {"0": {"path": "save"}}}}},
            20: {"appinfo": {"common": {"name": "Local", "type": "Game"}}},
            30: {"appinfo": {"common": {"name": "Redist", "type": "Tool"}}},
            40: {"appinfo": {"common": {"name": "Quota", "type": "Game"},
                             "ufs": {"quota": 100, "maxnumfiles": 5}}},
        })

    def test_finds_autocloud_games(self):
        self.assertIn(10, appinfo.cloud_appids(self.data))

    def test_finds_sdk_games_by_their_quota(self):
        self.assertIn(40, appinfo.cloud_appids(self.data))

    def test_leaves_games_with_no_cloud_alone(self):
        self.assertNotIn(20, appinfo.cloud_appids(self.data))

    def test_reads_names_and_types(self):
        found = appinfo.scan(self.data)
        self.assertEqual(found["names"][30], "Redist")
        self.assertEqual(found["types"][30], "tool")

    def test_refuses_a_file_it_does_not_understand(self):
        with self.assertRaises(appinfo.AppInfoError):
            list(appinfo.iter_apps(b"\x01\x02\x03\x04\x00\x00\x00\x00"))


# ----------------------------------------------------------------- wrap


class LaunchOptionText(unittest.TestCase):
    def test_wraps_an_empty_option(self):
        built = wrap.build("/py", "/sp.py")
        self.assertEqual(built, "/py /sp.py -- %command%")

    def test_keeps_flags_that_had_no_command_token(self):
        built = wrap.build("/py", "/sp.py", "-windowed")
        self.assertEqual(built, "/py /sp.py -- %command% -windowed")

    def test_keeps_an_option_that_places_the_token_itself(self):
        built = wrap.build("/py", "/sp.py", "DXVK_HUD=1 %command% -nosplash")
        self.assertEqual(built, "/py /sp.py -- DXVK_HUD=1 %command% -nosplash")

    def test_quotes_a_path_with_spaces(self):
        built = wrap.build("C:/Program Files/py.exe", "/sp.py")
        self.assertTrue(built.startswith('"C:/Program Files/py.exe"'))

    def test_recognises_its_own_work(self):
        self.assertTrue(wrap.is_wrapped(wrap.build("/py", "/sp.py")))
        self.assertFalse(wrap.is_wrapped("-novid %command%"))
        self.assertFalse(wrap.is_wrapped(""))

    def test_strips_back_to_nothing(self):
        self.assertEqual(wrap.strip(wrap.build("/py", "/sp.py")), "")

    def test_strips_back_to_the_original_flags(self):
        built = wrap.build("/py", "/sp.py", "-windowed")
        self.assertEqual(wrap.strip(built), "%command% -windowed")

    def test_round_trips_a_tree(self):
        built = wrap.build("/py", "/sp.py", tree="RetroFrontend")
        self.assertEqual(wrap.tree_of(built), "RetroFrontend")

    def test_names_the_engine_it_points_at(self):
        built = wrap.build("/py", "/opt/savepick.py")
        self.assertEqual(wrap.engine_of(built), "/opt/savepick.py")

    def test_a_shortcut_names_its_target_rather_than_the_token(self):
        exe, options = wrap.build_shortcut("/py", "/sp.py", "C:/Games/rb.exe",
                                           tree="RetroFrontend")
        self.assertEqual(exe, "/py")
        self.assertNotIn("%command%", options)
        self.assertIn("C:/Games/rb.exe", options)

    def test_a_shortcut_unwraps_to_what_it_started_as(self):
        exe, options = wrap.build_shortcut("/py", "/sp.py", "C:/My Games/rb.exe",
                                           "-fullscreen")
        back_exe, back_options = wrap.unwrap_shortcut(exe, options)
        self.assertEqual(back_exe, "C:/My Games/rb.exe")
        self.assertEqual(back_options, "-fullscreen")

    def test_unwrapping_something_never_wrapped_changes_nothing(self):
        self.assertEqual(wrap.unwrap_shortcut("a.exe", "-x"), ("a.exe", "-x"))

    def test_borderless_only_carries_no_saves(self):
        built = wrap.build("/py", "/sp.py", borderless=True, sync=False)
        self.assertEqual(built, "/py /sp.py --borderless --no-sync -- %command%")
        self.assertTrue(wrap.is_wrapped(built))
        self.assertTrue(wrap.borderless_of(built))
        self.assertFalse(wrap.syncs(built))

    def test_sync_and_borderless_together(self):
        built = wrap.build("/py", "/sp.py", borderless=True)
        self.assertTrue(wrap.borderless_of(built))
        self.assertTrue(wrap.syncs(built))

    def test_a_plain_wrap_syncs_and_is_not_borderless(self):
        built = wrap.build("/py", "/sp.py")
        self.assertTrue(wrap.syncs(built))
        self.assertFalse(wrap.borderless_of(built))

    def test_nothing_unwrapped_syncs_or_is_borderless(self):
        self.assertFalse(wrap.syncs("-windowed"))
        self.assertFalse(wrap.borderless_of("-borderless %command%"))

    def test_a_game_flag_after_the_separator_is_not_ours(self):
        built = wrap.build("/py", "/sp.py", "%command% --borderless --no-sync")
        self.assertFalse(wrap.borderless_of(built))
        self.assertTrue(wrap.syncs(built))

    def test_a_save_set_is_dropped_without_sync(self):
        built = wrap.build("/py", "/sp.py", tree="RetroFrontend",
                           borderless=True, sync=False)
        self.assertIsNone(wrap.tree_of(built))

    def test_borderless_strips_back_to_the_original(self):
        built = wrap.build("/py", "/sp.py", "-windowed", borderless=True,
                           sync=False)
        self.assertEqual(wrap.strip(built), "%command% -windowed")

    def test_a_borderless_shortcut_unwraps_to_what_it_started_as(self):
        exe, options = wrap.build_shortcut("/py", "/sp.py", "C:/G/ds2.exe",
                                           "-x", borderless=True, sync=False)
        self.assertTrue(wrap.borderless_of(options))
        self.assertFalse(wrap.syncs(options))
        self.assertEqual(wrap.unwrap_shortcut(exe, options),
                         ("C:/G/ds2.exe", "-x"))


EXE = "C:/Apps/BlockSlot/Blockslot.exe"


class LaunchOptionTextFromTheExe(unittest.TestCase):
    """The Windows exe hosts the engine: `<Blockslot.exe> --pick -- ...`."""

    def test_the_three_cases_keep_what_was_there(self):
        self.assertEqual(wrap.build(EXE, wrap.PICK),
                         EXE + " --pick -- %command%")
        self.assertEqual(wrap.build(EXE, wrap.PICK, "-windowed"),
                         EXE + " --pick -- %command% -windowed")
        self.assertEqual(wrap.build(EXE, wrap.PICK, "DXVK_HUD=1 %command%"),
                         EXE + " --pick -- DXVK_HUD=1 %command%")

    def test_a_path_with_spaces_is_quoted_and_still_recognised(self):
        exe = "C:/Program Files/BlockSlot/Blockslot.exe"
        built = wrap.build(exe, wrap.PICK, "-windowed", borderless=True)
        self.assertEqual(built, '"%s" --pick --borderless -- %%command%% '
                                '-windowed' % exe)
        self.assertTrue(wrap.is_wrapped(built))
        self.assertTrue(wrap.borderless_of(built))
        self.assertTrue(wrap.syncs(built))
        self.assertEqual(wrap.engine_of(built), exe)

    def test_it_strips_back_to_the_original(self):
        self.assertEqual(wrap.strip(wrap.build(EXE, wrap.PICK)), "")
        self.assertEqual(wrap.strip(wrap.build(EXE, wrap.PICK, "-windowed")),
                         "%command% -windowed")
        self.assertEqual(
            wrap.strip(wrap.build(EXE, wrap.PICK, "DXVK_HUD=1 %command%")),
            "DXVK_HUD=1 %command%")

    def test_its_switches_round_trip(self):
        built = wrap.build(EXE, wrap.PICK, tree="RetroFrontend")
        self.assertEqual(wrap.tree_of(built), "RetroFrontend")
        built = wrap.build(EXE, wrap.PICK, borderless=True, sync=False)
        self.assertEqual(built, EXE + " --pick --borderless --no-sync -- "
                                      "%command%")
        self.assertFalse(wrap.syncs(built))

    def test_a_player_option_that_only_mentions_pick_is_not_ours(self):
        self.assertFalse(wrap.is_wrapped("%command% --pick -- x"))
        self.assertFalse(wrap.is_wrapped("--pick %command%"))
        self.assertFalse(wrap.is_wrapped("game.bin --pick -- x"))

    def test_a_shortcut_puts_the_exe_in_its_exe_field(self):
        exe, options = wrap.build_shortcut(EXE, wrap.PICK, "C:/My Games/rb.exe",
                                           "-fullscreen", tree="Retro")
        self.assertEqual(exe, EXE)
        self.assertEqual(options,
                         '--pick --tree Retro -- "C:/My Games/rb.exe" '
                         '-fullscreen')
        self.assertTrue(wrap.is_wrapped(options))
        self.assertEqual(wrap.tree_of(options), "Retro")
        self.assertEqual(wrap.unwrap_shortcut(exe, options),
                         ("C:/My Games/rb.exe", "-fullscreen"))

    def test_a_shortcut_names_its_engine_through_its_exe(self):
        _exe, options = wrap.build_shortcut(EXE, wrap.PICK, "C:/G/a.exe")
        self.assertEqual(wrap.engine_of(options, '"%s"' % EXE), EXE)
        self.assertEqual(wrap.engine_of(options), wrap.PICK)

    def test_an_old_python_wrap_is_rewrapped_keeping_the_players_option(self):
        old = wrap.build("C:/Python313/pythonw.exe",
                         "C:/Users/p/.local/bin/savepick.py", "-windowed",
                         borderless=True)
        self.assertEqual(wrap.engine_of(old),
                         "C:/Users/p/.local/bin/savepick.py")
        new = wrap.build(EXE, wrap.PICK, wrap.strip(old), borderless=True)
        self.assertEqual(new, EXE + " --pick --borderless -- %command% "
                                    "-windowed")
        # And back again, for a copy run from source after the exe.
        again = wrap.build("/py", "/sp.py", wrap.strip(new))
        self.assertEqual(again, "/py /sp.py -- %command% -windowed")

    def test_an_old_python_shortcut_unwraps_to_its_target(self):
        exe, options = wrap.build_shortcut("C:/Py/pythonw.exe", "C:/sp.py",
                                           "C:/G/ds2.exe", "-x")
        target, rest = wrap.unwrap_shortcut(exe, options)
        new_exe, new_options = wrap.build_shortcut(EXE, wrap.PICK, target,
                                                   rest)
        self.assertEqual((new_exe, new_options),
                         (EXE, '--pick -- C:/G/ds2.exe -x'))
        self.assertEqual(wrap.unwrap_shortcut(new_exe, new_options),
                         ("C:/G/ds2.exe", "-x"))


# ----------------------------------------------------------------- launchopts


class LaunchOptionsFile(Temp):
    def test_reads_every_option(self):
        found = launchopts.read_all(LOCALCONFIG)
        self.assertEqual(found, {220: "-novid %command%"})

    def test_reads_other_fields_too(self):
        apps = launchopts.read_apps(LOCALCONFIG)
        self.assertEqual(apps[400]["LastPlayed"], "1700000000")
        self.assertNotIn("LaunchOptions", apps[400])

    def test_changes_one_in_place(self):
        out = launchopts.write_all(LOCALCONFIG, {220: "new"})
        self.assertEqual(launchopts.read_all(out), {220: "new"})
        self.assertEqual(len(out.splitlines()), len(LOCALCONFIG.splitlines()))

    def test_adds_one_to_an_app_that_had_none(self):
        out = launchopts.write_all(LOCALCONFIG, {400: "added"})
        self.assertEqual(launchopts.read_all(out)[400], "added")
        self.assertEqual(vdf.parse_text(out)["UserLocalConfigStore"]["Software"]
                         ["Valve"]["Steam"]["apps"]["400"]["LastPlayed"],
                         "1700000000")

    def test_adds_a_whole_block_for_an_app_steam_has_never_seen(self):
        out = launchopts.write_all(LOCALCONFIG, {999: "brand new"})
        self.assertEqual(launchopts.read_all(out)[999], "brand new")
        vdf.parse_text(out)

    def test_removes_one(self):
        out = launchopts.write_all(LOCALCONFIG, {220: None})
        self.assertEqual(launchopts.read_all(out), {})
        vdf.parse_text(out)

    def test_escapes_a_value_with_quotes(self):
        out = launchopts.write_all(LOCALCONFIG, {220: 'say "hi"'})
        self.assertEqual(launchopts.read_all(out)[220], 'say "hi"')

    def test_leaves_everything_else_byte_for_byte(self):
        out = launchopts.write_all(LOCALCONFIG, {220: "new"})
        before = LOCALCONFIG.replace('"-novid %command%"', "")
        after = out.replace('"new"', "")
        self.assertEqual(before, after)

    def test_a_file_with_no_apps_block_is_refused(self):
        with self.assertRaises(vdf.VdfError):
            launchopts.write_all('"Other" { }', {1: "x"})

    def test_save_keeps_a_backup(self):
        path = self.dir / "localconfig.vdf"
        path.write_text(LOCALCONFIG, encoding="utf-8")
        launchopts.save(path, "changed")
        self.assertEqual(path.read_text(encoding="utf-8"), "changed")
        self.assertEqual(
            Path(str(path) + ".blockslot.bak").read_text(encoding="utf-8"),
            LOCALCONFIG)


# ----------------------------------------------------------------- steamdir


class SteamDirectory(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.root = build_steam(self.dir / "Steam")

    def test_finds_a_root_by_its_userdata(self):
        self.assertEqual(steamdir.find_root([self.dir / "nope", self.root]),
                         self.root)

    def test_finds_nothing_when_there_is_nothing(self):
        self.assertIsNone(steamdir.find_root([self.dir / "nope"]))

    def test_lists_installed_games(self):
        games = steamdir.installed_games(self.root)
        self.assertEqual([game.name for game in games],
                         ["Half-Life 2", "Portal"])

    def test_reads_the_signed_in_user(self):
        self.assertEqual(steamdir.user_ids(self.root), [40000001])
        self.assertEqual(steamdir.persona_names(self.root)[40000001], "Tester")

    def test_skips_a_library_that_is_not_there(self):
        (self.root / "steamapps" / "libraryfolders.vdf").write_text(
            LIBRARYFOLDERS % "/nowhere/at/all", encoding="utf-8")
        self.assertEqual(len(steamdir.installed_games(self.root)), 2)

    def test_an_unreadable_manifest_is_skipped_not_fatal(self):
        (self.root / "steamapps" / "appmanifest_999.acf").write_text(
            "not a vdf {{{", encoding="utf-8")
        self.assertEqual(len(steamdir.installed_games(self.root)), 2)


# ----------------------------------------------------------------- shortcuts


class Shortcuts(Temp):
    def make(self, path):
        entry = shortcuts.Shortcut("0", {
            "appid": 12345, "AppName": "Retro", "exe": '"C:\\py.exe"',
            "StartDir": "C:\\", "LaunchOptions": "sp.py -- rb.exe",
            "tags": {},
        })
        shortcuts.write(path, [entry], backup=False)
        return entry

    def test_round_trips_a_file(self):
        path = self.dir / "shortcuts.vdf"
        self.make(path)
        first = path.read_bytes()
        shortcuts.write(path, shortcuts.read(path), backup=False)
        self.assertEqual(path.read_bytes(), first)

    def test_reads_the_fields_that_matter(self):
        path = self.dir / "shortcuts.vdf"
        self.make(path)
        entry = shortcuts.read(path)[0]
        self.assertEqual(entry.name, "Retro")
        self.assertEqual(entry.appid, 12345)
        self.assertEqual(entry.launch_options, "sp.py -- rb.exe")

    def test_a_missing_file_is_an_empty_list(self):
        self.assertEqual(shortcuts.read(self.dir / "nothing.vdf"), [])

    def test_a_negative_appid_reads_as_unsigned(self):
        entry = shortcuts.Shortcut("0", {"appid": -1615330106, "AppName": "x"})
        self.assertEqual(entry.appid, 2679637190)

    def test_generated_appid_has_the_top_bit_set(self):
        self.assertTrue(shortcuts.generated_appid("C:/a.exe", "A") & 0x80000000)

    def test_a_new_entry_carries_every_field_steam_writes(self):
        entry = shortcuts.new_entry("Blockslot", "C:/py.exe", "C:/dir", "x.py")
        for key, _default in shortcuts.TEMPLATE:
            self.assertIn(key, entry.fields, key)
        self.assertIn("tags", entry.fields)
        self.assertEqual(entry.exe, '"C:/py.exe"')
        self.assertEqual(entry.start_dir, "C:/dir")

    def test_a_new_entry_survives_a_write_and_a_read(self):
        path = self.dir / "shortcuts.vdf"
        entry = shortcuts.new_entry("Blockslot", "C:/py.exe", "C:/dir",
                                    "x.py --fullscreen")
        shortcuts.write(path, [entry], backup=False)
        again = shortcuts.read(path)[0]
        self.assertEqual(again.name, "Blockslot")
        self.assertEqual(again.appid, entry.appid)
        self.assertEqual(again.launch_options, "x.py --fullscreen")

    def test_an_id_with_the_top_bit_set_is_stored_signed(self):
        self.assertEqual(shortcuts.as_signed32(0x80000001), -2147483647)
        self.assertEqual(shortcuts.as_signed32(5), 5)

    def test_finds_its_own_entry_from_a_source_install(self):
        entries = [shortcuts.new_entry("Blockslot", "C:/Python313/pythonw.exe",
                                       "", '"C:/Apps/gui/blockslot.py" --fullscreen'),
                   shortcuts.new_entry("Other", "other.exe", "", "")]
        found = shortcuts.find_own(entries, "C:/Apps/gui/blockslot.py")
        self.assertEqual([entry.name for entry in found], ["Blockslot"])

    def test_finds_its_own_entry_when_it_is_the_built_program(self):
        # The built exe names no script, which is how it used to miss itself
        # and append another copy on every "Add to Steam".
        entries = [shortcuts.new_entry(
            "Blockslot", "C:\\Users\\me\\Apps\\Blockslot\\Blockslot.exe",
            "", "--fullscreen")]
        found = shortcuts.find_own(
            entries, "C:\\Users\\me\\Apps\\Blockslot\\Blockslot.exe")
        self.assertEqual(len(found), 1)

    def test_a_source_entry_is_found_again_after_moving_to_the_exe(self):
        entries = [shortcuts.new_entry("Blockslot", "pythonw.exe", "",
                                       '"C:/Apps/gui/blockslot.py" --fullscreen')]
        self.assertEqual(
            len(shortcuts.find_own(entries, "C:/Apps/Blockslot.exe")), 1)

    def test_a_game_in_a_folder_called_blockslot_is_not_ours(self):
        entries = [shortcuts.new_entry("A Game", "C:/Blockslot/game.exe",
                                       "C:/Blockslot", "-windowed")]
        self.assertEqual(shortcuts.find_own(entries, "C:/Apps/Blockslot.exe"),
                         [])


# ----------------------------------------------------------------- settings


class SettingsFile(Temp):
    def test_a_missing_file_reads_as_empty(self):
        conf = settings.Settings.load(self.dir / "none.json")
        self.assertEqual(conf.data, {})
        self.assertFalse(conf.sync_is_complete())

    def test_round_trips(self):
        path = self.dir / "savepick.json"
        conf = settings.Settings.load(path)
        conf.set_sync(url="http://x", apikey="k", folder="f", device_dir="d")
        conf.save()
        again = settings.Settings.load(path)
        self.assertTrue(again.sync_is_complete())
        self.assertEqual(again.sync["folder"], "f")

    def test_keeps_a_backup_of_what_was_there(self):
        path = self.dir / "savepick.json"
        path.write_text('{"trees": {"a": {}}}', encoding="utf-8")
        conf = settings.Settings.load(path)
        conf.set_sync(url="http://x")
        conf.save()
        self.assertIn("trees", json.loads(
            Path(str(path) + ".blockslot.bak").read_text(encoding="utf-8")))

    def test_names_the_keys_that_are_missing(self):
        conf = settings.Settings(data={"syncthing": {"url": "http://x"}})
        self.assertEqual(sorted(conf.missing_sync_keys()),
                         ["apikey", "device_dir", "folder"])

    def test_device_labels_fall_back_to_the_directory(self):
        conf = settings.Settings(data={"syncthing": {
            "device_names": {"deck": "Steam Deck"}}})
        self.assertEqual(conf.device_label("deck"), "Steam Deck")
        self.assertEqual(conf.device_label("desktop"), "desktop")

    def test_trees_survive_a_write(self):
        path = self.dir / "savepick.json"
        conf = settings.Settings.load(path)
        conf.set_tree("RetroFrontend", {"roots": {"deck": "/a"}})
        conf.save()
        self.assertEqual(settings.Settings.load(path).tree_names(),
                         ["RetroFrontend"])

    def test_a_new_save_set_is_refused_without_a_name_or_with_a_used_one(self):
        conf = settings.Settings(data={})
        self.assertEqual(conf.add_tree("  Retro  "), "Retro")
        self.assertEqual(conf.tree("Retro"), {"roots": {}})
        with self.assertRaises(ValueError):
            conf.add_tree("Retro")
        with self.assertRaises(ValueError):
            conf.add_tree("   ")

    def test_one_games_own_folder_carries_every_file(self):
        conf = settings.Settings(data={})
        conf.add_tree("Bloodborne", every_file=True)
        self.assertEqual(conf.tree("Bloodborne")["extensions"], "*")

    def test_a_root_is_set_for_this_device_only(self):
        conf = settings.Settings(data={
            "syncthing": {"device_dir": "deck"},
            "trees": {"Retro": {"roots": {"desktop": "C:/saves"}}}})
        self.assertEqual(conf.set_tree_root("Retro", " /run/saves/ "),
                         "/run/saves")
        self.assertEqual(conf.tree("Retro")["roots"],
                         {"desktop": "C:/saves", "deck": "/run/saves"})
        self.assertEqual(conf.tree_rows(),
                         [("Retro", "/run/saves", 2, False)])

    def test_a_root_needs_this_device_to_be_named_first(self):
        conf = settings.Settings(data={"trees": {"Retro": {"roots": {}}}})
        with self.assertRaises(ValueError):
            conf.set_tree_root("Retro", "/a")

    def test_fill_defaults_never_overwrites(self):
        conf = settings.Settings(data={"syncthing": {"folder": "mine"}})
        conf.fill_defaults()
        self.assertEqual(conf.sync["folder"], "mine")
        self.assertEqual(conf.sync["url"], "http://127.0.0.1:8384")


# ----------------------------------------------------------------- catalog


class TheCatalog(Temp):
    def index(self, rows):
        path = self.dir / "games.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    def test_reads_the_index(self):
        path = self.index({"_meta": {}, "A Game": {"s": 10, "f": [{"p": "x"}]}})
        cat = catalog.Catalog.from_index(path)
        self.assertEqual(cat.name(10), "A Game")
        self.assertTrue(cat.saves(10))

    def test_a_missing_index_still_answers(self):
        cat = catalog.Catalog.from_index(self.dir / "nothing.json")
        self.assertIsNone(cat.cloud(10))
        self.assertIsNone(cat.saves(10))

    def test_steam_beats_the_manifest_on_cloud(self):
        path = self.index({"DS3": {"s": 374320, "f": [{"p": "x"}]}})
        cat = catalog.Catalog.from_index(path)
        self.assertFalse(cat.cloud(374320))
        cat.steam_cloud = {374320}
        self.assertTrue(cat.cloud(374320))

    def test_appinfo_answers_cloud_and_type(self):
        data = build_appinfo({
            10: {"appinfo": {"common": {"name": "C", "type": "Game"},
                             "ufs": {"savefiles": {"0": {}}}}},
            30: {"appinfo": {"common": {"name": "T", "type": "Tool"}}}})
        appinfo_file = self.dir / "appinfo.vdf"
        appinfo_file.write_bytes(data)
        cat = catalog.Catalog()
        self.assertTrue(cat.load_steam_cloud(appinfo_file))
        self.assertTrue(cat.cloud(10))
        self.assertFalse(cat.is_game(30))
        self.assertTrue(cat.is_game(10))

    def test_the_cache_is_used_and_is_invalidated(self):
        data = build_appinfo({10: {"appinfo": {"common": {"type": "Game"},
                                               "ufs": {"savefiles": {"0": {}}}}}})
        appinfo_file = self.dir / "appinfo.vdf"
        appinfo_file.write_bytes(data)
        cache = self.dir / "cache.json"
        first = catalog.Catalog()
        first.load_steam_cloud(appinfo_file, cache)
        self.assertTrue(cache.is_file())

        second = catalog.Catalog()
        second.load_steam_cloud(appinfo_file, cache)
        self.assertEqual(second.steam_cloud, {10})

        appinfo_file.write_bytes(build_appinfo(
            {20: {"appinfo": {"common": {"type": "Game"},
                              "ufs": {"savefiles": {"0": {}}}}}}))
        os.utime(str(appinfo_file), (1, 1))
        third = catalog.Catalog()
        third.load_steam_cloud(appinfo_file, cache)
        self.assertEqual(third.steam_cloud, {20})

    def test_a_broken_appinfo_is_not_fatal(self):
        appinfo_file = self.dir / "appinfo.vdf"
        appinfo_file.write_bytes(b"rubbish")
        cat = catalog.Catalog()
        self.assertFalse(cat.load_steam_cloud(appinfo_file))
        self.assertIsNone(cat.cloud(1))


# ----------------------------------------------------------------- model


class Discovery(Temp):
    def test_a_folder_with_no_steam_in_it_is_reported(self):
        library = model.Library.discover(self.dir / "not-steam")
        self.assertIsNone(library.root)
        self.assertIn("No Steam", library.error)

    def test_a_real_folder_is_found_with_its_user(self):
        root = build_steam(self.dir / "Steam")
        library = model.Library.discover(root)
        self.assertEqual(library.root, root)
        self.assertEqual(library.user_id, 40000001)
        self.assertIsNone(library.error)

    def test_steam_with_nobody_signed_in_says_so(self):
        root = build_steam(self.dir / "Steam")
        shutil.rmtree(str(root / "userdata" / "40000001"))
        library = model.Library.discover(root)
        self.assertIsNone(library.user_id)
        self.assertIn("signed in", library.error)


class TheLibrary(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.root = build_steam(self.dir / "Steam")
        self.library = model.Library(root=self.root, user_id=40000001,
                                     settings=settings.Settings(data={}))
        # This Steam directory is one the test built. Whether a real client is
        # running somewhere else says nothing about it.
        self.library.steam_running = lambda: False
        self.library.catalog = catalog.Catalog(
            entries={220: catalog.Entry("Half-Life 2", 220, [{"p": "x"}], [], 0),
                     400: catalog.Entry("Portal", 400, [{"p": "x"}], ["steam"], 0)},
            steam_cloud={400})
        self.library.load()

    def test_lists_installed_games_with_their_state(self):
        names = sorted(row.name for row in self.library.rows)
        self.assertEqual(names, ["Half-Life 2", "Portal"])

    def test_hides_cloud_games_by_default(self):
        shown = [row.name for row in self.library.visible()]
        self.assertEqual(shown, ["Half-Life 2"])
        shown = [row.name for row in self.library.visible(hide_cloud=False)]
        self.assertEqual(sorted(shown), ["Half-Life 2", "Portal"])

    def test_search_matches_part_of_a_name(self):
        shown = [row.name for row in self.library.visible(search="half")]
        self.assertEqual(shown, ["Half-Life 2"])

    def test_planning_keeps_existing_flags(self):
        steam_changes, shortcut_changes = self.library.plan([220], True)
        self.assertEqual(shortcut_changes, [])
        self.assertIn("-novid", steam_changes[220])
        self.assertTrue(wrap.is_wrapped(steam_changes[220]))

    def test_planning_a_second_time_changes_nothing(self):
        steam_changes, _ = self.library.plan([220], True)
        self.library.apply(steam_changes, [])
        self.library.load()
        again, _ = self.library.plan([220], True)
        self.assertEqual(again, {})

    def test_removing_gives_the_option_back(self):
        steam_changes, _ = self.library.plan([220], True)
        self.library.apply(steam_changes, [])
        self.library.load()
        back, _ = self.library.plan([220], False)
        self.assertEqual(back[220], "-novid %command%")

    def test_applying_writes_the_file(self):
        steam_changes, _ = self.library.plan([220], True)
        count = self.library.apply(steam_changes, [])
        self.assertEqual(count, 1)
        text = launchopts.load(steamdir.localconfig_path(self.root, 40000001))
        self.assertTrue(wrap.is_wrapped(launchopts.read_all(text)[220]))

    def _write(self, plan):
        self.library.apply(plan[0], plan[1])
        self.library.load()
        return {row.appid: row for row in self.library.rows}

    def test_borderless_alone_leaves_saves_alone(self):
        rows = self._write(self.library.plan_borderless([400], True))
        self.assertTrue(rows[400].borderless)
        self.assertFalse(rows[400].syncing)
        self.assertTrue(rows[400].wrapped)
        # A cloud game made borderless is not counted as on Blockslot, and
        # the list still shows it with the cloud filter on.
        self.assertEqual(self.library.counts()["wrapped"], 0)
        self.assertIn("Portal", [r.name for r in self.library.visible()])

    def test_borderless_then_sync_keeps_both(self):
        self._write(self.library.plan_borderless([220], True))
        rows = self._write(self.library.plan([220], True))
        self.assertTrue(rows[220].borderless)
        self.assertTrue(rows[220].syncing)
        self.assertIn("-novid", rows[220].launch_options)

    def test_sync_off_keeps_borderless(self):
        self._write(self.library.plan([220], True))
        self._write(self.library.plan_borderless([220], True))
        rows = self._write(self.library.plan([220], False))
        self.assertTrue(rows[220].borderless)
        self.assertFalse(rows[220].syncing)

    def test_borderless_off_keeps_sync(self):
        self._write(self.library.plan([220], True))
        self._write(self.library.plan_borderless([220], True))
        rows = self._write(self.library.plan_borderless([220], False))
        self.assertFalse(rows[220].borderless)
        self.assertTrue(rows[220].syncing)

    def test_both_off_gives_the_option_back(self):
        self._write(self.library.plan_borderless([220], True))
        rows = self._write(self.library.plan_borderless([220], False))
        self.assertFalse(rows[220].wrapped)
        self.assertEqual(rows[220].launch_options, "-novid %command%")

    def test_borderless_twice_changes_nothing(self):
        self._write(self.library.plan_borderless([220], True))
        self.assertEqual(self.library.plan_borderless([220], True), ({}, []))

    def test_sync_off_on_a_game_that_never_had_it_changes_nothing(self):
        self._write(self.library.plan_borderless([220], True))
        self.assertEqual(self.library.plan([220], False), ({}, []))

    def test_a_save_set_is_only_for_a_shortcut(self):
        steam_changes, _ = self.library.plan([220], True, tree="RetroFrontend")
        self.assertIsNone(wrap.tree_of(steam_changes[220]))

    def test_a_hub_backup_is_hung_on_the_row_it_belongs_to(self):
        matched = self.library.attach_backups(
            {"Half-Life 2": ("2026-09-01T10:00:00Z", "deck")})
        self.assertEqual(matched, 1)
        row = [r for r in self.library.rows if r.name == "Half-Life 2"][0]
        self.assertEqual(row.synced[1], "deck")
        other = [r for r in self.library.rows if r.name == "Portal"][0]
        self.assertIsNone(other.synced)

    def test_a_store_answer_is_matched_by_key(self):
        key = lambda name: name.replace(" ", "_").replace("-", "_")
        matched = self.library.attach_backups(
            {"Half_Life_2": ("2026-09-24T01:00:00Z", "deck")}, key=key)
        self.assertEqual(matched, 1)
        row = [r for r in self.library.rows if r.name == "Half-Life 2"][0]
        self.assertEqual(row.synced[1], "deck")

    def test_a_wrapped_set_is_matched_by_the_sets_name(self):
        row = self.library.rows[0]
        row.launch_options = "/py /sp.py --tree RetroFrontend -- %command%"
        self.library.attach_backups(
            {"RetroFrontend": ("2026-09-01T10:00:00Z", "deck")})
        self.assertEqual(row.synced[1], "deck")

    def test_a_write_that_did_not_land_is_put_back(self):
        """A write that produces the wrong text must not be left in place."""
        path = steamdir.localconfig_path(self.root, 40000001)
        before = launchopts.load(path)
        steam_changes, _ = self.library.plan([220], True)

        original = launchopts.write_all

        def writes_something_else(text, changes):
            return original(text, {key: "not what was asked" for key in changes})

        launchopts.write_all = writes_something_else
        try:
            with self.assertRaises(IOError):
                self.library.apply(steam_changes, [])
        finally:
            launchopts.write_all = original
        self.assertEqual(launchopts.load(path), before)

    def test_a_running_steam_stops_a_write(self):
        self.library.steam_running = lambda: True
        steam_changes, _ = self.library.plan([220], True)
        with self.assertRaises(launchopts.SteamBusy):
            self.library.apply(steam_changes, [])

    def test_counts_report_what_the_screen_shows(self):
        counts = self.library.counts()
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["cloud"], 1)
        self.assertEqual(counts["wrapped"], 0)

    def test_a_game_with_no_known_saves_is_still_a_candidate(self):
        row = [r for r in self.library.rows if r.name == "Half-Life 2"][0]
        row.saves = None
        self.assertTrue(row.needs_blockslot)

    def test_a_game_with_saves_known_absent_is_not(self):
        row = [r for r in self.library.rows if r.name == "Half-Life 2"][0]
        row.saves = False
        self.assertFalse(row.needs_blockslot)


# ----------------------------------------------------------------- backups


FAKE_LUDUSAVI = """#!/usr/bin/env python3
\"\"\"A stand-in for ludusavi: answers `backups --api` from a JSON file.\"\"\"
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
path = args[args.index("--path") + 1] if "--path" in args else None
name = os.path.basename(path) if path else "own"
try:
    with open(os.path.join(here, name + ".json")) as handle:
        sys.stdout.write(handle.read())
except OSError:
    sys.stdout.write(json.dumps({"games": {}}))
"""


class LaunchInterpreter(unittest.TestCase):
    def _running_as(self, executable, frozen=False):
        self.addCleanup(setattr, sys, "executable", sys.executable)
        sys.executable = executable
        if frozen:
            sys.frozen = True
            self.addCleanup(delattr, sys, "frozen")

    def test_python_itself_is_named(self):
        if paths.is_windows():
            self.skipTest("the Windows answer is pythonw.exe beside it")
        self.assertEqual(paths.python_for_launch(), Path(sys.executable))

    def test_deckys_loader_is_never_named(self):
        self._running_as("/home/deck/homebrew/services/PluginLoader")
        self.assertTrue(
            paths.python_for_launch().name.lower().startswith("python"))

    def test_the_built_exe_is_never_named(self):
        self._running_as("/opt/blockslot/python-looking-Blockslot", frozen=True)
        self._running_as("/opt/blockslot/Blockslot")
        self.assertTrue(
            paths.python_for_launch().name.lower().startswith("python"))


class TheLibraryFromTheExe(Temp):
    """Planning as the frozen Windows build does: the exe hosts the engine."""

    def setUp(self):
        Temp.setUp(self)
        self.root = build_steam(self.dir / "Steam")
        self._as_windows_exe(EXE)
        # What a launch option names: the path as this OS spells it.
        self.exe = str(Path(EXE))
        self.library = model.Library(root=self.root, user_id=40000001,
                                     settings=settings.Settings(data={}))
        self.library.steam_running = lambda: False
        self.library.load()

    def _as_windows_exe(self, executable):
        for name, value in (("is_frozen", lambda: True),
                            ("is_windows", lambda: True)):
            self.addCleanup(setattr, paths, name, getattr(paths, name))
            setattr(paths, name, value)
        self.addCleanup(setattr, sys, "executable", sys.executable)
        sys.executable = executable

    def _rows(self, plan):
        self.library.apply(plan[0], plan[1])
        self.library.load()
        return {row.appid: row for row in self.library.rows}

    def test_a_wrap_names_the_exe_with_pick(self):
        steam_changes, _ = self.library.plan([220], True)
        self.assertEqual(steam_changes[220],
                         self.exe + " --pick -- -novid %command%")

    def test_planning_a_second_time_changes_nothing(self):
        self._rows(self.library.plan([220], True))
        self.assertEqual(self.library.plan([220], True), ({}, []))

    def test_removing_gives_the_option_back(self):
        self._rows(self.library.plan([220], True))
        rows = self._rows(self.library.plan([220], False))
        self.assertFalse(rows[220].wrapped)
        self.assertEqual(rows[220].launch_options, "-novid %command%")

    def test_an_old_python_wrap_is_recognised_and_rewrapped(self):
        old = wrap.build("C:/Python313/pythonw.exe",
                         "C:/Users/p/.local/bin/savepick.py",
                         "-novid %command%", borderless=True)
        rows = self._rows(({220: old}, []))
        self.assertTrue(rows[220].wrapped and rows[220].borderless)
        steam_changes, _ = self.library.plan([220], True)
        self.assertEqual(steam_changes[220],
                         self.exe + " --pick --borderless -- -novid %command%")
        rows = self._rows((steam_changes, []))
        self.assertTrue(rows[220].syncing and rows[220].borderless)
        # And it takes off cleanly, to what the player had.
        rows = self._rows(self.library.plan_borderless([220], False))
        rows = self._rows(self.library.plan([220], False))
        self.assertEqual(rows[220].launch_options, "-novid %command%")

    def test_a_moved_exe_is_rewrapped_at_its_new_path(self):
        self._rows(self.library.plan([220], True))
        sys.executable = "D:/Tools/Blockslot.exe"
        steam_changes, _ = self.library.plan([220], True)
        self.assertEqual(steam_changes[220],
                         str(Path("D:/Tools/Blockslot.exe"))
                         + " --pick -- -novid %command%")

    def test_the_source_install_rewraps_an_exe_wrap_with_python(self):
        self._rows(self.library.plan([220], True))
        paths.is_frozen = lambda: False
        steam_changes, _ = self.library.plan([220], True)
        value = steam_changes[220]
        self.assertEqual(wrap.engine_of(value), str(paths.engine_path()))
        self.assertEqual(wrap.strip(value), "-novid %command%")

    def _add_shortcut(self, exe, options=""):
        path = steamdir.shortcuts_path(self.root, 40000001)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = shortcuts.new_entry("Retro", exe, start_dir="C:/G",
                                    options=options)
        shortcuts.write(path, [entry])
        self.library.load()
        return [row for row in self.library.rows
                if row.kind == model.KIND_SHORTCUT][0]

    def test_a_shortcut_is_wrapped_with_the_exe_and_unwrapped_again(self):
        row = self._add_shortcut("C:/G/rb.exe", "-fullscreen")
        _, changes = self.library.plan([row.appid], True, tree="Retro")
        # Steam keeps a shortcut's exe in quotes, and they are carried over.
        self.assertEqual(changes, [(row.index, self.exe,
                                    '--pick --tree Retro -- "C:/G/rb.exe" '
                                    '-fullscreen')])
        self.library.apply({}, changes)
        self.library.load()
        # The same plan again is already done: the exe field names this exe.
        row = [r for r in self.library.rows if r.kind == model.KIND_SHORTCUT][0]
        self.assertTrue(row.syncing)
        self.assertEqual(self.library.plan([row.appid], True), ({}, []))
        _, changes = self.library.plan([row.appid], False)
        self.assertEqual(changes, [(row.index, "C:/G/rb.exe", "-fullscreen")])

    def test_an_old_python_shortcut_is_rewrapped_with_the_exe(self):
        exe, options = wrap.build_shortcut("C:/Py/pythonw.exe",
                                           "C:/u/.local/bin/savepick.py",
                                           "C:/G/rb.exe", "-x", tree="Retro")
        row = self._add_shortcut(exe, options)
        self.assertTrue(row.syncing)
        _, changes = self.library.plan([row.appid], True)
        self.assertEqual(changes, [(row.index, self.exe,
                                    "--pick --tree Retro -- C:/G/rb.exe -x")])

    def test_a_wrapped_shortcut_is_never_taken_for_blockslots_own(self):
        exe, options = wrap.build_shortcut(EXE, wrap.PICK, "C:/G/rb.exe")
        entries = [shortcuts.new_entry("Retro", exe, options=options),
                   shortcuts.new_entry("BlockSlot", EXE,
                                       options="--fullscreen")]
        found = shortcuts.find_own(entries, EXE)
        self.assertEqual([entry.name for entry in found], ["BlockSlot"])


class TheTemporaryPlace(Temp):
    def test_the_temp_folder_is_temporary(self):
        import tempfile
        inside = Path(tempfile.gettempdir()) / "Temp1_BlockSlot.zip" / "B.exe"
        self.assertTrue(paths.in_temporary_place(inside))

    def test_a_zip_folder_is_temporary_wherever_it_is(self):
        self.assertTrue(paths.in_temporary_place(
            "/home/p/Downloads/BlockSlot.zip/Blockslot.exe"))

    def test_a_folder_of_its_own_is_not(self):
        self.assertFalse(paths.in_temporary_place(
            "/home/p/Apps/BlockSlot/Blockslot.exe"))


class TheEngine(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.shipped = self.dir / "shipped.py"
        self.shipped.write_text("print('engine')\n", encoding="utf-8")
        self.installed = self.dir / "bin" / paths.ENGINE_NAME
        real = paths.engine_path
        paths.engine_path = lambda: self.installed
        self.addCleanup(setattr, paths, "engine_path", real)

    def test_nothing_installed_is_not_current(self):
        self.assertFalse(engine.is_current(self.shipped))

    def test_an_install_is_current_until_the_shipped_copy_moves(self):
        self.assertEqual(engine.install(self.shipped), self.installed)
        self.assertTrue(engine.is_current(self.shipped))
        self.shipped.write_text("print('newer!')\n", encoding="utf-8")
        self.assertFalse(engine.is_current(self.shipped))

    def test_the_companions_are_installed_beside_it(self):
        for name in engine.COMPANIONS:
            (self.shipped.parent / name).write_text("# " + name)
        engine.install(self.shipped)
        for name in engine.COMPANIONS:
            self.assertEqual((self.installed.parent / name).read_text(), "# " + name)
        self.assertTrue(engine.is_current(self.shipped))
        (self.shipped.parent / "slotd.py").write_text("# newer")
        self.assertFalse(engine.is_current(self.shipped))

    def test_no_shipped_engine_is_not_current(self):
        self.assertFalse(engine.is_current(None))


class InstallingLudusavi(Temp):
    def setUp(self):
        Temp.setUp(self)
        import tarfile
        payload = self.dir / "ludusavi"
        payload.write_bytes(b"#!/bin/sh\necho ludusavi\n")
        self.archive = self.dir / "ludusavi-linux.tar.gz"
        with tarfile.open(str(self.archive), "w:gz") as bundle:
            bundle.add(str(payload), arcname="ludusavi")
        payload.unlink()
        self.target = self.dir / "bin" / "ludusavi"
        real = paths.ludusavi_path
        paths.ludusavi_path = lambda: self.target
        self.addCleanup(setattr, paths, "ludusavi_path", real)

    def test_the_release_archive_is_unpacked_and_made_runnable(self):
        self.assertEqual(engine.install_ludusavi(self.archive), self.target)
        self.assertEqual(self.target.read_bytes(), b"#!/bin/sh\necho ludusavi\n")
        if not paths.is_windows():
            self.assertTrue(os.access(str(self.target), os.X_OK))

    def test_an_installed_ludusavi_is_never_replaced(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"mine")
        with self.assertRaises(engine.AlreadyThere):
            engine.install_ludusavi(self.archive)
        self.assertEqual(self.target.read_bytes(), b"mine")

    def test_a_bad_archive_leaves_nothing_behind(self):
        self.archive.write_bytes(b"not a tarball")
        with self.assertRaises(OSError):
            engine.install_ludusavi(self.archive)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])


class InstallingLudusaviOnWindows(Temp):
    """The official win64 zip, checked against a pinned hash, never over
    one that is there."""

    PAYLOAD = b"MZ fake ludusavi"

    def setUp(self):
        Temp.setUp(self)
        import hashlib
        import zipfile
        self.archive = self.dir / "ludusavi-v0.31.0-win64.zip"
        with zipfile.ZipFile(str(self.archive), "w") as bundle:
            bundle.writestr("ludusavi.exe", self.PAYLOAD)
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.target = self.dir / "bin" / "ludusavi.exe"
        real = paths.ludusavi_path
        paths.ludusavi_path = lambda: self.target
        self.addCleanup(setattr, paths, "ludusavi_path", real)

    def test_the_pin_is_the_win64_release_of_the_decky_version(self):
        package = json.loads((ROOT / "decky" / "package.json").read_text())
        urls = [item.get("url", "") for item in package.get("remote_binary", [])]
        self.assertTrue(any("/v%s/" % engine.LUDUSAVI_VERSION in url
                            for url in urls), urls)
        self.assertTrue(engine.LUDUSAVI_WINDOWS_URL.endswith(
            "ludusavi-v%s-win64.zip" % engine.LUDUSAVI_VERSION))
        self.assertEqual(len(engine.LUDUSAVI_WINDOWS_SHA256), 64)

    def test_the_right_hash_installs_it(self):
        self.assertEqual(engine.install_ludusavi(self.archive, sha256=self.sha),
                         self.target)
        self.assertEqual(self.target.read_bytes(), self.PAYLOAD)

    def test_the_wrong_hash_installs_nothing(self):
        with self.assertRaises(engine.WrongFile):
            engine.install_ludusavi(self.archive, sha256="0" * 64)
        self.assertFalse(self.target.exists())

    def test_an_installed_ludusavi_is_never_replaced(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"mine")
        with self.assertRaises(engine.AlreadyThere):
            engine.install_ludusavi(self.archive, sha256=self.sha)
        self.assertEqual(self.target.read_bytes(), b"mine")

    def _opener(self, calls):
        def opener(url):
            calls.append(url)
            return open(str(self.archive), "rb")
        return opener

    def test_a_download_is_checked_then_installed(self):
        calls, said = [], []
        got = engine.download_ludusavi("https://example.invalid/l.zip",
                                       sha256=self.sha, say=said.append,
                                       opener=self._opener(calls))
        self.assertEqual(got, self.target)
        self.assertEqual(self.target.read_bytes(), self.PAYLOAD)
        self.assertEqual(calls, ["https://example.invalid/l.zip"])
        self.assertTrue(said)
        # Nothing but ludusavi.exe is left behind.
        self.assertEqual([p.name for p in self.target.parent.iterdir()],
                         ["ludusavi.exe"])

    def test_a_download_with_the_wrong_hash_leaves_nothing(self):
        with self.assertRaises(engine.WrongFile):
            engine.download_ludusavi("https://example.invalid/l.zip",
                                     sha256="f" * 64, opener=self._opener([]))
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_an_installed_ludusavi_is_not_even_downloaded(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"mine")
        calls = []
        with self.assertRaises(engine.AlreadyThere):
            engine.download_ludusavi("https://example.invalid/l.zip",
                                     sha256=self.sha,
                                     opener=self._opener(calls))
        self.assertEqual(calls, [])
        self.assertEqual(self.target.read_bytes(), b"mine")

    def test_no_network_says_so_plainly(self):
        def offline(_url):
            raise OSError("no route to host")
        with self.assertRaises(OSError) as caught:
            engine.download_ludusavi("https://example.invalid/l.zip",
                                     sha256=self.sha, opener=offline)
        self.assertIn("online", str(caught.exception))
        self.assertFalse(self.target.exists())


class TheLog(Temp):
    def test_a_line_is_classified_by_the_engines_own_words(self):
        self.assertEqual(logview.classify("Backup FAILED for DS2"), "bad")
        self.assertEqual(logview.classify("hub not confirmed"), "warn")
        self.assertEqual(logview.classify("restored from desktop"), "good")
        self.assertEqual(logview.classify("launching"), "")

    def test_a_count_of_no_failures_is_not_a_failure(self):
        self.assertEqual(logview.classify("tree: 0 copied, 0 failed"), "good")
        self.assertEqual(logview.classify("tree: 3 copied, 0 failed"), "good")
        self.assertEqual(logview.classify("tree: 3 copied, 1 failed"), "bad")
        self.assertEqual(logview.classify("tree: 0 copied, 10 failed"), "bad")

    def test_a_backup_that_may_not_have_run_is_not_coloured_as_one(self):
        self.assertEqual(logview.classify(
            "received signal 2; ludusavi may not finish its exit backup"),
            "warn")
        self.assertEqual(logview.classify(
            "second signal 2; giving up on the exit backup"), "bad")

    def test_the_tail_is_whole_lines_from_the_end(self):
        log = self.dir / "savepick.log"
        log.write_text("".join("line %d\n" % n for n in range(5000)),
                       encoding="utf-8")
        self.assertEqual(logview.tail(log, 2), ["line 4998", "line 4999"])
        short = logview.tail(log, 5000, max_bytes=100)
        self.assertTrue(all(line.startswith("line ") for line in short))
        self.assertEqual(short[-1], "line 4999")

    def test_a_missing_log_raises(self):
        with self.assertRaises(OSError):
            logview.tail(self.dir / "nothing.log", 5)


class SteamClosed(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.calls = []
        self.running = True
        self.closes = True
        for name, stand_in in (
                ("steam_running", lambda: self.running),
                ("stop_steam", self._stop),
                ("start_steam", lambda root=None: self.calls.append("start")),
                ("wait_until_settled",
                 lambda path: self.calls.append("settle"))):
            self.addCleanup(setattr, launchopts, name,
                            getattr(launchopts, name))
            setattr(launchopts, name, stand_in)

    def _stop(self, root=None):
        self.calls.append("stop")
        return self.closes

    def test_the_order_is_stop_settle_work_start(self):
        answer = launchopts.with_steam_closed(
            None, self.dir / "f", lambda: self.calls.append("work") or 7)
        self.assertEqual(answer, 7)
        self.assertEqual(self.calls, ["stop", "settle", "work", "start"])

    def test_steam_that_was_not_running_is_left_alone(self):
        self.running = False
        launchopts.with_steam_closed(None, self.dir / "f",
                                     lambda: self.calls.append("work"))
        self.assertEqual(self.calls, ["work"])

    def test_nothing_is_written_when_steam_stays_open(self):
        self.closes = False
        with self.assertRaises(launchopts.SteamStayedOpen):
            launchopts.with_steam_closed(None, self.dir / "f",
                                         lambda: self.calls.append("work"))
        self.assertEqual(self.calls, ["stop"])

    def test_steam_comes_back_after_a_failed_write(self):
        def work():
            raise OSError("disk full")
        with self.assertRaises(OSError):
            launchopts.with_steam_closed(None, self.dir / "f", work)
        self.assertEqual(self.calls, ["stop", "settle", "start"])


class Backups(Temp):
    """ludusavi is replaced by a script that prints what it is told to.

    The stand-in is called as [python, script], which `ask` supports on
    purpose: an executable bit does not travel to Windows, and this suite runs
    there too.
    """

    def setUp(self):
        Temp.setUp(self)
        script = self.dir / "fake_ludusavi.py"
        script.write_text(FAKE_LUDUSAVI, encoding="utf-8")
        self.binary = [sys.executable, str(script)]
        self.share = self.dir / "gamesaves"
        (self.share / "here").mkdir(parents=True)
        (self.share / "deck").mkdir(parents=True)
        self.answer("own", {"A Game": "2026-09-01T10:00:00.000000Z",
                            "Old One": "2026-01-01T10:00:00.000000Z"},
                    root=self.share / "here")
        self.answer("deck", {"A Game": "2026-09-05T10:00:00.000000Z"},
                    root=self.share / "deck")

    def answer(self, name, games, root):
        payload = {"games": {}}
        for game, when in games.items():
            payload["games"][game] = {
                "backupPath": str(root / game),
                "backups": [{"name": "backup-x", "when": when}]}
        (self.dir / (name + ".json")).write_text(json.dumps(payload),
                                                 encoding="utf-8")

    def test_reads_this_devices_backups(self):
        rows = backups.backups(self.binary)
        self.assertIn("A Game", rows)
        self.assertEqual(rows["A Game"][0]["when"],
                         "2026-09-01T10:00:00.000000Z")

    def test_finds_this_devices_directory(self):
        own = backups.own_directory(self.binary)
        self.assertEqual(Path(own).name, "here")

    def test_lists_the_other_devices(self):
        peers = backups.peer_directories(self.share / "here")
        self.assertEqual([peer.name for peer in peers], ["deck"])

    def test_the_newest_backup_wins_and_names_its_device(self):
        newest = backups.newest_everywhere(self.binary)
        self.assertEqual(newest["A Game"][1], "deck")
        self.assertEqual(newest["Old One"][1], "here")

    def test_a_game_nobody_has_is_absent(self):
        self.assertNotIn("Never Played", backups.newest_everywhere(self.binary))

    def test_an_unrunnable_ludusavi_is_empty_not_an_error(self):
        missing = str(self.dir / "nothing")
        self.assertEqual(backups.backups(missing), {})
        self.assertEqual(backups.newest_everywhere(missing), {})

    def test_reads_ludusavis_timestamps(self):
        import calendar
        import time
        now = calendar.timegm(time.strptime("2026-09-03T10:00:00",
                                            "%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(
            backups.when_text("2026-09-01T10:00:00.123456789Z", now=now),
            "2d ago")
        self.assertEqual(
            backups.when_text("2026-09-03T09:00:00Z", now=now), "1h ago")
        self.assertEqual(backups.when_text(""), "")
        self.assertEqual(backups.when_text("not a time"), "")


# ----------------------------------------------------------------- syncthing


class FakeSyncthing(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):  # noqa: N802
        if self.headers.get("X-API-Key") != "right-key":
            self.send_response(403)
            self.end_headers()
            return
        body = self.routes.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class SyncthingClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeSyncthing.routes = {
            "/rest/system/status": {"myID": "AAAAAAA-BBBBBBB"},
            "/rest/config/folders/gamesaves": {"path": "/data/gamesaves"},
            "/rest/db/status": {"state": "idle", "needFiles": 0, "errors": 0},
            "/rest/system/connections": {"connections": {"HUB-ID": {"connected": True}}},
        }
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSyncthing)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def config(self, **extra):
        base = {"url": self.url, "apikey": "right-key", "folder": "gamesaves",
                "hub_id": "HUB-ID", "hub_name": "hub"}
        base.update(extra)
        return base

    def test_every_check_passes_against_a_working_server(self):
        steps = syncthing.check(self.config())
        self.assertTrue(all(ok for _label, ok, _detail in steps), steps)

    def test_a_wrong_key_says_so_and_stops(self):
        steps = syncthing.check(self.config(apikey="wrong"))
        self.assertEqual(len(steps), 1)
        self.assertFalse(steps[0][1])
        self.assertIn("API key", steps[0][2])

    def test_no_address_is_reported_not_raised(self):
        steps = syncthing.check({"url": "", "apikey": "k"})
        self.assertFalse(steps[0][1])

    def test_an_unreachable_server_is_reported(self):
        answer = syncthing.status({"url": "http://127.0.0.1:1", "apikey": "k"})
        self.assertFalse(answer.ok)
        self.assertIn("cannot reach", answer.error)

    def test_a_missing_folder_is_reported(self):
        steps = syncthing.check(self.config(folder="nope"))
        self.assertFalse(steps[-1][1])

    def test_describes_a_folder_that_is_behind(self):
        text = syncthing.describe_folder(
            {"state": "syncing", "needFiles": 3, "needBytes": 2048, "errors": 1})
        self.assertIn("3 files to go", text)
        self.assertIn("1 error", text)

    def test_reads_an_api_key_out_of_a_config_file(self):
        with tempfile.TemporaryDirectory() as where:
            path = Path(where) / "config.xml"
            path.write_text(
                '<configuration><gui tls="false"><address>0.0.0.0:8384</address>'
                '<apikey>secret</apikey></gui>'
                '<folder id="gamesaves" label="Saves" path="/data"/>'
                '<device id="ABC" name="hub"/></configuration>',
                encoding="utf-8")
            found = syncthing.read_local_config([path])
        self.assertEqual(found["apikey"], "secret")
        self.assertEqual(found["url"], "http://127.0.0.1:8384")
        self.assertEqual(found["folders"][0]["id"], "gamesaves")

    def test_no_config_file_anywhere_is_none(self):
        self.assertIsNone(syncthing.read_local_config([Path("/nowhere.xml")]))


# ----------------------------------------------------------------- store


class StoreSettings(Temp):
    def test_an_empty_file_has_no_store(self):
        conf = settings.Settings.load(self.dir / "none.json")
        self.assertEqual(conf.store(), {})
        self.assertFalse(conf.store_is_complete())
        self.assertEqual(conf.missing_store_keys(), ["type"])

    def test_each_kind_names_what_it_still_needs(self):
        conf = settings.Settings(data={})
        conf.set_store(type="s3", endpoint="https://s3.example")
        self.assertEqual(conf.missing_store_keys(),
                         ["bucket", "access_key", "secret_key"])
        conf.set_store(type="ssh", host="nas")
        self.assertEqual(conf.missing_store_keys(), ["root"])
        conf.set_store(type="local", root="/mnt/saves")
        self.assertTrue(conf.store_is_complete())

    def test_none_or_empty_removes_a_key(self):
        conf = settings.Settings(data={})
        conf.set_store(type="ssh", host="a", user="me")
        conf.set_store(user=None, host="")
        self.assertEqual(conf.store(), {"type": "ssh"})

    def test_round_trips_and_secrets_open_again(self):
        path = self.dir / "savepick.json"
        conf = settings.Settings.load(path)
        conf.set_store(type="s3", endpoint="https://s3", bucket="b",
                       access_key="AK", secret_key="SK",
                       cf_client_id="id", cf_client_secret="CS")
        conf.save()
        again = settings.Settings.load(path)
        self.assertTrue(again.store_is_complete())
        opened = again.store_for_engine()
        self.assertEqual(opened["secret_key"], "SK")
        self.assertEqual(opened["cf_client_secret"], "CS")
        if paths.is_windows():
            self.assertTrue(again.store()["secret_key"].startswith("dpapi:"))

    def test_a_sealed_secret_is_not_sealed_twice(self):
        conf = settings.Settings(data={})
        conf.set_store(secret_key="dpapi:abc")
        self.assertEqual(conf.store()["secret_key"], "dpapi:abc")

    def test_clear_removes_the_whole_section(self):
        conf = settings.Settings(data={"store": {"type": "local"}, "trees": {}})
        conf.clear_store()
        self.assertEqual(conf.data, {"trees": {}})

    def test_the_file_is_private_off_windows(self):
        if paths.is_windows():
            self.skipTest("Windows seals the secret instead")
        path = self.dir / "savepick.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(str(path), 0o644)
        conf = settings.Settings.load(path)
        conf.set_store(type="local", root="/x")
        conf.save()
        self.assertEqual(os.stat(str(path)).st_mode & 0o777, 0o600)
        backup = Path(str(path) + ".blockslot.bak")
        self.assertEqual(os.stat(str(backup)).st_mode & 0o777, 0o600)

    def test_the_device_name_falls_back_to_the_hostname(self):
        import socket
        conf = settings.Settings(data={})
        self.assertEqual(conf.store_device(), socket.gethostname())
        conf.set_store(device="desktop")
        self.assertEqual(conf.store_device(), "desktop")


class FakeStore(object):
    """A store that fails one call the way a real one does."""

    label = "fake"

    def __init__(self, fail_on, error):
        self.fail_on = fail_on
        self.error = error
        self.objects = {}

    def _maybe(self, call):
        if call == self.fail_on:
            raise self.error

    def list(self, prefix):
        self._maybe("list")
        return [key for key in self.objects if key.startswith(prefix)]

    def put(self, key, data):
        self._maybe("put")
        self.objects[key] = data

    def get(self, key):
        self._maybe("get")
        return self.objects[key]

    def delete(self, key):
        self._maybe("delete")
        self.objects.pop(key, None)


class StoreCheck(Temp):
    def setUp(self):
        Temp.setUp(self)
        self.ss = settings.engine_module("slotstore")
        self.root = self.dir / "store"
        self.root.mkdir()

    def conf(self, **store):
        return settings.Settings(data={"store": store},
                                 path=self.dir / "savepick.json")

    def test_a_folder_store_passes_every_step_and_leaves_nothing(self):
        lines = storecheck.test_store(self.conf(type="local", root=str(self.root),
                                                device="deck"))
        self.assertTrue(all(ok for _label, ok, _detail in lines), lines)
        self.assertEqual(len(lines), 5)
        self.assertIn("probe/deck-", lines[2][2])
        self.assertEqual(self.ss.LocalStore(str(self.root)).list(self.ss.PREFIX), [])

    def test_missing_settings_stop_before_anything_is_tried(self):
        lines = storecheck.test_store(self.conf(type="s3", endpoint="https://x"))
        self.assertEqual(len(lines), 1)
        self.assertFalse(lines[0][1])
        self.assertIn("bucket", lines[0][2])
        lines = storecheck.test_store(self.conf())
        self.assertIn("Pick S3, SSH or Folder", lines[0][2])

    def test_a_setup_code_fills_the_store(self):
        import base64
        body = {"type": "s3", "endpoint": "https://saves.example", "bucket": "blockslot",
                "region": "garage", "access_key": "GK1", "secret_key": "s",
                "device": "deck", "cf_client_id": "id.access", "cf_client_secret": "cs"}
        code = base64.b64encode(json.dumps(body).encode()).decode()
        values = storecheck.parse_setup_code(" %s\n" % code[:20] + "\n" + code[20:])
        self.assertEqual(values, body)

    def test_a_setup_code_without_cloudflare(self):
        import base64
        body = {"type": "s3", "endpoint": "http://x:3900", "bucket": "b",
                "access_key": "k", "secret_key": "s"}
        values = storecheck.parse_setup_code(base64.b64encode(json.dumps(body).encode()).decode())
        self.assertNotIn("cf_client_id", values)

    def test_a_bad_setup_code_says_why(self):
        import base64
        for text, words in (("", "Copy the setup code"), ("hello there", "not a BlockSlot"),
                            (base64.b64encode(b'{"type":"s3"}').decode(), "has no endpoint")):
            with self.assertRaises(ValueError) as caught:
                storecheck.parse_setup_code(text)
            self.assertIn(words, str(caught.exception))

    def test_a_missing_folder_is_an_unreachable_server(self):
        lines = storecheck.test_store(self.conf(type="local",
                                                root=str(self.dir / "gone")))
        self.assertFalse(lines[-1][1])
        self.assertTrue(lines[-1][2].startswith("Could not reach the server"))

    def check_with(self, fail_on, error, **store):
        section = dict({"type": "local", "root": "/x"}, **store)
        return storecheck.test_store(self.conf(**section),
                                     store=FakeStore(fail_on, error))

    def test_offline_is_said_plainly(self):
        lines = self.check_with("list", self.ss.StoreOffline("timed out"))
        self.assertEqual(lines[-1][0], "Reach the store")
        self.assertEqual(lines[-1][2], "Could not reach the server: timed out")

    def test_a_refusal_carries_the_reason(self):
        lines = self.check_with("put", self.ss.StoreRefused("AccessDenied: no"))
        self.assertEqual(lines[-1][0], "Write a test file")
        self.assertEqual(lines[-1][2], "The server said no: AccessDenied: no")
        self.assertNotIn("Cloudflare", lines[-1][2])

    def test_a_cloudflare_refusal_names_the_token(self):
        lines = self.check_with("list", self.ss.StoreRefused("HTTP 403"),
                                cf_client_id="id", cf_client_secret="s")
        self.assertIn("The server said no: HTTP 403", lines[-1][2])
        self.assertIn("Cloudflare token is missing, wrong or expired", lines[-1][2])

    def test_the_engines_own_cloudflare_words_are_not_repeated(self):
        reason = ("Cloudflare refused the request (HTTP 403). The service token "
                  "is missing, wrong or expired.")
        lines = self.check_with("list", self.ss.StoreRefused(reason))
        self.assertEqual(lines[-1][2].count("missing, wrong or expired"), 1)

    def test_a_failed_read_back_still_tidies_up(self):
        store = FakeStore("get", self.ss.StoreOffline("dropped"))
        lines = storecheck.test_store(self.conf(type="local", root="/x"), store=store)
        self.assertEqual(lines[-1][0], "Read it back")
        self.assertFalse(lines[-1][1])
        self.assertEqual(store.objects, {})

    def test_a_failed_delete_is_reported(self):
        lines = self.check_with("delete", self.ss.StoreRefused("read only"))
        self.assertEqual(lines[-1][0], "Delete it")
        self.assertFalse(lines[-1][1])

    def test_an_unexpected_error_is_still_a_line(self):
        lines = self.check_with("list", ValueError("odd"))
        self.assertEqual(lines[-1][2], "Something went wrong: odd")


class StoreImport(Temp):
    def gamesaves(self):
        root = self.dir / "gamesaves"
        game = root / "deck" / "DS2"
        backup = game / "backup-20260924T000000Z"
        backup.mkdir(parents=True)
        (game / "mapping.yaml").write_text(
            "name: DS2\nbackups: [backup-20260924T000000Z]\n", encoding="utf-8")
        (backup / "save.sl2").write_bytes(b"save")
        return root

    def test_imports_a_syncthing_folder_onto_a_folder_store(self):
        store_root = self.dir / "store"
        store_root.mkdir()
        conf = settings.Settings(data={"store": {"type": "local",
                                                 "root": str(store_root)}})
        root = self.gamesaves()
        self.assertEqual(storecheck.count_backups(str(root)), (1, 1, 1))
        said = []
        heads = storecheck.import_folder(conf, str(root), say=said.append)
        self.assertEqual(list(heads), ["DS2"])
        self.assertEqual(said, ["1 of 1: deck DS2"])
        # The old folder is only read.
        self.assertTrue((root / "deck" / "DS2" / "backup-20260924T000000Z"
                         / "save.sl2").is_file())

    def test_the_default_folder_comes_from_syncthings_own_config(self):
        conf = settings.Settings(data={"syncthing": {"folder": "gamesaves",
                                                     "device_dir": "deck"}})
        found = storecheck.default_import_folder(
            conf, {"folders": [{"id": "other", "path": "/a"},
                               {"id": "gamesaves", "path": "/srv/gamesaves"}]})
        self.assertEqual(found, str(Path("/srv/gamesaves")))
        self.assertEqual(storecheck.default_import_folder(conf, {"folders": []}), "")

    def test_an_absolute_device_folder_names_its_parent(self):
        where = str(self.dir / "gamesaves" / "deck")
        conf = settings.Settings(data={"syncthing": {"device_dir": where}})
        self.assertEqual(storecheck.default_import_folder(conf, {}),
                         str(self.dir / "gamesaves"))


class DaemonLine(unittest.TestCase):
    def test_no_daemon(self):
        self.assertEqual(storecheck.describe_daemon(None),
                         "The daemon is not running.")

    def test_queue_error_and_last_contact(self):
        text = storecheck.describe_daemon({
            "queued": [{"id": "a"}, {"id": "b"}],
            "error": {"kind": "offline", "message": "cannot reach nas"},
            "last_ok": "2026-09-24T06:04:29Z"})
        self.assertIn("2 saves waiting to upload.", text)
        self.assertIn("Last error: cannot reach nas", text)
        self.assertIn("Last reached the store 2026-09-2", text)

    def test_a_paused_daemon_says_so(self):
        self.assertIn("Uploads are paused.",
                      storecheck.describe_daemon({"queued": [], "paused": True}))

    def test_an_idle_daemon(self):
        self.assertEqual(storecheck.describe_daemon({"queued": []}),
                         "Nothing waiting to upload.")

    def test_a_state_dir_with_no_daemon_reads_as_none(self):
        with tempfile.TemporaryDirectory() as where:
            conf = settings.Settings(data={"store": {"state_dir": where}})
            self.assertIsNone(storecheck.daemon_status(conf))


class RestartingTheDaemon(unittest.TestCase):
    def test_argv_matches_the_run_value(self):
        from gui.core import autostart
        self.assertEqual(autostart.command_argv(frozen=True, executable="C:/B.exe"),
                         ["C:/B.exe", "--daemon"])
        argv = autostart.command_argv(frozen=False, script="/r/gui/blockslot.py",
                                      python="/usr/bin/python3")
        self.assertEqual(argv, ["/usr/bin/python3", "/r/gui/blockslot.py", "--daemon"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
