"""savepick.json, the one config the engine already reads.

The GUI does not get a config of its own. Two files describing the same sync
would drift, and the one the engine reads is the one that decides behaviour, so
that is the file the GUI edits.

Every write is whole-file and atomic, and keeps a copy of what was there. The
file carries tree definitions that were built by hand over weeks; a partial
write would be expensive to notice and worse to recover.
"""

import importlib
import json
import os
import shutil
import sys
from pathlib import Path

from . import paths

# The keys the engine insists on before it will trust the syncthing block.
SYNC_REQUIRED = ("url", "apikey", "folder", "device_dir")

# What each kind of store cannot work without (slotstore.store_from_settings).
STORE_REQUIRED = {
    "s3": ("endpoint", "bucket", "access_key", "secret_key"),
    "ssh": ("host", "root"),
    "local": ("root",),
}

# Every field that belongs to one kind of store, so that switching kinds can
# drop the old kind's fields instead of leaving a stale key in the file.
STORE_FIELDS = {
    "s3": ("endpoint", "bucket", "region", "access_key", "secret_key"),
    "ssh": ("host", "user", "port", "root", "identity"),
    "local": ("root",),
}

# Written sealed with DPAPI on Windows. Elsewhere the file itself is 0600.
STORE_SECRETS = ("secret_key", "cf_client_secret")


def engine_module(name):
    """Import slotd or slotstore from the engine that ships with Blockslot.

    They are not a package: savepick imports them from its own folder, and the
    exe carries them under "engine". So the folder is found the same way the
    engine itself is found, the bundle first and then the source tree, with the
    installed copy beside savepick as the last resort.
    """
    if name in sys.modules:
        return sys.modules[name]
    found = paths.resource("engine/%s.py" % name)
    folder = found.parent if found else None
    if folder is None and (paths.bin_dir() / (name + ".py")).is_file():
        folder = paths.bin_dir()
    if folder is None:
        raise ImportError("BlockSlot cannot find engine/%s.py" % name)
    if str(folder) not in sys.path:
        sys.path.append(str(folder))
    return importlib.import_module(name)

DEFAULTS = {
    "syncthing": {
        "url": "http://127.0.0.1:8384",
        "apikey": "",
        "folder": "gamesaves",
        "hub_id": "",
        "hub_name": "",
        "device_dir": "",
        "device_names": {},
    },
    "trees": {},
}


class Settings(object):
    """savepick.json in memory, with the shape the engine expects."""

    def __init__(self, data=None, path=None):
        self.path = Path(path) if path else paths.config_path()
        self.data = data if data is not None else {}

    # ------------------------------------------------------------ loading

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else paths.config_path()
        try:
            with open(str(path), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        return cls(data, path)

    def save(self, path=None):
        target = Path(path) if path else self.path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if target.is_file():
            try:
                shutil.copy2(str(target), str(target) + ".blockslot.bak")
            except OSError:
                pass
        temp = str(target) + ".tmp"
        private = not paths.is_windows()
        if private:
            # The store section holds keys in plain text off Windows, so the
            # file is this user's alone, and so is the temporary copy it is
            # written through. Made private at creation, not after, so there
            # is no moment when another user could read it.
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            handle = os.fdopen(fd, "w", encoding="utf-8")
        else:
            handle = open(temp, "w", encoding="utf-8")
        with handle:
            json.dump(self.data, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            os.chmod(temp, 0o600)
        os.replace(temp, str(target))
        if private:
            backup = str(target) + ".blockslot.bak"
            if os.path.isfile(backup):
                try:
                    os.chmod(backup, 0o600)
                except OSError:
                    pass

    # ------------------------------------------------------------ syncthing

    @property
    def sync(self):
        block = self.data.get("syncthing")
        return block if isinstance(block, dict) else {}

    def set_sync(self, **values):
        block = dict(self.sync)
        for key, value in values.items():
            if value is None:
                block.pop(key, None)
            else:
                block[key] = value
        self.data["syncthing"] = block

    def sync_is_complete(self):
        block = self.sync
        return all(block.get(key) for key in SYNC_REQUIRED)

    def missing_sync_keys(self):
        block = self.sync
        return [key for key in SYNC_REQUIRED if not block.get(key)]

    def device_dir(self):
        return self.sync.get("device_dir") or ""

    def device_names(self):
        names = self.sync.get("device_names")
        return dict(names) if isinstance(names, dict) else {}

    def device_label(self, dirname):
        names = self.sync.get("device_names")
        if not isinstance(names, dict):
            return dirname
        return names.get(dirname, dirname)

    def set_device_name(self, dirname, label):
        names = self.device_names()
        if label:
            names[dirname] = label
        else:
            names.pop(dirname, None)
        self.set_sync(device_names=names)

    # ------------------------------------------------------------ store

    def store(self):
        """The "store" section as written: secrets still sealed."""
        block = self.data.get("store")
        return dict(block) if isinstance(block, dict) else {}

    def store_type(self):
        return (self.store().get("type") or "").lower()

    def set_store(self, **values):
        """Merge values into the store section. None or "" removes a key.

        A secret is sealed on the way in, so a plain key never reaches the
        file on Windows. One that is already sealed is left as it is.
        """
        block = self.store()
        for key, value in values.items():
            if value is None or value == "":
                block.pop(key, None)
                continue
            if key in STORE_SECRETS and not str(value).startswith("dpapi:"):
                # With the Windows service installed, LocalSystem must be able
                # to open it too, so it is sealed for the machine.
                value = engine_module("slotd").protect(
                    str(value), machine=bool(self.store().get("service")))
            block[key] = value
        self.data["store"] = block

    def clear_store(self):
        self.data.pop("store", None)

    def missing_store_keys(self):
        block = self.store()
        kind = self.store_type()
        if kind not in STORE_REQUIRED:
            return ["type"]
        return [key for key in STORE_REQUIRED[kind] if not block.get(key)]

    def store_is_complete(self):
        return not self.missing_store_keys()

    def has_secret(self, key):
        return bool(self.store().get(key))

    def store_for_engine(self):
        """The store section with its secrets opened, as slotstore wants it.

        Raises slotstore.StoreRefused when a secret was sealed for another
        Windows user or on another machine, which is a thing a person has to
        fix by typing the secret again.
        """
        block = self.store()
        slotd = engine_module("slotd")
        for key in STORE_SECRETS:
            if isinstance(block.get(key), str):
                block[key] = slotd.unprotect(block[key])
        return block

    def store_device(self):
        """This device's name on the store, as the daemon will use it."""
        import socket
        return self.store().get("device") or self.data.get("device") \
            or socket.gethostname()

    # ------------------------------------------------------------ trees

    @property
    def trees(self):
        block = self.data.get("trees")
        return block if isinstance(block, dict) else {}

    def tree_names(self):
        return sorted(self.trees)

    def tree(self, name):
        tree = self.trees.get(name)
        return tree if isinstance(tree, dict) else {}

    def set_tree(self, name, definition):
        trees = dict(self.trees)
        trees[name] = definition
        self.data["trees"] = trees

    def remove_tree(self, name):
        trees = dict(self.trees)
        trees.pop(name, None)
        self.data["trees"] = trees

    def add_tree(self, name, every_file=False, one_game=None, system=None, label=None):
        """A new emulator library, or one emulator game, with no folders yet.

        ValueError says why not. `one_game` makes it a single game (Bloodborne
        in shadPS4); otherwise the folder is split into one save per game.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("An emulator library or game needs a name.")
        if name in self.trees:
            raise ValueError("There is already one called %s." % name)
        definition = {"roots": {}}
        if one_game:
            definition.update({"one_game": one_game, "extensions": "*"})
            if system:
                definition["system"] = system
            if label:
                definition["label"] = label
        if every_file:
            # One game's own folder: a console save often has no extension at
            # all, and an allow list would refuse every file in it.
            definition["extensions"] = "*"
        self.set_tree(name, definition)
        return name

    def set_tree_root(self, name, folder):
        """Point a save set at its folder ON THIS DEVICE. Returns the folder."""
        here = self.device_dir()
        if not here:
            raise ValueError("Set this device's folder in the share first.")
        folder = (folder or "").strip().rstrip("/\\")
        if not folder:
            raise ValueError("No folder given.")
        tree = dict(self.tree(name))
        roots = dict(tree.get("roots") or {})
        roots[here] = folder
        tree["roots"] = roots
        self.set_tree(name, tree)
        return folder

    def tree_rows(self):
        """(name, folder on this device, devices that know it, every file)."""
        here = self.device_dir()
        rows = []
        for name in self.tree_names():
            tree = self.tree(name)
            roots = tree.get("roots") or {}
            rows.append((name, roots.get(here, ""), len(roots),
                         tree.get("extensions") == "*"))
        return rows

    # ------------------------------------------------------------ the rest

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value

    def fill_defaults(self):
        """Add only the keys that are missing. Never change one that is set."""
        for key, value in DEFAULTS.items():
            if key not in self.data:
                self.data[key] = json.loads(json.dumps(value))
            elif isinstance(value, dict) and isinstance(self.data[key], dict):
                for inner, default in value.items():
                    self.data[key].setdefault(inner, default)
        return self
