#!/usr/bin/env python3
"""Zip the built plugin the way Decky installs it.

    pnpm install && pnpm build && python3 scripts/package.py

Writes out/Blockslot.zip: one folder named after the plugin, holding the
backend, the built panel, the shared code and, at its root, what defaults/
holds. That is the shape Decky's store build produces, and what Decky
Loader's "Install Plugin from ZIP" (Settings, Developer) expects.

In the main repo the shared code is staged in first. In the published plugin
repo it is already there, and stage.py is not.
"""

import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
NAME = "Blockslot"          # the plugin's identity in Decky; never renamed

FILES = ("main.py", "plugin.json", "package.json", "LICENSE", "README.md")
DIRECTORIES = ("dist", "py_modules")


def add_tree(bundle, source, inside):
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            bundle.write(str(path), "%s/%s" % (inside, path.relative_to(source).as_posix()))


def main():
    stage = HERE / "stage.py"
    if stage.is_file() and (PLUGIN.parent / "gui").is_dir():
        if subprocess.run([sys.executable, str(stage)]).returncode:
            return 1
    if not (PLUGIN / "dist" / "index.js").is_file():
        print("no dist/index.js: run pnpm install and pnpm build first", file=sys.stderr)
        return 1
    out = PLUGIN / "out"
    out.mkdir(exist_ok=True)
    target = out / (NAME + ".zip")
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:
            bundle.write(str(PLUGIN / name), "%s/%s" % (NAME, name))
        for name in DIRECTORIES:
            add_tree(bundle, PLUGIN / name, "%s/%s" % (NAME, name))
        # defaults/ lands in the plugin's root, as the store build does it.
        add_tree(bundle, PLUGIN / "defaults", NAME)
    print("wrote %s (%.1f MB)" % (target, target.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
