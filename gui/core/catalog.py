"""What is known about a game: does it save, and does Steam already sync it.

Two sources, and they disagree often enough to matter.

    index/games.json    built from the ludusavi manifest in CI. Knows where
                        saves live for 13,000 games. Its cloud field is
                        community data and can be out of date.
    appinfo.vdf         Steam's own cache on this machine. The authority on
                        Steam Cloud, and silent about save paths.

Steam wins on cloud. The manifest said Dark Souls III had no cloud support
while Steam's own cache said it did, and believing the manifest there would
have told a player to set up syncing they did not need.
"""

import json
import os
import time
from pathlib import Path

from . import appinfo

# Rebuilding the appid to cloud map takes a couple of seconds on a 10 MB
# appinfo.vdf, so the answer is cached until Steam rewrites the file.
CACHE_NAME = "cloud-cache.json"
CACHE_VERSION = 2

UNKNOWN = None


class Entry(object):
    """One game, as the index describes it."""

    __slots__ = ("name", "appid", "save_files", "cloud_stores", "registry")

    def __init__(self, name, appid, save_files, cloud_stores, registry):
        self.name = name
        self.appid = appid
        self.save_files = save_files or []
        self.cloud_stores = cloud_stores or []
        self.registry = bool(registry)

    @property
    def has_saves(self):
        return bool(self.save_files)

    @property
    def manifest_says_cloud(self):
        return any(store == "steam" for store in self.cloud_stores)


class Catalog(object):
    """The index, plus Steam's own cloud answer when it can be read."""

    # Steam's own type for things that are not games. None of them have saves
    # and all of them turn up in an install folder.
    NOT_GAMES = frozenset(("tool", "config", "music", "video", "series",
                           "hardware", "dlc", "driver"))

    def __init__(self, entries=None, steam_cloud=None, meta=None, types=None):
        self.by_appid = entries or {}
        self.steam_cloud = steam_cloud
        self.types = types or {}
        self.meta = meta or {}

    # ------------------------------------------------------------ loading

    @classmethod
    def from_index(cls, path):
        """Read index/games.json. A missing index is not fatal: the GUI can
        still list games and say it does not know what they save."""
        try:
            with open(str(path), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return cls()
        meta = data.get("_meta") or {}
        entries = {}
        for name, row in data.items():
            if name == "_meta" or not isinstance(row, dict):
                continue
            appid = row.get("s")
            if not appid:
                continue
            entry = Entry(name, int(appid), row.get("f"), row.get("c"), row.get("r"))
            previous = entries.get(entry.appid)
            # The manifest has a handful of duplicate ids. Keep the entry that
            # actually knows something.
            if previous is None or (not previous.has_saves and entry.has_saves):
                entries[entry.appid] = entry
        return cls(entries, meta=meta)

    def load_steam_cloud(self, appinfo_file, cache_file=None):
        """Read Steam's own answers, from cache when the file has not moved."""
        appinfo_file = Path(appinfo_file)
        try:
            stat = appinfo_file.stat()
        except OSError:
            return False
        signature = [CACHE_VERSION, int(stat.st_mtime), stat.st_size]
        if cache_file is not None:
            cached = _read_cache(cache_file, signature)
            if cached is not None:
                self.steam_cloud = set(cached.get("cloud") or ())
                self.types = {int(key): value for key, value
                              in (cached.get("types") or {}).items()}
                return True
        try:
            with open(str(appinfo_file), "rb") as handle:
                data = handle.read()
            found = appinfo.scan(data)
        except (OSError, ValueError):
            return False
        self.steam_cloud = found["cloud"]
        # Only the types worth filtering on are kept. Storing "game" for every
        # app would triple the cache for nothing.
        self.types = {appid: kind for appid, kind in found["types"].items()
                      if kind in self.NOT_GAMES}
        if cache_file is not None:
            _write_cache(cache_file, signature,
                         {"cloud": sorted(self.steam_cloud),
                          "types": {str(k): v for k, v in self.types.items()}})
        return True

    def is_game(self, appid):
        """False when Steam calls this a tool, a driver or a video."""
        return self.types.get(int(appid)) not in self.NOT_GAMES

    # ------------------------------------------------------------ asking

    def entry(self, appid):
        return self.by_appid.get(int(appid))

    def cloud(self, appid):
        """True, False, or None when nothing on this machine knows.

        None is a real answer and the UI shows it as one. Telling a player a
        game has no cloud support when the truth is unknown is how saves get
        lost twice.
        """
        appid = int(appid)
        if self.steam_cloud is not None:
            return appid in self.steam_cloud
        entry = self.entry(appid)
        if entry is None:
            return UNKNOWN
        return entry.manifest_says_cloud

    def saves(self, appid):
        """True when saves are known to exist, False when known not to, else None."""
        entry = self.entry(appid)
        if entry is None:
            return UNKNOWN
        return entry.has_saves

    def name(self, appid):
        entry = self.entry(appid)
        return entry.name if entry else None


def _read_cache(path, signature):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if data.get("signature") != signature:
        return None
    return data.get("steam") or None


def _write_cache(path, signature, steam):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = str(path) + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"signature": signature, "steam": steam,
                       "written": int(time.time())}, handle)
        os.replace(temp, str(path))
    except OSError:
        pass


def default_index_path(start=None):
    """Find index/games.json: inside the built exe, or in the source tree."""
    from . import paths
    found = paths.resource("index/games.json")
    if found:
        return found
    here = Path(start or __file__).resolve()
    for parent in list(here.parents)[:4]:
        candidate = parent / "index" / "games.json"
        if candidate.is_file():
            return candidate
    return None
