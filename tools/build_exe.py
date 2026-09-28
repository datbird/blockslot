#!/usr/bin/env python3
"""Build Blockslot as one self-contained program.

    python3 tools/build_exe.py            build for this machine
    python3 tools/build_exe.py --clean    throw the work directory away first
    python3 tools/build_exe.py --zip      on a Mac, also zip the .app to ship

The result is `dist/Blockslot.exe` on Windows, `dist/Blockslot` on Linux, and
`dist/Blockslot.app` on macOS. It carries the game index and the engine inside
it, so it is the only thing that has to be copied to a machine.

THE MAC BUILD

A folder bundle, not one file: PyInstaller 6 deprecates --onefile with an
.app, whose one file would unpack itself into a temporary folder on every
start, and that is every game launch, because Steam runs the app's binary
with --pick in front of each wrapped game. It is built for arm64
(--target-arch; the python doing the build has to be universal2 or arm64,
which python.org's installer is), named com.datbird.blockslot, and signed
ad hoc: an Apple Silicon Mac runs nothing unsigned. The icon is
assets/blockslot.icns, which tools/make_icon.py writes.

`--zip` then makes dist/Blockslot-mac-arm64.zip with ditto, the way Finder's
own Compress does, so the bundle's links, attributes and signature survive.

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
BUNDLE_ID = "com.datbird.blockslot"
MAC_ARCH = "arm64"
MAC_ZIP = "Blockslot-mac-arm64.zip"

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


def is_mac():
    return sys.platform == "darwin"


def mac_arguments(arch=MAC_ARCH):
    """What only the Mac build adds: a named, arm64 .app bundle."""
    return ["--target-arch", arch,
            "--osx-bundle-identifier", BUNDLE_ID]


def icon_path(mac=False):
    if mac:
        name = "blockslot.icns"
    elif os.name == "nt":
        name = "blockslot.ico"
    else:
        name = "blockslot.png"
    return ROOT / "assets" / name


def build_command(debug=False, mac=None, arch=MAC_ARCH):
    """The PyInstaller command line. `mac` says which build it is for."""
    mac = is_mac() if mac is None else mac
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    # A Mac .app is a folder bundle; see THE MAC BUILD above.
    if not mac:
        command.append("--onefile")
    if not debug:
        command.append("--windowed")
    if mac:
        command += mac_arguments(arch)
    icon = icon_path(mac)
    if icon.is_file():
        command += ["--icon", str(icon)]
    command += data_arguments()
    # tkinter is imported inside a function, so PyInstaller's scan can miss it.
    command += ["--hidden-import", "tkinter", "--hidden-import", "tkinter.font"]
    for module in DAEMON_IMPORTS + PICK_IMPORTS:
        command += ["--hidden-import", module]
    command.append(str(ROOT / "gui" / "blockslot.py"))
    return command


def build(clean=False, debug=False, arch=MAC_ARCH, version=None):
    if not have_pyinstaller():
        raise SystemExit(
            "PyInstaller is not installed.\n"
            "    %s -m pip install --user pyinstaller" % sys.executable)

    command = build_command(debug, arch=arch)
    if clean:
        for path in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(str(path), ignore_errors=True)

    print(" ".join(command))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit("the build failed with %s" % result.returncode)

    app = ROOT / "dist" / (NAME + ".app")
    if is_mac() and app.is_dir():
        finish_app(app, version)
        print("built %s" % app)
        return 0
    produced = ROOT / "dist" / (NAME + (".exe" if os.name == "nt" else ""))
    if produced.is_file():
        print("built %s (%.1f MB)"
              % (produced, produced.stat().st_size / (1024.0 * 1024.0)))
    else:
        print("built into %s" % (ROOT / "dist"))
    return 0


def plain_version(text):
    """1.2.3 from v1.2.3, or None for a branch name on a manual run."""
    text = (text or "").strip()
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if text and all(part.isdigit() for part in parts):
        return text
    return None


def finish_app(app, version=None):
    """Put the version in Info.plist, then sign the bundle ad hoc again.

    PyInstaller's command line has no version for a bundle, and it signs
    the bundle itself, so any change to Info.plist after it has to be
    followed by a new signature or the seal no longer matches.
    """
    import plistlib
    info = Path(app) / "Contents" / "Info.plist"
    with open(str(info), "rb") as handle:
        data = plistlib.load(handle)
    data["CFBundleDisplayName"] = "BlockSlot"
    data["NSHighResolutionCapable"] = True
    version = plain_version(version)
    if version:
        data["CFBundleShortVersionString"] = version
        data["CFBundleVersion"] = version
    with open(str(info), "wb") as handle:
        plistlib.dump(data, handle)
    run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])


def zip_app(app=None, out=None):
    """dist/Blockslot-mac-arm64.zip, made by ditto so the bundle survives."""
    if not is_mac():
        raise SystemExit("--zip makes the Mac download, and needs a Mac")
    app = Path(app or ROOT / "dist" / (NAME + ".app"))
    if not app.is_dir():
        raise SystemExit("no %s to zip; build it first" % app)
    out = Path(out or ROOT / "dist" / MAC_ZIP)
    if out.exists():
        out.unlink()
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
         str(app), str(out)])
    print("zipped %s (%.1f MB)"
          % (out, out.stat().st_size / (1024.0 * 1024.0)))
    return out


def run(command):
    print(" ".join(command))
    result = subprocess.run(command)
    if result.returncode != 0:
        raise SystemExit("%s failed with %s" % (command[0], result.returncode))


def main():
    parser = argparse.ArgumentParser(description="Build the Blockslot exe")
    parser.add_argument("--clean", action="store_true",
                        help="remove build and dist first")
    parser.add_argument("--debug", action="store_true",
                        help="keep a console, for finding out why it will not "
                             "start (on a Mac this builds no .app)")
    parser.add_argument("--arch", default=MAC_ARCH,
                        help="the Mac build's --target-arch (default %s)"
                             % MAC_ARCH)
    parser.add_argument("--version",
                        help="the Mac bundle's version, such as v1.0.1")
    parser.add_argument("--zip", action="store_true",
                        help="on a Mac, also make dist/%s" % MAC_ZIP)
    parser.add_argument("--zip-only", action="store_true",
                        help="zip the .app already in dist, without building")
    args = parser.parse_args()
    if not args.zip_only:
        build(args.clean, args.debug, args.arch, args.version)
    if args.zip or args.zip_only:
        zip_app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
