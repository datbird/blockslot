#!/usr/bin/env python3
"""Copy the shared parts into the plugin tree before a build.

The engine, the core and the game index all live once, at the top of the repo.
A plugin has to carry its own copy, because Decky ships a directory. Copying
them here, from a script, is what stops the two faces of Blockslot from slowly
disagreeing about which save wins.

WHY defaults/

The store builds a plugin with the decky CLI, and that packs a fixed list:
dist, bin, py_modules, main.py, plugin.json, package.json, LICENSE, README.md,
and the CONTENTS of defaults/, which land in the plugin's root. A directory
called engine/ beside main.py is silently left out. So the engine, the index
and the third party notices are staged under defaults/, and an installed
plugin finds them at engine/, index/ and THIRD-PARTY-NOTICES.md.

    python3 scripts/stage.py
"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
ROOT = PLUGIN.parent

COPIES = (
    ("gui/core", "py_modules/blockslot_core", "dir", True),
    ("index/games.json", "defaults/index/games.json", "file", True),
    ("engine/savepick.py", "defaults/engine/savepick.py", "file", True),
    ("engine/slotstore.py", "defaults/engine/slotstore.py", "file", True),
    ("engine/slotd.py", "defaults/engine/slotd.py", "file", True),
    ("engine/saveunits.py", "defaults/engine/saveunits.py", "file", True),
    ("THIRD-PARTY-NOTICES.md", "defaults/THIRD-PARTY-NOTICES.md", "file", True),
    # A Decky plugin is a redistribution, so the licence travels with it.
    ("LICENSE", "LICENSE", "file", True),
)


def main():
    for source, target, kind, required in COPIES:
        src = ROOT / source
        dst = PLUGIN / target
        if not src.exists():
            if required:
                print("missing: %s" % src, file=sys.stderr)
                return 1
            print("skipped %s, not in the repo yet" % source)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if kind == "dir":
            shutil.rmtree(str(dst), ignore_errors=True)
            shutil.copytree(str(src), str(dst),
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(str(src), str(dst))
        print("staged %s" % target)
    # The package needs to be importable by name from py_modules.
    init = PLUGIN / "py_modules" / "blockslot_core" / "__init__.py"
    if not init.is_file():
        init.write_text('"""Blockslot core, copied from gui/core by '
                        'scripts/stage.py."""\n', encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
