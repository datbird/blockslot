"""slotd - the Blockslot daemon: one per device, owning the store and the queue.

The picker (savepick) talks to it for every save it sends or fetches. See
docs/superpowers/specs/2026-09-24-store-and-daemon-design.md.

    python slotd.py --serve        run the daemon
    python slotd.py --status       print what it is doing
    python slotd.py --import DIR [--dry-run]
                                   put a Syncthing gamesaves folder's ludusavi
                                   backups on the store

THREE WAYS TO REACH THE SAME WORK

`Daemon` does everything. `serve()` puts it behind HTTP on 127.0.0.1 for the
picker. `connect()` is what the picker calls: it returns an HTTP client when a
daemon answers, starts one when none does, and when that fails too, returns a
`Daemon` in the picker's own process. The queue and the lock are on disk, so a
daemon that starts later carries on from where the picker stopped.

Standard library only; Python 3.9.
"""

import base64
import datetime
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import slotstore as ss  # noqa: E402

BACKOFF_FIRST = 30.0
BACKOFF_MAX = 15 * 60.0
IDLE_TICK = 60.0
CLEAN_EVERY = 24 * 3600.0
CONFIG_EVERY = 5 * 60.0
CONNECT_TIMEOUT = 2.0
START_WAIT = 3.0
PAUSED_MESSAGE = ("uploads are paused on this device. Resume them from the "
                  "BlockSlot icon in the notification area.")


# ------------------------------------------------------------------ places


def default_state_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Blockslot", "store")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Blockslot/store")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "blockslot", "store")


def default_config_path():
    """The same savepick.json the picker reads (savepick.config_path)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "savepick.json")
    return os.path.join(os.path.expanduser("~"), ".config", "savepick.json")


def load_settings(path=None):
    """The "store" section and the device name, secrets decrypted."""
    path = path or default_config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None, None
    store = data.get("store")
    if not store:
        return None, None
    store = dict(store)
    for field in ("secret_key", "cf_client_secret"):
        if isinstance(store.get(field), str) and store[field].startswith("dpapi:"):
            store[field] = unprotect(store[field])
    # The Syncthing device directory is the name tree roots are keyed by, so
    # a device keeps it on the store unless it is given another.
    device = (store.get("device") or data.get("device")
              or (data.get("syncthing") or {}).get("device_dir") or socket.gethostname())
    return store, ss.device_key(device)


# ------------------------------------------------------------------ secrets


def protect(text, machine=False):
    """Encrypt a secret for this Windows user. Plain text elsewhere, where
    the settings file is mode 0600 instead.

    `machine` seals it for this PC instead of this user, so the Blockslot
    service (LocalSystem) can open it as well as the user can.
    """
    if sys.platform != "win32":
        return text
    blob = _dpapi(text.encode("utf-8"), encrypt=True, machine=machine)
    return "dpapi:" + base64.b64encode(blob).decode("ascii")


def unprotect(text):
    if not text.startswith("dpapi:"):
        return text
    if sys.platform != "win32":
        raise ss.StoreRefused("this secret was encrypted on Windows and cannot "
                              "be read here")
    return _dpapi(base64.b64decode(text[6:]), encrypt=False).decode("utf-8")


def _dpapi(data, encrypt, machine=False):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                         ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt32 = ctypes.windll.crypt32
    call = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    CRYPTPROTECT_LOCAL_MACHINE = 0x4
    if encrypt:
        ok = call(ctypes.byref(source), None, None, None, None,
                  CRYPTPROTECT_LOCAL_MACHINE if machine else 0, ctypes.byref(out))
    else:
        ok = call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise ss.StoreRefused("Windows could not %s the secret"
                              % ("encrypt" if encrypt else "decrypt"))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# ------------------------------------------------------------------ central settings
#
# The BlockSlot server's web UI keeps the settings every device shares on the
# store (docs/superpowers/specs/2026-09-25-server-design.md):
#
#     blockslot/v1/config/shared.json            libraries and emulator games
#     blockslot/v1/config/devices/<device>.json  one device's name and folders
#
# The daemon copies them into savepick.json, which is all the picker and the
# apps read, so none of them has to know the server exists.

CONFIG_PREFIX = ss.PREFIX + "config/"
SHARED_KEY = CONFIG_PREFIX + "shared.json"
# What shared.json decides for a library. Folders are per device, so "roots"
# comes from the device files instead.
CENTRAL_FIELDS = ("one_game", "system", "label", "extensions", "system_aliases",
                  "always_dirs")


def device_config_key(device):
    return "%sdevices/%s.json" % (CONFIG_PREFIX, ss.device_key(device))


def read_central(store):
    """(shared or None, {device: its file}). A file that is not JSON is
    skipped: one bad write must not wipe every device's folders."""
    def load(key):
        try:
            value = json.loads(store.get(key).decode("utf-8"))
        except ss.NotFound:
            return None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    shared = load(SHARED_KEY)
    devices = {}
    for key in store.list(CONFIG_PREFIX + "devices/"):
        if not key.endswith(".json"):
            continue
        doc = load(key)
        if doc and doc.get("device"):
            devices[ss.device_key(doc["device"])] = doc
    return shared, devices


def central_merge(data, shared, devices, device, now=None):
    """savepick.json with the central settings in it, and this device's own
    file when the store needs a new one.

    Returns (new data, own file or None). `data` is not changed.

    - shared.json, when there is one, decides which libraries exist and what
      each one is. A library it does not list is removed here.
    - Each device's file decides that device's folders and name.
    - This device's folders are set in two places: the web UI and this
      device's own app. "central.own" remembers what the two last agreed on,
      so whichever side changed since then wins, without trusting either
      clock. When both changed, the edit made on this device wins: it is the
      one that can see the disk.
    """
    data = json.loads(json.dumps(data))
    me = ss.device_key(device)
    trees = data.get("trees") or {}
    if shared is not None:
        libraries = shared.get("libraries") or {}
        merged = {}
        for name, spec in libraries.items():
            tree = dict(trees.get(name) or {})
            tree["roots"] = dict(tree.get("roots") or {})
            for field in CENTRAL_FIELDS:
                if field in (spec or {}):
                    tree[field] = spec[field]
                else:
                    tree.pop(field, None)
            merged[name] = tree
        trees = merged
    data["trees"] = trees
    block = data.get("syncthing")
    if not isinstance(block, dict):
        block = data["syncthing"] = {}
    names = dict(block.get("device_names") or {})

    central = dict(data.get("central") or {})
    agreed = central.get("own")
    local = {"roots": {name: tree["roots"][me] for name, tree in trees.items()
                       if me in (tree.get("roots") or {})},
             "name": names.get(me) or ""}
    own = devices.get(me)
    remote = None
    if own is not None:
        remote = {"roots": {name: path for name, path in (own.get("roots") or {}).items()
                            if name in trees},
                  "name": own.get("name") or ""}
    write = None
    ours = own is not None and own.get("set_by") == "device"
    if (remote is None or (agreed is None and ours and local != remote)
            or (agreed is not None and local != agreed and local != remote)):
        # First time on this store, changed on this device since the last
        # sync, or the store holds only this device's own earlier word (its
        # write here was never recorded, so nothing newer came from the web):
        # the store takes this device's word.
        chosen = local
        write = {"version": 1, "device": me, "name": local["name"] or me,
                 "roots": local["roots"], "updated": ss.iso(now), "set_by": "device"}
    else:
        chosen = remote
    central["own"] = chosen

    for dev, doc in sorted(devices.items()):
        if dev == me:
            continue
        roots = doc.get("roots") or {}
        for name, tree in trees.items():
            if name in roots:
                tree["roots"][dev] = roots[name]
            else:
                tree["roots"].pop(dev, None)
        if doc.get("name"):
            names[dev] = doc["name"]
    for name, tree in trees.items():
        if name in chosen["roots"]:
            tree["roots"][me] = chosen["roots"][name]
        else:
            tree["roots"].pop(me, None)
    if chosen["name"]:
        names[me] = chosen["name"]
    block["device_names"] = names
    if shared is not None:
        central["shared_updated"] = shared.get("updated")
    data["central"] = central
    return data, write


def write_config(path, data):
    """Replace savepick.json in one step, keeping its permissions."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    handle, temp = tempfile.mkstemp(prefix=".savepick-", suffix=".json", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2)
            out.write("\n")
        if sys.platform != "win32":
            os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ the work


def _describe(view, snap_id):
    manifest = view.manifests.get(snap_id) or {}
    played = manifest.get("played") or {}
    return {"id": snap_id, "device": manifest.get("device") or ss.snap_device(snap_id),
            "created": manifest.get("created"), "played_end": played.get("end"),
            "played_start": played.get("start"),
            "bytes": sum(r.get("size", 0) for r in manifest.get("files") or [])}


class Daemon(object):
    """Everything the picker and the UIs ask for, in one place."""

    def __init__(self, store, device, state_dir=None, store_label=None):
        self.store = store
        self.device = ss.device_key(device)
        self.state = ss.LocalState(state_dir or default_state_dir())
        self.store_label = store_label or getattr(store, "label", "the store")
        self.kick = threading.Event()
        self.lock = threading.Lock()
        self.progress = {}          # snap_id -> (done, total)
        self.last_error = None      # (kind, message, when)
        self.last_ok = None
        self.stopping = False
        # Set from the tray. Nothing uploads while it is on, not even the
        # picker's own wait: a person who paused uploads (a metered link, a
        # slow hotel network) meant all of them.
        self.paused = False
        self._backoff = BACKOFF_FIRST
        self.config_path = None     # savepick.json, for the central settings
        self._config_at = 0.0

    # -- reading

    def status(self):
        queued = []
        for snap_id in self.state.queued():
            manifest = self.state.queued_manifest(snap_id) or {}
            queued.append({"id": snap_id, "game": manifest.get("game"),
                           "bytes": sum(r.get("size", 0) for r in manifest.get("files") or []),
                           "progress": self.progress.get(snap_id)})
        error = None
        if self.last_error:
            error = {"kind": self.last_error[0], "message": self.last_error[1],
                     "at": self.last_error[2]}
        return {"device": self.device, "store": self.store_label, "queued": queued,
                "error": error, "last_ok": self.last_ok, "paused": self.paused}

    def decide(self, game, local_hashes):
        """The launch decision for this game, with what the picker needs to
        say it. Never raises: an unreachable store is UNKNOWN."""
        base = self.state.base(game)
        queued = self.state.queued(game)
        try:
            view = ss.read_game(self.store, game, cache_dir=self.state.cache_dir)
        except ss.StoreError as exc:
            self._note_error(exc)
            return {"action": ss.UNKNOWN, "reachable": False,
                    "error": str(exc), "base": base, "queued": queued}
        self._note_ok()
        if queued:
            # This device has saves the store has not seen. They are newer than
            # anything it knows of here, so the local save stands.
            action, detail = ss.LAUNCH, None
            if len(view.heads) > 1 or (view.heads and base and
                                       not view.descends_from(view.heads[-1], base)
                                       and view.heads[-1] != base):
                action, detail = ss.ASK, view.heads
        else:
            action, detail = ss.decide(view, base, set(local_hashes or []), self.device)
            pending = self.state.restore_pending(game)
            heads = view.heads
            if (pending and action == ss.LAUNCH and len(heads) == 1
                    and set(local_hashes or []) != ss.save_hashes(view.manifests[heads[0]])):
                # A choice made from the tray or the panel, not yet on this
                # device. Restore it now.
                action, detail = ss.RESTORE, heads[0]
        answer = {"action": action, "reachable": True, "base": base,
                  "queued": queued, "heads": [_describe(view, h) for h in view.heads]}
        if action == ss.RESTORE:
            answer["restore"] = _describe(view, detail)
        elif action == ss.ASK:
            answer["choices"] = [_describe(view, h) for h in detail]
        elif action == ss.WAIT:
            answer["pending"] = [dict(intent or {}, id=sid) for sid, intent in detail.items()]
        elif action == ss.LAUNCH and detail:
            answer["adopt"] = detail
        return answer

    def fetch(self, game, snap_id, into):
        """Lay a snapshot out in `into` as a ludusavi backup directory."""
        view = ss.read_game(self.store, game, cache_dir=self.state.cache_dir)
        manifest = view.manifests.get(snap_id)
        if manifest is None:
            raise ss.NotFound(snap_id)
        ss.fetch(self.store, manifest, into)
        return {"ok": True, "path": into}

    # -- writing

    def tree(self, game):
        """{device: manifest} of each device's newest tree of a save set."""
        view = ss.read_game(self.store, game, cache_dir=self.state.cache_dir)
        return {"devices": ss.newest_per_device(view), "me": self.device}

    def library(self, library, units):
        """Launch decisions for every game in an emulator library.

        units: {unit name: {"hashes": [...], "unit": unit id}} for the games
        on this device. Returns only the games that need something: a restore,
        a choice, or another device still uploading. A game whose save here is
        already the store's is adopted as the base on the spot.
        """
        views = ss.library_views(self.store, library, cache_dir=self.state.cache_dir)
        out = {}
        adopted = 0
        for unit_name in set(views) | set(units):
            local = set((units.get(unit_name) or {}).get("hashes") or [])
            view = views.get(unit_name)
            if view is None or self.state.queued(unit_name):
                continue
            action, detail = ss.decide(view, self.state.base(unit_name), local, self.device)
            if action == ss.LAUNCH:
                if detail:
                    self.state.set_base(unit_name, detail)
                    adopted += 1
                continue
            entry = {"action": action}
            if action == ss.RESTORE:
                head = view.manifests[detail]
                entry["restore"] = dict(_describe(view, detail), files=head["files"],
                                        unit=head.get("unit"))
            elif action == ss.ASK:
                entry["choices"] = [dict(_describe(view, h), unit=view.manifests[h].get("unit"),
                                         files=view.manifests[h]["files"]) for h in detail]
            elif action == ss.WAIT:
                entry["pending"] = sorted(detail)
            out[unit_name] = entry
        # Games here that the store has never seen: the picker uploads them.
        new_here = sorted(set(units) - set(views))
        return {"units": out, "store_units": len(views), "adopted": adopted,
                "new_here": new_here}

    def library_list(self, library):
        """Every game in a library as the UIs show it, newest save first.

        [{name, title, system, label, when, device, heads, base}] from one
        listing and the manifest cache. heads above 1 means two devices both
        played it and nobody has chosen yet; such a row also carries
        "choices": [{id, device, when}], one per head, so a UI can offer the
        choice and pass the id straight to choose().
        """
        views = ss.library_views(self.store, library, cache_dir=self.state.cache_dir)
        rows = []
        for unit_name, view in views.items():
            head = view.newest_head()
            if head is None:
                continue
            manifest = view.manifests[head]
            unit = manifest.get("unit") or {}
            played = manifest.get("played") or {}
            rows.append({"name": unit_name, "title": unit.get("title") or unit_name,
                         "system": unit.get("system") or "",
                         # Snapshots made before content folders had a label.
                         "label": unit.get("label") or ("RetroArch" if not unit.get("system") else ""),
                         "when": played.get("end") or manifest.get("created"),
                         "device": manifest.get("device"), "heads": len(view.heads),
                         "base": self.state.base(unit_name)})
            if len(view.heads) > 1:
                # What a UI needs to offer the choice itself, without a
                # second read of the game per fork.
                rows[-1]["choices"] = [
                    {"id": h, "device": d["device"],
                     "when": d["played_end"] or d["created"]}
                    for h, d in ((h, _describe(view, h)) for h in view.heads)]
        rows.sort(key=lambda row: row.get("when") or "", reverse=True)
        return {"library": library, "games": rows}

    def fetch_blobs(self, items):
        written, failed = ss.fetch_blobs(self.store, items)
        return {"written": written, "failed": failed}

    def stage(self, game, source, played=None, mode="game", unit=None):
        manifest = self.state.stage(game, self.device, source, played=played, mode=mode,
                                    unit=unit)
        self.kick.set()
        return {"snap": manifest["id"], "parents": manifest["parents"]}

    def set_base(self, game, snap_id, merge=None):
        """Record what this device runs on. `merge` names heads the player
        chose against; the next save names them as parents, closing the fork."""
        self.state.set_base(game, snap_id, merge=merge)
        self.state.set_restore_pending(game, None)
        return {"ok": True}

    def choose(self, game, snap_id):
        """A person picked one head of a fork. Record it on the store with no
        upload: a merge snapshot of the chosen files, naming every head."""
        view = ss.read_game(self.store, game, cache_dir=self.state.cache_dir)
        heads = view.heads
        if snap_id not in view.manifests:
            raise ss.NotFound(snap_id)
        self.state.set_base(game, snap_id,
                            merge=[h for h in heads if h != snap_id])
        # Chosen from the tray or the Deck panel, the save on this device may
        # still be the other one. The next launch restores the choice here;
        # the picker clears this itself when it restores straight away.
        self.state.set_restore_pending(game, snap_id)
        if len(heads) > 1:
            manifest = self.state.stage_merge(game, self.device,
                                              view.manifests[snap_id], heads)
            self.kick.set()
            return {"ok": True, "merge": manifest["id"]}
        return {"ok": True}

    def upload_now(self, only=None):
        """Drain the queue once, in this thread. Returns (committed, error).

        Paused is not an error: nothing is wrong with the store, so the tray
        must not show a fault for it.
        """
        if self.paused:
            return [], None
        with self.lock:
            committed, error = ss.drain(
                self.store, self.state, only=only,
                progress=lambda sid, done, total: self.progress.__setitem__(sid, (done, total)))
        for snap_id in committed:
            self.progress.pop(snap_id, None)
        if error is not None:
            self._note_error(error)
        elif committed:
            self._note_ok()
        return committed, error

    def wait(self, snap_id, timeout):
        """Upload until this snapshot is on the store, or say why not.

        committed | offline | refused | uploading (timeout, still going)
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if snap_id not in self.state.queued():
                return {"state": "committed"}
            if self.paused:
                # Said at once, as a refusal with its reason, so the picker
                # tells the player the save stayed queued instead of spinning.
                return {"state": "refused", "paused": True,
                        "message": PAUSED_MESSAGE}
            committed, error = self.upload_now(only=snap_id)
            if snap_id in committed or snap_id not in self.state.queued():
                return {"state": "committed"}
            if isinstance(error, ss.StoreRefused):
                return {"state": "refused", "message": str(error)}
            if isinstance(error, ss.StoreOffline):
                return {"state": "offline", "message": str(error)}
            if error is not None:
                return {"state": "refused", "message": str(error)}
            if time.monotonic() >= deadline:
                done, total = self.progress.get(snap_id, (0, 0))
                return {"state": "uploading", "done": done, "total": total}
            # Another process holds the upload lock. Give it a moment.
            time.sleep(0.5)

    # -- the background loop

    def _note_error(self, exc):
        kind = "refused" if isinstance(exc, ss.StoreRefused) else "offline"
        self.last_error = (kind, str(exc), ss.iso())

    def _note_ok(self):
        self.last_error = None
        self.last_ok = ss.iso()
        self._backoff = BACKOFF_FIRST

    keeper = False
    server = None

    def shutdown(self):
        """Stop serving and uploading. Safe from any thread, including a
        request handler, because the server stops on a thread of its own."""
        self.stopping = True
        self.kick.set()
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        return {"ok": True}

    def sync_config(self, now=None, force=False):
        """Bring the central settings into savepick.json, every CONFIG_EVERY.

        Returns True when savepick.json changed. Only a daemon that knows its
        savepick.json does this; one the picker runs in its own process for a
        single launch does not.
        """
        if not self.config_path:
            return False
        now = now or time.time()
        if not force and now - self._config_at < CONFIG_EVERY:
            return False
        self._config_at = now
        try:
            shared, devices = read_central(self.store)
        except ss.StoreError as exc:
            self._note_error(exc)
            return False
        try:
            with open(self.config_path, "rb") as handle:
                before = handle.read()
            data = json.loads(before.decode("utf-8"))
        except (OSError, ValueError):
            return False
        merged, own = central_merge(data, shared, devices, self.device)
        if own is not None:
            try:
                self.store.put(device_config_key(self.device),
                               json.dumps(own, indent=1).encode("utf-8"))
            except ss.StoreError as exc:
                # Not agreed yet: keep the old "central.own" so the next try
                # still sees this device's change as unsent.
                self._note_error(exc)
                merged["central"]["own"] = (data.get("central") or {}).get("own")
        if merged == data:
            return False
        # The app, the tray or a person may have saved the file while the
        # store was being read. Writing now would put their change back to
        # what it was, silently. Leave it, and merge their version on the next
        # pass instead. (What is left is the instant between this read and the
        # replace below, not the seconds a store round trip takes.)
        try:
            with open(self.config_path, "rb") as handle:
                if handle.read() != before:
                    self._config_at = 0.0
                    return False
            write_config(self.config_path, merged)
        except OSError:
            return False
        return True

    def maybe_clean(self, now=None):
        """Clean the store once a day, on the one device set as keeper.

        One device only, so two clean-ups never race. It rides the uploader's
        own loop after the queue is empty; there is no separate schedule.
        """
        if not self.keeper or self.paused:
            return None
        stamp_path = os.path.join(self.state.root, "last_clean")
        now = now or time.time()
        try:
            if now - os.path.getmtime(stamp_path) < CLEAN_EVERY:
                return None
        except OSError:
            pass
        try:
            result = ss.clean(self.store)
        except ss.StoreError as exc:
            self._note_error(exc)
            return None
        if not result.get("skipped"):
            os.makedirs(self.state.root, exist_ok=True)
            with open(stamp_path, "w") as handle:
                handle.write(ss.iso())
        return result

    def run_uploader(self):
        """Retry the queue until it is empty, backing off while it cannot.

        No schedule: with an empty queue this sleeps until kicked, waking once
        a minute only to notice a jump in the wall clock, which is how a wake
        from sleep shows up.
        """
        wall = time.time()
        mono = time.monotonic()
        while not self.stopping:
            if self.state.queued() and not self.paused:
                committed, error = self.upload_now()
                if error is not None:
                    self.kick.wait(self._backoff)
                    self._backoff = min(self._backoff * 2, BACKOFF_MAX)
                    self.kick.clear()
                    continue
            self.maybe_clean()
            self.sync_config()
            self.kick.wait(IDLE_TICK)
            self.kick.clear()
            now_wall, now_mono = time.time(), time.monotonic()
            if abs((now_wall - wall) - (now_mono - mono)) > 30:
                # Slept. The network may be different now; try at once.
                self._backoff = BACKOFF_FIRST
            wall, mono = now_wall, now_mono


# ------------------------------------------------------------------ HTTP


class _Handler(http.server.BaseHTTPRequestHandler):
    daemon_obj = None
    token = None

    def log_message(self, *_args):
        pass

    def _reply(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        return secrets.compare_digest(self.headers.get("X-Blockslot-Token", ""),
                                      self.token or "")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _dispatch(self, method):
        if not self._authorised():
            return self._reply(403, {"error": "bad token"})
        parsed = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        d = self.daemon_obj
        try:
            body = self._body() if method == "POST" else {}
            route = (method, parsed.path)
            if route == ("GET", "/status"):
                return self._reply(200, d.status())
            if route == ("POST", "/decide"):
                return self._reply(200, d.decide(body["game"], body.get("hashes") or []))
            if route == ("POST", "/tree"):
                return self._reply(200, d.tree(body["game"]))
            if route == ("POST", "/library_list"):
                return self._reply(200, d.library_list(body["library"]))
            if route == ("POST", "/library"):
                return self._reply(200, d.library(body["library"], body.get("units") or {}))
            if route == ("POST", "/fetch_blobs"):
                return self._reply(200, d.fetch_blobs(body["items"]))
            if route == ("POST", "/fetch"):
                return self._reply(200, d.fetch(body["game"], body["snap"], body["into"]))
            if route == ("POST", "/stage"):
                return self._reply(200, d.stage(body["game"], body["source"],
                                                played=body.get("played"),
                                                mode=body.get("mode") or "game",
                                                unit=body.get("unit")))
            if route == ("GET", "/wait"):
                return self._reply(200, d.wait(query["snap"],
                                               float(query.get("timeout") or 0)))
            if route == ("POST", "/base"):
                return self._reply(200, d.set_base(body["game"], body.get("snap"),
                                                   merge=body.get("merge")))
            if route == ("POST", "/choose"):
                return self._reply(200, d.choose(body["game"], body["snap"]))
            if route == ("POST", "/kick"):
                d.kick.set()
                return self._reply(200, {"ok": True})
            if route == ("POST", "/pause"):
                d.paused = bool(body.get("paused"))
                if not d.paused:
                    d.kick.set()
                return self._reply(200, {"paused": d.paused})
            if route == ("POST", "/stop"):
                # Settings changed: the host starts a new daemon after this.
                return self._reply(200, d.shutdown())
            return self._reply(404, {"error": "no such call"})
        except ss.NotFound as exc:
            return self._reply(404, {"error": "not found: %s" % exc})
        except ss.StoreRefused as exc:
            return self._reply(502, {"error": str(exc), "kind": "refused"})
        except ss.StoreError as exc:
            return self._reply(503, {"error": str(exc), "kind": "offline"})
        except (KeyError, ValueError) as exc:
            return self._reply(400, {"error": "bad request: %s" % exc})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def serve(daemon_obj, state_dir=None, port=0):
    """Run the HTTP face and the uploader. Blocks until stopped."""
    state_dir = state_dir or daemon_obj.state.root
    os.makedirs(state_dir, exist_ok=True)
    token = secrets.token_hex(24)
    handler = type("Handler", (_Handler,), {"daemon_obj": daemon_obj, "token": token})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    # A host (the tray, the Decky backend) stops the daemon through this.
    daemon_obj.server = server
    info = {"port": server.server_address[1], "token": token, "pid": os.getpid()}
    info_path = os.path.join(state_dir, "daemon.json")
    ss._write_json_atomic(info_path, info)
    if sys.platform != "win32":
        os.chmod(info_path, 0o600)
    uploader = threading.Thread(target=daemon_obj.run_uploader, daemon=True)
    uploader.start()
    try:
        # shutdown() sets stopping, then looks for the server; this sets the
        # server (above), then looks at stopping. So a stop that came before
        # the server existed is seen here instead of being lost, and the
        # daemon never serves on after its host asked it to go.
        if not daemon_obj.stopping:
            server.serve_forever(poll_interval=0.5)
    finally:
        daemon_obj.stopping = True
        daemon_obj.kick.set()
        server.server_close()
        # Read, close, then delete: Windows refuses to delete an open file,
        # and a stale daemon.json would send the picker to a dead port.
        try:
            with open(info_path, "r", encoding="utf-8") as handle:
                mine = json.load(handle).get("pid") == os.getpid()
            if mine:
                os.unlink(info_path)
        except (OSError, ValueError):
            pass
    return server


# ------------------------------------------------------------------ the client


class DaemonUnavailable(Exception):
    pass


class Client(object):
    """The picker's side of the HTTP face. Same calls as Daemon."""

    def __init__(self, port, token, timeout=CONNECT_TIMEOUT):
        self.base = "http://127.0.0.1:%d" % port
        self.token = token
        self.timeout = timeout
        self.store_label = None

    def _call(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"X-Blockslot-Token": self.token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}")
            except ValueError:
                detail = {}
            message = detail.get("error") or "HTTP %d" % exc.code
            if exc.code == 404:
                raise ss.NotFound(message)
            if detail.get("kind") == "refused":
                raise ss.StoreRefused(message)
            if detail.get("kind") == "offline":
                raise ss.StoreOffline(message)
            raise DaemonUnavailable(message)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DaemonUnavailable(str(exc))

    def status(self):
        answer = self._call("GET", "/status")
        self.store_label = answer.get("store")
        return answer

    def decide(self, game, local_hashes):
        return self._call("POST", "/decide", {"game": game, "hashes": sorted(local_hashes or [])},
                          timeout=60)

    def tree(self, game):
        return self._call("POST", "/tree", {"game": game}, timeout=120)

    def kick(self):
        return self._call("POST", "/kick", {})

    def stop(self):
        return self._call("POST", "/stop", {})

    def pause(self, paused):
        return self._call("POST", "/pause", {"paused": bool(paused)})

    def library_list(self, library):
        return self._call("POST", "/library_list", {"library": library}, timeout=300)

    def library(self, library, units):
        return self._call("POST", "/library", {"library": library, "units": units},
                          timeout=600)

    def fetch_blobs(self, items):
        return self._call("POST", "/fetch_blobs", {"items": items}, timeout=900)

    def fetch(self, game, snap_id, into):
        return self._call("POST", "/fetch", {"game": game, "snap": snap_id, "into": into},
                          timeout=600)

    def stage(self, game, source, played=None, mode="game", unit=None):
        return self._call("POST", "/stage", {"game": game, "source": source,
                                             "played": played, "mode": mode,
                                             "unit": unit}, timeout=300)

    def wait(self, snap_id, timeout):
        return self._call("GET", "/wait?snap=%s&timeout=%s"
                          % (urllib.parse.quote(snap_id), timeout),
                          timeout=float(timeout) + 30)

    def set_base(self, game, snap_id, merge=None):
        return self._call("POST", "/base", {"game": game, "snap": snap_id,
                                            "merge": merge or []})

    def choose(self, game, snap_id):
        return self._call("POST", "/choose", {"game": game, "snap": snap_id}, timeout=60)


def daemon_from_settings(settings, device, state_dir=None, config_path=None):
    """The one way every host builds a Daemon, so none forgets a setting.

    `config_path` is the savepick.json the settings came from. With it, the
    daemon keeps that file in step with the central settings on the store.
    """
    store = ss.store_from_settings(settings)
    daemon_obj = Daemon(store, device,
                        state_dir=state_dir or settings.get("state_dir") or default_state_dir(),
                        store_label=ss.store_name(settings))
    daemon_obj.keeper = bool(settings.get("keeper"))
    daemon_obj.config_path = config_path
    return daemon_obj


def _client_from_info(state_dir):
    try:
        with open(os.path.join(state_dir, "daemon.json"), "r", encoding="utf-8") as handle:
            info = json.load(handle)
        client = Client(int(info["port"]), info["token"])
        client.status()
        return client
    except (OSError, ValueError, KeyError, DaemonUnavailable):
        return None


def start_detached(config_path=None):
    """Start `slotd.py --serve` with no window and no tie to this process."""
    if getattr(sys, "frozen", False):
        # Inside Blockslot.exe: the exe is the daemon host, with its tray.
        argv = [sys.executable, "--daemon"]
    else:
        argv = [_python_for_daemon(), os.path.join(HERE, "slotd.py"), "--serve"]
    if config_path:
        argv += ["--config", config_path]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
        return True
    except OSError:
        return False


def _python_for_daemon():
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("python.exe"):
        quiet = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(quiet):
            return quiet
    return exe


def connect(settings=None, device=None, state_dir=None, config_path=None,
            start=True, log=None):
    """What the picker calls. Returns (worker, how) or (None, reason).

    worker has the Daemon methods. how is "daemon", "started" or "local".
    """
    say = log or (lambda _msg: None)
    if settings is None:
        settings, device = load_settings(config_path)
    if not settings:
        return None, "no store is set up"
    state_dir = state_dir or settings.get("state_dir") or default_state_dir()
    client = _client_from_info(state_dir)
    if client:
        return client, "daemon"
    # With the Windows service installed, the service is the daemon. A user
    # daemon started here would hold the shared queue and keep the service
    # waiting, so the picker works in its own process instead.
    if start and not settings.get("service") and start_detached(config_path):
        deadline = time.monotonic() + START_WAIT
        while time.monotonic() < deadline:
            time.sleep(0.2)
            client = _client_from_info(state_dir)
            if client:
                return client, "started"
        say("store: the daemon did not start in %.0fs; working in this process"
            % START_WAIT)
    try:
        return daemon_from_settings(settings, device, state_dir), "local"
    except (ss.StoreError, KeyError) as exc:
        return None, "the store settings are wrong: %s" % exc


# ------------------------------------------------------------------ main


def main(argv):
    config = None
    if "--config" in argv:
        config = argv[argv.index("--config") + 1]
    settings, device = load_settings(config)
    if not settings:
        print("no store section in %s" % (config or default_config_path()))
        return 2
    state_dir = settings.get("state_dir") or default_state_dir()
    if "--status" in argv:
        client = _client_from_info(state_dir)
        if not client:
            print("the daemon is not running")
            return 1
        print(json.dumps(client.status(), indent=1))
        return 0
    if "--serve" in argv:
        if _client_from_info(state_dir):
            # One daemon per device. A second start is a no-op, not an error.
            return 0
        serve(daemon_from_settings(settings, device, state_dir,
                                   config or default_config_path()), state_dir)
        return 0
    if "--import" in argv:
        root = argv[argv.index("--import") + 1]
        dry = "--dry-run" in argv
        store = ss.store_from_settings(settings)

        def show(done, total, game, device):
            print("%d/%d  %s  %s" % (done, total, device, game))

        state = ss.LocalState(settings.get("state_dir") or default_state_dir())
        known = state.known_blobs()
        try:
            heads = ss.import_backups(store, root, dry_run=dry, progress=show, known=known)
        finally:
            if not dry:
                state.save_known_blobs(known)
        print("\n%s %d games:" % ("would import" if dry else "imported", len(heads)))
        for game, head in sorted(heads.items()):
            print("  %s  %s" % (head, game))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
