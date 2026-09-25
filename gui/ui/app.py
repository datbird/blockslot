"""The window: a nav rail, one screen at a time, and a footer of hints.

Focus is the whole design here. In Game Mode there is no pointer, so every
screen publishes the order its controls should be reached in, and the window
moves between them. A list keeps its own up and down, and hands focus on when
the cursor runs off the end.
"""

import tkinter as tk
import tkinter.font as tkfont

from . import theme, widgets

TITLE = "BlockSlot"


class NavRail(tk.Canvas):
    """The left hand list of screens, drawn rather than packed with buttons."""

    def __init__(self, parent, metrics, items, on_select):
        self.metrics = metrics
        self.items = items
        self.on_select = on_select
        self.active = items[0][0] if items else None
        self.cursor = 0
        tk.Canvas.__init__(self, parent, width=metrics.nav_width, bg=theme.PANEL,
                           highlightthickness=0, bd=0, takefocus=1)
        self.bind("<Configure>", lambda event: self.redraw())
        self.bind("<FocusIn>", lambda event: self.redraw())
        self.bind("<FocusOut>", lambda event: self.redraw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Up>", lambda event: self.move(-1))
        self.bind("<Down>", lambda event: self.move(1))
        self.bind("<Return>", lambda event: self.choose(self.cursor))
        self.bind("<space>", lambda event: self.choose(self.cursor))

    def move(self, delta):
        self.cursor = max(0, min(self.cursor + delta, len(self.items) - 1))
        self.choose(self.cursor)
        return "break"

    def choose(self, index):
        self.cursor = index
        key = self.items[index][0]
        self.active = key
        self.redraw()
        self.on_select(key)
        return "break"

    def select(self, key):
        for index, (item_key, _label) in enumerate(self.items):
            if item_key == key:
                self.cursor = index
                self.active = key
                break
        self.redraw()

    def _on_click(self, event):
        index = int((event.y - self._top()) // self._item_height())
        if 0 <= index < len(self.items):
            self.focus_set()
            self.choose(index)
        return "break"

    def _item_height(self):
        return int(self.metrics.row_height * 1.18)

    def _top(self):
        return int(self.metrics.pad * 5.5)

    def redraw(self):
        self.delete("all")
        width = self.metrics.nav_width
        height = int(self.winfo_height() or 800)
        self.create_rectangle(0, 0, width, height, fill=theme.PANEL, outline="")
        self.create_text(self.metrics.pad, self.metrics.pad * 1.8, anchor="w",
                         text=TITLE, fill=theme.TEXT,
                         font=self.metrics.font("large", bold=True))
        self.create_text(self.metrics.pad, self.metrics.pad * 3.5, anchor="w",
                         text="save sync", fill=theme.TEXT_FAINT,
                         font=self.metrics.font("small"))
        focused = self.focus_get() is self
        item_height = self._item_height()
        y = self._top()
        for index, (key, label) in enumerate(self.items):
            active = key == self.active
            if active:
                widgets.round_rect(self, self.metrics.gap, y + 2,
                                   width - self.metrics.gap, y + item_height - 2,
                                   self.metrics.radius, fill=theme.PANEL_HI,
                                   outline="")
                self.create_rectangle(self.metrics.gap, y + 2,
                                      self.metrics.gap + int(4 * self.metrics.scale),
                                      y + item_height - 2,
                                      fill=theme.ACCENT, outline="")
            if focused and index == self.cursor:
                widgets.round_rect(self, self.metrics.gap, y + 2,
                                   width - self.metrics.gap, y + item_height - 2,
                                   self.metrics.radius, fill="",
                                   outline=theme.FOCUS, width=2)
            self.create_text(self.metrics.pad * 2.2, y + item_height / 2,
                             anchor="w", text=label,
                             fill=theme.TEXT if active else theme.TEXT_DIM,
                             font=self.metrics.font(bold=active))
            y += item_height


class Footer(tk.Canvas):
    """The hint line. In Game Mode it is the only manual anyone reads."""

    def __init__(self, parent, metrics):
        self.metrics = metrics
        self.hints = []
        tk.Canvas.__init__(self, parent, height=int(34 * metrics.scale),
                           bg=theme.PANEL, highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda event: self.redraw())

    def set_hints(self, hints):
        self.hints = hints
        self.redraw()

    def redraw(self):
        self.delete("all")
        width = int(self.winfo_width() or 1000)
        height = int(self.winfo_height() or 34)
        self.create_rectangle(0, 0, width, height, fill=theme.PANEL, outline="")
        x = self.metrics.pad
        for key, label in self.hints:
            box = self.create_text(x, height / 2, anchor="w", text=key,
                                   fill=theme.ACCENT,
                                   font=self.metrics.font("small", bold=True))
            x = self.bbox(box)[2] + int(6 * self.metrics.scale)
            text = self.create_text(x, height / 2, anchor="w", text=label,
                                    fill=theme.TEXT_DIM,
                                    font=self.metrics.font("small"))
            x = self.bbox(text)[2] + self.metrics.pad * 1.6


class Screen(tk.Frame):
    """One page. Subclasses fill `body` and list their controls in order."""

    title = ""
    hints = ()

    def __init__(self, app):
        tk.Frame.__init__(self, app.content, bg=theme.BG)
        self.app = app
        self.metrics = app.metrics

    def focus_order(self):
        return []

    def on_show(self):
        pass

    def on_hide(self):
        pass

    def write_settings(self, message):
        """Save the settings file and say so on this screen's banner."""
        try:
            self.settings.save()
        except OSError as exc:
            self.banner.show("Could not save: %s" % exc, "bad")
            return False
        self.banner.show(message, "good")
        return True


class App(tk.Tk):
    def __init__(self, controller, fullscreen=False, size=(1280, 800)):
        tk.Tk.__init__(self)
        self.controller = controller
        self.title(TITLE)
        self.configure(bg=theme.BG)
        self._set_icon()
        width, height = size
        if fullscreen:
            width = self.winfo_screenwidth()
            height = self.winfo_screenheight()
        self.geometry("%dx%d" % (width, height))
        self.minsize(960, 600)
        theme.pick_fonts(tkfont.families(self))
        self.metrics = theme.Metrics(height, width)
        if fullscreen:
            try:
                self.attributes("-fullscreen", True)
            except tk.TclError:
                pass

        self.screens = {}
        self.current = None
        self.nav = None
        self.content = tk.Frame(self, bg=theme.BG)
        self.footer = Footer(self, self.metrics)
        self._build_layout()
        self._bind_keys()

    def _set_icon(self):
        """The window and taskbar icon. A missing file leaves Tk's default."""
        from ..core import paths
        try:
            ico = paths.resource("assets/blockslot.ico")
            if ico and paths.is_windows():
                # The .ico carries every size, so Windows picks a sharp one
                # for the title bar and another for the taskbar.
                self.iconbitmap(default=str(ico))
                return
            png = paths.resource("assets/blockslot.png")
            if png:
                self._icon_image = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_image)
        except (tk.TclError, OSError):
            pass

    # ------------------------------------------------------------ layout

    def _build_layout(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.footer.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    def add_screens(self, items):
        """items: [(key, label, screen_factory)] in nav order."""
        nav_items = [(key, label) for key, label, _factory in items]
        self.nav = NavRail(self, self.metrics, nav_items, self.show)
        self.nav.grid(row=0, column=0, sticky="ns")
        for key, _label, factory in items:
            screen = factory(self)
            self.screens[key] = screen
            screen.grid(row=0, column=0, sticky="nsew")
            screen.grid_remove()
        if items:
            self.show(items[0][0])

    def show(self, key):
        screen = self.screens.get(key)
        if screen is None or screen is self.current:
            if screen is not None:
                self.focus_first()
            return
        if self.current is not None:
            self.current.on_hide()
            self.current.grid_remove()
        self.current = screen
        screen.grid()
        if self.nav:
            self.nav.select(key)
        self.footer.set_hints(list(screen.hints))
        screen.on_show()
        self.focus_first()

    def focus_first(self):
        order = self.current.focus_order() if self.current else []
        if order:
            self._focus(order[0])

    def _focus(self, widget):
        target = getattr(widget, "focus_widget", None)
        if callable(target):
            widget = target()
        try:
            widget.focus_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ keys

    def _bind_keys(self):
        self.bind_all("<Escape>", self._on_escape)
        self.bind_all("<Up>", lambda event: self._move_focus(-1))
        self.bind_all("<Down>", lambda event: self._move_focus(1))
        self.bind_all("<Left>", self._on_left)
        self.bind_all("<Right>", self._on_right)
        self.bind_all("<Tab>", lambda event: self._move_focus(1))
        self.bind_all("<Shift-Tab>", lambda event: self._move_focus(-1))
        self.bind_all("<Prior>", lambda event: self.step_screen(-1))
        self.bind_all("<Next>", lambda event: self.step_screen(1))
        self.bind_all("<F5>", lambda event: self.refresh())
        self.bind_all("<F11>", lambda event: self.toggle_fullscreen())
        self.bind_all("<<ListEdge>>", lambda event: self._move_focus(-1))
        self.bind_all("<<ListEdgeDown>>", lambda event: self._move_focus(1))

    def _order(self):
        return self.current.focus_order() if self.current else []

    def _current_index(self, order):
        focused = self.focus_get()
        for index, widget in enumerate(order):
            target = getattr(widget, "focus_widget", None)
            real = target() if callable(target) else widget
            if real is focused or widget is focused:
                return index
            inner = getattr(widget, "canvas", None)
            if inner is not None and inner is focused:
                return index
        return -1

    def _move_focus(self, delta):
        order = self._order()
        if not order:
            return "break"
        index = self._current_index(order)
        if index < 0:
            self._focus(order[0])
            return "break"
        self._focus(order[(index + delta) % len(order)])
        return "break"

    def _on_left(self, _event):
        if isinstance(self.focus_get(), tk.Entry):
            return None
        if self.nav:
            self.nav.focus_set()
        return "break"

    def _on_right(self, _event):
        if isinstance(self.focus_get(), tk.Entry):
            return None
        if self.focus_get() is self.nav:
            self.focus_first()
            return "break"
        return None

    def _on_escape(self, _event):
        if self.nav and self.focus_get() is not self.nav:
            self.nav.focus_set()
            return "break"
        self.quit_app()
        return "break"

    # ------------------------------------------------------------ actions

    def step_screen(self, delta):
        """The shoulder buttons move between screens, as they do in Steam."""
        if not self.nav or not self.nav.items:
            return "break"
        index = (self.nav.cursor + delta) % len(self.nav.items)
        self.nav.cursor = index
        self.show(self.nav.items[index][0])
        return "break"

    def refresh(self):
        if self.current is not None and hasattr(self.current, "refresh"):
            self.current.refresh()
        return "break"

    def toggle_fullscreen(self):
        try:
            self.attributes("-fullscreen", not self.attributes("-fullscreen"))
        except tk.TclError:
            pass
        return "break"

    def quit_app(self):
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------ overlays

    def _overlay(self, title, message):
        """A panel over the whole window. Not a Toplevel on purpose.

        gamescope gives a second window its own surface and it can land behind
        the first one, where nobody can answer it. A frame inside the window
        cannot go anywhere.
        """
        frame = tk.Frame(self, bg=theme.BG)
        frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel = tk.Frame(frame, bg=theme.PANEL, highlightthickness=1,
                         highlightbackground=theme.LINE)
        panel.place(relx=0.5, rely=0.5, anchor="center",
                    relwidth=0.66, relheight=0.56)
        tk.Label(panel, text=title, bg=theme.PANEL, fg=theme.TEXT,
                 font=self.metrics.font("large", bold=True), anchor="w"
                 ).pack(fill="x", padx=self.metrics.pad * 2,
                        pady=(self.metrics.pad * 1.5, 0))
        body = tk.Label(panel, text=message, bg=theme.PANEL, fg=theme.TEXT_DIM,
                        font=self.metrics.font(), anchor="w", justify="left",
                        wraplength=int(self.winfo_width() * 0.58) or 600)
        body.pack(fill="x", padx=self.metrics.pad * 2, pady=self.metrics.pad)
        return frame, panel, body

    def confirm(self, title, message, ok_label="Do it", cancel_label="Cancel",
                kind="primary"):
        """Ask, and wait for the answer. True when the person said yes."""
        answer = tk.IntVar(value=-1)
        frame, panel, _body = self._overlay(title, message)
        row = tk.Frame(panel, bg=theme.PANEL)
        row.pack(side="bottom", fill="x", padx=self.metrics.pad * 2,
                 pady=self.metrics.pad * 1.5)
        ok = widgets.Button(row, self.metrics, ok_label, kind=kind,
                            command=lambda: answer.set(1))
        order = [ok]
        if cancel_label:
            cancel = widgets.Button(row, self.metrics, cancel_label,
                                    command=lambda: answer.set(0))
            cancel.pack(side="right", padx=(self.metrics.gap, 0))
            order.append(cancel)
        ok.pack(side="right")
        frame.bind_all("<Escape>", lambda event: answer.set(0))
        for index, button in enumerate(order):
            button.bind("<Left>",
                        lambda event, i=index: order[(i + 1) % len(order)].focus_set())
            button.bind("<Right>",
                        lambda event, i=index: order[(i + 1) % len(order)].focus_set())
        ok.focus_set()
        self.wait_variable(answer)
        frame.destroy()
        self._bind_keys()
        self.focus_first()
        return answer.get() == 1

    def choose(self, title, message, options, cancel_label="Cancel"):
        """Pick one of a few named things. Returns the value, or None.

        Used where a yes or no would be a lie: a non-Steam shortcut has no
        Steam app id, so savepick cannot work out which game it is. It needs
        to be told which save set the shortcut owns.
        """
        answer = tk.StringVar(value="")
        frame, panel, _body = self._overlay(title, message)
        holder = tk.Frame(panel, bg=theme.PANEL)
        holder.pack(fill="both", expand=True, padx=self.metrics.pad * 2,
                    pady=self.metrics.pad)
        buttons = []
        for label, value in options:
            button = widgets.Button(holder, self.metrics, label, kind="normal",
                                    command=lambda v=value: answer.set(v))
            button.pack(fill="x", pady=(0, self.metrics.gap))
            buttons.append(button)
        cancel = widgets.Button(holder, self.metrics, cancel_label,
                                command=lambda: answer.set(""))
        cancel.pack(fill="x", pady=(self.metrics.gap, 0))
        buttons.append(cancel)
        for index, button in enumerate(buttons):
            button.bind("<Up>", lambda event, i=index:
                        buttons[(i - 1) % len(buttons)].focus_set() or "break")
            button.bind("<Down>", lambda event, i=index:
                        buttons[(i + 1) % len(buttons)].focus_set() or "break")
        frame.bind_all("<Escape>", lambda event: answer.set(""))
        buttons[0].focus_set()
        self.wait_variable(answer)
        frame.destroy()
        self._bind_keys()
        self.focus_first()
        return answer.get() or None

    def ask_text(self, title, message, initial="", ok_label="Save",
                 hint=None):
        """One line of text. Returns it, or None when cancelled.

        There is no on-screen keyboard here. In Game Mode Steam puts its own
        up for a focused text box, which is why the entry takes focus as soon
        as this opens.
        """
        answer = tk.StringVar(value="")
        done = tk.IntVar(value=0)
        frame, panel, _body = self._overlay(title, message)
        holder = tk.Frame(panel, bg=theme.PANEL)
        holder.pack(fill="x", expand=False, padx=self.metrics.pad * 2)
        field = widgets.Field(holder, self.metrics, hint or "", initial)
        field.pack(fill="x")
        row = tk.Frame(panel, bg=theme.PANEL)
        row.pack(side="bottom", fill="x", padx=self.metrics.pad * 2,
                 pady=self.metrics.pad * 1.5)

        def accept():
            answer.set(field.get().strip())
            done.set(1)

        ok = widgets.Button(row, self.metrics, ok_label, kind="primary",
                            command=accept)
        cancel = widgets.Button(row, self.metrics, "Cancel",
                                command=lambda: done.set(1))
        cancel.pack(side="right", padx=(self.metrics.gap, 0))
        ok.pack(side="right")
        field.entry.bind("<Return>", lambda event: accept())
        frame.bind_all("<Escape>", lambda event: done.set(1))
        field.entry.focus_set()
        field.entry.select_range(0, "end")
        self.wait_variable(done)
        frame.destroy()
        self._bind_keys()
        self.focus_first()
        return answer.get() or None

    def busy(self, title, message=""):
        """A panel that reports a long job. The caller drives it."""
        frame, panel, body = self._overlay(title, message)
        log = tk.Text(panel, bg=theme.BG, fg=theme.TEXT_DIM, relief="flat",
                      font=self.metrics.mono("small"), height=8, wrap="word",
                      highlightthickness=0)
        log.pack(fill="both", expand=True, padx=self.metrics.pad * 2,
                 pady=(0, self.metrics.pad * 2))
        log.configure(state="disabled")
        return BusyPanel(self, frame, body, log)


class BusyPanel(object):
    """The handle a worker uses to say what it is doing."""

    def __init__(self, app, frame, body, log):
        self.app = app
        self.frame = frame
        self.body = body
        self.log = log
        self.closed = False

    def say(self, text):
        """Safe to call from a worker thread."""
        self.app.after(0, self._say, text)

    def _say(self, text):
        if self.closed:
            return
        try:
            self.body.configure(text=text)
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except tk.TclError:
            pass

    def close(self):
        self.app.after(0, self._close)

    def _close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.frame.destroy()
        except tk.TclError:
            pass
        self.app.focus_first()
