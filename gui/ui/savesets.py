"""Emulator games: the saves that do not come from Steam.

A Steam game tells the engine what it is through its app id. An emulator has
to be told, and a tree in savepick.json is how: a name, and the folder that
holds its saves on each device. Each device fills in its own folder, so nobody
types another machine's paths.

Every game is one save. A tree is either an emulator LIBRARY, a folder that
holds many games (RetroBat's or RetroDECK's saves folder, split into one save
per game by the engine), or one emulator GAME ("one_game", such as Bloodborne
in shadPS4). With a store set up, the right side lists the games the store
holds for the library picked on the left, each labelled with the emulator that
owns its save, and a game two devices both played can be settled here.

The module keeps its old name, and the window its "sets" key, so nothing that
imports it has to change.
"""

import threading
import tkinter as tk
from pathlib import Path

from ..core import backups, settings as settings_mod, storecheck
from . import app as app_mod
from . import theme, widgets

TITLE = "Emulator games"

# What a new tree is. A library's folder mixes battery saves with save states
# and screenshots, which the engine's default allow list sorts out. One game's
# own folder needs every file, because a console save often has no extension
# at all and an allow list would refuse the lot.
KIND_LIBRARY = "library"
KIND_GAME = "game"

NO_UPLOADER = "The uploader is not running"


# ------------------------------------------------------------ pure parts


def kind_word(tree):
    """What a tree is, in the words the screen uses."""
    return "Emulator game" if (tree or {}).get("one_game") else "Emulator library"


def saved_text(when, device, device_label, now=None):
    """"saved 2d ago from Deck", or as much of it as is known."""
    ago = backups.when_text(when, now=now) if when else ""
    who = device_label(device) if device else ""
    if ago and who:
        return "saved %s from %s" % (ago, who)
    if ago:
        return "saved %s" % ago
    if who:
        return "saved from %s" % who
    return ""


def fork_marker(heads):
    """The mark on a game two devices both played and nobody chose between."""
    if heads == 2:
        return "two saves"
    if heads > 2:
        return "%d saves" % heads
    return ""


class GameRow(object):
    """One game of a library, as Daemon.library_list returns it."""

    def __init__(self, entry):
        self.name = entry.get("name") or ""
        # The list keys rows by appid, and the unit name is unique per library.
        self.appid = self.name
        self.title = entry.get("title") or self.name
        self.label = entry.get("label") or ""
        self.when = entry.get("when")
        self.device = entry.get("device") or ""
        self.heads = int(entry.get("heads") or 1)
        self.choices = list(entry.get("choices") or [])


def row_texts(row, device_label, now=None):
    """(title, label, saved, marker) for one game row."""
    return (row.title, row.label,
            saved_text(row.when, row.device, device_label, now=now),
            fork_marker(row.heads))


def filter_games(rows, search):
    """The rows whose title holds every word typed, in any case."""
    words = (search or "").casefold().split()
    if not words:
        return list(rows)
    return [row for row in rows
            if all(word in row.title.casefold() for word in words)]


def choice_options(choices, device_label, now=None):
    """[(button text, snapshot id)] for a fork, newest save first."""
    ordered = sorted(choices, key=lambda c: c.get("when") or "", reverse=True)
    options = []
    for choice in ordered:
        who = device_label(choice.get("device")) if choice.get("device") else "another device"
        ago = backups.when_text(choice.get("when"), now=now)
        text = "The %s save, %s" % (who, ago) if ago else "The %s save" % who
        options.append((text, choice.get("id")))
    return options


def open_worker(settings):
    """(worker, running) for reading and settling a library.

    The running uploader is asked first: it holds the manifest cache, and a
    choice made through it uploads at once. When none answers, the same Daemon
    is built in this process on the same state folder. Reading works the same
    way, and a choice is queued on disk for the uploader to send when it next
    runs, which is why the caller is told which one it got.
    """
    slotd = settings_mod.engine_module("slotd")
    state_dir = storecheck.daemon_state_dir(settings)
    client = slotd._client_from_info(state_dir)
    if client is not None:
        return client, True
    worker = slotd.daemon_from_settings(settings.store_for_engine(),
                                        settings.store_device(),
                                        state_dir=state_dir)
    return worker, False


class SetRow(object):
    def __init__(self, name, root, devices, every_file, tree=None):
        self.name = name
        self.root = root
        self.devices = devices
        self.every_file = every_file
        self.tree = tree or {}
        self.appid = name


# ------------------------------------------------------------ the screen


class SaveSetsScreen(app_mod.Screen):
    title = TITLE
    hints = (("Up/Down", "pick"), ("Right", "its settings"),
             ("Enter", "settle two saves"), ("LB/RB", "screens"),
             ("F5", "refresh"))

    def __init__(self, app):
        app_mod.Screen.__init__(self, app)
        self.settings = app.controller.settings
        # {library: [GameRow]} read this session. The first read of a big
        # library fetches every manifest and takes about a minute; the store
        # does not change under a person looking at this screen.
        self._games = {}
        self._loading = set()
        self._running = None
        self._build()

    def _build(self):
        pad = self.metrics.pad
        self.columnconfigure(0, weight=2, uniform="cols")
        self.columnconfigure(1, weight=3, uniform="cols")
        self.rowconfigure(2, weight=1)

        header = tk.Frame(self, bg=theme.BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=pad,
                    pady=(pad, 0))
        tk.Label(header, text=TITLE, bg=theme.BG, fg=theme.TEXT,
                 anchor="w", font=self.metrics.font("huge", bold=True)
                 ).pack(side="left")
        self.add_button = widgets.Button(header, self.metrics, "Add one",
                                         kind="primary", command=self.add)
        self.add_button.pack(side="right")

        self.banner = widgets.Banner(self, self.metrics)
        self.banner.grid(row=1, column=0, columnspan=2, sticky="ew", padx=pad,
                         pady=pad)

        self.list = widgets.ListView(self, self.metrics, self._render,
                                     selectable=False, chevrons=True,
                                     row_height=int(self.metrics.row_height * 1.25),
                                     on_cursor=self._select,
                                     on_activate=lambda row: self._into_detail())
        self.list.grid(row=2, column=0, sticky="nsew", padx=(pad, pad // 2),
                       pady=(0, pad))

        side = tk.Frame(self, bg=theme.BG)
        side.grid(row=2, column=1, sticky="nsew", padx=(pad // 2, pad),
                  pady=(0, pad))
        self.detail = widgets.Detail(side, self.metrics)
        self.detail.pack(side="top", fill="x")

        # The games of the picked library. Only with a store: Syncthing
        # carries a library as one folder and has no games to list.
        self.games_panel = tk.Frame(side, bg=theme.BG)
        controls = tk.Frame(self.games_panel, bg=theme.BG)
        controls.pack(side="top", fill="x", pady=(pad, pad // 2))
        self.search = widgets.Field(controls, self.metrics, "Search games",
                                    width=18,
                                    on_change=lambda value: self.apply_filter())
        self.search.pack(side="left")
        self.status = tk.Label(controls, text="", bg=theme.BG,
                               fg=theme.TEXT_DIM, anchor="e",
                               font=self.metrics.font("small"))
        self.status.pack(side="right", anchor="s")
        self.games = widgets.ListView(self.games_panel, self.metrics,
                                      self._render_game, selectable=False,
                                      row_height=int(self.metrics.row_height * 1.25),
                                      on_activate=self._settle)
        self.games.pack(side="top", fill="both", expand=True)

    def focus_order(self):
        order = [self.list] + self.detail.focus_order()
        if self.games_panel.winfo_manager():
            order += [self.search, self.games]
        return order + [self.add_button]

    def _into_detail(self):
        order = self.detail.focus_order()
        if order:
            self.app._focus(order[0])

    # ------------------------------------------------------------ the list

    def _render(self, canvas, row, y, width, height, is_cursor):
        middle = y + height / 2
        left = self.metrics.pad
        canvas.create_text(left, middle - self.metrics.small * 0.85, anchor="w",
                           text=row.name, fill=theme.TEXT,
                           font=self.metrics.font(bold=is_cursor))
        spec = self.metrics.font("small")
        kind = "Game" if row.tree.get("one_game") else "Library"
        if row.root:
            text, colour = "%s   %s" % (kind, row.root), theme.TEXT_DIM
        else:
            text, colour = "%s   no folder here yet" % kind, theme.WARN
        canvas.create_text(left, middle + self.metrics.small * 0.95, anchor="w",
                           text=widgets.elide(text, spec,
                                              width - left * 2),
                           fill=colour, font=spec)

    def _render_game(self, canvas, row, y, width, height, is_cursor):
        title, label, saved, marker = row_texts(row, self.settings.device_label)
        middle = y + height / 2
        left = self.metrics.pad
        right = width - left
        spec = self.metrics.font("small")
        if marker:
            pill_h = int(22 * self.metrics.scale)
            pill_w = widgets.width_of(marker, spec) + int(18 * self.metrics.scale)
            x = right - pill_w
            top = middle - self.metrics.small * 0.85 - pill_h / 2
            widgets.round_rect(canvas, x, top, right, top + pill_h, pill_h / 2,
                               fill=theme.WARN, outline="")
            canvas.create_text(x + pill_w / 2, top + pill_h / 2, text=marker,
                               fill=theme.BG,
                               font=self.metrics.font("small", bold=True))
            right = x - int(8 * self.metrics.scale)
        title_spec = self.metrics.font(bold=is_cursor)
        canvas.create_text(left, middle - self.metrics.small * 0.85, anchor="w",
                           text=widgets.elide(title, title_spec, right - left),
                           fill=theme.TEXT, font=title_spec)
        second = "   ".join(part for part in (label, saved) if part)
        canvas.create_text(left, middle + self.metrics.small * 0.95, anchor="w",
                           text=widgets.elide(second, spec, width - left * 2),
                           fill=theme.TEXT_DIM, font=spec)

    def on_show(self):
        self._load()

    def refresh(self):
        """F5: re-read the settings, and the picked library's games afresh."""
        row = self.list.current()
        if row is not None:
            self._games.pop(row.name, None)
        self._load()

    def _load(self):
        here = self.settings.device_dir()
        rows = []
        for name, root, devices, every_file in self.settings.tree_rows():
            rows.append(SetRow(name, root, devices, every_file,
                               self.settings.tree(name)))
        self.list.set_rows(rows)
        if not rows:
            self.detail.show(
                TITLE, "None yet",
                note="Add an emulator here: a frontend such as RetroBat or "
                     "RetroDECK that holds many games, or one game such as "
                     "Bloodborne in shadPS4. Steam games need nothing here.",
                actions=[("Add one", self.add)])
            self.banner.show("Nothing here yet, and that is fine if every "
                             "game you sync comes from Steam.", "info")
        elif not here:
            self.banner.show("Set this device's folder on the Sync screen "
                             "before pointing an emulator at a folder.", "warn")
        else:
            missing = [row.name for row in rows if not row.root]
            if missing:
                self.banner.show("No folder set here for %s."
                                 % ", ".join(missing), "warn")
            else:
                self.banner.show("Every emulator knows its folder on this "
                                 "device.", "good")

    def _select(self, row):
        if row is None:
            self.games_panel.pack_forget()
            return
        here = self.settings.device_dir()
        tree = self.settings.tree(row.name)
        roots = tree.get("roots") or {}
        lines = []
        for device in sorted(roots):
            label = self.settings.device_label(device)
            mark = "this device" if device == here else label
            # Name on one line, path on the next. Padding does not align in a
            # proportional font and a long path wraps anyway.
            lines.append("%s\n    %s" % (mark, roots[device]))
        if not roots:
            lines.append("no device has a folder for this yet")
        state = ("Ready on this device." if row.root
                 else "This device has no folder for it.",
                 theme.GOOD if row.root else theme.WARN)
        if tree.get("one_game"):
            note = "One game. Every file in this folder is its save."
            label = tree.get("label")
            if label:
                note += " Its save belongs to %s." % label
        else:
            note = ("Many games. Each game in this folder is its own save, "
                    "with its own history.")
        self.detail.show(
            kind_word(tree), row.name, state=state, lines=lines, note=note,
            actions=[("Set the folder here", self.set_root),
                     ("Remove", self.drop, "danger")])
        self._show_games(row.name)

    # ------------------------------------------------------------ the games

    def _show_games(self, library):
        if not storecheck.using_store(self.settings):
            self.games_panel.pack_forget()
            return
        if not self.games_panel.winfo_manager():
            self.games_panel.pack(side="top", fill="both", expand=True)
        if library in self._games:
            self.apply_filter()
            self._say_count()
            return
        self.games.set_rows([])
        self.status.configure(text="Reading the store ...")
        if library in self._loading:
            return
        self._loading.add(library)
        threading.Thread(target=self._read_worker, args=(library,),
                         daemon=True).start()

    def _later(self, callback, *args):
        """Hand a worker's answer to the Tk thread. The window may be gone."""
        try:
            self.app.after(0, callback, *args)
        except (RuntimeError, tk.TclError):
            pass

    def _read_worker(self, library):
        try:
            worker, running = open_worker(self.settings)
            answer = worker.library_list(library)
            rows = [GameRow(entry) for entry in answer.get("games") or []]
            error = None
        except Exception as exc:
            rows, running, error = None, None, storecheck.explain(exc)
        self._later(self._games_ready, library, rows, running, error)

    def _games_ready(self, library, rows, running, error):
        self._loading.discard(library)
        if rows is not None:
            self._games[library] = rows
            self._running = running
        current = self.list.current()
        if current is None or current.name != library:
            return
        if error:
            self.games.set_rows([])
            self.status.configure(text="")
            self.banner.show("Could not read %s: %s" % (library, error), "bad")
            return
        self.apply_filter()
        self._say_count()

    def _say_count(self):
        row = self.list.current()
        rows = self._games.get(row.name, []) if row else []
        forks = sum(1 for game in rows if game.heads > 1)
        text = "%d game%s" % (len(rows), "" if len(rows) == 1 else "s")
        if forks:
            text += ", %d with two saves" % forks
        if self._running is False:
            text += ". %s." % NO_UPLOADER
        self.status.configure(text=text)

    def apply_filter(self):
        row = self.list.current()
        rows = self._games.get(row.name, []) if row else []
        self.games.set_rows(filter_games(rows, self.search.get()))

    def _settle(self, game):
        """Two devices both played it: the person picks the save to keep."""
        if game.heads < 2 or not game.choices:
            self.banner.show("%s has one save. Nothing to settle." % game.title,
                             "info")
            return
        picked = self.app.choose(
            "Which save of %s?" % game.title,
            "Two devices both played it. The one you pick becomes the save "
            "on every device. The other stays in its history.",
            choice_options(game.choices, self.settings.device_label))
        if not picked:
            return
        library = self.list.current().name
        self.status.configure(text="Saving the choice ...")
        threading.Thread(target=self._choose_worker,
                         args=(library, game, picked), daemon=True).start()

    def _choose_worker(self, library, game, snap_id):
        try:
            worker, running = open_worker(self.settings)
            worker.choose(game.name, snap_id)
            error = None
        except Exception as exc:
            running, error = None, storecheck.explain(exc)
        self._later(self._chosen, library, game, running, error)

    def _chosen(self, library, game, running, error):
        if error:
            self.status.configure(text="")
            self.banner.show("Could not settle %s: %s" % (game.title, error),
                             "bad")
            return
        if running:
            self.banner.show("Settled %s." % game.title, "good")
        else:
            self.banner.show("Settled %s. %s, so it goes up when it next "
                             "starts." % (game.title, NO_UPLOADER), "warn")
        self._games.pop(library, None)
        current = self.list.current()
        if current is not None and current.name == library:
            self._show_games(library)

    # ------------------------------------------------------------ actions

    def add(self):
        kind = self.app.choose(
            "What are you adding?",
            "A library holds many games in one saves folder. One game has a "
            "folder of its own.",
            [("A library, such as RetroBat or RetroDECK", KIND_LIBRARY),
             ("One game, such as Bloodborne in shadPS4", KIND_GAME)])
        if not kind:
            return
        if kind == KIND_LIBRARY:
            name = self.app.ask_text(
                "Name the library",
                "The same name is used on every device, so keep it short and "
                "plain.", hint="Name")
        else:
            name = self.app.ask_text(
                "Name the game",
                "The same name is used on every device.", hint="Game")
        if not name:
            return
        name = name.strip()
        if name in self.settings.trees:
            self.banner.show("There is already one called %s." % name, "warn")
            return
        extra = {}
        if kind == KIND_GAME:
            system = self.app.ask_text(
                "Which system is %s for?" % name,
                "The short name, such as ps4, gc or psp.", hint="System")
            if not system:
                return
            label = self.app.ask_text(
                "Which emulator runs %s?" % name,
                "Its name as you would say it, such as shadPS4.",
                hint="Emulator")
            if not label:
                return
            extra = {"one_game": name, "system": system.strip().lower(),
                     "label": label.strip()}
        try:
            self.settings.add_tree(name, every_file=kind == KIND_GAME)
        except ValueError as exc:
            self.banner.show(str(exc), "warn")
            return
        if extra:
            tree = dict(self.settings.tree(name))
            tree.update(extra)
            self.settings.set_tree(name, tree)
        if self.write_settings("Added %s." % name):
            self._load()
            self.set_root(name)

    def set_root(self, name=None):
        row = self.list.current()
        name = name or (row.name if row else None)
        if not name:
            return
        here = self.settings.device_dir()
        if not here:
            self.banner.show("Set this device's folder on the Sync screen "
                             "first, so BlockSlot knows which device this is.",
                             "warn")
            return
        roots = self.settings.tree(name).get("roots") or {}
        folder = self.app.ask_text(
            "Where does %s keep its saves here?" % name,
            "The full path to the save folder on THIS device.",
            initial=roots.get(here, ""), hint="Folder")
        if not folder:
            return
        folder = folder.strip().rstrip("/\\")
        if not Path(folder).is_dir():
            if not self.app.confirm(
                    "That folder is not there",
                    "%s does not exist on this device.\n\n"
                    "Save it anyway? A folder that is not there means nothing "
                    "is carried for this device." % folder,
                    ok_label="Save it anyway"):
                return
        self.settings.set_tree_root(name, folder)
        if self.write_settings("%s now points at %s." % (name, folder)):
            self._load()

    def drop(self):
        row = self.list.current()
        if row is None:
            return
        if not self.app.confirm(
                "Remove %s?" % row.name,
                "This removes it from this device's settings. No save file "
                "is touched and no other device changes.\n\n"
                "Any game set up with it will launch and sync nothing until "
                "it is given another one.",
                ok_label="Remove it", kind="danger"):
            return
        self.settings.remove_tree(row.name)
        if self.write_settings("Removed %s." % row.name):
            self._load()
