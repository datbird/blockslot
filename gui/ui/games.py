"""The games screen: what Steam does not sync, and what to do about it."""

import threading
import time
import tkinter as tk

from ..core import backups, launchopts, paths, steamdir, storecheck
from . import app as app_mod
from . import theme, widgets


def played(stamp):
    if not stamp:
        return ""
    delta = time.time() - stamp
    if delta < 0:
        return ""
    for seconds, label in ((86400, "today"), (172800, "yesterday")):
        if delta < seconds:
            return label
    return backups.ago_text(delta)


def tree_text(name, tree):
    """The SAVE PATHS cell for a shortcut that carries a tree.

    "game: Bloodborne" for a tree holding one game, "library: RetroBat" for
    one holding many. Trimmed, because the column is narrow.
    """
    one_game = (tree or {}).get("one_game")
    word, value = ("game", one_game) if one_game else ("library", name)
    value = value if len(value) <= 18 else value[:17] + "..."
    return "%s: %s" % (word, value)


class GamesScreen(app_mod.Screen):
    title = "Games"
    hints = (("Up/Down", "move"), ("Space", "pick"), ("Enter", "about it"),
             ("A", "pick all shown"), ("LB/RB", "screens"), ("F5", "refresh"))

    def __init__(self, app):
        app_mod.Screen.__init__(self, app)
        self.library = app.controller.library
        self.rows = []
        self._backups_read = False
        self._newest = None
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self):
        pad = self.metrics.pad
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        header = tk.Frame(self, bg=theme.BG)
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, 0))
        self.heading = tk.Label(header, text="Games", bg=theme.BG,
                                fg=theme.TEXT, anchor="w",
                                font=self.metrics.font("huge", bold=True))
        self.heading.pack(side="left")
        self.summary = tk.Label(header, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                                anchor="e", font=self.metrics.font())
        self.summary.pack(side="right")

        controls = tk.Frame(self, bg=theme.BG)
        controls.grid(row=1, column=0, sticky="ew", padx=pad, pady=(pad, pad))
        self.search = widgets.Field(controls, self.metrics, "Search",
                                    width=18,
                                    on_change=lambda value: self.apply_filter())
        self.search.pack(side="left")
        self.hide_cloud = widgets.Toggle(
            controls, self.metrics, "Hide cloud games", value=True,
            command=lambda value: self.apply_filter(),
            width=int(250 * self.metrics.scale))
        self.hide_cloud.pack(side="left", padx=(pad, 0), pady=(pad, 0))
        self.only_on = widgets.Toggle(
            controls, self.metrics, "Only BlockSlot", value=False,
            command=lambda value: self.apply_filter(),
            width=int(230 * self.metrics.scale))
        self.only_on.pack(side="left", padx=(pad, 0), pady=(pad, 0))
        self.show_all = widgets.Toggle(
            controls, self.metrics, "Show uninstalled", value=False,
            command=lambda value: self.reload_all(),
            width=int(255 * self.metrics.scale))
        self.show_all.pack(side="left", padx=(pad, 0), pady=(pad, 0))

        self.banner = widgets.Banner(self, self.metrics)
        self.banner.grid(row=5, column=0, sticky="ew", padx=pad,
                         pady=(0, self.metrics.gap))
        self.banner.grid_remove()

        self.head = tk.Canvas(self, height=int(26 * self.metrics.scale),
                              bg=theme.BG, highlightthickness=0, bd=0)
        self.head.grid(row=2, column=0, sticky="ew", padx=pad)
        self.head.bind("<Configure>", lambda event: self._draw_head())

        self.list = widgets.ListView(self, self.metrics, self._render_row,
                                     on_activate=self._show_details,
                                     on_selection_change=self._selection_changed)
        self.list.grid(row=3, column=0, sticky="nsew", padx=pad)

        actions = tk.Frame(self, bg=theme.BG)
        actions.grid(row=4, column=0, sticky="ew", padx=pad, pady=pad)
        # Two buttons, and both say what they will do to what. Four buttons
        # that act on an invisible selection is how the first version read.
        self.add_button = widgets.Button(actions, self.metrics,
                                         "Turn on sync", kind="primary",
                                         command=lambda: self._apply(True))
        self.add_button.pack(side="left")
        self.remove_button = widgets.Button(actions, self.metrics,
                                            "Turn off sync",
                                            command=lambda: self._apply(False))
        self.remove_button.pack(side="left", padx=(self.metrics.gap, 0))
        # Borderless is its own switch, with or without sync. Windows only:
        # gamescope already shows every game fullscreen on the Deck.
        self.borderless_button = None
        if paths.is_windows():
            self.borderless_button = widgets.Button(
                actions, self.metrics, "Make borderless",
                command=self._toggle_borderless)
            self.borderless_button.pack(side="left",
                                        padx=(self.metrics.gap * 2, 0))
        self.status = tk.Label(actions, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                               font=self.metrics.font())
        self.status.pack(side="left", padx=(self.metrics.gap * 2, 0))

        self.bind_all("<a>", lambda event: self._select_all_if_mine())
        self._selection_changed([])

    def focus_order(self):
        buttons = [self.add_button, self.remove_button]
        if self.borderless_button is not None:
            buttons.append(self.borderless_button)
        return [self.list] + buttons + [self.search, self.hide_cloud,
                                        self.only_on, self.show_all]

    # ------------------------------------------------------------ drawing

    # PLAYED moved into the details view. The hub's answer is the column
    # worth the width: it is the question the whole tool exists to answer.
    COLUMNS = ((0.50, "STEAM CLOUD"), (0.63, "SAVE PATHS"), (0.78, "ON THE HUB"))

    def _draw_head(self):
        canvas = self.head
        canvas.delete("all")
        width = int(canvas.winfo_width() or 900)
        height = int(canvas.winfo_height() or 26)
        canvas.create_text(int(self.metrics.row_height * 1.05), height / 2,
                           anchor="w", text="GAME", fill=theme.TEXT_FAINT,
                           font=self.metrics.font("small", bold=True))
        for fraction, label in self.COLUMNS:
            canvas.create_text(width * fraction, height / 2, anchor="w",
                               text=label, fill=theme.TEXT_FAINT,
                               font=self.metrics.font("small", bold=True))
        canvas.create_line(0, height - 1, width, height - 1, fill=theme.LINE)

    def _render_row(self, canvas, row, y, width, height, is_cursor):
        metrics = self.metrics
        left = int(metrics.row_height * 1.05)
        middle = y + height / 2
        name_colour = theme.TEXT if row.installed else theme.TEXT_DIM
        canvas.create_text(left, middle, anchor="w", text=row.name,
                           fill=name_colour, font=metrics.font(bold=is_cursor),
                           width=int(width * 0.46))

        columns = (
            (self.COLUMNS[0][0], self._cloud_text(row)),
            (self.COLUMNS[1][0], self._saves_text(row)),
            (self.COLUMNS[2][0], self._synced_text(row)),
        )
        for fraction, (text, colour) in columns:
            canvas.create_text(width * fraction, middle, anchor="w", text=text,
                               fill=colour, font=metrics.font("small"))

        # Right to left: ON for sync, then BORDERLESS beside it.
        right = width - int(24 * metrics.scale)
        pills = []
        if row.syncing:
            pills.append(("ON", 46))
        if row.borderless:
            pills.append(("BORDERLESS", 110))
        pill_h = int(22 * metrics.scale)
        for text, base in pills:
            pill_w = int(base * metrics.scale)
            x = right - pill_w
            widgets.round_rect(canvas, x, middle - pill_h / 2, x + pill_w,
                               middle + pill_h / 2, pill_h / 2,
                               fill=theme.ACCENT_DEEP, outline="")
            canvas.create_text(x + pill_w / 2, middle, text=text,
                               fill=theme.TEXT,
                               font=metrics.font("small", bold=True))
            right = x - int(8 * metrics.scale)

    def _cloud_text(self, row):
        if row.cloud is True:
            return ("Steam Cloud", theme.ACCENT)
        if row.cloud is False:
            return ("no cloud", theme.WARN)
        return ("cloud unknown", theme.TEXT_FAINT)

    def _synced_text(self, row):
        """The newest backup the hub holds for this game, and whose it is."""
        if not self._backups_read:
            return ("...", theme.TEXT_FAINT)
        if not row.synced:
            return ("", theme.TEXT_FAINT)
        when, device = row.synced
        label = self.app.controller.settings.device_label(device)
        return ("%s  %s" % (backups.when_text(when), label), theme.GOOD)

    def _saves_text(self, row):
        # The tree named in the launch option is the only thing that tells the
        # engine what a non-Steam entry owns, so it belongs in the column, not
        # in a detail view. It is an emulator library or one emulator game.
        if row.tree:
            return (tree_text(row.tree, self.app.controller.settings.tree(row.tree)),
                    theme.ACCENT)
        if row.saves is True:
            return ("saves known", theme.TEXT_DIM)
        if row.saves is False:
            return ("no saves", theme.TEXT_FAINT)
        return ("saves unknown", theme.TEXT_FAINT)

    # ------------------------------------------------------------ data

    def on_show(self):
        if not self.library.rows:
            self.refresh()
            return
        self._update_summary()
        self.apply_filter()
        if not self._backups_read:
            # The controller loads the library before the window opens, so the
            # first showing never went through refresh and never asked the hub.
            self._read_backups()

    def reload_all(self):
        """Re-read Steam, because the uninstalled ones are not in memory.

        A game with no install folder is still in localconfig, which is how
        you can set one up before you install it, or keep one set up while it
        is uninstalled. The hub has not changed, so it is not asked again.
        """
        self.refresh(ask_hub=False)

    def refresh(self, ask_hub=True):
        self.status.configure(text="Reading Steam ...")
        self.update_idletasks()
        self.library.load(include_uninstalled=self.show_all.value)
        self._update_summary()
        self.apply_filter()
        self.status.configure(text="")
        if ask_hub or self._newest is None:
            self._read_backups()
        else:
            self.library.attach_backups(self._newest, key=getattr(self, "_hub_key", None))
            self.list.redraw()

    def _read_backups(self):
        """Ask ludusavi what the hub holds, off the main thread.

        It takes a couple of seconds per device, which is fine in the
        background and not fine in a redraw.
        """
        binary = paths.ludusavi_path()
        settings = self.app.controller.settings
        if storecheck.using_store(settings):
            threading.Thread(target=self._store_worker, args=(settings,),
                             daemon=True).start()
            return
        if not binary.is_file():
            self._backups_read = True
            self.list.redraw()
            return
        threading.Thread(target=self._backups_worker, args=(binary,),
                         daemon=True).start()

    def _store_worker(self, settings):
        """The store's newest save per game, from one listing."""
        try:
            newest, key = storecheck.newest_on_store(settings)
        except Exception:
            newest, key = {}, None
        self._hub_key = key
        try:
            self.app.after(0, self._backups_ready, newest)
        except Exception:
            pass

    def _backups_worker(self, binary):
        try:
            newest = backups.newest_everywhere(binary)
        except Exception:
            newest = {}
        try:
            self.app.after(0, self._backups_ready, newest)
        except Exception:
            # The window closed while ludusavi was still answering. Nothing
            # to hand the answer to, and nothing worth saying about it.
            pass

    def _backups_ready(self, newest):
        self._newest = newest
        matched = self.library.attach_backups(newest, key=getattr(self, "_hub_key", None))
        self._backups_read = True
        self.list.redraw()
        if newest and not matched:
            self.status.configure(text="no backups matched these games")
        if self.library.error:
            self.banner.show(self.library.error, "bad")
            self.banner.grid()
        else:
            self.banner.grid_remove()

    def _update_summary(self):
        counts = self.library.counts()
        self.summary.configure(
            text="%d games   %d on BlockSlot   %d covered by Steam Cloud"
                 % (counts["total"], counts["wrapped"], counts["cloud"]))

    def apply_filter(self):
        self.rows = self.library.visible(
            search=self.search.get(),
            hide_cloud=self.hide_cloud.value,
            only_wrapped=self.only_on.value,
            only_installed=not self.show_all.value)
        self.list.set_rows(self.rows)

    # ------------------------------------------------------------ actions

    def _select_all(self):
        """Tick everything shown, or untick it when it is all already ticked."""
        if len(self.list.selected_rows()) >= len(self.rows) and self.rows:
            self.list.clear_selection()
        else:
            self.list.select_all(self.rows)

    def _select_all_if_mine(self):
        if self.app.current is not self:
            return None
        if isinstance(self.focus_get(), tk.Entry):
            return None
        self._select_all()
        return "break"

    def _selection_changed(self, rows):
        """The buttons say what they would do, to how many, or why they cannot."""
        count = len(rows)
        can_add = sum(1 for row in rows if not row.syncing)
        can_remove = sum(1 for row in rows if row.syncing)
        self.add_button.set_enabled(can_add > 0)
        self.remove_button.set_enabled(can_remove > 0)
        self.add_button.configure_text(
            "Turn on sync for %d" % can_add if can_add > 1 else "Turn on sync")
        self.remove_button.configure_text(
            "Turn off sync for %d" % can_remove if can_remove > 1
            else "Turn off sync")
        if self.borderless_button is not None:
            self.borderless_button.set_enabled(count > 0)
            making = self._borderless_target(rows)
            self.borderless_button.configure_text(
                "Make borderless" if making else "Remove borderless")
        if not count:
            self.status.configure(text="Space ticks the game under the cursor")
        else:
            self.status.configure(text="%d picked" % count)

    def _show_details(self, row):
        lines = [
            "App id: %s" % row.appid,
            "Kind: %s" % ("non-Steam shortcut" if row.kind == "shortcut" else "Steam game"),
            "Installed: %s" % ("yes" if row.installed else "no"),
            "Steam Cloud: %s" % {True: "yes", False: "no", None: "unknown"}[row.cloud],
            "Known save files: %s" % {True: "yes", False: "no", None: "unknown"}[row.saves],
            "Save sync: %s" % ("on" if row.syncing else "off"),
            "Borderless: %s" % ("on" if row.borderless else "off"),
        ]
        if row.tree:
            lines.append("Emulator saves: %s" % row.tree)
        if row.synced:
            when, device = row.synced
            lines.append("Newest backup: %s on %s"
                         % (backups.when_text(when),
                            self.app.controller.settings.device_label(device)))
        if row.last_played:
            lines.append("Last played: %s" % played(row.last_played))
        if row.launch_options:
            lines.append("")
            lines.append("Launch options:")
            lines.append(row.launch_options)
        self.app.confirm(row.name, "\n".join(lines), ok_label="Close",
                         cancel_label="", kind="normal")

    def _apply(self, enable):
        rows = self.list.selected_rows()
        if not rows:
            return
        tree = None
        if enable:
            tree = self._tree_for(rows)
            if tree is False:
                return
        steam_changes, shortcut_changes = self.library.plan(
            [row.appid for row in rows], enable, tree=tree)
        verb = "Add to BlockSlot" if enable else "Remove from BlockSlot"
        self._confirm_and_write(rows, verb, steam_changes, shortcut_changes)

    @staticmethod
    def _borderless_target(rows):
        """True to make the picked games borderless, False to take it off.

        Any picked game that is not borderless yet means the button adds it,
        so a mixed pick ends up all borderless rather than all flipped.
        """
        return not rows or any(not row.borderless for row in rows)

    def _toggle_borderless(self):
        rows = self.list.selected_rows()
        if not rows:
            return
        enable = self._borderless_target(rows)
        steam_changes, shortcut_changes = self.library.plan_borderless(
            [row.appid for row in rows], enable)
        verb = "Make borderless" if enable else "Remove borderless"
        note = ("Set each game to windowed in its own options. BlockSlot "
                "takes the frame off the window and fits it to the screen. "
                "Saves are not touched unless sync is on as well.\n\n"
                if enable else "")
        self._confirm_and_write(rows, verb, steam_changes, shortcut_changes,
                                note=note)

    def _confirm_and_write(self, rows, verb, steam_changes, shortcut_changes,
                           note=""):
        if not steam_changes and not shortcut_changes:
            self.status.configure(text="Nothing to change")
            return
        names = ", ".join(row.name for row in rows[:6])
        if len(rows) > 6:
            names += " and %d more" % (len(rows) - 6)
        message = (
            "%s\n\n%s"
            "Steam has to close first. It keeps these settings in memory and "
            "writes them out when it exits, so an edit made while it is "
            "running would be thrown away.\n\n"
            "BlockSlot will close Steam, make the change, and start it again."
            % (names, note))
        if paths.engine_in_exe() and paths.in_temporary_place():
            message = ("Careful: BlockSlot is running from a temporary "
                       "folder, and these games will point at %s. Move "
                       "Blockslot.exe somewhere it can stay first.\n\n"
                       % paths.launch_program()) + message
        if not self.app.confirm(verb, message, ok_label="Close Steam and do it"):
            return
        panel = self.app.busy(verb, "Working ...")
        thread = threading.Thread(target=self._worker,
                                  args=(panel, steam_changes, shortcut_changes),
                                  daemon=True)
        thread.start()

    def _tree_for(self, rows):
        """Which save set a non-Steam shortcut belongs to.

        A Steam game tells savepick what it is through its app id. A non-Steam
        shortcut has no app id that means anything, so without a save set the
        wrap would start the game and sync nothing, quietly. Asking is the only
        honest option.
        """
        needs = [row for row in rows
                 if row.kind == "shortcut" and not row.tree]
        if not needs:
            return None
        names = self.app.controller.settings.tree_names()
        if not names:
            self.app.confirm(
                "Where are its saves?",
                "%s is not a Steam game, so BlockSlot cannot work out which "
                "saves belong to it.\n\n"
                "Add it on the Emulator games screen first, as an emulator "
                "library or one emulator game, with its save folder on each "
                "device."
                % needs[0].name,
                ok_label="Close", cancel_label="")
            return False
        chosen = self.app.choose(
            "Which saves does it own?",
            "%s is not a Steam game. Pick the emulator library or game "
            "whose saves it uses." % needs[0].name,
            [(name, name) for name in names])
        return chosen if chosen else False

    def _worker(self, panel, steam_changes, shortcut_changes):
        try:
            count = launchopts.with_steam_closed(
                self.library.root,
                steamdir.localconfig_path(self.library.root,
                                          self.library.user_id),
                lambda: self.library.apply(steam_changes, shortcut_changes),
                panel.say)
            panel.say("Changed %d game%s." % (count, "" if count == 1 else "s"))
        except launchopts.SteamStayedOpen as exc:
            panel.say(str(exc))
        except Exception as exc:
            panel.say("Stopped: %s" % exc)
        time.sleep(1.6)
        panel.close()
        self.app.after(0, self.refresh)
