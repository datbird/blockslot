"""The small set of controls the screens are built from.

Tk's own widgets are used where they are honest (an entry is an entry) and
drawn on a canvas where they are not. A list of 700 games with a checkbox, a
name, three state columns and a focus ring is not a Listbox, and pretending it is
costs more than drawing it.

Every control here is keyboard first, because in Game Mode there is no mouse:
whatever the pad sends arrives as a key. See `pad.py`.
"""

import tkinter as tk
import tkinter.font as tkfont

from . import theme

# Text is measured, never counted. Guessing a pixel width from a character
# count is what put a path underneath its neighbour on every screen.
_FONTS = {}
_FONT_ROOT = None


def font_for(spec):
    """A measurable font, belonging to the window that is open now.

    A tkfont.Font is tied to one interpreter. Caching them across a window
    being destroyed and another opened, which is exactly what the tests do,
    hands back a font whose interpreter is gone.
    """
    global _FONT_ROOT
    root = tk._default_root
    if root is not _FONT_ROOT:
        _FONTS.clear()
        _FONT_ROOT = root
    key = tuple(spec)
    if key not in _FONTS:
        family, size = spec[0], spec[1]
        weight = spec[2] if len(spec) > 2 else "normal"
        _FONTS[key] = tkfont.Font(family=family, size=size, weight=weight)
    return _FONTS[key]


def width_of(text, spec):
    try:
        return font_for(spec).measure(text)
    except tk.TclError:
        # No window to measure with. Only reachable while one is closing.
        return int(len(text or "") * spec[1] * 0.6)


def elide(text, spec, room, keep="end"):
    """Shorten text to fit `room` pixels, keeping the end or the start.

    A path is elided from the LEFT, because the file name is the part worth
    reading. A sentence is elided from the right.
    """
    text = text or ""
    if room <= 0 or width_of(text, spec) <= room:
        return text
    dots = "..."
    if width_of(dots, spec) > room:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        piece = (dots + text[-middle:]) if keep == "end" else (text[:middle] + dots)
        if width_of(piece, spec) <= room:
            low = middle
        else:
            high = middle - 1
    return (dots + text[-low:]) if keep == "end" else (text[:low] + dots)


def round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """A rounded rectangle, which Tk's canvas does not have."""
    radius = max(0, min(radius, int((x2 - x1) / 2), int((y2 - y1) / 2)))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Button(tk.Canvas):
    """A push button that shows its focus loudly enough to see across a room."""

    def __init__(self, parent, metrics, text, command=None, kind="normal",
                 width=None, **kwargs):
        self.metrics = metrics
        self.text = text
        self.command = command
        self.kind = kind
        self.enabled = True
        self._pressed = False
        width = width or self._natural_width(text)
        tk.Canvas.__init__(self, parent, width=width,
                           height=metrics.button_height, bg=theme.BG,
                           highlightthickness=0, bd=0, takefocus=1, **kwargs)
        self.bind("<Configure>", lambda event: self.redraw())
        self.bind("<FocusIn>", lambda event: self.redraw())
        self.bind("<FocusOut>", lambda event: self.redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", self._on_key)
        self.bind("<KP_Enter>", self._on_key)
        self.bind("<space>", self._on_key)
        self.redraw()

    def _natural_width(self, text):
        return max(int(self.metrics.base * 0.72 * len(text)) + self.metrics.pad * 3,
                   int(120 * self.metrics.scale))

    def configure_text(self, text):
        self.text = text
        self.redraw()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.configure(takefocus=1 if enabled else 0)
        self.redraw()

    def _on_press(self, _event):
        if not self.enabled:
            return
        self._pressed = True
        self.focus_set()
        self.redraw()

    def _on_release(self, _event):
        if not self.enabled:
            return
        self._pressed = False
        self.redraw()
        self.invoke()

    def _on_key(self, _event):
        if self.enabled:
            self.invoke()
        return "break"

    def invoke(self):
        if self.enabled and self.command:
            self.command()

    def redraw(self):
        self.delete("all")
        width = int(self.winfo_width() or self["width"])
        height = int(self.winfo_height() or self["height"])
        focused = self.focus_get() is self
        fill = {
            "normal": theme.PANEL_HI,
            "primary": theme.ACCENT_DEEP,
            "danger": theme.BAD,
            "quiet": theme.BG,
        }.get(self.kind, theme.PANEL_HI)
        text_colour = theme.TEXT
        if not self.enabled:
            fill = theme.PANEL
            text_colour = theme.TEXT_FAINT
        if self._pressed:
            fill = theme.ACCENT if self.kind == "primary" else theme.LINE
        outline = theme.FOCUS if focused else theme.LINE
        round_rect(self, 2, 2, width - 2, height - 2, self.metrics.radius,
                   fill=fill, outline=outline, width=3 if focused else 1)
        self.create_text(width / 2, height / 2, text=self.text,
                         fill=text_colour, font=self.metrics.font(bold=True))


class Toggle(tk.Canvas):
    """An on/off switch with its label, as one focusable unit."""

    def __init__(self, parent, metrics, text, value=False, command=None, **kwargs):
        self.metrics = metrics
        self.text = text
        self.value = bool(value)
        self.command = command
        tk.Canvas.__init__(self, parent, height=metrics.button_height,
                           bg=theme.BG, highlightthickness=0, bd=0,
                           takefocus=1, **kwargs)
        self.bind("<Configure>", lambda event: self.redraw())
        self.bind("<FocusIn>", lambda event: self.redraw())
        self.bind("<FocusOut>", lambda event: self.redraw())
        self.bind("<Button-1>", lambda event: self.toggle())
        self.bind("<Return>", lambda event: self.toggle())
        self.bind("<space>", lambda event: self.toggle())
        self.redraw()

    def toggle(self, *_args):
        self.focus_set()
        self.value = not self.value
        self.redraw()
        if self.command:
            self.command(self.value)
        return "break"

    def set(self, value):
        self.value = bool(value)
        self.redraw()

    def redraw(self):
        self.delete("all")
        width = int(self.winfo_width() or 220)
        height = int(self.winfo_height() or self.metrics.button_height)
        focused = self.focus_get() is self
        track_w = int(46 * self.metrics.scale)
        track_h = int(24 * self.metrics.scale)
        top = (height - track_h) / 2
        if focused:
            round_rect(self, 1, 1, width - 1, height - 1, self.metrics.radius,
                       fill=theme.PANEL_HI, outline=theme.FOCUS, width=2)
        round_rect(self, self.metrics.gap, top, self.metrics.gap + track_w,
                   top + track_h, track_h / 2,
                   fill=theme.ACCENT_DEEP if self.value else theme.LINE,
                   outline="")
        knob = track_h - int(6 * self.metrics.scale)
        knob_x = (self.metrics.gap + track_w - knob - 3) if self.value \
            else (self.metrics.gap + 3)
        self.create_oval(knob_x, top + 3, knob_x + knob, top + 3 + knob,
                         fill=theme.TEXT, outline="")
        self.create_text(self.metrics.gap + track_w + self.metrics.pad,
                         height / 2, text=self.text, anchor="w",
                         fill=theme.TEXT, font=self.metrics.font())


class Field(tk.Frame):
    """A labelled text box. The label is above, so long values have room."""

    def __init__(self, parent, metrics, label, value="", secret=False,
                 width=30, on_change=None):
        tk.Frame.__init__(self, parent, bg=theme.BG)
        self.metrics = metrics
        self.on_change = on_change
        self.label = tk.Label(self, text=label, bg=theme.BG, fg=theme.TEXT_DIM,
                              font=metrics.font("small"), anchor="w")
        self.label.pack(fill="x")
        self.var = tk.StringVar(value=value or "")
        self.entry = tk.Entry(
            self, textvariable=self.var, width=width,
            font=metrics.mono("base") if secret else metrics.font(),
            bg=theme.PANEL_HI, fg=theme.TEXT, insertbackground=theme.ACCENT,
            relief="flat", highlightthickness=2, highlightbackground=theme.LINE,
            highlightcolor=theme.FOCUS, show="*" if secret else "")
        self.entry.pack(fill="x", ipady=int(7 * metrics.scale))
        if on_change:
            self.var.trace_add("write", lambda *args: on_change(self.var.get()))

    def get(self):
        return self.var.get()

    def set(self, value):
        self.var.set(value or "")

    def focus_widget(self):
        return self.entry


class ListView(tk.Frame):
    """A drawn table with a cursor, checkboxes and its own scrolling.

    Rows are supplied as objects and rendered by a callback, so the games
    screen decides what a row looks like and this decides how a list behaves.
    """

    def __init__(self, parent, metrics, render_row, on_activate=None,
                 on_selection_change=None, selectable=True, interactive=True,
                 on_cursor=None, chevrons=False, row_height=None):
        """render_row draws one row. The rest is behaviour.

        `selectable` gives every row a checkbox. `interactive` is whether the
        list can be moved through at all: a readout sets it False, and then it
        takes no focus and draws no cursor, so it stops looking like a menu.
        `chevrons` marks rows that lead somewhere.
        """
        tk.Frame.__init__(self, parent, bg=theme.PANEL)
        self.metrics = metrics
        self.render_row = render_row
        self.on_activate = on_activate
        self.on_selection_change = on_selection_change
        self.on_cursor = on_cursor
        self.selectable = selectable
        self.interactive = interactive
        self.chevrons = chevrons
        # A row carrying a label and a line under it needs more height than a
        # single line of text. The caller knows which it is drawing.
        self.row_height = row_height or metrics.row_height
        self.rows = []
        self.selected = set()
        self.cursor = 0
        self.top = 0
        self.canvas = tk.Canvas(self, bg=theme.PANEL, highlightthickness=0,
                                bd=0, takefocus=1 if interactive else 0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda event: self.redraw())
        self.canvas.bind("<FocusIn>", lambda event: self.redraw())
        self.canvas.bind("<FocusOut>", lambda event: self.redraw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda event: self._scroll(-3))
        self.canvas.bind("<Button-5>", lambda event: self._scroll(3))
        for key, delta in (("<Up>", -1), ("<Down>", 1)):
            self.canvas.bind(key, lambda event, d=delta: self.move(d))
        self.canvas.bind("<Home>", lambda event: self.move_to(0))
        self.canvas.bind("<End>", lambda event: self.move_to(len(self.rows) - 1))
        self.canvas.bind("<space>", lambda event: self.toggle_current())
        self.canvas.bind("<Return>", lambda event: self.activate_current())

    # ------------------------------------------------------------ data

    def set_rows(self, rows, keep_cursor=True, announce=True):
        previous = self.current()
        self.rows = list(rows)
        self.selected &= set(self.key(row) for row in self.rows)
        if keep_cursor and previous is not None:
            for index, row in enumerate(self.rows):
                if self.key(row) == self.key(previous):
                    self.cursor = index
                    break
            else:
                self.cursor = 0
        else:
            self.cursor = 0
        self.cursor = max(0, min(self.cursor, max(0, len(self.rows) - 1)))
        self._clamp_top()
        self.redraw()
        if announce and self.on_cursor:
            self.on_cursor(self.current())

    def key(self, row):
        return getattr(row, "appid", None) or id(row)

    def current(self):
        if 0 <= self.cursor < len(self.rows):
            return self.rows[self.cursor]
        return None

    def selected_rows(self):
        keys = self.selected
        return [row for row in self.rows if self.key(row) in keys]

    # ------------------------------------------------------------ moving

    def visible_count(self):
        height = int(self.canvas.winfo_height() or self.row_height * 8)
        return max(1, int(height // self.row_height))

    def move(self, delta):
        if not self.rows:
            return "break"
        target = self.cursor + delta
        if target < 0 or target >= len(self.rows):
            # Let the screen move focus off the list at either end.
            self.event_generate("<<ListEdge>>" if target < 0 else "<<ListEdgeDown>>")
            target = max(0, min(target, len(self.rows) - 1))
        self.move_to(target)
        return "break"

    def move_to(self, index):
        if not self.rows:
            return "break"
        previous = self.cursor
        self.cursor = max(0, min(index, len(self.rows) - 1))
        self._clamp_top()
        self.redraw()
        if self.on_cursor and self.cursor != previous:
            self.on_cursor(self.current())
        return "break"

    def _clamp_top(self):
        visible = self.visible_count()
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + visible:
            self.top = self.cursor - visible + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - visible)))

    def _scroll(self, delta):
        self.top = max(0, min(self.top + delta,
                              max(0, len(self.rows) - self.visible_count())))
        self.redraw()
        return "break"

    def _on_wheel(self, event):
        return self._scroll(-3 if event.delta > 0 else 3)

    # ------------------------------------------------------------ choosing

    def toggle_current(self):
        row = self.current()
        if row is None or not self.selectable:
            return "break"
        key = self.key(row)
        if key in self.selected:
            self.selected.discard(key)
        else:
            self.selected.add(key)
        self.redraw()
        if self.on_selection_change:
            self.on_selection_change(self.selected_rows())
        return "break"

    def select_all(self, rows=None):
        for row in (rows if rows is not None else self.rows):
            self.selected.add(self.key(row))
        self.redraw()
        if self.on_selection_change:
            self.on_selection_change(self.selected_rows())

    def clear_selection(self):
        self.selected.clear()
        self.redraw()
        if self.on_selection_change:
            self.on_selection_change([])

    def activate_current(self):
        row = self.current()
        if row is not None and self.on_activate:
            self.on_activate(row)
        return "break"

    def _on_click(self, event):
        if not self.interactive:
            return "break"
        self.canvas.focus_set()
        index = self.top + int(event.y // self.row_height)
        if 0 <= index < len(self.rows):
            if index == self.cursor and event.x < self.metrics.row_height:
                self.toggle_current()
            else:
                self.move_to(index)
                if event.x < self.metrics.row_height:
                    self.toggle_current()
        return "break"

    # ------------------------------------------------------------ drawing

    def redraw(self):
        canvas = self.canvas
        canvas.delete("all")
        width = int(canvas.winfo_width() or 900)
        height = int(canvas.winfo_height() or 400)
        row_height = self.row_height
        focused = self.focus_get() is canvas
        visible = self.visible_count()
        if not self.rows:
            canvas.create_text(width / 2, height / 2,
                               text="Nothing to show", fill=theme.TEXT_FAINT,
                               font=self.metrics.font("large"))
            return
        for offset in range(visible + 1):
            index = self.top + offset
            if index >= len(self.rows):
                break
            row = self.rows[index]
            y = offset * row_height
            is_cursor = index == self.cursor
            checked = self.key(row) in self.selected
            background = theme.ROW if index % 2 == 0 else theme.ROW_ALT
            if checked:
                background = theme.ROW_SELECTED
            canvas.create_rectangle(0, y, width, y + row_height,
                                    fill=background, outline="")
            if is_cursor and self.interactive:
                canvas.create_rectangle(
                    1, y + 1, width - 1, y + row_height - 1, outline=
                    theme.FOCUS if focused else theme.TEXT_FAINT,
                    width=2 if focused else 1)
            if self.chevrons:
                self._draw_chevron(canvas, y, width, row_height,
                                   is_cursor and focused)
            if self.selectable:
                self._draw_check(canvas, y, checked)
            usable = width - (int(26 * self.metrics.scale) if self.chevrons
                              else 0)
            self.render_row(canvas, row, y, usable, row_height, is_cursor)
        self._draw_scrollbar(canvas, width, height, visible)

    def _draw_chevron(self, canvas, y, width, height, lit):
        """The mark that says this row leads somewhere. Without it a list of
        rows that open something is indistinguishable from a list of facts."""
        size = int(5 * self.metrics.scale)
        x = width - int(18 * self.metrics.scale)
        middle = y + height / 2
        canvas.create_line(x - size, middle - size, x, middle,
                           x - size, middle + size,
                           fill=theme.TEXT if lit else theme.TEXT_FAINT,
                           width=max(2, int(2 * self.metrics.scale)),
                           capstyle="round", joinstyle="round")

    def _draw_check(self, canvas, y, checked):
        size = int(self.row_height * 0.42)
        left = int(self.row_height * 0.28)
        top = y + (self.row_height - size) / 2
        round_rect(canvas, left, top, left + size, top + size,
                   int(3 * self.metrics.scale),
                   fill=theme.ACCENT_DEEP if checked else "",
                   outline=theme.ACCENT if checked else theme.TEXT_FAINT,
                   width=2)
        if checked:
            canvas.create_line(left + size * 0.22, top + size * 0.55,
                               left + size * 0.42, top + size * 0.75,
                               left + size * 0.78, top + size * 0.28,
                               fill=theme.TEXT, width=int(2.4 * self.metrics.scale),
                               capstyle="round", joinstyle="round")

    def _draw_scrollbar(self, canvas, width, height, visible):
        if len(self.rows) <= visible:
            return
        track = int(5 * self.metrics.scale)
        x = width - track - 2
        canvas.create_rectangle(x, 0, x + track, height, fill=theme.BG, outline="")
        span = max(0.06, visible / float(len(self.rows)))
        start = self.top / float(len(self.rows))
        canvas.create_rectangle(x, height * start, x + track,
                                height * min(1.0, start + span),
                                fill=theme.LINE, outline="")

    def focus_widget(self):
        """The window focuses this, not the frame around it."""
        return self.canvas


class Banner(tk.Canvas):
    """One line of state at the top of a screen: good, warning or bad."""

    def __init__(self, parent, metrics, text="", kind="info"):
        self.metrics = metrics
        self.text = text
        self.kind = kind
        tk.Canvas.__init__(self, parent, height=int(38 * metrics.scale),
                           bg=theme.BG, highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda event: self.redraw())

    def show(self, text, kind="info"):
        self.text = text
        self.kind = kind
        self.redraw()

    def redraw(self):
        self.delete("all")
        width = int(self.winfo_width() or 600)
        height = int(self.winfo_height() or 38)
        if not self.text:
            return
        colour = {"info": theme.ACCENT, "good": theme.GOOD,
                  "warn": theme.WARN, "bad": theme.BAD}.get(self.kind, theme.ACCENT)
        round_rect(self, 0, 2, width, height - 2, self.metrics.radius,
                   fill=theme.PANEL, outline="")
        self.create_rectangle(0, 2, int(4 * self.metrics.scale), height - 2,
                              fill=colour, outline="")
        self.create_text(self.metrics.pad * 1.5, height / 2, anchor="w",
                         text=self.text, fill=theme.TEXT,
                         font=self.metrics.font())


class Detail(tk.Frame):
    """The right hand half of a master and detail screen.

    One selected thing, explained, with the one or two actions that belong to
    IT. Actions live here rather than in a bar along the bottom, because a
    button in a bar has to be read twice: once to know what it does and once
    to work out what it would do it to.
    """

    def __init__(self, parent, metrics):
        tk.Frame.__init__(self, parent, bg=theme.PANEL)
        self.metrics = metrics
        self.buttons = []
        self.field = None
        self._body = None
        self._wrapping = []
        self.bind("<Configure>", lambda event: self._rewrap())
        self.show(None, "Nothing selected", [])

    def _rewrap(self):
        """Text is wrapped to the pane's real width, which it learns late.

        A wraplength fixed when the widget was built wraps to whatever tk
        guessed before the layout ran, which is how a two word column happens.
        """
        room = self._wrap()
        for label in self._wrapping:
            try:
                label.configure(wraplength=room)
            except tk.TclError:
                pass

    def focus_order(self):
        """The controls the window can move focus to, in reading order."""
        order = []
        if self.field is not None:
            order.append(self.field)
        order += self.buttons
        return order

    def show(self, kind, title, lines=(), actions=(), field=None,
             state=None, note=None):
        """Rebuild the pane for one selected thing.

        lines   plain text, one per line, paths included
        actions [(label, callback, kind)] in order, primary first
        field   (label, value, on_change) to edit something in place
        state   (text, colour) for a status line with a dot
        note    a sentence about what this is for
        """
        if self._body is not None:
            self._body.destroy()
        self.buttons = []
        self.field = None
        self._wrapping = []
        pad = self.metrics.pad
        body = tk.Frame(self, bg=theme.PANEL)
        body.pack(fill="both", expand=True, padx=pad * 1.5, pady=pad * 1.2)
        self._body = body

        if kind:
            tk.Label(body, text=kind.upper(), bg=theme.PANEL,
                     fg=theme.TEXT_FAINT, anchor="w",
                     font=self.metrics.font("small", bold=True)
                     ).pack(fill="x")
        tk.Label(body, text=title, bg=theme.PANEL, fg=theme.TEXT, anchor="w",
                 justify="left", font=self.metrics.font("large", bold=True)
                 ).pack(fill="x", pady=(0, pad // 2))

        if state:
            text, colour = state
            row = tk.Frame(body, bg=theme.PANEL)
            row.pack(fill="x", pady=(0, pad // 2))
            dot = tk.Canvas(row, width=int(14 * self.metrics.scale),
                            height=int(14 * self.metrics.scale),
                            bg=theme.PANEL, highlightthickness=0, bd=0)
            size = int(9 * self.metrics.scale)
            dot.create_oval(2, 3, size, size + 1, fill=colour, outline="")
            dot.pack(side="left")
            tk.Label(row, text=text, bg=theme.PANEL, fg=theme.TEXT, anchor="w",
                     font=self.metrics.font()).pack(side="left")

        for line in lines:
            label = tk.Label(body, text=line, bg=theme.PANEL, fg=theme.TEXT_DIM,
                             anchor="w", justify="left",
                             font=self.metrics.mono("small"),
                             wraplength=self._wrap())
            label.pack(fill="x", pady=(0, 2))
            self._wrapping.append(label)

        if note:
            label = tk.Label(body, text=note, bg=theme.PANEL, fg=theme.TEXT_DIM,
                             anchor="w", justify="left",
                             font=self.metrics.font("small"),
                             wraplength=self._wrap())
            label.pack(fill="x", pady=(pad // 2, 0))
            self._wrapping.append(label)

        if field:
            label, value, on_change = field
            self.field = Field(body, self.metrics, label, value,
                               on_change=on_change)
            self.field.pack(fill="x", pady=(pad, 0))

        if actions:
            row = tk.Frame(body, bg=theme.PANEL)
            row.pack(fill="x", pady=(pad * 1.2, 0))
            for index, action in enumerate(actions):
                label, command = action[0], action[1]
                style = action[2] if len(action) > 2 else (
                    "primary" if index == 0 else "normal")
                button = Button(row, self.metrics, label, command=command,
                                kind=style)
                button.pack(side="left", padx=(0, self.metrics.gap))
                self.buttons.append(button)

    def _wrap(self):
        width = int(self.winfo_width() or 520)
        return max(260, width - self.metrics.pad * 4)
