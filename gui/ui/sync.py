"""The sync screen: is the hub reachable, and what is it waiting on."""

import threading
import tkinter as tk

from ..core import syncthing
from . import app as app_mod
from . import theme, widgets


class Check(object):
    """One line of the connection test."""

    def __init__(self, label, ok, detail):
        self.label = label
        self.ok = ok
        self.detail = detail
        self.appid = label


class SyncScreen(app_mod.Screen):
    title = "Sync"
    hints = (("Up/Down", "move"), ("Enter", "save and test"),
             ("LB/RB", "screens"), ("F5", "test again"))

    def __init__(self, app):
        app_mod.Screen.__init__(self, app)
        self.settings = app.controller.settings
        self._build()

    def _build(self):
        pad = self.metrics.pad
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)

        tk.Label(self, text="Sync", bg=theme.BG, fg=theme.TEXT, anchor="w",
                 font=self.metrics.font("huge", bold=True)
                 ).grid(row=0, column=0, columnspan=2, sticky="ew",
                        padx=pad, pady=(pad, 0))
        tk.Label(self, text="WHAT IS WORKING", bg=theme.BG,
                 fg=theme.TEXT_FAINT, anchor="w",
                 font=self.metrics.font("small", bold=True)
                 ).grid(row=2, column=0, sticky="ew", padx=(pad, pad // 2))
        tk.Label(self, text="THE SERVER TO USE", bg=theme.BG,
                 fg=theme.TEXT_FAINT, anchor="w",
                 font=self.metrics.font("small", bold=True)
                 ).grid(row=2, column=1, sticky="ew", padx=(pad // 2, pad))

        self.banner = widgets.Banner(self, self.metrics)
        self.banner.grid(row=1, column=0, columnspan=2, sticky="ew",
                         padx=pad, pady=pad)

        self.checks = widgets.ListView(self, self.metrics, self._render_check,
                                       selectable=False, interactive=False,
                                       row_height=int(self.metrics.row_height * 1.25))
        self.checks.grid(row=3, column=0, sticky="nsew", padx=(pad, pad // 2),
                         pady=(0, pad))

        form = tk.Frame(self, bg=theme.BG)
        form.grid(row=3, column=1, sticky="nsew", padx=(pad // 2, pad),
                  pady=(0, pad))
        block = self.settings.sync
        self.url = widgets.Field(form, self.metrics, "Syncthing address",
                                 block.get("url", "http://127.0.0.1:8384"))
        self.apikey = widgets.Field(form, self.metrics, "API key",
                                    block.get("apikey", ""), secret=True)
        self.folder = widgets.Field(form, self.metrics, "Folder id",
                                    block.get("folder", "gamesaves"))
        self.hub_name = widgets.Field(form, self.metrics, "Hub name",
                                      block.get("hub_name", ""))
        self.hub_id = widgets.Field(form, self.metrics, "Hub device id",
                                    block.get("hub_id", ""))
        self.device_dir = widgets.Field(
            form, self.metrics, "This device's folder inside the share",
            block.get("device_dir", ""))
        for field in (self.url, self.apikey, self.folder, self.hub_name,
                      self.hub_id, self.device_dir):
            field.pack(fill="x", pady=(0, self.metrics.gap))

        actions = tk.Frame(form, bg=theme.BG)
        actions.pack(fill="x", pady=(self.metrics.gap, 0))
        self.save_button = widgets.Button(actions, self.metrics,
                                          "Save and test", kind="primary",
                                          command=self.save)
        self.save_button.pack(side="left")
        self.find_button = widgets.Button(actions, self.metrics,
                                          "Fill in from Syncthing",
                                          command=self.find)
        self.find_button.pack(side="left", padx=(self.metrics.gap, 0))
        self.status = tk.Label(actions, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                               font=self.metrics.font("small"))
        self.status.pack(side="right")
        self.test_button = self.save_button

    def focus_order(self):
        return [self.url, self.apikey, self.folder, self.hub_name,
                self.hub_id, self.device_dir, self.save_button,
                self.find_button]

    def _render_check(self, canvas, row, y, width, height, is_cursor):
        """A check is a fact, so it is drawn on two lines and never as a menu."""
        middle = y + height / 2
        dot = int(9 * self.metrics.scale)
        colour = theme.GOOD if row.ok else theme.BAD
        canvas.create_oval(self.metrics.pad, middle - dot / 2,
                           self.metrics.pad + dot, middle + dot / 2,
                           fill=colour, outline="")
        left = self.metrics.pad * 2.4
        canvas.create_text(left, middle - self.metrics.small * 0.85, anchor="w",
                           text=row.label, fill=theme.TEXT,
                           font=self.metrics.font(bold=True))
        spec = self.metrics.font("small")
        canvas.create_text(left, middle + self.metrics.small * 0.95, anchor="w",
                           text=widgets.elide(row.detail or "", spec,
                                              width - left - self.metrics.pad,
                                              keep="start"),
                           fill=theme.TEXT_DIM, font=spec)

    # ------------------------------------------------------------ actions

    def current_config(self):
        return {
            "url": self.url.get().strip(),
            "apikey": self.apikey.get().strip(),
            "folder": self.folder.get().strip(),
            "hub_id": self.hub_id.get().strip(),
            "hub_name": self.hub_name.get().strip(),
            "device_dir": self.device_dir.get().strip(),
        }

    def on_show(self):
        if not self.checks.rows:
            # A first run has nothing to test. Read Syncthing's own config
            # instead, which is the step people get wrong by hand.
            if not self.apikey.get().strip():
                self.find()
            self.test()

    def refresh(self):
        self.test()

    def test(self):
        self.status.configure(text="Testing ...")
        config = self.current_config()
        thread = threading.Thread(target=self._test_worker, args=(config,),
                                  daemon=True)
        thread.start()

    def _test_worker(self, config):
        steps = syncthing.check(config)
        try:
            self.app.after(0, self._show_checks, steps)
        except RuntimeError:
            # The window closed while Syncthing was still answering. Nothing
            # to show the answer in.
            pass

    def _show_checks(self, steps):
        rows = [Check(label, ok, detail) for label, ok, detail in steps]
        self.checks.set_rows(rows, keep_cursor=False)
        self.status.configure(text="")
        if not rows:
            self.banner.show("Nothing to test yet.", "warn")
        elif all(row.ok for row in rows):
            self.banner.show("Sync is working.", "good")
        else:
            first = next(row for row in rows if not row.ok)
            self.banner.show("%s: %s" % (first.label, first.detail), "bad")

    def find(self):
        found = syncthing.read_local_config()
        if not found:
            self.banner.show("No Syncthing config found on this machine.", "warn")
            return
        self.url.set(found["url"])
        if found.get("apikey"):
            self.apikey.set(found["apikey"])
        folders = found.get("folders") or []
        if folders and not self.folder.get().strip():
            self.folder.set(folders[0]["id"])
        self.banner.show("Read %s" % found["path"], "good")

    def save(self):
        self.settings.set_sync(**self.current_config())
        if self.write_settings("Saved to %s" % self.settings.path):
            self.test()
