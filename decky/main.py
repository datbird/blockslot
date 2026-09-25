"""Blockslot's Decky backend.

THE ONE THING THIS BACKEND DOES NOT DO IS WRITE A LAUNCH OPTION.

On a desktop, Blockslot closes Steam, edits localconfig.vdf and starts Steam
again, because Steam holds that file in memory and writes it out on exit. A
plugin cannot do that: it lives inside the Steam it would have to close.

In Game Mode it does not have to. The frontend asks Steam itself, through
SteamClient.Apps.SetAppLaunchOptions, and Steam writes the file when it feels
like it. So the split is:

    this backend      reads: what is installed, what has saves, what the hub
                      holds, whether sync is working. Builds the exact launch
                      option string for a game.
    the frontend      writes, by asking Steam.

Everything here is the same code the desktop window runs. `py_modules` carries
a copy of gui/core, so a rule about which save wins cannot drift between the
two faces of the same product.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import decky

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "py_modules"))

# HOME has to point at the deck user before the core reads a config path. The
# backend runs unprivileged here, but Decky's environment is not a login shell.
if not os.environ.get("HOME") or os.environ.get("HOME") == "/":
    os.environ["HOME"] = decky.DECKY_USER_HOME

from blockslot_core import backups, engine, logview, model    # noqa: E402
from blockslot_core import paths, settings as settings_mod    # noqa: E402
from blockslot_core import storecheck, syncthing, wrap        # noqa: E402
import blockslot_store                                        # noqa: E402

INDEX = HERE / "index" / "games.json"
ENGINE_SOURCE = HERE / "engine" / paths.ENGINE_NAME

# The store daemon's settings live in the same savepick.json the picker reads,
# in the deck user's home. Named from DECKY_USER_HOME rather than from HOME,
# because the picker runs from Steam as the deck user and this file has to be
# the one it reads.
STORE_CONFIG = Path(decky.DECKY_USER_HOME) / ".config" / "savepick.json"

# package.json names ludusavi's own release as a remote_binary, so Decky
# downloads the official archive here on install and checks its hash. The
# plugin carries no copy of ludusavi.
LUDUSAVI_ARCHIVE = HERE / "bin" / "ludusavi-linux.tar.gz"

# The Syncthing settings the Sync page edits, and the only ones it may write.
SYNC_FIELDS = ("url", "apikey", "folder", "hub_id", "hub_name", "device_dir")

# A hub scan starts ludusavi once per device. The panel and the full page both
# ask for one as they open, so an answer this fresh is handed back as it is.
HUB_FRESH = 60

# An emulator library's listing is one store listing plus a manifest read per
# game, a minute the first time. Kept this long, so typing in the filter asks
# the list in hand, not the store.
LIBRARY_FRESH = 120

# Decky ships a frozen python. Its bundle carries the standard library modules
# Decky itself needs and no others, so a module that is fine everywhere else can
# simply not be there. This says which, once, instead of leaving a feature that
# fails with no explanation.
OPTIONAL = ("urllib.request", "xml.etree.ElementTree", "subprocess", "shutil",
            "tarfile")


def _log(message):
    try:
        decky.logger.info("Blockslot: %s", message)
    except Exception:
        pass


async def _off_loop(work, *args):
    """Run blocking work on a thread, so the panel's other calls are not held."""
    return await asyncio.get_event_loop().run_in_executor(None, work, *args)


def _missing_modules():
    missing = []
    for name in OPTIONAL:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    return missing


class Plugin:
    """Every method here is called from the panel and returns plain data.

    plugin.json says api_version 1, so the loader makes ONE instance of this
    class and hands each call its arguments by position, in the order they are
    declared here. Without that line the loader passes the class itself as
    `self`, and any method that calls another one fails.
    """

    async def _main(self):
        self.settings = settings_mod.Settings.load()
        self.library = model.Library.discover()
        self.library.settings = self.settings
        self.hub = {}
        self.hub_key = None     # the store names games by key, not by name
        self.hub_read = 0.0
        self.libraries = {}     # library name: (read at, [rows])
        self._loaded = False
        # On the Deck this backend is the store daemon's host: it is already
        # long running, as the deck user, and nothing else starts at login.
        # Started on a thread, because the first thing it does may be an ssh
        # round trip, and the panel must not wait on that to open.
        self.store = blockslot_store.Host(STORE_CONFIG, decky.DECKY_USER_HOME,
                                          HERE / "engine", log=_log)
        await _off_loop(self.store.start)
        missing = _missing_modules()
        if missing:
            _log("this python has no %s" % ", ".join(missing))
        _log("started, steam at %s" % (self.library.root or "not found"))

    async def _unload(self):
        # Nothing is awaited here on purpose: a coroutine awaited during
        # teardown never resumes, and the loader moves on without us. The
        # daemon stops in a bounded second; an upload cut short carries on
        # from the queue on disk next time.
        store = getattr(self, "store", None)
        if store is not None:
            try:
                store.stop()
            except Exception as exc:
                _log("could not stop the store daemon cleanly: %s" % exc)
        _log("stopped")

    async def _uninstall(self):
        # The engine, ludusavi and the settings stay. A game that was turned
        # on still names the engine in its launch option, and Steam would fail
        # to start that game if the engine went with the plugin.
        _log("uninstalled. The engine and the settings were left in place, "
             "because games that were turned on still start through them.")

    # ------------------------------------------------------------ reading

    def _load(self, force=False):
        if self._loaded and not force:
            return
        self.library.load_catalog(INDEX if INDEX.is_file() else None)
        self.library.load()
        self._loaded = True

    async def games(self, include_cloud=False, refresh=False):
        """The games worth showing, newest played first.

        Ordered by when they were last played, because on a handheld the game
        you want is nearly always the one you just put down.
        """
        try:
            self._load(force=refresh)
        except Exception as exc:
            _log("could not read the library: %s" % exc)
            return {"error": str(exc), "games": []}
        rows = self.library.visible(hide_cloud=not include_cloud)
        rows = sorted(rows, key=lambda row: (-row.last_played,
                                             (row.name or "").lower()))
        # The core decides which backup belongs to which row, for both faces.
        self.library.attach_backups(self.hub, key=self.hub_key)
        out = []
        for row in rows:
            hub = row.synced
            out.append({
                "appid": row.appid,
                "name": row.name,
                "cloud": row.cloud,
                "saves": row.saves,
                "on": row.syncing,
                "set": row.tree,
                "set_caption": (blockslot_store.tree_caption(
                    row.tree, self.settings.tree(row.tree)) if row.tree else ""),
                "shortcut": row.kind == model.KIND_SHORTCUT,
                "hub": backups.when_text(hub[0]) if hub else "",
                "hub_device": self.settings.device_label(hub[1]) if hub else "",
            })
        return {"games": out, "counts": self.library.counts()}

    async def status(self):
        """One line per thing that has to be true, for the panel's header."""
        engine = paths.engine_path()
        ludusavi = paths.ludusavi_path()
        if self.store.configured:
            # Saves go to the store. Syncthing is not asked anything, so its
            # checks would only report on something no longer in use.
            steps = [await _off_loop(self._store_step)]
        elif not self.settings.sync_is_complete():
            steps = [{"label": "Sync", "ok": False,
                      "detail": "not set up on this device"}]
        else:
            steps = await self._sync_steps()
        steps.append({"label": "Engine", "ok": engine.is_file(),
                      "detail": str(engine) if engine.is_file()
                      else "not installed"})
        steps.append({"label": "ludusavi", "ok": ludusavi.is_file(),
                      "detail": str(ludusavi) if ludusavi.is_file()
                      else "missing"})
        ok = all(step["ok"] for step in steps)
        return {"ok": ok, "steps": steps,
                "device": self.settings.device_label(
                    self.settings.device_dir() or "this device")}

    def _store_step(self):
        status = self.store.status()
        error = status.get("error") or {}
        name = status.get("store") or "the store"
        if error:
            return {"label": "Store", "ok": False,
                    "detail": "%s: %s" % (name, error.get("message") or "not reachable")}
        return {"label": "Store", "ok": True, "detail": "saves go to %s" % name}

    async def _sync_steps(self):
        """Every Syncthing check, in the shape the panel draws."""
        found = await _off_loop(syncthing.check, self.settings.sync)
        return [{"label": label, "ok": bool(ok), "detail": detail or ""}
                for label, ok, detail in found]

    async def read_hub(self, force=False):
        """What the hub holds, per game. Slow enough to be asked for."""
        if not force and time.time() - self.hub_read < HUB_FRESH:
            return {"games": len(self.hub)}
        try:
            if self.store.configured:
                self.hub = await _off_loop(self._store_newest)
                self.hub_key = self.store.ss.game_key
            else:
                self.hub = await _off_loop(backups.newest_everywhere,
                                           paths.ludusavi_path())
                self.hub_key = None
            self.hub_read = time.time()
        except Exception as exc:
            _log("could not read the hub: %s" % exc)
            self.hub = {}
        return {"games": len(self.hub)}

    def _store_newest(self):
        """{game key: (when, device)} in one listing of the store."""
        store = self.store.ss.store_from_settings(self.store.settings)
        return self.store.ss.newest_on_store(store)

    # ------------------------------------------------------------ the store

    async def store_status(self):
        """Where saves go, what is queued, and the one line that sums it up.

        {"configured": false} when savepick.json has no store section, and
        the panel then shows nothing new.
        """
        return await _off_loop(self.store.status)

    async def store_pair(self, address="", code=""):
        """Set this device up from a BlockSlot server's pairing code.

        The server's Devices page shows its address and a code like
        K7QX-4MPA, good once for 15 minutes. The answer replaces the whole
        store section, so pairing again moves this device to a new server.
        """
        try:
            values = await _off_loop(storecheck.pair, address, code)
            base = storecheck.server_address(address)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self.settings = settings_mod.Settings.load()
        self.settings.clear_store()
        self.settings.set_store(**values)
        self.settings.data["server_address"] = base
        answer = self._save()
        if not answer["ok"]:
            return answer
        _log("paired with %s as %s" % (base, values.get("device") or "this device"))
        await _off_loop(self.store.restart)
        answer["status"] = await _off_loop(self.store.status)
        answer["address"] = base
        return answer

    async def store_upload_now(self):
        return await _off_loop(self.store.upload_now)

    async def store_forks(self):
        """Games whose store holds more than one head, each a real save.

        Asked about: what is queued here, what this device has a base for,
        and the games turned on in the library if it was already read.
        """
        extra = []
        if self._loaded:
            for row in self.library.rows:
                if row.syncing:
                    extra.append(row.tree or row.name)
        return await _off_loop(self.store.forks, extra)

    async def store_choose(self, game, snap_id):
        """A person picked one save of a fork. Recorded as a merge snapshot,
        which the uploader sends; the other save stays on the store."""
        answer = await _off_loop(self.store.choose, game, snap_id)
        if answer.get("ok"):
            _log("store: chose %s for %s" % (snap_id, game))
            # The listing still shows two saves until it is read again.
            self.libraries.clear()
        return answer

    async def sets(self):
        """Every tree in savepick.json: an emulator library or one emulator
        game, and its folder on this device."""
        label_for = self._label_for()
        rows = []
        for name, root, devices, _every in self.settings.tree_rows():
            row = {"name": name, "root": root, "devices": devices}
            row.update(blockslot_store.tree_kind(self.settings.tree(name), label_for))
            rows.append(row)
        return {"sets": rows}

    def _label_for(self):
        """saveunits.label_for from the plugin's engine copy, or None."""
        try:
            if str(HERE / "engine") not in sys.path:
                sys.path.insert(0, str(HERE / "engine"))
            import saveunits
            return saveunits.label_for
        except Exception:
            return None

    async def library_games(self, library, search="", limit=200, refresh=False):
        """The games of one emulator library, as the page draws them.

        Filtered by title, games with two saves first, then newest. At most
        `limit` rows go back, with the total, because a library can hold two
        thousand games and the Deck must never draw them all.
        """
        library = library or ""
        cached = self.libraries.get(library)
        if refresh or cached is None or time.time() - cached[0] > LIBRARY_FRESH:
            try:
                games = await _off_loop(self._library_list, library)
            except Exception as exc:
                _log("could not list %s: %s" % (library, exc))
                return {"library": library, "games": [], "total": 0, "all": 0,
                        "error": str(exc) or "the store did not answer"}
            cached = (time.time(), games)
            self.libraries[library] = cached
        answer = blockslot_store.library_games(cached[1], search, limit,
                                               when_text=backups.when_text,
                                               device_label=self.settings.device_label)
        answer["library"] = library
        answer["error"] = None
        return answer

    def _library_list(self, library):
        if not self.store.configured:
            self.store.start()
        if not self.store.configured:
            raise RuntimeError("no store is set up on this device")
        worker = self.store.worker()
        if worker is None:
            raise RuntimeError(self.store.fault or "the uploader is not running")
        answer = worker.library_list(library) or {}
        return answer.get("games") or []

    async def engine_state(self):
        """Everything the Settings page says about the engine and its tools."""
        installed = paths.engine_path()
        ludusavi = paths.ludusavi_path()
        return {
            "engine": str(installed),
            "installed": installed.is_file(),
            "current": engine.is_current(ENGINE_SOURCE),
            "have_source": ENGINE_SOURCE.is_file(),
            "ludusavi": str(ludusavi),
            "ludusavi_found": ludusavi.is_file(),
            "ludusavi_archive": LUDUSAVI_ARCHIVE.is_file(),
            "device_name": self.settings.device_label(
                self.settings.device_dir() or ""),
            "settings_file": str(self.settings.path),
            "log": str(paths.log_path()),
            "steam": str(self.library.root or ""),
        }

    async def sync_settings(self):
        """The Syncthing block, as the Sync page edits it."""
        block = self.settings.sync
        return {key: block.get(key, "") for key in SYNC_FIELDS}

    async def log_tail(self, lines=120):
        """The end of the engine's log, which is its only record of a launch."""
        path = paths.log_path()
        try:
            found = logview.tail(path, lines)
        except OSError as exc:
            return {"lines": [], "error": str(exc), "path": str(path)}
        return {"lines": [{"text": line, "kind": logview.classify(line)}
                          for line in found],
                "path": str(path)}

    # ------------------------------------------------------------ writing

    async def launch_option(self, appid, on, existing="", save_set=None):
        """The exact launch option Steam should be given for this game.

        The frontend hands back what Steam currently has, because while Steam
        is running that is the truth and the file on disk is not.

        A non-Steam shortcut needs an emulator library or game, since its app
        id means nothing to savepick. One whose name matches one is matched to
        it, which is not a guess: it is the same name. Otherwise the names are
        handed back and the panel asks.
        """
        try:
            self._load()
        except Exception:
            pass
        row = None
        for candidate in self.library.rows:
            if candidate.appid == int(appid):
                row = candidate
                break
        engine = paths.engine_path()
        python = paths.python_for_launch()
        # Borderless is a Windows switch, set from the desktop window. A
        # launch option that carries it keeps it through a sync change here.
        borderless = wrap.borderless_of(existing)
        if not on:
            if borderless:
                return {"option": wrap.build(python, engine, wrap.strip(existing),
                                             borderless=True, sync=False),
                        "ok": True}
            return {"option": wrap.strip(existing), "ok": True}
        tree = row.tree if row is not None else None
        if row is not None and row.kind == model.KIND_SHORTCUT and not tree:
            tree = save_set or self._matching_set(row.name)
            if not tree:
                names = self.settings.tree_names()
                if not names:
                    return {"ok": False,
                            "error": "This is not a Steam game, so it needs an "
                                     "emulator library or game, and there are "
                                     "none yet."}
                return {"ok": False, "choose_set": names,
                        "error": "Which emulator library or game does %s use?"
                                 % row.name}
        option = wrap.build(python, engine, wrap.strip(existing), tree=tree,
                            borderless=borderless)
        return {"option": option, "ok": True}

    def _matching_set(self, name):
        """A tree named after this game, or None. Matched, never guessed."""
        wanted = (name or "").strip().lower()
        for candidate in self.settings.tree_names():
            if candidate.strip().lower() == wanted:
                return candidate
        return None

    async def install_engine(self):
        """Copy the engine that ships with this plugin into ~/.local/bin."""
        if not ENGINE_SOURCE.is_file():
            return {"ok": False, "error": "this plugin has no engine to install"}
        try:
            target = engine.install(ENGINE_SOURCE)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        _log("installed the engine to %s" % target)
        return {"ok": True, "path": str(target)}

    async def install_ludusavi(self):
        """Unpack the ludusavi that Decky downloaded. Never replaces one."""
        if not LUDUSAVI_ARCHIVE.is_file():
            return {"ok": False,
                    "error": "This install has no ludusavi download. Get it "
                             "from its own site and put it at %s."
                             % paths.ludusavi_path()}
        try:
            target = await _off_loop(engine.install_ludusavi, LUDUSAVI_ARCHIVE)
        except engine.AlreadyThere as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        _log("installed ludusavi to %s" % target)
        return {"ok": True, "path": str(target)}

    def _save(self, **extra):
        """Write the settings file, and answer the way every write here does."""
        try:
            self.settings.save()
        except OSError as exc:
            _log("could not save the settings: %s" % exc)
            return {"ok": False, "error": str(exc)}
        return dict(extra, ok=True)

    async def save_sync(self, values):
        """Write the Syncthing block, then say whether it works.

        `values` is the block as sync_settings handed it out. Saved first and
        tested second on purpose: a setting that is right but untestable right
        now, because the hub is asleep, is still the setting you meant.
        """
        self.settings.set_sync(**{
            key: str((values or {}).get(key, "")).strip()
            for key in SYNC_FIELDS})
        answer = self._save()
        if answer["ok"]:
            answer["steps"] = await self._sync_steps()
        return answer

    async def set_device_name(self, name=""):
        device_dir = self.settings.device_dir()
        if not device_dir:
            return {"ok": False,
                    "error": "Set this device's folder in the share first."}
        self.settings.set_device_name(device_dir, (name or "").strip())
        return self._save()

    async def add_set(self, name="", every_file=False):
        try:
            self.settings.add_tree(name, every_file)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return self._save()

    async def set_root(self, name="", folder=""):
        """Point an emulator library or game at its folder ON THIS DEVICE."""
        try:
            folder = self.settings.set_tree_root(name, folder)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return self._save(exists=Path(folder).is_dir())

    async def remove_set(self, name=""):
        self.settings.remove_tree(name)
        return self._save()

    async def refresh(self):
        self._loaded = False
        try:
            self._load(force=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}
