"""Where Steam is, who is signed in, and which games are installed.

Everything here is read-only and takes an explicit root, so a test can point it
at a directory tree it built itself. Nothing in this module needs Steam to be
installed, or running, or even to exist.
"""

import os
import re
import sys
from pathlib import Path

from . import paths, vdf

APPMANIFEST = re.compile(r"^appmanifest_(\d+)\.acf$")


class Game(object):
    """One installed Steam game, as the install folder describes it."""

    def __init__(self, appid, name, library, install_dir=None, last_updated=0,
                 size_on_disk=0):
        self.appid = int(appid)
        self.name = name
        self.library = Path(library)
        self.install_dir = install_dir
        self.last_updated = int(last_updated or 0)
        self.size_on_disk = int(size_on_disk or 0)

    def __repr__(self):
        return "Game(%d, %r)" % (self.appid, self.name)


# Where each way of installing Steam on Linux keeps its data, under the home.
NATIVE = "native"
SNAP = "snap"
FLATPAK = "flatpak"
SNAP_DATA = ("snap", "steam", "common", ".local", "share", "Steam")
FLATPAK_DATA = (".var", "app", "com.valvesoftware.Steam", ".local", "share",
                "Steam")


def candidate_roots(home=None):
    """Every place Steam is normally installed on this operating system.

    On Linux there are three installs, and each keeps its data somewhere
    else: the distribution's package in ~/.local/share/Steam (with ~/.steam
    pointing at it), the snap in ~/snap/steam/common/.local/share/Steam
    (Ubuntu's Software centre installs this one), and the flatpak under
    ~/.var/app. The snap gives Steam its own HOME, so its ~/.steam lives
    inside the snap too and never points at it from the real home.

    Order matters only as a tie-break: find_root prefers the Steam that is
    running, then the one signed into last.
    """
    home = Path(home) if home is not None else paths.home()
    if sys.platform == "win32":
        found = []
        registered = _windows_install_path()
        if registered:
            found.append(registered)
        for base in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
            value = os.environ.get(base)
            if value:
                found.append(Path(value) / "Steam")
        found.append(Path("C:/Program Files (x86)/Steam"))
        return found
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Steam"]
    return [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".steam" / "root",
        home.joinpath(*SNAP_DATA),
        home.joinpath(*FLATPAK_DATA),
    ]


def install_kind(root):
    """NATIVE, SNAP or FLATPAK: how the Steam at `root` was installed.

    It decides what a launch option can name (paths.python_for_steam): a snap
    or flatpak game starts in a sandbox with its own /usr. Read from the
    path, resolved, so ~/.steam/steam pointing into the snap counts as snap.
    """
    try:
        parts = Path(root).resolve().parts
    except (OSError, RuntimeError):
        parts = Path(root).parts
    if _contains(parts, SNAP_DATA):
        return SNAP
    if _contains(parts, FLATPAK_DATA):
        return FLATPAK
    return NATIVE


def _contains(parts, run):
    size = len(run)
    return any(tuple(parts[at:at + size]) == run
               for at in range(len(parts) - size + 1))


def running_roots(proc="/proc", home=None):
    """The Steam folders a running Steam client was started from (Linux).

    The client is `<root>/ubuntu12_32/steam` for every install, snap
    included, and its first argument is that full path. Read from
    /proc/<pid>/cmdline, which any process of the same user can read, so it
    needs neither pgrep nor psutil. Empty anywhere else.

    The flatpak is the exception: its sandbox mounts ~/.var/app/<id> over
    the home, so its path reads like a native install's. Its environment
    names it (FLATPAK_ID), and that is mapped back to the real folder.
    """
    home = Path(home) if home is not None else paths.home()
    found = []
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                first = handle.read(4096).split(b"\0", 1)[0]
        except OSError:
            continue
        program = Path(os.fsdecode(first))
        if program.name != "steam" or program.parent.name != "ubuntu12_32":
            continue
        root = program.parent.parent
        if _is_flatpak(os.path.join(proc, pid)):
            root = home.joinpath(*FLATPAK_DATA)
        if root not in found:
            found.append(root)
    return found


def _windows_install_path():
    try:
        import winreg
    except ImportError:
        return None
    for root, key in ((0x80000001, r"Software\Valve\Steam"),
                      (0x80000002, r"Software\WOW6432Node\Valve\Steam")):
        try:
            with winreg.OpenKey(root, key) as handle:
                for name in ("SteamPath", "InstallPath"):
                    try:
                        value, _ = winreg.QueryValueEx(handle, name)
                    except OSError:
                        continue
                    if value:
                        return Path(value)
        except OSError:
            continue
    return None


def _is_flatpak(where):
    try:
        with open(os.path.join(where, "environ"), "rb") as handle:
            return b"FLATPAK_ID=com.valvesoftware.Steam" in handle.read()
    except OSError:
        return False


def find_root(candidates=None, running=None):
    """The Steam directory in use, or None.

    A directory only counts when it holds userdata. An empty Steam folder left
    behind by an uninstall would otherwise be picked over the real one.

    With more than one (a native Steam tried once, then the snap), the one
    running now wins, then the one whose loginusers.vdf was written last:
    Steam rewrites it at every sign-in, so it names the install a person
    actually uses. `running` is running_roots() unless given.
    """
    if candidates is None:
        candidates = candidate_roots()
    unique = []
    for path in candidates:
        path = Path(path)
        key = _same(path)
        if key not in [_same(seen) for seen in unique]:
            unique.append(path)
    with_users = [path for path in unique if (path / "userdata").is_dir()]
    if with_users:
        if running is None:
            running = running_roots() if sys.platform.startswith("linux") else []
        live = set(_same(path) for path in running)
        for path in with_users:
            if _same(path) in live:
                return path
        return max(with_users, key=lambda path: (
            _mtime(path / "config" / "loginusers.vdf"),
            -with_users.index(path)))
    for path in unique:
        if (path / "steamapps").is_dir():
            return path
    return None


def _same(path):
    """A path with its links resolved, so ~/.steam/steam and the folder it
    points at are one install, not two."""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(path)


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def library_paths(root):
    """Every library folder, starting with Steam's own.

    libraryfolders.vdf has had three shapes. Current Steam writes a numbered
    map of objects with a `path`. Older Steam wrote a numbered map of plain
    strings. Both are read here, because a machine that has not updated in a
    year is exactly the machine with games on a second drive.
    """
    root = Path(root)
    found = [root]
    for path in (root / "steamapps" / "libraryfolders.vdf",
                 root / "config" / "libraryfolders.vdf"):
        if not path.is_file():
            continue
        try:
            tree = vdf.load_text(path)
        except (OSError, vdf.VdfError):
            continue
        block = tree.get("libraryfolders") or tree.get("LibraryFolders") or {}
        for key, value in block.items():
            if not key.isdigit():
                continue
            entry = value.get("path") if isinstance(value, dict) else value
            if not entry:
                continue
            candidate = Path(entry)
            if candidate not in found and candidate.is_dir():
                found.append(candidate)
    return found


def installed_games(root):
    """Every installed game on every library, newest install first.

    A library with no steamapps directory is skipped rather than reported: a
    drive can be unplugged and that is not an error worth a dialog.
    """
    games = {}
    for library in library_paths(root):
        steamapps = Path(library) / "steamapps"
        if not steamapps.is_dir():
            continue
        try:
            entries = os.listdir(str(steamapps))
        except OSError:
            continue
        for name in entries:
            match = APPMANIFEST.match(name)
            if not match:
                continue
            game = read_manifest(steamapps / name)
            if game is not None:
                games[game.appid] = game
    return sorted(games.values(), key=lambda game: game.name.lower())


def read_manifest(path):
    """One appmanifest_<id>.acf, or None when it is unreadable."""
    try:
        tree = vdf.load_text(path)
    except (OSError, vdf.VdfError):
        return None
    state = tree.get("AppState") or {}
    appid = state.get("appid") or state.get("AppID")
    name = state.get("name")
    if not appid or not str(appid).isdigit():
        return None
    return Game(
        appid=int(appid),
        name=name or ("App %s" % appid),
        library=Path(path).parent.parent,
        install_dir=state.get("installdir"),
        last_updated=_int(state.get("LastUpdated")),
        size_on_disk=_int(state.get("SizeOnDisk")),
    )


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def user_ids(root):
    """Every Steam3 account id with a userdata directory, newest login first."""
    userdata = Path(root) / "userdata"
    try:
        entries = [name for name in os.listdir(str(userdata)) if name.isdigit()]
    except OSError:
        return []
    entries = [name for name in entries if name != "0"]
    recent = most_recent_user(root)
    entries.sort(key=lambda name: (name != str(recent), name))
    return [int(name) for name in entries]


def most_recent_user(root):
    """The account id Steam last signed in as, as a Steam3 id, or None."""
    path = Path(root) / "config" / "loginusers.vdf"
    try:
        tree = vdf.load_text(path)
    except (OSError, vdf.VdfError):
        return None
    users = tree.get("users") or {}
    best = None
    for steam64, info in users.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("MostRecent")) == "1":
            best = steam64
            break
    if best is None:
        return None
    try:
        return int(best) - 76561197960265728
    except (TypeError, ValueError):
        return None


def persona_names(root):
    """Steam3 account id to display name, from loginusers.vdf."""
    path = Path(root) / "config" / "loginusers.vdf"
    try:
        tree = vdf.load_text(path)
    except (OSError, vdf.VdfError):
        return {}
    names = {}
    for steam64, info in (tree.get("users") or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            account = int(steam64) - 76561197960265728
        except (TypeError, ValueError):
            continue
        name = info.get("PersonaName") or info.get("AccountName")
        if name:
            names[account] = name
    return names


def config_dir(root, user_id):
    return Path(root) / "userdata" / str(user_id) / "config"


def localconfig_path(root, user_id):
    return config_dir(root, user_id) / "localconfig.vdf"


def shortcuts_path(root, user_id):
    return config_dir(root, user_id) / "shortcuts.vdf"


def appinfo_path(root):
    return Path(root) / "appcache" / "appinfo.vdf"
