#!/usr/bin/env python3
"""Run every Blockslot GUI test.

    python3 gui/tests/run.py

The window tests need a display and skip themselves without one, so this is
the same command on a desktop, over ssh and in CI.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(HERE.parents[1]))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py",
                                                top_level_dir=str(HERE.parents[1]))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
