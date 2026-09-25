#!/usr/bin/env python3
"""Build Blockslot as one executable file.

    python3 tools/build_exe.py            build for this machine
    python3 tools/build_exe.py --clean    throw the work directory away first

The result is `dist/Blockslot.exe` on Windows, `dist/Blockslot` elsewhere, and
`dist/Blockslot.app` on macOS. It carries the game index and the engine inside
it, so it is the only file that has to be copied to a machine.

PyInstaller is needed HERE, to build. It is not needed on the machine that runs
the result, and Blockslot itself still imports nothing outside the standard
library. If that ever stops being true, this script is where it will show up,
because the hidden imports list would start growing.

WHY A WINDOWED BUILD

A console build flashes a black window every time it starts, which is exactly
the complaint that started all of this. So the build has no console at all, and
`--check` attaches to the terminal that started it when there is one. See
`attach_console` in gui/blockslot.py.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "Blockslot"

# What ships inside the executable, as (source, destination directory).
DATA = (
    ("index/games.json", "index"),
    ("engine/savepick.py", "engine"),
    ("engine/slotstore.py", "engine"),
    ("engine/slotd.py", "engine"),
    ("engine/saveunits.py", "engine"),
    # The tray icon. Without it the tray falls back to the exe's own icon.
    ("assets/blockslot.ico", "assets"),
    ("assets/blockslot-tray.ico", "assets"),
    ("assets/blockslot.png", "assets"),
)

# slotd and slotstore ship as data files, so PyInstaller never sees what they
# import. These are what the daemon needs that the window does not.
DAEMON_IMPORTS = ("http.server", "socketserver", "gzip", "hmac", "secrets",
                  "urllib.request", "urllib.error", "shlex", "ctypes.wintypes",
                  "concurrent.futures", "ssl")

# savepick ships as a data file too. On Windows the exe runs it for every
# wrapped game (`Blockslot.exe --pick`), so what it imports has to be inside.
PICK_IMPORTS = ("glob", "select", "signal", "socket", "struct", "hashlib",
                "tempfile", "threading", "urllib.parse")


def have_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return False


def data_arguments():
    separator = ";" if os.name == "nt" else ":"
    out = []
    for source, target in DATA:
        path = ROOT / source
        if not path.is_file():
            raise SystemExit("missing %s, which the build has to include"
                             % source)
        out += ["--add-data", "%s%s%s" % (path, separator, target)]
    return out


def build(clean=False, debug=False):
    if not have_pyinstaller():
        raise SystemExit(
            "PyInstaller is not installed.\n"
            "    %s -m pip install --user pyinstaller" % sys.executable)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if not debug:
        command.append("--windowed")
    icon = ROOT / "assets" / ("blockslot.ico" if os.name == "nt"
                              else "blockslot.png")
    if icon.is_file():
        command += ["--icon", str(icon)]
    command += data_arguments()
    # tkinter is imported inside a function, so PyInstaller's scan can miss it.
    command += ["--hidden-import", "tkinter", "--hidden-import", "tkinter.font"]
    for module in DAEMON_IMPORTS + PICK_IMPORTS:
        command += ["--hidden-import", module]
    command.append(str(ROOT / "gui" / "blockslot.py"))

    if clean:
        for path in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(str(path), ignore_errors=True)

    print(" ".join(command))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit("the build failed with %s" % result.returncode)

    produced = ROOT / "dist" / (NAME + (".exe" if os.name == "nt" else ""))
    if produced.is_file():
        print("built %s (%.1f MB)"
              % (produced, produced.stat().st_size / (1024.0 * 1024.0)))
    else:
        print("built into %s" % (ROOT / "dist"))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Build the Blockslot exe")
    parser.add_argument("--clean", action="store_true",
                        help="remove build and dist first")
    parser.add_argument("--debug", action="store_true",
                        help="keep a console, for finding out why it will not "
                             "start")
    args = parser.parse_args()
    return build(args.clean, args.debug)


if __name__ == "__main__":
    sys.exit(main())
