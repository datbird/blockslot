#!/usr/bin/env python3
"""Build the tree that the public plugin repo holds.

    python3 scripts/publish.py <output directory>

The Decky store takes a plugin as a git submodule, and the root of that
repository has to BE the plugin. Blockslot's plugin lives in decky/ and carries
staged copies of code that lives elsewhere, so the store cannot point here.
This writes a complete, self-contained plugin into a directory, which is then
committed to the plugin's own repository.

It copies a fixed list and nothing else. A file is published because it is
named here, never because it happened to be lying in decky/.

Then it scans what it wrote for the owner's own names with the scan of
tools/publish_public.py, and fails if it finds one. That script (and so its
list of names) is kept out of the public repo; a copy without it says so and
publishes unscanned.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
ROOT = PLUGIN.parent

FILES = ("main.py", "plugin.json", "package.json", "pnpm-lock.yaml",
         "rollup.config.js", "tsconfig.json", "LICENSE", "README.md",
         "scripts/package.py")
DIRECTORIES = ("src", "py_modules", "defaults")
ASSETS = (("assets/blockslot.png", "assets/blockslot.png"),)

IGNORE = "node_modules/\ndist/\nout/\n__pycache__/\n*.pyc\n"
PRIVACY = ROOT / "tools" / "publish_public.py"
# The game index is public data: real titles, scanned for network and
# account names only.
DATA_FILES = ("defaults/index/games.json",)


def privacy_scan(out):
    """[(file, what matched)], or None when there is no list to scan with."""
    if not PRIVACY.is_file():
        return None
    spec = importlib.util.spec_from_file_location("publish_public", str(PRIVACY))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scan(out, DATA_FILES)


def main():
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(sys.argv[1]).resolve()
    if subprocess.run([sys.executable, str(HERE / "stage.py")]).returncode:
        return 1

    # Everything but .git is replaced, so a file dropped from the lists above
    # also leaves the published tree.
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():
        if old.name == ".git":
            continue
        if old.is_dir():
            shutil.rmtree(str(old))
        else:
            old.unlink()

    for name in FILES:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(PLUGIN / name), str(out / name))
    for name in DIRECTORIES:
        shutil.copytree(str(PLUGIN / name), str(out / name),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for source, target in ASSETS:
        (out / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(ROOT / source), str(out / target))
    (out / ".gitignore").write_text(IGNORE, encoding="utf-8")

    count = sum(1 for item in out.rglob("*")
                if item.is_file() and ".git" not in item.parts)
    found = privacy_scan(out)
    if found is None:
        print("note: no %s here, so no privacy scan" % PRIVACY.relative_to(ROOT),
              file=sys.stderr)
    elif found:
        for name, text in found:
            print("PRIVATE %s: %s" % (name, text), file=sys.stderr)
        print("refusing: the tree above names private things", file=sys.stderr)
        return 1
    print("published %d files to %s%s" % (count, out,
                                          ", privacy scan clean" if found == [] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
