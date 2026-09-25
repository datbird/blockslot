"""The store daemon, hosted by the Decky plugin, and what the panel asks of it.

On the Deck the plugin backend IS the daemon's host (see
docs/superpowers/specs/2026-09-24-store-and-daemon-design.md, "The daemon"):
it is already long running, it runs as the deck user, and it has a panel to
show the queue in. Nothing else on the Deck starts at login.

This module knows nothing about Decky. main.py hands it the paths, so the
tests can drive it with a LocalStore in a temporary directory, and a rule about
what the panel says cannot hide inside code that only runs on a Deck.

Standard library only; Python 3.9. Decky's frozen Python has no xml.etree,
which slotstore already avoids.
"""

import os
import sys
import threading

# How long _unload may spend stopping the daemon. The loader does not wait for
# a plugin that dawdles, and an interrupted upload is safe: the queue is on
# disk and every commit step can be cut off and carried on.
STOP_WAIT = 1.0
RESTART_WAIT = 10.0

NUMBERS = {2: "two", 3: "three", 4: "four"}


def engine_modules(engine_dir):
    """(slotd, slotstore), imported from the plugin's own engine/ copy.

    Imported here rather than at the top, because a plugin staged before
    slotd.py shipped has no copy, and the Syncthing half must still load.
    """
    engine_dir = str(engine_dir)
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    import slotd
    import slotstore
    return slotd, slotstore


def state_dir_for(settings, home):
    """The daemon's state directory: the one the picker finds.

    The picker runs from Steam as the deck user and uses
    slotd.default_state_dir(), which is ~/.local/state/blockslot/store when
    XDG_STATE_HOME is unset, as it is in Steam's environment. The plugin's
    environment comes from the loader's root service, not from a login, so it
    is not trusted to agree; the path is built from the deck user's home.
    """
    if settings and settings.get("state_dir"):
        return settings["state_dir"]
    # posixpath, not os.path: this runs on the Deck only, and building it with
    # the host's separator made the Windows test run disagree with the Deck.
    import posixpath
    return posixpath.join(str(home), ".local", "state", "blockslot", "store")


# ------------------------------------------------------------------ reading


def _progress(value):
    """(done, total) from a Daemon, or [done, total] after a trip over JSON."""
    if not value:
        return None
    try:
        done, total = value
        return {"done": int(done), "total": int(total)}
    except (TypeError, ValueError):
        return None


def _saves(count):
    return "1 save" if count == 1 else "%d saves" % count


def summary(status):
    """The one line the panel shows for the store, and how it should look.

    Returns {"line", "tone"}, tone one of good, warn, bad. The order matters:
    a refusal outranks everything, because it will not clear on its own and a
    person has to act on it. Offline clears itself, so it only says so.
    """
    name = status.get("store") or "the store"
    queued = status.get("queued") or []
    error = status.get("error") or {}
    if error.get("kind") == "refused":
        return {"line": "Refused: %s" % (error.get("message") or "no reason given"),
                "tone": "bad"}
    if status.get("paused") and queued:
        return {"line": "%s waiting, uploads paused" % _saves(len(queued)),
                "tone": "warn"}
    if queued and error.get("kind") == "offline":
        return {"line": "%s waiting, offline" % _saves(len(queued)), "tone": "warn"}
    if queued:
        if any(_progress(item.get("progress")) for item in queued):
            return {"line": "Uploading to %s, %s waiting" % (name, _saves(len(queued))),
                    "tone": "warn"}
        return {"line": "%s waiting to upload to %s" % (_saves(len(queued)), name),
                "tone": "warn"}
    return {"line": "All saves uploaded to %s" % name, "tone": "good"}


def panel_status(status):
    """Daemon.status() in the shape the panel draws, with its summary."""
    queued = []
    for item in status.get("queued") or []:
        queued.append({"id": item.get("id"), "game": item.get("game") or "",
                       "bytes": item.get("bytes") or 0,
                       "progress": _progress(item.get("progress"))})
    error = status.get("error") or None
    answer = {"configured": True, "running": True,
              "store": status.get("store") or "the store",
              "device": status.get("device") or "",
              "queue": queued,
              "error": ({"kind": error.get("kind") or "", "message": error.get("message") or ""}
                        if error else None),
              "last_ok": status.get("last_ok")}
    answer.update(summary(status))
    return answer


def _head(view, snap_id, ss):
    manifest = view.manifests.get(snap_id) or {}
    played = manifest.get("played") or {}
    return {"id": snap_id,
            "device": manifest.get("device") or ss.snap_device(snap_id),
            "created": manifest.get("created"),
            "played_end": played.get("end")}


def candidate_games(state, ss, extra=()):
    """The games worth asking the store about, one per game key.

    The queue and bases.json cover every game that has been through the store
    on this device. bases.json holds game keys, not names; game_key is
    idempotent, so a key reads the same store folder the name does. Each game
    costs a listing on the store, which is an ssh round trip on an SSH store,
    so the whole library is not asked.
    """
    seen = {}
    for snap_id in state.queued():
        manifest = state.queued_manifest(snap_id) or {}
        if manifest.get("game"):
            seen.setdefault(ss.game_key(manifest["game"]), manifest["game"])
    for key in state.bases():
        seen.setdefault(ss.game_key(key), key)
    for name in extra or ():
        if name:
            seen.setdefault(ss.game_key(name), name)
    return [seen[key] for key in sorted(seen)]


def list_forks(store, state, ss, extra=()):
    """Every candidate game with more than one head.

    Returns {"forks": [...], "error": text or None}. Stops at the first store
    failure: an offline store would fail the same way for every game, and the
    panel should say so once, fast, not after one timeout per game.
    """
    forks = []
    for game in candidate_games(state, ss, extra):
        try:
            view = ss.read_game(store, game, cache_dir=state.cache_dir)
        except ss.StoreError as exc:
            return {"forks": forks, "error": str(exc)}
        heads = view.heads
        if len(heads) < 2:
            continue
        # The manifest carries the real name; a key from bases.json does not.
        name = (view.manifests.get(heads[-1]) or {}).get("game") or game
        forks.append({"game": name,
                      "label": "%s: %s different saves"
                               % (name, NUMBERS.get(len(heads), str(len(heads)))),
                      "heads": [_head(view, h, ss) for h in reversed(heads)]})
    return {"forks": forks, "error": None}


# ------------------------------------------------------------------ emulator games


def tree_kind(tree, label_for=None):
    """How the page names one tree of savepick.json.

    {"kind": "game" or "library", "one_game", "label"}. A tree with
    "one_game" is one emulator game, Bloodborne in shadPS4 say; any other
    is an emulator library, split into one save per game. The label names
    the emulator that owns the save, never the frontend.
    """
    tree = tree if isinstance(tree, dict) else {}
    one_game = tree.get("one_game") or ""
    label = tree.get("label") or ""
    if not label and one_game and tree.get("system") and label_for is not None:
        label = label_for(tree["system"])
    return {"kind": "game" if one_game else "library", "one_game": one_game,
            "label": label}


def tree_caption(name, tree):
    """What a game tied to this tree says about it on the panel."""
    kind = tree_kind(tree)
    if kind["kind"] == "game":
        return "game: %s" % kind["one_game"]
    return "library: %s" % name


def matches(row, search):
    """True when the title holds the search text, ignoring case."""
    wanted = (search or "").strip().lower()
    if not wanted:
        return True
    return wanted in (row.get("title") or row.get("name") or "").lower()


def ordered(rows):
    """Two-save games first, each group newest save first.

    Two stable sorts: by time, then by whether a person has to choose.
    """
    rows = sorted(rows, key=lambda row: row.get("when") or "", reverse=True)
    return sorted(rows, key=lambda row: 0 if (row.get("heads") or 0) > 1 else 1)


def _named(device, device_label):
    """The name a person gave a device ("Steam Deck"), or its id."""
    if not device:
        return ""
    return (device_label(device) if device_label else None) or device


def saved_line(row, when_text=None, device_label=None):
    """ "saved 3h ago from Steam Deck", or as much of it as is known."""
    ago = when_text(row.get("when")) if when_text and row.get("when") else ""
    device = _named(row.get("device"), device_label)
    if ago and device:
        return "saved %s from %s" % (ago, device)
    if ago:
        return "saved %s" % ago
    if device:
        return "saved from %s" % device
    return "not saved yet"


def game_row(row, when_text=None, device_label=None):
    """One library row in the shape the page draws."""
    heads = row.get("heads") or 0
    choices = row.get("choices")
    out = {"name": row.get("name") or "",
           "title": row.get("title") or row.get("name") or "",
           "system": row.get("system") or "",
           "label": row.get("label") or "",
           "when": row.get("when") or "",
           # Display only: choosing a save sends its id, never this name.
           "device": _named(row.get("device"), device_label),
           "line": saved_line(row, when_text, device_label),
           "two": heads > 1,
           "heads": heads,
           # Only when the store listing said which saves they are. Without
           # the ids there is nothing to hand to choose(), so the page offers
           # no buttons rather than buttons that cannot work.
           "choices": None}
    if heads > 1 and isinstance(choices, list):
        out["choices"] = [{"id": c.get("id") or "",
                           "device": _named(c.get("device"), device_label),
                           "when": c.get("when") or ""}
                          for c in choices if isinstance(c, dict) and c.get("id")] or None
    return out


def library_games(games, search="", limit=200, when_text=None, device_label=None):
    """{"games": at most `limit` rows, "total": how many matched, "all": how
    many the library holds}. A library can hold two thousand games, and the
    Deck panel must never be asked to draw them all."""
    games = [g for g in games or [] if isinstance(g, dict)]
    found = ordered(g for g in games if matches(g, search))
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 200
    return {"games": [game_row(g, when_text, device_label) for g in found[:limit]],
            "total": len(found), "all": len(games)}


# ------------------------------------------------------------------ hosting


class Host(object):
    """Starts the daemon inside this process, or finds the one that runs.

    One daemon per device. If one already answers on this state directory,
    started by the picker perhaps, the panel talks to that one, because two
    would each think the queue was theirs to report on.
    """

    def __init__(self, config_path, home, engine_dir, log=None):
        self.config_path = str(config_path)
        self.home = str(home)
        self.engine_dir = str(engine_dir)
        self.log = log or (lambda _msg: None)
        self.slotd = None
        self.ss = None
        self.settings = None
        self.device = None
        self.state_dir = None
        self.daemon = None          # ours, when this process serves
        self.client = None          # someone else's, when one already answered
        self.thread = None
        self.fault = None           # why there is no worker, in words
        self.stopped = False        # set by stop(), so nothing starts it again
        self._lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.settings)

    def start(self):
        """Read the settings and bring a worker up. Never raises."""
        with self._lock:
            if self.stopped:
                return
            if self.daemon is not None and self.thread and self.thread.is_alive():
                return
            try:
                if self.slotd is None:
                    self.slotd, self.ss = engine_modules(self.engine_dir)
                self.settings, self.device = self.slotd.load_settings(self.config_path)
            except Exception as exc:    # a missing or broken engine copy
                self.fault = "the store code did not load: %s" % exc
                self.log(self.fault)
                return
            if not self.settings:
                self.fault = None
                return
            self.state_dir = state_dir_for(self.settings, self.home)
            client = self.slotd._client_from_info(self.state_dir)
            if client is not None:
                self.client, self.daemon, self.fault = client, None, None
                self.log("store: using the daemon that already runs")
                return
            try:
                self.daemon = self.slotd.daemon_from_settings(
                    self.settings, self.device, self.state_dir, self.config_path)
            except (self.ss.StoreError, KeyError) as exc:
                self.fault = "the store settings are wrong: %s" % exc
                self.log(self.fault)
                return
            self.client, self.fault = None, None
            self.thread = threading.Thread(target=self._serve, name="blockslot-slotd",
                                           daemon=True)
            self.thread.start()
            self.log("store: daemon started for %s" % self.daemon.store_label)

    def _serve(self):
        daemon = self.daemon
        try:
            self.slotd.serve(daemon, self.state_dir)
        except Exception as exc:
            self.fault = "the daemon stopped: %s" % exc
            self.log(self.fault)

    def stop(self, wait=STOP_WAIT):
        """Stop our daemon quickly. Synchronous and bounded, because _unload
        cannot await: a coroutine awaited during teardown never resumes."""
        self.stopped = True
        daemon, thread = self.daemon, self.thread
        if daemon is None:
            return
        # shutdown() stops the server on a thread of its own; the wait for
        # serve() to return is ours to bound.
        daemon.shutdown()
        if thread is not None:
            thread.join(wait)
        self.daemon = None
        self.log("store: daemon stopped")

    def restart(self):
        """Stop the daemon and start again from the settings file, after
        pairing wrote a new store section. Bounded, like stop()."""
        with self._lock:
            daemon, thread = self.daemon, self.thread
            self.daemon = self.client = self.thread = None
            self.settings = None
        if daemon is not None:
            daemon.shutdown()
            if thread is not None:
                # All the way down, not STOP_WAIT: until serve() returns, the
                # old daemon still answers on daemon.json, and start() would
                # take it for another process's daemon and keep using it.
                thread.join(RESTART_WAIT)
        self.stopped = False
        self.start()

    def worker(self):
        """Ours, or the one that answers. Starts again if it went away."""
        if self.stopped:
            return None
        if self.daemon is not None:
            if self.thread is not None and self.thread.is_alive():
                return self.daemon
            self.daemon = None      # serve() ended; self.fault says why
        if self.client is not None:
            return self.client
        self.start()
        return self.daemon or self.client

    # -- what the panel calls

    def status(self):
        if not self.configured:
            self.start()        # a store section written since the plugin loaded
        if not self.configured:
            return {"configured": False, "error": ({"kind": "broken", "message": self.fault}
                                                   if self.fault else None)}
        worker = self.worker()
        if worker is None:
            return self._not_running()
        try:
            status = worker.status()
        except Exception as exc:
            if worker is self.client:
                # The other daemon went away. Host one here instead.
                self.client = None
                worker = self.worker()
                if worker is not None:
                    try:
                        status = worker.status()
                    except Exception as again:
                        self.fault = str(again)
                        return self._not_running()
                else:
                    return self._not_running()
            else:
                self.fault = str(exc)
                return self._not_running()
        return panel_status(status)

    def _not_running(self):
        message = self.fault or "the uploader is not running"
        return {"configured": True, "running": False,
                "store": self.ss.store_name(self.settings) if self.ss else "the store",
                "device": self.device or "", "queue": [], "last_ok": None,
                "error": {"kind": "broken", "message": message},
                "line": "Not running: %s" % message, "tone": "bad"}

    def upload_now(self):
        """Wake the uploader. It does the upload on its own thread; the panel
        sees the progress in the next status."""
        worker = self.worker()
        if worker is None:
            return {"ok": False, "error": self.fault or "the uploader is not running"}
        if worker is self.daemon:
            worker.kick.set()
            return {"ok": True}
        try:
            worker.kick()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _reader(self):
        """(store, state) to read lineage with. Ours when we host; built from
        the same settings when another daemon does, since reading is safe
        from any number of processes."""
        if self.daemon is not None:
            return self.daemon.store, self.daemon.state
        store = self.ss.store_from_settings(self.settings)
        return store, self.ss.LocalState(self.state_dir)

    def forks(self, extra=()):
        if not self.configured:
            self.start()
        if not self.configured or self.ss is None:
            return {"forks": [], "error": None}
        try:
            store, state = self._reader()
        except Exception as exc:
            return {"forks": [], "error": str(exc)}
        return list_forks(store, state, self.ss, extra)

    def choose(self, game, snap_id):
        worker = self.worker()
        if worker is None:
            return {"ok": False, "error": self.fault or "the uploader is not running"}
        try:
            answer = worker.choose(game, snap_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return dict(answer or {}, ok=True)
