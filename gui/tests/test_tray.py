#!/usr/bin/env python3
"""Tests for the daemon host: gui/tray.py and gui/core/autostart.py.

The words, the menu, the balloons and the command lines are plain functions
and run everywhere. The Win32 icon itself runs only on Windows, where it can
at least show that the window is made and Shell_NotifyIconW accepts it.

    python3 gui/tests/test_tray.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui import tray  # noqa: E402
from gui.core import autostart  # noqa: E402

WINDOWS = sys.platform == "win32"


def status(queued=(), error=None, paused=False, store="nas"):
    return {"device": "pc", "store": store, "queued": list(queued),
            "error": error, "last_ok": None, "paused": paused}


def item(snap, progress=None):
    return {"id": snap, "game": "Hades", "bytes": 10, "progress": progress}


OFFLINE = {"kind": "offline", "message": "no network", "at": "x"}
REFUSED = {"kind": "refused", "message": "the CF token has expired", "at": "x"}


class TheWords(unittest.TestCase):
    def test_nothing_queued_is_all_uploaded(self):
        self.assertEqual(tray.tooltip(status()), "BlockSlot: all saves uploaded")
        self.assertEqual(tray.state_of(None), tray.IDLE)

    def test_uploading_says_which_of_how_many(self):
        text = tray.tooltip(status([item("a"), item("b", [40, 100]), item("c")]))
        self.assertEqual(text, "BlockSlot: uploading 2 of 3 (40%)")

    def test_offline_counts_what_waits(self):
        self.assertEqual(tray.tooltip(status([item("a"), item("b")], OFFLINE)),
                         "BlockSlot: 2 saves queued, offline")
        self.assertEqual(tray.tooltip(status([item("a")], OFFLINE)),
                         "BlockSlot: 1 save queued, offline")

    def test_queued_with_no_error_is_waiting(self):
        self.assertEqual(tray.state_of(status([item("a")])), tray.WAITING)

    def test_a_refusal_wins_and_carries_its_reason(self):
        text = tray.tooltip(status([item("a", [1, 2])], REFUSED, paused=True))
        self.assertEqual(text, "BlockSlot: uploads refused. the CF token has expired")

    def test_paused_beats_uploading(self):
        self.assertEqual(tray.tooltip(status([item("a", [1, 2])], paused=True)),
                         "BlockSlot: uploads paused, 1 save waiting")

    def test_the_tip_fits_what_windows_keeps(self):
        long = dict(REFUSED, message="x" * 400)
        text = tray.tooltip(status(error=long))
        self.assertLessEqual(len(text), tray.TIP_LIMIT)
        self.assertTrue(text.endswith("..."))

    def test_the_menu_names_the_pause_it_would_do(self):
        self.assertIn((tray.MENU_PAUSE, "Pause uploads"), tray.menu_items(False))
        self.assertIn((tray.MENU_PAUSE, "Resume uploads"), tray.menu_items(True))
        labels = [label for _id, label in tray.menu_items(False) if label]
        self.assertEqual(labels, ["Open BlockSlot", "Upload now", "Pause uploads", "Quit"])


class Balloons(unittest.TestCase):
    def test_a_save_that_waited_offline_is_announced_once(self):
        w = tray.Watcher()
        self.assertEqual(w.update(status([item("a")], OFFLINE)), [])
        self.assertEqual(w.update(status([item("a", [1, 2])])), [])
        shown = w.update(status())
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0][2], "info")
        self.assertIn("1 save that waited for a connection is now on nas", shown[0][1])
        self.assertEqual(w.update(status()), [])

    def test_an_ordinary_upload_is_quiet(self):
        w = tray.Watcher()
        w.update(status([item("a")]))
        self.assertEqual(w.update(status()), [])

    def test_a_refusal_is_said_once_per_reason(self):
        w = tray.Watcher()
        first = w.update(status([item("a")], REFUSED))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0][2], "error")
        self.assertIn("expired", first[0][1])
        self.assertEqual(w.update(status([item("a")], REFUSED)), [])
        other = dict(REFUSED, message="access denied")
        self.assertEqual(len(w.update(status([item("a")], other))), 1)
        w.update(status())
        self.assertEqual(len(w.update(status([item("a")], REFUSED))), 1)


class Commands(unittest.TestCase):
    def test_the_exe_opens_itself(self):
        self.assertEqual(tray.gui_command(frozen=True, executable="C:/B/Blockslot.exe"),
                         ["C:/B/Blockslot.exe"])

    def test_the_source_tree_needs_its_interpreter(self):
        argv = tray.gui_command(frozen=False, python="pythonw.exe", script="gui/blockslot.py",
                                config="c.json")
        self.assertEqual(argv, ["pythonw.exe", "gui/blockslot.py", "--config", "c.json"])
        self.assertNotIn("--daemon", argv)

    def test_autostart_runs_the_exe_with_daemon(self):
        self.assertEqual(autostart.command_line(frozen=True, executable=r"C:\B\Blockslot.exe"),
                         r'"C:\B\Blockslot.exe" --daemon')

    def test_autostart_from_source_names_pythonw_and_the_script(self):
        line = autostart.command_line(frozen=False, python=r"C:\Py\pythonw.exe",
                                      script=r"C:\repo\gui\blockslot.py")
        self.assertEqual(line, r'"C:\Py\pythonw.exe" "C:\repo\gui\blockslot.py" --daemon')

    def test_autostart_default_script_is_this_tree(self):
        line = autostart.command_line(frozen=False, python="py")
        self.assertIn(str(ROOT / "gui" / "blockslot.py"), line)


class FakeKey(object):
    def __init__(self, reg):
        self.reg = reg

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry(object):
    """The part of winreg autostart uses, over a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1
    REG_SZ = 1

    def __init__(self, key_exists=True):
        self.values = {}
        self.key_exists = key_exists

    def OpenKey(self, root, path, reserved, access):
        assert root == "HKCU" and path == autostart.RUN_KEY
        if not self.key_exists:
            raise FileNotFoundError(path)
        return FakeKey(self)

    def CreateKeyEx(self, root, path, reserved, access):
        self.key_exists = True
        return self.OpenKey(root, path, reserved, access)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class Autostart(unittest.TestCase):
    def test_enable_writes_this_install(self):
        reg = FakeRegistry(key_exists=False)
        self.assertFalse(autostart.is_enabled(reg, windows=True))
        self.assertTrue(autostart.enable(reg, windows=True))
        self.assertEqual(reg.values["Blockslot"], autostart.command_line())
        self.assertTrue(autostart.is_enabled(reg, windows=True))

    def test_disable_removes_it_and_is_happy_when_gone(self):
        reg = FakeRegistry()
        autostart.enable(reg, windows=True)
        self.assertTrue(autostart.disable(reg, windows=True))
        self.assertNotIn("Blockslot", reg.values)
        self.assertTrue(autostart.disable(reg, windows=True))
        self.assertFalse(autostart.is_enabled(reg, windows=True))

    def test_a_value_from_a_moved_install_is_not_enabled(self):
        reg = FakeRegistry()
        reg.values["Blockslot"] = '"D:\\old\\Blockslot.exe" --daemon'
        self.assertFalse(autostart.is_enabled(reg, windows=True))

    def test_nothing_happens_off_windows(self):
        reg = FakeRegistry()
        self.assertFalse(autostart.enable(reg, windows=False))
        self.assertFalse(autostart.disable(reg, windows=False))
        self.assertFalse(autostart.is_enabled(reg, windows=False))
        self.assertEqual(reg.values, {})


class FakeTray(object):
    def __init__(self):
        self.tips = []
        self.balloons = []
        self.stopped = False
        self.failed = None

    def set_tip(self, text):
        self.tips.append(text)

    def balloon(self, title, text, kind):
        self.balloons.append((title, text, kind))

    def stop(self):
        self.stopped = True


class FakeDaemon(object):
    def __init__(self):
        self.paused = False
        self.stopping = False
        self.kick = threading.Event()
        self.answer = status()

    def status(self):
        return dict(self.answer, paused=self.paused)


class TheHost(unittest.TestCase):
    def setUp(self):
        self.daemon = FakeDaemon()
        self.tray = FakeTray()
        self.host = tray.TrayHost(self.daemon, tray=self.tray)

    def test_a_tick_sets_the_tip_and_balloons(self):
        self.daemon.answer = status([item("a")], REFUSED)
        self.host.tick()
        self.assertIn("refused", self.tray.tips[-1])
        self.assertEqual(self.tray.balloons[-1][2], "error")

    def test_pause_and_resume(self):
        self.host.command(tray.MENU_PAUSE)
        self.assertTrue(self.daemon.paused)
        self.assertIn("paused", self.tray.tips[-1])
        self.host.command(tray.MENU_PAUSE)
        self.assertFalse(self.daemon.paused)
        self.assertTrue(self.daemon.kick.is_set())

    def test_upload_now_kicks_and_lifts_a_pause(self):
        self.daemon.paused = True
        self.host.command(tray.MENU_UPLOAD)
        self.assertTrue(self.daemon.kick.is_set())
        self.assertFalse(self.daemon.paused)

    def test_quit_stops_the_tray(self):
        self.host.command(tray.MENU_QUIT)
        self.assertTrue(self.tray.stopped)


class RunDaemon(unittest.TestCase):
    """--daemon without a tray, which is what Linux and macOS get."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blockslot-tray-")
        self.state = os.path.join(self.dir, "state")
        self.config = os.path.join(self.dir, "savepick.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_config(self, store):
        with open(self.config, "w") as handle:
            json.dump({"store": store}, handle)

    def test_no_store_exits_quietly(self):
        with open(self.config, "w") as handle:
            json.dump({}, handle)
        self.assertEqual(tray.run_daemon(self.config, with_tray=False), 0)

    def test_it_serves_and_a_second_start_is_a_no_op(self):
        os.makedirs(os.path.join(self.dir, "store"))
        self.write_config({"type": "local", "root": os.path.join(self.dir, "store"),
                           "state_dir": self.state, "device": "pc"})
        thread = threading.Thread(target=tray.run_daemon, args=(self.config, False),
                                  daemon=True)
        thread.start()
        slotd = tray.load_slotd()
        client = None
        for _ in range(100):
            client = slotd._client_from_info(self.state)
            if client:
                break
            time.sleep(0.05)
        self.assertIsNotNone(client)
        self.assertEqual(client.status()["device"], "pc")
        started = time.monotonic()
        self.assertEqual(tray.run_daemon(self.config, with_tray=False), 0)
        self.assertLess(time.monotonic() - started, 5)

    def test_stopping_withdraws_the_daemon_file(self):
        slotd = tray.load_slotd()
        os.makedirs(self.state)
        info = os.path.join(self.state, "daemon.json")
        with open(info, "w") as handle:
            json.dump({"port": 1, "token": "t", "pid": os.getpid()}, handle)
        store = slotd.ss.LocalStore(os.path.join(self.dir, "store"))
        daemon = slotd.Daemon(store, "pc", state_dir=self.state)
        tray.stop_daemon(daemon, self.state, grace=1)
        self.assertFalse(os.path.exists(info))
        self.assertTrue(daemon.stopping)


@unittest.skipUnless(WINDOWS, "the notification area is Windows only")
class TheRealIcon(unittest.TestCase):
    def test_the_icon_is_added_and_removed(self):
        commands = []
        icon = tray.Tray(on_command=commands.append, menu=lambda: tray.menu_items(False),
                         tick_seconds=60).start()
        try:
            self.assertIsNone(icon.failed)
            self.assertTrue(icon.hwnd)
            self.assertTrue(icon.hicon)
            print("\n  NIM_ADD accepted: %s, hwnd %#x" % (icon.added, icon.hwnd))
            if icon.added:
                icon.set_tip("Blockslot: test")
        finally:
            icon.stop()
            icon.join(5)
        self.assertIsNone(icon.hwnd)
        self.assertFalse(icon.added)


class StoppedFromOutside(unittest.TestCase):
    def test_the_icon_goes_when_the_daemon_is_stopped(self):
        from gui import tray

        class FakeTray(object):
            stopped = False

            def stop(self):
                self.stopped = True

        daemon = type("D", (), {"stopping": True, "paused": False})()
        host = tray.TrayHost(daemon, tray=FakeTray())
        host.tick()
        self.assertTrue(host.tray.stopped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
