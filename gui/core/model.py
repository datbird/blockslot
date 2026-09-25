"""One view of everything, so the screens never have to join it themselves.

The UI asks this module three things: what games are there, what is true about
each one, and please change these. Nothing above this line knows what a vdf is,
and nothing below it knows what a window is.
"""

import os
import shutil

from . import catalog as catalog_mod
from . import launchopts, paths, settings as settings_mod, shortcuts, steamdir
from . import vdf, wrap

# What a row is for. A game Steam Cloud covers is not this tool's business.
KIND_STEAM = "steam"
KIND_SHORTCUT = "shortcut"


class Row(object):
    """One game as the games screen shows it."""

    __slots__ = ("appid", "name", "kind", "installed", "cloud", "saves",
                 "_launch_options", "wrapped", "syncing", "borderless", "tree",
                 "last_played", "index",
                 "library", "is_game", "catalog_name", "synced")

    def __init__(self, appid, name, kind, installed=False, cloud=None,
                 saves=None, launch_options="", last_played=0, index=None,
                 library=None, is_game=True, catalog_name=None):
        self.appid = appid
        self.name = name
        self.kind = kind
        self.installed = installed
        self.cloud = cloud
        self.saves = saves
        self.launch_options = launch_options or ""
        self.last_played = last_played or 0
        self.index = index
        self.library = library
        self.is_game = is_game
        # The name ludusavi and the manifest use, which is not always the name
        # in the install folder. "DARK SOULS II" and "Dark Souls II" are the
        # same game and only one of them matches a backup directory.
        self.catalog_name = catalog_name or name
        # (when, device) for the newest backup anywhere, filled in later
        # because reading it means running ludusavi once per device.
        self.synced = None

    @property
    def launch_options(self):
        return self._launch_options

    @launch_options.setter
    def launch_options(self, value):
        # Worked out once here: every redraw and every filter asks for both,
        # and each answer means splitting the whole string again.
        self._launch_options = value or ""
        # wrapped: the launch runs through savepick at all. syncing: it carries
        # saves. A borderless only game is wrapped and not syncing, and every
        # question about saves asks `syncing`.
        self.wrapped = wrap.is_wrapped(self._launch_options)
        self.syncing = wrap.syncs(self._launch_options)
        self.borderless = wrap.borderless_of(self._launch_options)
        self.tree = wrap.tree_of(self._launch_options)

    @property
    def needs_blockslot(self):
        """True when this is a game Blockslot is for and is not yet on.

        Unknown cloud support counts as needing it. A game nothing knows about
        is exactly the game whose saves nobody is carrying.
        """
        if self.syncing:
            return False
        if self.cloud is True:
            return False
        return self.saves is not False

    def sort_key(self):
        return (self.name or "").lower()

    def __repr__(self):
        return "Row(%s, %r)" % (self.appid, self.name)


class Library(object):
    """Everything Blockslot knows about this machine's Steam."""

    def __init__(self, root=None, user_id=None, settings=None, catalog=None):
        self.root = root
        self.user_id = user_id
        self.settings = settings or settings_mod.Settings.load()
        self.catalog = catalog or catalog_mod.Catalog()
        self.rows = []
        self.shortcuts = []
        self.error = None

    # ------------------------------------------------------------ loading

    @classmethod
    def discover(cls, root=None, user_id=None):
        """Find Steam, or say plainly that there is none.

        A root given on the command line is checked like any other. Reporting
        a path that holds no Steam as if it were Steam is how an empty games
        list reads as "no games" rather than "wrong folder".
        """
        if root is not None:
            root = steamdir.find_root([root])
        else:
            root = steamdir.find_root()
        if root is None:
            library = cls()
            library.error = ("No Steam here. BlockSlot looked in the usual "
                             "places and found no userdata folder.")
            return library
        users = steamdir.user_ids(root)
        if user_id is None:
            user_id = users[0] if users else None
        library = cls(root=root, user_id=user_id)
        if user_id is None:
            library.error = ("Steam is here but nobody has signed in on this "
                             "machine, so there are no settings to change.")
        return library

    def load_catalog(self, index_path=None):
        index_path = index_path or catalog_mod.default_index_path()
        if index_path:
            self.catalog = catalog_mod.Catalog.from_index(index_path)
        if self.root:
            self.catalog.load_steam_cloud(
                steamdir.appinfo_path(self.root),
                paths.cache_path(catalog_mod.CACHE_NAME))
        return self.catalog

    def load(self, include_uninstalled=False):
        """Build the row list. Safe to call again to refresh."""
        if self.root is None or self.user_id is None:
            self.error = self.error or "No Steam user to read."
            return self.rows
        rows = {}
        installed = {}
        for game in steamdir.installed_games(self.root):
            installed[game.appid] = game

        try:
            text = launchopts.load(steamdir.localconfig_path(self.root, self.user_id))
            apps = launchopts.read_apps(text)
        except OSError:
            apps = {}

        names = {}
        for appid in set(list(installed) + list(apps)):
            entry = self.catalog.entry(appid)
            if entry is not None:
                names[appid] = entry.name

        for appid, game in installed.items():
            record = apps.get(appid) or {}
            rows[appid] = Row(
                appid=appid,
                name=game.name or names.get(appid) or ("App %d" % appid),
                kind=KIND_STEAM,
                installed=True,
                cloud=self.catalog.cloud(appid),
                saves=self.catalog.saves(appid),
                launch_options=record.get("LaunchOptions", ""),
                last_played=_int(record.get("LastPlayed")),
                library=str(game.library),
                is_game=self.catalog.is_game(appid),
                catalog_name=names.get(appid),
            )

        for appid, record in apps.items():
            if appid in rows:
                continue
            wrapped = wrap.is_wrapped(record.get("LaunchOptions", ""))
            if not include_uninstalled and not wrapped:
                continue
            rows[appid] = Row(
                appid=appid,
                name=names.get(appid) or ("App %d" % appid),
                kind=KIND_STEAM,
                installed=False,
                cloud=self.catalog.cloud(appid),
                saves=self.catalog.saves(appid),
                launch_options=record.get("LaunchOptions", ""),
                last_played=_int(record.get("LastPlayed")),
                is_game=self.catalog.is_game(appid),
                catalog_name=names.get(appid),
            )

        self.shortcuts = shortcuts.read(
            steamdir.shortcuts_path(self.root, self.user_id))
        for position, entry in enumerate(self.shortcuts):
            appid = entry.appid or shortcuts.generated_appid(entry.exe, entry.name)
            rows[appid] = Row(
                appid=appid,
                name=entry.name or "Untitled shortcut",
                kind=KIND_SHORTCUT,
                installed=True,
                cloud=False,
                saves=None,
                launch_options=entry.launch_options,
                last_played=_int(entry._get("LastPlayTime")),
                index=position,
            )

        self.rows = sorted(rows.values(), key=Row.sort_key)
        return self.rows

    def attach_backups(self, newest, key=None):
        """Hang the hub's newest backup on each row it can be matched to.

        Matched by the manifest name first and the install name second, which
        is the same order the engine itself would use. A row wrapped with a
        save set is matched by the SET's name, because that is what the engine
        backs up for it: a retro frontend has one backup covering everything
        it holds, not one per game.

        A game with no match is left alone rather than shown as never synced:
        an unmatched name is not evidence of a missing backup.
        """
        # The store names games by a filesystem-safe key, not by name, so a
        # store answer passes the function that makes that key.
        def look(name):
            if not name:
                return None
            return newest.get(key(name) if key else name)

        matched = 0
        for row in self.rows:
            found = (look(row.tree) if row.tree else None) \
                or look(row.catalog_name) or look(row.name)
            row.synced = found
            if found:
                matched += 1
        return matched

    # ------------------------------------------------------------ filtering

    def visible(self, search="", hide_cloud=True, only_wrapped=False,
                only_installed=True, include_tools=False):
        search = (search or "").strip().lower()
        out = []
        for row in self.rows:
            if not include_tools and not row.is_game and not row.wrapped:
                continue
            if only_installed and not row.installed and not row.wrapped:
                continue
            if hide_cloud and row.cloud is True and not row.wrapped:
                continue
            if only_wrapped and not row.syncing:
                continue
            if search and search not in (row.name or "").lower():
                continue
            out.append(row)
        return out

    def counts(self):
        total = sum(1 for row in self.rows if row.is_game or row.wrapped)
        wrapped = sum(1 for row in self.rows if row.syncing)
        cloud = sum(1 for row in self.rows if row.cloud is True)
        candidates = sum(1 for row in self.rows if row.needs_blockslot)
        return {"total": total, "wrapped": wrapped, "cloud": cloud,
                "candidates": candidates}

    # ------------------------------------------------------------ changing

    def plan(self, appids, enable, tree=None):
        """What turning sync on or off would change, without changing anything.

        `tree` names a save set for the rows that need one. Only a non-Steam
        shortcut does: a Steam game tells savepick what it is through its app
        id, and giving it a save set as well would point it at another game's
        saves.

        Borderless is left as it is. Turning sync off on a borderless game
        keeps the wrap, with --no-sync.

        Returns (steam_changes, shortcut_changes). The UI shows this before it
        asks to close Steam, because closing Steam is the disruptive part and
        it should never happen for a no-op.
        """
        def wanted(row):
            row_tree = row.tree
            if row_tree is None and row.kind == KIND_SHORTCUT:
                row_tree = tree
            return enable, row.borderless, row_tree
        return self._plan(appids, wanted)

    def plan_borderless(self, appids, enable):
        """What turning borderless on or off would change. Sync is left alone."""
        def wanted(row):
            return row.syncing, enable, row.tree
        return self._plan(appids, wanted)

    def _plan(self, appids, wanted):
        """One plan for both switches. `wanted(row)` -> (sync, borderless, tree).

        The frozen Windows exe carries the engine, so its wraps name the exe
        with --pick in place of python and savepick.py. Either form written
        earlier is recognised, and one that is not this install's own is
        wrapped again in the current form.
        """
        if paths.engine_in_exe():
            python = paths.launch_program()
            engine = wrap.PICK
            runs_with = str(python)
        else:
            python = paths.python_for_launch()
            engine = paths.engine_path()
            runs_with = str(engine)
        steam_changes = {}
        shortcut_changes = []
        picked = set(int(appid) for appid in appids)
        for row in self.rows:
            if row.appid not in picked:
                continue
            sync, borderless, row_tree = wanted(row)
            if not sync:
                row_tree = None
            enable = sync or borderless
            entry = (self._shortcut_at(row.index)
                     if row.kind == KIND_SHORTCUT else None)
            already = (row.wrapped
                       and wrap.engine_of(row.launch_options,
                                          entry.exe if entry else None)
                       == runs_with
                       and row.syncing == sync and row.borderless == borderless
                       and row.tree == row_tree)
            if enable and already:
                continue
            if not enable and not row.wrapped:
                continue

            if row.kind == KIND_SHORTCUT:
                if entry is None:
                    continue
                exe, options = wrap.unwrap_shortcut(entry.exe, entry.launch_options)
                if enable:
                    new_exe, new_options = wrap.build_shortcut(
                        python, engine, exe, options, tree=row_tree,
                        borderless=borderless, sync=sync)
                else:
                    new_exe, new_options = exe, options
                shortcut_changes.append((row.index, new_exe, new_options))
                continue

            if enable:
                current = wrap.strip(row.launch_options)
                value = wrap.build(python, engine, current, tree=row_tree,
                                   borderless=borderless, sync=sync)
            else:
                value = wrap.strip(row.launch_options)
            steam_changes[row.appid] = value or None
        return steam_changes, shortcut_changes

    def _verify(self, path, changes):
        """Read the file back and prove every change is in it.

        This file holds a Steam account's whole local state. A write that went
        wrong has to be noticed here, while the backup beside it is one line
        away, and not at the next launch by a game that loads someone else's
        save.
        """
        try:
            after = launchopts.read_all(launchopts.load(path))
        except (OSError, vdf.VdfError) as exc:
            self._restore(path)
            raise IOError("the settings file did not read back: %s" % exc)
        for appid, value in changes.items():
            landed = after.get(int(appid))
            if (value or None) != (landed or None):
                self._restore(path)
                raise IOError("app %s did not take the change; the file has "
                              "been put back" % appid)

    def _restore(self, path):
        backup = str(path) + ".blockslot.bak"
        try:
            if os.path.isfile(backup):
                shutil.copy2(backup, str(path))
        except OSError:
            pass

    def _shortcut_at(self, index):
        if index is None or not (0 <= index < len(self.shortcuts)):
            return None
        return self.shortcuts[index]

    def steam_running(self):
        """Whether a Steam client is up. A seam, so a test can say no.

        The real check asks the operating system, which is right in the
        product and wrong in a test: a test writes into a Steam directory it
        built itself, which the running client has never heard of.
        """
        return launchopts.steam_running()

    def apply(self, steam_changes, shortcut_changes):
        """Write the plan. Steam must already be closed.

        Raises SteamBusy rather than write into a file Steam will overwrite.
        """
        if self.steam_running():
            raise launchopts.SteamBusy(
                "Steam is running. It would overwrite this on exit.")
        written = 0
        if steam_changes:
            path = steamdir.localconfig_path(self.root, self.user_id)
            text = launchopts.load(path)
            launchopts.save(path, launchopts.write_all(text, steam_changes))
            self._verify(path, steam_changes)
            written += len(steam_changes)
        if shortcut_changes:
            path = steamdir.shortcuts_path(self.root, self.user_id)
            entries = shortcuts.read(path)
            for index, new_exe, new_options in shortcut_changes:
                if 0 <= index < len(entries):
                    entries[index].exe = _quoted(new_exe)
                    entries[index].launch_options = new_options
                    written += 1
            shortcuts.write(path, entries)
        return written


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _quoted(path):
    """Steam stores a shortcut's exe with its quotes, so keep them."""
    text = str(path)
    if text.startswith('"') and text.endswith('"'):
        return text
    return '"%s"' % text
