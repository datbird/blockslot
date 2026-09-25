"""saves - what the web UI reads from and writes to the store.

Every game on the store and its history, restoring an older save, settling
two saves, a snapshot as a zip, the central settings documents and the
clean-up. All of it goes through engine/slotstore.py, so the server writes
exactly the objects a device would write.

RESTORE WITHOUT AN UPLOAD

Restoring an older save and settling two saves both write one new merge
snapshot: the chosen snapshot's files, with every current head as a parent.
It becomes the only head, so each device restores it at its next launch
through the ordinary lineage rule (the head descends from the device's base).
The blobs are already on the store, so nothing but a small manifest is
written; slotstore.commit proves each blob is still there first.
"""

import concurrent.futures
import gzip
import json
import os
import re
import threading
import time
import zipfile

from . import ENGINE_DIR  # noqa: F401  (puts slotstore on the path)
import saveunits
import slotstore as ss

CONFIG = ss.PREFIX + "config/"
SHARED_KEY = CONFIG + "shared.json"
DEVICES_CONFIG = CONFIG + "devices/"

# The device name the server writes its own merge snapshots under. It is not
# a device anyone plays on, so the device list leaves it out.
SERVER_DEVICE = "server"

GAME_DIR = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
SNAP_ID = re.compile(r"^\d{8}T\d{6}Z_[a-z0-9-]+_[0-9a-f]{8}$")

DEFAULT_RETENTION = {"per_device": ss.KEEP_PER_DEVICE, "days": ss.KEEP_DAYS}


class SavesError(Exception):
    """A request the store cannot do, in words a person can act on."""


# ------------------------------------------------------------------ catalog


class Catalog(object):
    """Every game on the store, from one listing plus cached manifests.

    A manifest never changes once written, so each is read from the store
    once and kept on disk; after the first visit a listing costs one S3
    LIST and nothing else. The listing itself is reused for LISTING_TTL
    seconds so moving between screens does not relist 5,000 keys.
    """

    LISTING_TTL = 30

    def __init__(self, store, cache_dir=None, workers=16, now=time.time):
        self.store = store
        self.cache_dir = cache_dir
        self.workers = workers
        self.now = now
        self.lock = threading.Lock()
        self.manifests = {}
        self._listing = None
        self._listed = 0

    def invalidate(self):
        with self.lock:
            self._listing = None

    def listing(self, refresh=False):
        """[(key, when)] of everything under games/."""
        with self.lock:
            if refresh or self._listing is None or self.now() - self._listed > self.LISTING_TTL:
                self._listing = self.store.list_times(ss.PREFIX + "games/")
                self._listed = self.now()
            return list(self._listing)

    def _manifest(self, snap_id, key):
        found = self.manifests.get(snap_id)
        if found is not None:
            return found
        path = os.path.join(self.cache_dir, snap_id + ".json") if self.cache_dir else None
        manifest = None
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as handle:
                    manifest = json.loads(handle.read())
            except (OSError, ValueError):
                manifest = None
        if manifest is None:
            data = self.store.get(key)
            manifest = json.loads(data)
            if path:
                try:
                    os.makedirs(self.cache_dir, exist_ok=True)
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as handle:
                        handle.write(data)
                    os.replace(tmp, path)
                except OSError:
                    pass
        self.manifests[snap_id] = manifest
        return manifest

    def _layout(self, refresh=False, only=None):
        """{game dir: {"snapshots": {id: key}, "pending": {id: key}}}."""
        games = {}
        for key, _when in self.listing(refresh):
            parts = key.split("/")
            if len(parts) != 6 or not parts[5].endswith(".json"):
                continue
            game_dir, kind, snap_id = parts[3], parts[4], parts[5][:-5]
            if only is not None and game_dir != only:
                continue
            entry = games.setdefault(game_dir, {"snapshots": {}, "pending": {}})
            if kind == "snapshots":
                entry["snapshots"][snap_id] = key
            elif kind == "pending":
                entry["pending"][snap_id] = key
        return games

    def _load(self, layout):
        jobs = [(sid, key) for entry in layout.values()
                for sid, key in entry["snapshots"].items() if sid not in self.manifests]
        if jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(lambda job: self._manifest(*job), jobs))

    def view(self, game_dir, refresh=False):
        """The slotstore.GameView of one game, or None when it has no snapshots."""
        if not GAME_DIR.match(game_dir or ""):
            return None
        layout = self._layout(refresh, only=game_dir)
        entry = layout.get(game_dir)
        if not entry or not entry["snapshots"]:
            return None
        self._load(layout)
        manifests = {sid: self.manifests[sid] for sid in entry["snapshots"]}
        pending = {sid: None for sid in entry["pending"] if sid not in manifests}
        return ss.GameView(game_dir, manifests, pending)

    def games(self, refresh=False):
        """Every game as the games list shows it, newest save first."""
        layout = self._layout(refresh)
        self._load(layout)
        rows = []
        for game_dir, entry in layout.items():
            if not entry["snapshots"]:
                continue
            manifests = {sid: self.manifests[sid] for sid in entry["snapshots"]}
            view = ss.GameView(game_dir, manifests, {})
            head = view.newest_head()
            if head is None:
                continue
            row = describe_game(game_dir, manifests[head])
            played = manifests[head].get("played") or {}
            pending = [sid for sid in entry["pending"] if sid not in manifests]
            row.update({
                "when": played.get("end") or manifests[head].get("created"),
                "device": manifests[head].get("device") or ss.snap_device(head),
                "heads": len(view.heads), "snapshots": len(manifests),
                "uploading": len(pending)})
            rows.append(row)
        rows.sort(key=lambda row: row.get("when") or "", reverse=True)
        return rows


def describe_game(game_dir, manifest):
    """Title, library and emulator label of a game, from one of its manifests."""
    unit = manifest.get("unit") or {}
    is_library = game_dir.startswith(ss.LIBRARY_PREFIX)
    if is_library:
        system = unit.get("system") or ""
        library = unit.get("library") or game_dir[len(ss.LIBRARY_PREFIX):].split("--", 1)[0]
        return {"key": game_dir, "title": unit.get("title") or game_dir,
                "library": library, "system": system,
                "label": unit.get("label") or saveunits.label_for(system)}
    return {"key": game_dir, "title": manifest.get("game") or game_dir,
            "library": None, "system": "", "label": None}


def history(view):
    """Every snapshot of a game, newest first, as the game page shows it."""
    heads = set(view.heads)
    rows = []
    for snap_id, manifest in view.manifests.items():
        played = manifest.get("played") or {}
        rows.append({
            "id": snap_id,
            "device": manifest.get("device") or ss.snap_device(snap_id),
            "created": manifest.get("created"),
            "played_start": played.get("start"), "played_end": played.get("end"),
            "bytes": sum(r.get("size", 0) for r in manifest.get("files") or []),
            "files": len(manifest.get("files") or []),
            "head": snap_id in heads,
            "merge": bool(manifest.get("merge_only")),
            "restored_from": manifest.get("restored_from"),
            "imported": bool(manifest.get("imported")),
            "parents": manifest.get("parents") or []})
    # Two saves made in the same second have ids that sort by hash; the
    # number of ancestors puts a child after its parent whatever its name.
    depth = {sid: len(view.ancestors(sid)) for sid in view.manifests}
    rows.sort(key=lambda row: (row["created"] or "", depth[row["id"]], row["id"]), reverse=True)
    return rows


# ------------------------------------------------------------------ restore


def restore(store, view, snap_id, settle=False, now=None):
    """Make snap_id's files the game's save again, with no upload.

    settle=True is "Settle two saves": snap_id must be one of the heads of a
    game that has more than one. Returns the new manifest.
    """
    if view is None:
        raise SavesError("That game is not on the store.")
    chosen = view.manifests.get(snap_id)
    if chosen is None:
        raise SavesError("That save is not on the store any more.")
    heads = view.heads
    if settle:
        if len(heads) < 2:
            raise SavesError("This game has one save; there is nothing to settle.")
        if snap_id not in heads:
            raise SavesError("Pick one of the two newest saves.")
    elif heads == [snap_id]:
        raise SavesError("That is already the current save.")
    if ss.game_key(chosen["game"]) != view.game:
        raise SavesError("That save names a different game; it cannot be restored here.")
    manifest = ss.make_manifest(chosen["game"], SERVER_DEVICE, list(chosen["files"]),
                                heads, played=chosen.get("played"),
                                mode=chosen.get("mode") or "game", created=now)
    manifest["merge_only"] = True
    manifest["restored_from"] = snap_id
    if chosen.get("unit"):
        manifest["unit"] = chosen["unit"]
    ss.commit(store, manifest, "")
    return manifest


# ------------------------------------------------------------------ zip


def zip_name(view, snap_id):
    return "%s_%s.zip" % (view.game, snap_id)


def write_zip(store, manifest, out):
    """A snapshot's files as a zip, each proven by its hash on the way."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for record in manifest.get("files") or []:
            data = gzip.decompress(store.get(ss.blob_key(record["sha256"])))
            if ss.sha256_bytes(data) != record["sha256"]:
                raise SavesError("The store's copy of %s is damaged." % record["path"])
            when = ss.parse_iso(record.get("mtime"))
            stamp = when.timetuple()[:6] if when and when.year >= 1980 else (1980, 1, 1, 0, 0, 0)
            info = zipfile.ZipInfo(record["path"].lstrip("/"), date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return out


# ------------------------------------------------------------------ settings documents


def _read_json(store, key):
    try:
        return json.loads(store.get(key))
    except ss.NotFound:
        return None
    except ValueError:
        raise SavesError("%s on the store is not valid JSON." % key)


def _put_json(store, key, doc):
    store.put(key, json.dumps(doc, sort_keys=True, indent=1).encode("utf-8"))


def _str_list(value, what):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SavesError("%s must be a list of names." % what)
    return [v.strip() for v in value if v.strip()]


def clean_library(name, data):
    """One library entry, checked, with empty fields left out."""
    if not isinstance(data, dict):
        raise SavesError("Library %s is not an object." % name)
    out = {}
    for field in ("one_game", "system", "label"):
        value = data.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise SavesError("%s of %s must be text." % (field, name))
        out[field] = value.strip()
    extensions = data.get("extensions")
    if extensions == "*":
        out["extensions"] = "*"
    elif extensions not in (None, [], ""):
        out["extensions"] = [e.lower().lstrip(".") for e in _str_list(extensions, "extensions")]
    aliases = data.get("system_aliases")
    if aliases:
        if not isinstance(aliases, list):
            raise SavesError("system_aliases of %s must be a list of groups." % name)
        groups = [_str_list(group, "each alias group") for group in aliases]
        out["system_aliases"] = [g for g in groups if len(g) >= 2]
        if not out["system_aliases"]:
            del out["system_aliases"]
    always = data.get("always_dirs")
    if always:
        out["always_dirs"] = _str_list(always, "always_dirs")
    return out


def clean_shared(doc):
    """shared.json as the server writes it, or SavesError saying what is wrong."""
    if not isinstance(doc, dict):
        raise SavesError("The settings must be an object.")
    libraries = doc.get("libraries")
    libraries = {} if libraries is None else libraries
    if not isinstance(libraries, dict):
        raise SavesError("libraries must be an object.")
    out_libs = {}
    for name, data in libraries.items():
        name = (name or "").strip()
        if not name or len(name) > 80 or "/" in name:
            raise SavesError("A library needs a name of up to 80 characters, with no slash.")
        out_libs[name] = clean_library(name, data)
    retention = dict(DEFAULT_RETENTION)
    given = doc.get("retention") or {}
    if not isinstance(given, dict):
        raise SavesError("retention must be an object.")
    for field, low, high in (("per_device", 1, 1000), ("days", 0, 3650)):
        if field in given:
            value = given[field]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise SavesError("retention %s must be a whole number from %d to %d."
                                 % (field, low, high))
            retention[field] = value
    return {"version": 1, "updated": ss.iso(), "libraries": out_libs, "retention": retention}


def read_shared(store):
    return _read_json(store, SHARED_KEY)


def write_shared(store, doc):
    cleaned = clean_shared(doc)
    _put_json(store, SHARED_KEY, cleaned)
    return cleaned


def device_doc_key(device):
    return "%s%s.json" % (DEVICES_CONFIG, ss.device_key(device))


def read_device(store, device):
    return _read_json(store, device_doc_key(device))


def write_device(store, device, name=None, roots=None, set_by="web"):
    device = ss.device_key(device)
    if roots is not None and (not isinstance(roots, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in roots.items())):
        raise SavesError("Save folders must map a library name to a path.")
    current = read_device(store, device) or {}
    doc = {"version": 1, "device": device,
           "name": (name if name is not None else current.get("name") or device).strip() or device,
           "roots": {k.strip(): v.strip() for k, v in (roots if roots is not None
                                                       else current.get("roots") or {}).items()
                     if k.strip() and v.strip()},
           "updated": ss.iso(), "set_by": set_by}
    _put_json(store, device_doc_key(device), doc)
    return doc


def device_docs(store):
    """{device: doc} for every devices/<device>.json on the store."""
    out = {}
    for key in store.list(DEVICES_CONFIG):
        name = key.rsplit("/", 1)[-1]
        if not name.endswith(".json"):
            continue
        try:
            doc = _read_json(store, key)
        except SavesError:
            continue
        if isinstance(doc, dict):
            out[name[:-5]] = doc
    return out


def parse_trees(text):
    """(libraries, {device: roots}) from a pasted savepick.json.

    Takes the whole file, {"trees": ...}, or the trees object alone, because
    a person copying "the trees section" copies any of the three.
    """
    try:
        data = json.loads(text)
    except ValueError:
        raise SavesError("That is not valid JSON.")
    if not isinstance(data, dict):
        raise SavesError("Paste the trees section of savepick.json.")
    trees = data.get("trees") if isinstance(data.get("trees"), dict) else data
    libraries, roots = {}, {}
    for name, tree in trees.items():
        if not isinstance(tree, dict):
            raise SavesError("Paste the trees section of savepick.json.")
        settings = {k: v for k, v in tree.items() if k != "roots"}
        libraries[name] = clean_library(name, settings)
        for device, path in (tree.get("roots") or {}).items():
            if isinstance(path, str) and path.strip():
                roots.setdefault(ss.device_key(device), {})[name] = path.strip()
    if not libraries:
        raise SavesError("No libraries found in what was pasted.")
    return libraries, roots


def import_trees(store, text):
    """Write shared.json and each device's file from a savepick.json trees section."""
    libraries, roots = parse_trees(text)
    shared = write_shared(store, {"libraries": libraries,
                                  "retention": (read_shared(store) or {}).get("retention")})
    for device, device_roots in roots.items():
        merged = dict((read_device(store, device) or {}).get("roots") or {})
        merged.update(device_roots)
        write_device(store, device, roots=merged)
    return shared, sorted(roots)


# ------------------------------------------------------------------ devices


def devices_on_store(listing):
    """{device: newest snapshot time} from snapshot ids in a listing."""
    out = {}
    for key, _when in listing:
        if "/snapshots/" not in key or not key.endswith(".json"):
            continue
        snap_id = key.rsplit("/", 1)[-1][:-5]
        device = ss.snap_device(snap_id)
        when = ss.snap_time(snap_id)
        if not device or device == SERVER_DEVICE or when is None:
            continue
        stamp = ss.iso(when)
        if stamp > out.get(device, ""):
            out[device] = stamp
    return out


# ------------------------------------------------------------------ clean-up

_CLEAN_LOCK = threading.Lock()


def run_clean(store, retention=None, dry_run=False, now=None):
    """slotstore.clean with this store's retention numbers.

    slotstore reads its retention from module constants, the numbers every
    device uses by default. The server sets them from shared.json for the
    length of one clean, under a lock, and puts them back.
    """
    retention = dict(DEFAULT_RETENTION, **(retention or {}))
    lines = []
    with _CLEAN_LOCK:
        saved = (ss.KEEP_PER_DEVICE, ss.KEEP_DAYS)
        ss.KEEP_PER_DEVICE, ss.KEEP_DAYS = int(retention["per_device"]), int(retention["days"])
        try:
            result = ss.clean(store, now=now, dry_run=dry_run, log=lines.append)
        finally:
            ss.KEEP_PER_DEVICE, ss.KEEP_DAYS = saved
    result = dict(result)
    result["log"] = lines[-200:]
    result["dry_run"] = dry_run
    result["when"] = ss.iso(now)
    return result


def newest_upload(listing):
    times = [when for _key, when in listing if when is not None]
    return ss.iso(max(times)) if times else None
