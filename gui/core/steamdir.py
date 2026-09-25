"""Where Steam is, who is signed in, and which games are installed.

Everything here is read-only and takes an explicit root, so a test can point it
at a directory tree it built itself. Nothing in this module needs Steam to be
installed, or running, or even to exist.
"""

import os
import re
import sys
from pathlib import Path

from . import vdf

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


def candidate_roots():
    """Every place Steam is normally installed on this operating system.

    Order matters: the first one that holds a userdata directory wins, so a
    flatpak install does not shadow a native one that is actually in use.
    """
    home = Path.home()
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
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]


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


def find_root(candidates=None):
    """The Steam directory in use, or None.

    A directory only counts when it holds userdata. An empty Steam folder left
    behind by an uninstall would otherwise be picked over the real one.
    """
    for path in (candidates if candidates is not None else candidate_roots()):
        path = Path(path)
        if (path / "userdata").is_dir():
            return path
    for path in (candidates if candidates is not None else candidate_roots()):
        path = Path(path)
        if (path / "steamapps").is_dir():
            return path
    return None


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
