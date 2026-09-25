#!/usr/bin/env python3
"""Tests for `Blockslot.exe --pick`: the exe running the engine it carries.

A launch option on a Windows PC with no python is
`"<Blockslot.exe>" --pick [switches] -- %command%`. That has to be exactly
`pythonw savepick.py [switches] -- %command%`: the same argv reaches
savepick.main and its answer is the exit code.

    python3 gui/tests/test_pick.py
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui import blockslot  # noqa: E402


class FakeSavepick(object):
    def __init__(self, code):
        self.code = code
        self.calls = []

    def main(self, argv):
        self.calls.append(argv)
        return self.code


class TheHandOff(unittest.TestCase):
    def _with(self, fake):
        real = blockslot.load_savepick
        blockslot.load_savepick = lambda: fake
        self.addCleanup(setattr, blockslot, "load_savepick", real)

    def test_everything_after_pick_reaches_savepick_main(self):
        fake = FakeSavepick(0)
        self._with(fake)
        argv = ["--pick", "--tree", "Retro", "--borderless", "--",
                "C:/G/rb.exe", "--config", "x", "--check"]
        self.assertEqual(blockslot.main(argv), 0)
        self.assertEqual(fake.calls, [["--tree", "Retro", "--borderless", "--",
                                       "C:/G/rb.exe", "--config", "x",
                                       "--check"]])

    def test_the_exit_code_is_passed_through(self):
        self._with(FakeSavepick(7))
        self.assertEqual(blockslot.main(["--pick", "--", "cmd"]), 7)

    def test_it_reads_sys_argv_when_run_as_the_program(self):
        fake = FakeSavepick(3)
        self._with(fake)
        self.addCleanup(setattr, sys, "argv", sys.argv)
        sys.argv = ["Blockslot.exe", "--pick", "--no-sync", "--", "game.exe"]
        self.assertEqual(blockslot.main(), 3)
        self.assertEqual(fake.calls, [["--no-sync", "--", "game.exe"]])

    def test_pick_only_counts_first(self):
        # A game argument that happens to be --pick is the game's.
        fake = FakeSavepick(0)
        self._with(fake)
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            blockslot.main(["--check", "--pick"])
        self.assertEqual(fake.calls, [])

    def test_the_shipped_engine_is_what_is_loaded(self):
        module = blockslot.load_savepick()
        self.assertEqual(Path(module.__file__).resolve(),
                         (ROOT / "engine" / "savepick.py").resolve())
        self.assertTrue(callable(module.main))


class EndToEnd(unittest.TestCase):
    """The real engine through --pick, in a home of its own."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="blockslot-pick-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.env = dict(os.environ)
        for name in ("SteamAppId", "SteamGameId", "SAVEPICK_LUDUSAVI"):
            self.env.pop(name, None)
        self.env.update({
            "HOME": str(self.dir), "USERPROFILE": str(self.dir),
            "APPDATA": str(self.dir / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.dir / "AppData" / "Local"),
            "XDG_STATE_HOME": str(self.dir / "state"),
            "TEMP": str(self.dir / "tmp"), "TMP": str(self.dir / "tmp"),
            "TMPDIR": str(self.dir / "tmp"),
        })
        (self.dir / "tmp").mkdir()

    def _run(self, *switches, code=0):
        game = [sys.executable, "-c", "import sys; sys.exit(%d)" % code]
        return subprocess.run(
            [sys.executable, str(ROOT / "gui" / "blockslot.py"), "--pick"]
            + list(switches) + ["--"] + game,
            env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60)

    def _log(self):
        if sys.platform == "win32":
            path = self.dir / "tmp" / "savepick.log"
        else:
            path = self.dir / "state" / "blockslot" / "savepick.log"
        return path.read_text(encoding="utf-8")

    def test_no_sync_launches_and_returns_the_games_code(self):
        done = self._run("--no-sync", code=5)
        self.assertEqual(done.returncode, 5, done.stderr)
        log = self._log()
        self.assertIn("no-sync: launching without touching saves", log)
        self.assertIn("the game exited with 5", log)

    def test_sync_with_no_steam_id_still_launches(self):
        done = self._run(code=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no SteamAppId in the environment", self._log())


if __name__ == "__main__":
    unittest.main()
