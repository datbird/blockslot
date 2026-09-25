"""The activity screen: what the engine did, in its own words.

The log is the engine's only record of a launch, and it already says the things
that matter: which save won, whether a backup ran, whether the hub answered.
Showing it verbatim beats inventing a second history that could disagree.
"""

import time
import tkinter as tk

from ..core import backups, logview, paths
from . import app as app_mod
from . import theme, widgets

TAIL_LINES = 500
TAIL_BYTES = 256 * 1024
FOLLOW_MS = 1500


class ActivityScreen(app_mod.Screen):
    title = "Activity"
    hints = (("Up/Down", "scroll"), ("F5", "refresh"))

    def __init__(self, app):
        app_mod.Screen.__init__(self, app)
        self._following = True
        self._signature = None
        self._timer = None
        self._build()

    def _build(self):
        pad = self.metrics.pad
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = tk.Frame(self, bg=theme.BG)
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, 0))
        tk.Label(header, text="Activity", bg=theme.BG, fg=theme.TEXT,
                 anchor="w", font=self.metrics.font("huge", bold=True)
                 ).pack(side="left")
        self.where = tk.Label(header, text=str(paths.log_path()), bg=theme.BG,
                              fg=theme.TEXT_FAINT,
                              font=self.metrics.font("small"))
        self.where.pack(side="right")

        self.banner = widgets.Banner(self, self.metrics)
        self.banner.grid(row=1, column=0, sticky="ew", padx=pad, pady=pad)

        self.text = tk.Text(self, bg=theme.PANEL, fg=theme.TEXT, relief="flat",
                            font=self.metrics.mono("small"), wrap="none",
                            highlightthickness=0, padx=self.metrics.pad,
                            pady=self.metrics.pad, takefocus=1)
        self.text.grid(row=2, column=0, sticky="nsew", padx=pad)
        self.text.configure(state="disabled")
        # A Text widget scrolls on the arrow keys. Without these the window
        # would also move focus, and the log would jump away mid-read.
        self.text.bind("<Up>", lambda event: self._scroll(-2))
        self.text.bind("<Down>", lambda event: self._scroll(2))
        self.text.tag_configure("warn", foreground=theme.WARN)
        self.text.tag_configure("bad", foreground=theme.BAD)
        self.text.tag_configure("good", foreground=theme.GOOD)

        actions = tk.Frame(self, bg=theme.BG)
        actions.grid(row=3, column=0, sticky="ew", padx=pad, pady=pad)
        self.refresh_button = widgets.Button(actions, self.metrics, "Refresh",
                                             kind="primary", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.follow = widgets.Toggle(actions, self.metrics, "Follow", value=True,
                                     command=self._set_follow,
                                     width=int(150 * self.metrics.scale))
        self.follow.pack(side="left", padx=(self.metrics.gap * 2, 0))

    def focus_order(self):
        return [self.text, self.refresh_button, self.follow]

    def _scroll(self, lines):
        self.text.yview_scroll(lines, "units")
        return "break"

    # ------------------------------------------------------------ data

    def on_show(self):
        self.refresh()
        self._schedule()

    def on_hide(self):
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def _schedule(self):
        self._timer = self.after(FOLLOW_MS, self._tick)

    def _tick(self):
        if self._following:
            self.refresh(only_if_changed=True)
        self._schedule()

    def _set_follow(self, value):
        self._following = bool(value)

    def refresh(self, only_if_changed=False):
        path = paths.log_path()
        try:
            stat = path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            self.banner.show("No log yet. It appears the first time a game "
                             "starts through BlockSlot.", "warn")
            return
        if only_if_changed and signature == self._signature:
            return
        self._signature = signature
        try:
            lines = logview.tail(path, TAIL_LINES, TAIL_BYTES)
        except OSError as exc:
            self.banner.show("Could not read the log: %s" % exc, "bad")
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for line in lines:
            self.text.insert("end", line + "\n", logview.classify(line))
        self.text.configure(state="disabled")
        if self._following:
            self.text.see("end")
        self.banner.show("%d line%s, last written %s"
                         % (len(lines), "" if len(lines) == 1 else "s",
                            backups.ago_text(time.time() - stat.st_mtime)),
                         "info")
