"""Settings: pick a thing on the left, act on that thing on the right.

The first version of this screen was a readout that looked like a menu, with a
row of five buttons along the bottom that acted on whatever they felt like. It
broke the only rule that matters here: a control belongs beside the thing it
controls, and anything that looks selectable has to do something when it is
selected.

So every row is one thing. Selecting it explains it and offers its own
actions, and there is no bar of global buttons at all.
"""

import threading
import tkinter as tk

from ..core import engine as engine_mod
from ..core import launchopts, paths, readiness, steamdir, storecheck
from . import app as app_mod
from . import theme, widgets


def _temporary_warning():
    return ("BlockSlot is running from a temporary folder. Games you turn on "
            "will point at this copy, and they stop starting when it is "
            "cleaned up. " + paths.temporary_advice())


class Item(object):
    """One thing on the left: what it is, how it stands, and what to do.

    ok is True, False, or None while it is still being found out. A row that
    is not `required` is a nicety: when it is off it is shown as optional,
    not as a problem, and it never stops "Everything BlockSlot needs is set".
    """

    def __init__(self, key, label, state, ok, build, required=True):
        self.key = key
        self.label = label
        self.state = state
        self.ok = ok
        self.build = build
        self.required = required
        self.appid = key

    def colour(self):
        if self.ok:
            return theme.GOOD
        if self.ok is None or not self.required:
            return theme.TEXT_FAINT
        return theme.WARN


class SetupScreen(app_mod.Screen):
    title = "Settings"
    hints = (("Up/Down", "pick"), ("Right", "its settings"),
             ("LB/RB", "screens"), ("F5", "refresh"))

    def __init__(self, app):
        app_mod.Screen.__init__(self, app)
        self.settings = app.controller.settings
        self.controller = app.controller
        self._device_name = None
        self._daemon = readiness.CHECKING
        self._asking = False
        self._build()

    def _build(self):
        pad = self.metrics.pad
        self.columnconfigure(0, weight=2, uniform="cols")
        self.columnconfigure(1, weight=3, uniform="cols")
        self.rowconfigure(2, weight=1)

        tk.Label(self, text="Settings", bg=theme.BG, fg=theme.TEXT, anchor="w",
                 font=self.metrics.font("huge", bold=True)
                 ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=pad,
                        pady=(pad, 0))
        self.banner = widgets.Banner(self, self.metrics)
        self.banner.grid(row=1, column=0, columnspan=2, sticky="ew", padx=pad,
                         pady=pad)

        self.items = widgets.ListView(self, self.metrics, self._render_item,
                                      selectable=False, chevrons=True,
                                      row_height=int(self.metrics.row_height * 1.25),
                                      on_cursor=self._select,
                                      on_activate=lambda row: self._into_detail())
        self.items.grid(row=2, column=0, sticky="nsew", padx=(pad, pad // 2),
                        pady=(0, pad))

        self.detail = widgets.Detail(self, self.metrics)
        self.detail.grid(row=2, column=1, sticky="nsew", padx=(pad // 2, pad),
                         pady=(0, pad))

    def focus_order(self):
        return [self.items] + self.detail.focus_order()

    def _into_detail(self):
        order = self.detail.focus_order()
        if order:
            self.app._focus(order[0])

    # ------------------------------------------------------------ the list

    def _render_item(self, canvas, row, y, width, height, is_cursor):
        middle = y + height / 2
        dot = int(9 * self.metrics.scale)
        colour = row.colour()
        canvas.create_oval(self.metrics.pad, middle - dot / 2,
                           self.metrics.pad + dot, middle + dot / 2,
                           fill=colour, outline="")
        left = self.metrics.pad * 2.4
        canvas.create_text(left,
                           middle - self.metrics.px(self.metrics.small * 0.85),
                           anchor="w",
                           text=row.label, fill=theme.TEXT,
                           font=self.metrics.font(bold=is_cursor))
        spec = self.metrics.font("small")
        canvas.create_text(left,
                           middle + self.metrics.px(self.metrics.small * 0.95),
                           anchor="w",
                           text=widgets.elide(row.state, spec,
                                              width - left - self.metrics.pad,
                                              keep="start"),
                           fill=theme.TEXT_DIM, font=spec)

    # ------------------------------------------------------------ data

    def on_show(self):
        self.refresh()

    def refresh(self):
        """Show what is known now, and ask the store's daemon on a thread."""
        if readiness.using_store(self.settings):
            self._daemon = readiness.CHECKING
            if not self._asking:
                self._asking = True
                threading.Thread(target=self._daemon_worker,
                                 daemon=True).start()
        else:
            self._daemon = None
        self._show_items()

    def _daemon_worker(self):
        """Its own thread: never Tk here, the answer is posted."""
        try:
            status = storecheck.daemon_status(self.settings)
        except Exception:
            status = None
        self.app.post(self._daemon_ready, status)

    def _daemon_ready(self, status):
        self._asking = False
        self._daemon = status
        self._show_items()

    def items_now(self):
        """The rows, from what is on disk and what the daemon said."""
        engine = paths.engine_path()
        ludusavi = paths.ludusavi_path()
        shortcut = self._steam_shortcut()

        engine_ok = self._engine_ready()
        if paths.engine_in_exe():
            engine_state = ("built in, but in a temporary folder"
                            if paths.in_temporary_place() else "built in")
        else:
            engine_state = "installed" if engine.is_file() else "not installed"
        device_state, device_ok = readiness.device_state(self.settings,
                                                         self._daemon)
        sync_state, sync_ok = readiness.sync_state(self.settings, self._daemon)
        using_store = readiness.using_store(self.settings)

        return [
            Item("device", "This device", device_state, device_ok,
                 self._device_detail),
            Item("sync", "Store" if using_store else "Sync", sync_state,
                 sync_ok, self._sync_detail),
            Item("engine", "The engine", engine_state, engine_ok,
                 self._engine_detail),
            Item("ludusavi", "ludusavi",
                 "found" if ludusavi.is_file() else "missing",
                 ludusavi.is_file(), self._ludusavi_detail),
            # Optional: only Game Mode needs it, to open this window at all.
            Item("steam", "Steam",
                 "in your library" if shortcut
                 else "optional, not in your library",
                 bool(shortcut), self._steam_detail, required=False),
            Item("files", "Files",
                 "settings, log and cache", True, self._files_detail),
        ]

    def _show_items(self):
        items = self.items_now()
        self.items.set_rows(items, keep_cursor=True)
        text, kind = readiness.headline(items)
        if kind == "warn":
            text = self._advice(items) or text
        self.banner.show(text, kind)

    def _advice(self, items):
        """Say what to do about the first problem, where there is a clear
        next step. None leaves readiness.headline's plain account."""
        first = next(item for item in items if item.required and item.ok is False)
        using_store = readiness.using_store(self.settings)
        if first.key == "sync":
            missing = (self.settings.missing_store_keys() if using_store
                       else self.settings.missing_sync_keys())
            where = "Store" if using_store else "Sync"
            if missing:
                return ("%s is not set up yet. Open %s and fill in %s."
                        % (where, where, ", ".join(missing)))
            if using_store:
                return ("The uploader is not running. Saves still upload when "
                        "a game exits. Open Store to start it.")
        if first.key == "device" and not using_store:
            return "This device has no folder in the share yet. Open Sync to pick it."
        if first.key == "engine":
            if paths.engine_in_exe() and paths.in_temporary_place():
                return _temporary_warning()
            return ("The engine is not installed, so nothing syncs yet. Pick "
                    "The engine and install it.")
        return None

    @staticmethod
    def _engine_ready():
        """The built program carries the engine; elsewhere it is a file."""
        if paths.engine_in_exe():
            return not paths.in_temporary_place()
        return paths.engine_path().is_file()

    def _select(self, row):
        if row is not None:
            row.build()

    def _steam_shortcut(self):
        try:
            return self.controller.steam_shortcut()
        except Exception:
            return None

    # ------------------------------------------------------------ details

    def _device_detail(self):
        if readiness.using_store(self.settings):
            name, _ok = readiness.device_state(self.settings, self._daemon)
            self.detail.show(
                "This device", name,
                lines=["on the store as %s" % name],
                note="This name is what the other devices call this machine "
                     "when they report where a save came from. It is set on "
                     "the Store screen, with the rest of the store.",
                actions=[("Open Store", lambda: self.app.show("store"))])
            return
        device_dir = self.settings.device_dir()
        name = self.settings.device_label(device_dir) if device_dir else ""
        self._device_name = name
        if not device_dir:
            self.detail.show(
                "This device", "Not named yet",
                note="BlockSlot needs to know which folder inside the shared "
                     "folder belongs to this machine before it can name it. "
                     "That is on the Sync screen.",
                actions=[("Open Sync", lambda: self.app.show("sync"))])
            return
        self.detail.show(
            "This device", name or device_dir,
            lines=["folder in the share: %s" % device_dir],
            note="This name is what the other devices call this machine when "
                 "they report where a save came from.",
            field=("What to call this device", name, self._device_typed),
            actions=[("Save", self._save_device)])

    def _sync_detail(self):
        state, ok = readiness.sync_state(self.settings, self._daemon)
        colour = (theme.GOOD if ok else
                  theme.TEXT_FAINT if ok is None else theme.WARN)
        if readiness.using_store(self.settings):
            block = self.settings.store()
            lines = ["%-9s %s" % (key, block.get(key)) for key in
                     ("type", "endpoint", "bucket", "host", "root")
                     if block.get(key)]
            lines.append("device    %s" % readiness.device_state(
                self.settings, self._daemon)[0])
            if self._daemon is not readiness.CHECKING:
                lines.append(storecheck.describe_daemon(self._daemon))
            self.detail.show(
                "Store", readiness.describe_store(self.settings),
                state=(state[:1].upper() + state[1:] + ".", colour),
                lines=lines,
                note="Saves go to this store. The uploader is the daemon that "
                     "sends a save while you play on; without it a save "
                     "still goes up when the game exits.",
                actions=[("Open Store", lambda: self.app.show("store"))])
            return
        block = self.settings.sync
        self.detail.show(
            "Sync", "Syncthing",
            state=(state[:1].upper() + state[1:] + ".", colour),
            lines=["%-10s %s" % (key, block.get(key)) for key in
                   ("url", "folder", "device_dir") if block.get(key)],
            note="Saves travel through a Syncthing folder shared by every "
                 "device.",
            actions=[("Open Sync", lambda: self.app.show("sync"))])

    def _device_typed(self, value):
        self._device_name = value

    def _save_device(self):
        device_dir = self.settings.device_dir()
        if not device_dir:
            return
        self.settings.set_device_name(device_dir,
                                      (self._device_name or "").strip())
        if self.write_settings("Saved."):
            self.refresh()

    def _engine_detail(self):
        if paths.engine_in_exe():
            self._builtin_engine_detail()
            return
        engine = paths.engine_path()
        source = self.controller.engine_source()
        current = bool(source) and engine_mod.is_current(source)
        if not engine.is_file():
            state = ("Not installed. Nothing syncs until it is.", theme.WARN)
        elif current:
            state = ("Installed and up to date.", theme.GOOD)
        else:
            state = ("Installed, but older than this copy.", theme.WARN)
        actions = []
        if source:
            label = "Install it" if not engine.is_file() else (
                "Reinstall it" if current else "Update it")
            actions.append((label, self._install_engine))
        self.detail.show(
            "The engine", "savepick",
            state=state,
            lines=[str(engine)],
            note="savepick is the part that does the work. Steam starts it "
                 "instead of the game, it brings the newest save down first, "
                 "and it backs the save up when you quit. This window only "
                 "decides which games it does that for.",
            actions=actions)

    def _builtin_engine_detail(self):
        program = paths.launch_program()
        temporary = paths.in_temporary_place()
        self.detail.show(
            "The engine", "savepick, built in",
            state=((_temporary_warning(), theme.WARN) if temporary
                   else ("Built into BlockSlot. Nothing to install.",
                         theme.GOOD)),
            lines=[str(program)],
            note="savepick is the part that does the work. Steam starts "
                 "BlockSlot with --pick instead of the game, it brings the "
                 "newest save down first, and it backs the save up when you "
                 "quit. Each game you turn on names this file, so keep it "
                 "where it is. If you move it, turn the games on again.")

    def _install_engine(self):
        source = self.controller.engine_source()
        if not source:
            self.banner.show("This install has no copy of the engine.", "bad")
            return
        try:
            engine_mod.install(source)
        except OSError as exc:
            self.banner.show("Could not install the engine: %s" % exc, "bad")
            return
        self.banner.show("Installed the engine.", "good")
        self.refresh()

    def _ludusavi_detail(self):
        ludusavi = paths.ludusavi_path()
        found = ludusavi.is_file()
        listed = (engine_mod.ludusavi_config_dir(ludusavi)
                  / "manifest.yaml").is_file()
        try:
            engine_mod.ludusavi_asset()
            release = None
        except engine_mod.NoRelease as exc:
            release = str(exc)
        actions = []
        if not found and release is None:
            actions.append(("Install ludusavi", self._install_ludusavi))
        if found and not listed:
            actions.append(("Download its game list", self._update_manifest))
        if not found:
            state = ("Missing.", theme.WARN)
        elif not listed:
            state = ("Found, without its list of games.", theme.WARN)
        else:
            state = ("Found.", theme.GOOD)
        note = ("ludusavi knows where games keep their saves, and it does "
                "every copy. BlockSlot and the engine both call it. Without "
                "it a game can be set up, but nothing is backed up or "
                "restored.")
        if not found:
            note += (" " + release if release else
                     " Install ludusavi downloads version %s from its "
                     "official GitHub release and checks it before using it."
                     % engine_mod.LUDUSAVI_VERSION)
        self.detail.show(
            "ludusavi", "ludusavi",
            state=state,
            lines=[str(ludusavi)],
            note=note,
            actions=actions)

    def _install_ludusavi(self):
        panel = self.app.busy("Install ludusavi", "Working ...")
        threading.Thread(target=self._ludusavi_worker, args=(panel,),
                         daemon=True).start()

    def _update_manifest(self):
        panel = self.app.busy("ludusavi's game list", "Working ...")
        threading.Thread(target=self._manifest_worker, args=(panel,),
                         daemon=True).start()

    # The workers below run on their own threads and never touch Tk: the
    # panel only posts to the Tk thread (App.post), which also runs refresh.

    def _manifest_worker(self, panel):
        try:
            engine_mod.update_ludusavi_manifest(say=panel.say)
            panel.finish("Done.", True, then=self.refresh)
        except Exception as exc:
            panel.finish("Stopped: something unexpected went wrong. %s" % exc,
                         False, then=self.refresh)

    def _ludusavi_worker(self, panel):
        try:
            engine_mod.download_ludusavi(say=panel.say)
            # A fresh ludusavi knows no game until it has its manifest.
            # Fetched now, the first game launch does not wait for it.
            engine_mod.update_ludusavi_manifest(say=panel.say)
            panel.finish("Done.", True, then=self.refresh)
        except engine_mod.AlreadyThere as exc:
            panel.finish("%s. It was left as it is." % exc, True,
                         then=self.refresh)
        except OSError as exc:
            panel.finish("Stopped: %s" % exc, False, then=self.refresh)
        except Exception as exc:
            panel.finish("Stopped: something unexpected went wrong. %s" % exc,
                         False, then=self.refresh)

    def _steam_detail(self):
        shortcut = self._steam_shortcut()
        root = self.controller.library.root
        user = self.controller.library.user_id
        lines = ["Steam: %s" % (root or "not found")]
        if user:
            lines.append("account: %s" % user)
        actions = []
        if root:
            actions.append(("Update the entry" if shortcut else "Add to Steam",
                            self._add_to_steam))
        self.detail.show(
            "Steam", "BlockSlot in your library",
            state=("In your library." if shortcut
                   else "Not in your library. That is fine: it is optional.",
                   theme.GOOD if shortcut else theme.TEXT_FAINT),
            lines=lines,
            note="Game Mode on a Steam Deck only runs what Steam launches, so "
                 "this window has to be in the library to open there. On a "
                 "desktop it is just a handy way to start it.",
            actions=actions)

    def _files_detail(self):
        self.detail.show(
            "Files", "Where everything lives",
            lines=["settings   %s" % self.settings.path,
                   "log        %s" % paths.log_path(),
                   "cache      %s" % paths.state_dir()],
            note="The settings file is the one the engine reads. BlockSlot "
                 "writes to it and keeps a copy of the previous version beside "
                 "it every time.")

    # ------------------------------------------------------------ actions

    def _add_to_steam(self):
        if not self.controller.library.root:
            self.banner.show("No Steam here to add it to.", "bad")
            return
        entry = self.controller.build_steam_shortcut()
        if not self.app.confirm(
                "Add BlockSlot to Steam",
                "This adds a non-Steam entry so you can open this window from "
                "Steam, including in Game Mode.\n\n%s\n%s\n\n"
                "Steam has to close first, and BlockSlot will start it again."
                % (entry.exe, entry.launch_options),
                ok_label="Close Steam and add it"):
            return
        panel = self.app.busy("Add BlockSlot to Steam", "Working ...")
        threading.Thread(target=self._steam_worker, args=(panel,),
                         daemon=True).start()

    def _steam_worker(self, panel):
        library = self.controller.library
        try:
            launchopts.with_steam_closed(
                library.root,
                steamdir.shortcuts_path(library.root, library.user_id),
                self.controller.add_steam_shortcut, panel.say)
            panel.finish("Added. Look under non-Steam games.", True,
                         then=self.refresh)
        except launchopts.SteamStayedOpen as exc:
            panel.finish(str(exc), False, then=self.refresh)
        except Exception as exc:
            panel.finish("Stopped: %s" % exc, False, then=self.refresh)
