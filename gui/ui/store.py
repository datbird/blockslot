"""The store screen: where saves go, whether that works, and the old backups.

Laid out like the Sync screen on purpose. The two answer the same question
for two ways of moving saves, and a person moving from one to the other should
find the test on the left and the settings on the right in both.

Nothing slow runs on the Tk thread. A store test can wait 30 seconds on a
server that does not answer, and an import can take minutes, so both run in a
thread and hand their answer back with app.after, as the games screen does.
"""

import socket
import threading
import time
import tkinter as tk

from ..core import settings as settings_mod
from ..core import storecheck
from . import app as app_mod
from . import theme, widgets
from .sync import Check, SyncScreen

KINDS = (("s3", "S3"), ("ssh", "SSH"), ("local", "Folder"))


class StoreScreen(app_mod.Screen):
    title = "Store"
    hints = (("Up/Down", "move"), ("Enter", "press"),
             ("LB/RB", "screens"), ("F5", "test again"))

    # A check is drawn the same way on both screens.
    _render_check = SyncScreen._render_check

    def __init__(self, app):
        app_mod.Screen.__init__(self, app)
        self.settings = app.controller.settings
        self.kind = self.settings.store_type() or "s3"
        self._build()

    # ------------------------------------------------------------ layout

    def _build(self):
        pad = self.metrics.pad
        gap = self.metrics.gap
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)

        tk.Label(self, text="Store", bg=theme.BG, fg=theme.TEXT, anchor="w",
                 font=self.metrics.font("huge", bold=True)
                 ).grid(row=0, column=0, columnspan=2, sticky="ew",
                        padx=pad, pady=(pad, 0))
        self.banner = widgets.Banner(self, self.metrics)
        self.banner.grid(row=1, column=0, columnspan=2, sticky="ew",
                         padx=pad, pady=pad)
        tk.Label(self, text="WHAT IS WORKING", bg=theme.BG,
                 fg=theme.TEXT_FAINT, anchor="w",
                 font=self.metrics.font("small", bold=True)
                 ).grid(row=2, column=0, sticky="ew", padx=(pad, pad // 2))
        tk.Label(self, text="WHERE SAVES GO", bg=theme.BG,
                 fg=theme.TEXT_FAINT, anchor="w",
                 font=self.metrics.font("small", bold=True)
                 ).grid(row=2, column=1, sticky="ew", padx=(pad // 2, pad))

        left = tk.Frame(self, bg=theme.BG)
        left.grid(row=3, column=0, sticky="nsew", padx=(pad, pad // 2),
                  pady=(0, pad))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.checks = widgets.ListView(left, self.metrics, self._render_check,
                                       selectable=False, interactive=False,
                                       row_height=int(self.metrics.row_height * 1.25))
        self.checks.grid(row=0, column=0, sticky="nsew")
        self.daemon_line = tk.Label(left, text="Asking the daemon ...",
                                    bg=theme.BG, fg=theme.TEXT_DIM, anchor="w",
                                    justify="left", font=self.metrics.font("small"),
                                    wraplength=int(self.metrics.base * 30))
        self.daemon_line.grid(row=1, column=0, sticky="ew", pady=(gap, 0))

        form = tk.Frame(self, bg=theme.BG)
        form.grid(row=3, column=1, sticky="nsew", padx=(pad // 2, pad),
                  pady=(0, pad))
        self.form = form

        # Pairing first: with a BlockSlot server it is the whole setup. The
        # fields below are for any other S3 store, SSH or a folder.
        pairing = tk.Frame(form, bg=theme.BG)
        pairing.pack(fill="x", pady=(0, gap))
        pair_fields = self._pair(pairing)
        self.pair_address = self._field(pair_fields, "BlockSlot server address",
                                        self.settings.data.get("server_address"))
        self.pair_code = self._field(pair_fields, "Pairing code", "")
        self._place_pair(pair_fields, self.pair_address, self.pair_code)
        self.pair_button = widgets.Button(pairing, self.metrics, "Pair with the server",
                                          kind="primary", command=self.pair_with_server)
        self.pair_button.pack(anchor="w", pady=(gap // 2, 0))

        kinds = tk.Frame(form, bg=theme.BG)
        kinds.pack(fill="x", pady=(0, gap))
        self.kind_buttons = {}
        for key, label in KINDS:
            button = widgets.Button(kinds, self.metrics, label,
                                    command=lambda k=key: self.pick_kind(k))
            button.pack(side="left", padx=(0, gap))
            self.kind_buttons[key] = button

        block = self.settings.store()
        self.panels = {}

        # S3
        panel = tk.Frame(form, bg=theme.BG)
        self.endpoint = self._field(panel, "Endpoint URL", block.get("endpoint"))
        self.endpoint.pack(fill="x", pady=(0, gap))
        pair = self._pair(panel)
        self.bucket = self._field(pair, "Bucket", block.get("bucket"))
        self.region = self._field(pair, "Region", block.get("region") or "us-east-1")
        self._place_pair(pair, self.bucket, self.region)
        pair = self._pair(panel)
        self.access_key = self._field(pair, "Access key", block.get("access_key"))
        self.secret_key = self._field(pair, "Secret key", "", secret=True)
        self._place_pair(pair, self.access_key, self.secret_key)
        self.panels["s3"] = (panel, [self.endpoint, self.bucket, self.region,
                                     self.access_key, self.secret_key])

        # SSH
        panel = tk.Frame(form, bg=theme.BG)
        pair = self._pair(panel)
        self.host = self._field(pair, "Host", block.get("host"))
        self.port = self._field(pair, "Port", block.get("port"))
        self._place_pair(pair, self.host, self.port)
        pair = self._pair(panel)
        self.user = self._field(pair, "User", block.get("user"))
        self.identity = self._field(pair, "Identity file", block.get("identity"))
        self._place_pair(pair, self.user, self.identity)
        self.ssh_root = self._field(
            panel, "Folder on the server",
            block.get("root") if self.kind == "ssh" else "")
        self.ssh_root.pack(fill="x", pady=(0, gap))
        self.panels["ssh"] = (panel, [self.host, self.port, self.user,
                                      self.identity, self.ssh_root])

        # Folder
        panel = tk.Frame(form, bg=theme.BG)
        self.local_root = self._field(
            panel, "Folder (a mounted share or a USB disk)",
            block.get("root") if self.kind == "local" else "")
        self.local_root.pack(fill="x", pady=(0, gap))
        self.panels["local"] = (panel, [self.local_root])

        # Everything below the kind's own fields. A frame of its own, so the
        # kind's panel can be swapped in above it without repacking this.
        self.common = tk.Frame(form, bg=theme.BG)
        self.common.pack(fill="x")
        pair = self._pair(self.common)
        self.cf_id = self._field(pair, "Cloudflare token id (optional)",
                                 block.get("cf_client_id"))
        self.cf_secret = self._field(pair, "Cloudflare token secret", "",
                                     secret=True)
        self._place_pair(pair, self.cf_id, self.cf_secret)
        self.device = self._field(self.common, "This device's name",
                                  block.get("device") or socket.gethostname())
        self.device.pack(fill="x", pady=(0, gap))

        actions = tk.Frame(self.common, bg=theme.BG)
        actions.pack(fill="x", pady=(gap, 0))
        self.save_button = widgets.Button(actions, self.metrics, "Save and test",
                                          kind="primary", command=self.save)
        self.save_button.pack(side="left")
        self.import_button = widgets.Button(actions, self.metrics,
                                            "Import old backups",
                                            command=self.import_old)
        self.import_button.pack(side="left", padx=(gap, 0))
        self.paste_button = widgets.Button(actions, self.metrics,
                                           "Paste setup code",
                                           command=self.paste_setup_code)
        self.paste_button.pack(side="left", padx=(gap, 0))
        self.status = tk.Label(actions, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                               font=self.metrics.font("small"))
        self.status.pack(side="right")

        self._label_secrets()
        self.pick_kind(self.kind, focus=False)

    def _field(self, parent, label, value, secret=False):
        return widgets.Field(parent, self.metrics, label, value or "",
                             secret=secret, width=12)

    def _pair(self, parent):
        pair = tk.Frame(parent, bg=theme.BG)
        pair.pack(fill="x", pady=(0, self.metrics.gap))
        pair.columnconfigure(0, weight=1, uniform="pair")
        pair.columnconfigure(1, weight=1, uniform="pair")
        return pair

    def _place_pair(self, pair, first, second):
        first.grid(row=0, column=0, sticky="ew", padx=(0, self.metrics.gap // 2))
        second.grid(row=0, column=1, sticky="ew", padx=(self.metrics.gap // 2, 0))

    def _label_secrets(self):
        """A stored secret is never shown, not even masked: only that it is there.

        Putting it back in the box would put it back in memory as widget text
        for no reason, and a masked box full of stars cannot be checked by eye
        anyway. An empty box means keep the one saved.
        """
        for field, key, name in ((self.secret_key, "secret_key", "Secret key"),
                                 (self.cf_secret, "cf_client_secret",
                                  "Cloudflare token secret")):
            if self.settings.has_secret(key):
                field.label.configure(text="%s (saved; type to replace)" % name)
            else:
                field.label.configure(text=name)

    def pick_kind(self, kind, focus=True):
        self.kind = kind
        for key, button in self.kind_buttons.items():
            button.kind = "primary" if key == kind else "normal"
            button.redraw()
        for key, (panel, _fields) in self.panels.items():
            if key == kind:
                panel.pack(fill="x", before=self.common)
            else:
                panel.pack_forget()
        if focus:
            self.kind_buttons[kind].focus_set()

    def focus_order(self):
        order = [self.pair_address, self.pair_code, self.pair_button]
        order += [self.kind_buttons[key] for key, _label in KINDS]
        order += self.panels[self.kind][1]
        order += [self.cf_id, self.cf_secret, self.device, self.save_button,
                  self.import_button, self.paste_button]
        return order

    # ------------------------------------------------------------ values

    def form_values(self):
        """What to write to the store section.

        The other kinds' fields are sent as None so that a switch from S3 to
        SSH does not leave an S3 key sitting in the file. A secret box left
        empty is not sent at all, which keeps the saved one.
        """
        values = {}
        for kind, keys in settings_mod.STORE_FIELDS.items():
            if kind != self.kind:
                values.update((key, None) for key in keys)
        values["type"] = self.kind
        if self.kind == "s3":
            values.update(endpoint=self.endpoint.get().strip(),
                          bucket=self.bucket.get().strip(),
                          region=self.region.get().strip() or "us-east-1",
                          access_key=self.access_key.get().strip())
            if self.secret_key.get().strip():
                values["secret_key"] = self.secret_key.get().strip()
        elif self.kind == "ssh":
            port = self.port.get().strip()
            values.update(host=self.host.get().strip(),
                          port=int(port) if port.isdigit() else (port or None),
                          user=self.user.get().strip(),
                          identity=self.identity.get().strip(),
                          root=self.ssh_root.get().strip())
        else:
            values["root"] = self.local_root.get().strip()
        values["cf_client_id"] = self.cf_id.get().strip()
        if not values["cf_client_id"]:
            # No id means no token at all. A secret with no id is never used,
            # and leaving it would look like a token is set up.
            values["cf_client_secret"] = None
        elif self.cf_secret.get().strip():
            values["cf_client_secret"] = self.cf_secret.get().strip()
        values["device"] = self.device.get().strip()
        return values

    def pair_with_server(self):
        """Fetch this device's store settings from a BlockSlot server.

        The server's Devices page shows the address and a short code. The
        answer is saved and tested at once: it came from the server itself.
        """
        address, code = self.pair_address.get(), self.pair_code.get()
        self.pair_button.set_enabled(False)
        self.status.configure(text="Pairing ...")
        threading.Thread(target=self._pair_worker, args=(address, code),
                         daemon=True).start()

    def _pair_worker(self, address, code):
        try:
            values = storecheck.pair(address, code)
            base = storecheck.server_address(address)
        except ValueError as exc:
            self._later(self._paired, None, None, str(exc))
            return
        self._later(self._paired, values, base, None)

    def _paired(self, values, base, error):
        try:
            self.pair_button.set_enabled(True)
            self.status.configure(text="")
        except tk.TclError:
            return
        if error:
            self.banner.show(error, "warn")
            return
        self.settings.data["server_address"] = base
        self.pair_code.set("")
        self._fill(values)
        self.banner.show("Paired. Saving and testing ...", "good")
        self.save()

    def _fill(self, values):
        self.pick_kind("s3", focus=False)
        self.endpoint.set(values["endpoint"])
        self.bucket.set(values["bucket"])
        self.region.set(values.get("region") or "us-east-1")
        self.access_key.set(values["access_key"])
        self.secret_key.set(values["secret_key"])
        self.cf_id.set(values.get("cf_client_id"))
        self.cf_secret.set(values.get("cf_client_secret"))
        if values.get("device"):
            self.device.set(values["device"])

    def paste_setup_code(self):
        """Fill the form from a server setup code on the clipboard.

        Nothing is saved: the person still presses Save and test, so a wrong
        code never replaces working settings.
        """
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        try:
            values = storecheck.parse_setup_code(text)
        except ValueError as exc:
            self.banner.show(str(exc), "warn")
            return
        self._fill(values)
        self.banner.show("Setup code pasted. Press Save and test.", "good")
        self.save_button.focus_set()

    # ------------------------------------------------------------ show

    def on_show(self):
        self.refresh_daemon()
        if not self.checks.rows and self.settings.store_type():
            self.test()

    def refresh(self):
        self.test()
        self.refresh_daemon()

    def _later(self, callback, *args):
        """Hand a worker's answer to the Tk thread. The window may be gone."""
        try:
            self.app.after(0, callback, *args)
        except (RuntimeError, tk.TclError):
            pass

    def refresh_daemon(self):
        threading.Thread(target=self._daemon_worker, daemon=True).start()

    def _daemon_worker(self):
        try:
            text = storecheck.describe_daemon(storecheck.daemon_status(self.settings))
        except Exception as exc:
            text = "Could not ask the daemon: %s" % exc
        self._later(self._show_daemon, text)

    def _show_daemon(self, text):
        try:
            self.daemon_line.configure(text=text)
        except tk.TclError:
            pass

    # ------------------------------------------------------------ test

    def save(self):
        try:
            self.settings.set_store(**self.form_values())
        except Exception as exc:
            self.banner.show("Could not save: %s" % exc, "bad")
            return
        if self.write_settings("Saved to %s" % self.settings.path):
            self.secret_key.set("")
            self.cf_secret.set("")
            self._label_secrets()
            self.test()

    def test(self):
        self.status.configure(text="Testing ...")
        self.save_button.set_enabled(False)
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        try:
            steps = storecheck.test_store(self.settings)
        except Exception as exc:
            steps = [("Test", False, str(exc))]
        self._later(self._show_checks, steps)

    def _show_checks(self, steps):
        rows = [Check(label, ok, detail) for label, ok, detail in steps]
        self.checks.set_rows(rows, keep_cursor=False)
        self.status.configure(text="")
        self.save_button.set_enabled(True)
        if not rows:
            self.banner.show("Nothing to test yet.", "warn")
        elif all(row.ok for row in rows):
            self.banner.show("The store is working. Starting the uploader ...", "good")
            threading.Thread(target=self._restart_worker, daemon=True).start()
        else:
            first = next(row for row in rows if not row.ok)
            self.banner.show("%s: %s" % (first.label, first.detail), "bad")

    def _restart_worker(self):
        try:
            line = storecheck.restart_daemon(self.settings)
        except Exception as exc:
            line = "The uploader did not start: %s" % exc
        self._later(self._restarted, line)

    def _restarted(self, line):
        self.banner.show("The store is working. %s" % line, "good")
        self.refresh_daemon()

    # ------------------------------------------------------------ import

    def import_old(self):
        if not self.settings.store_is_complete():
            self.banner.show("Set up the store and save it first.", "warn")
            return
        try:
            start = storecheck.default_import_folder(self.settings)
        except Exception:
            start = ""
        root = self.app.ask_text(
            "Import old backups",
            "The Syncthing gamesaves folder. It holds one folder per device.",
            initial=start, ok_label="Next", hint="Folder")
        if not root:
            return
        message = (
            "BlockSlot will read every ludusavi backup in\n%s\n\n"
            "and copy each one to the store as a save. Games saved on more "
            "than one device start with the newest one.\n\n"
            "This changes nothing on this device. The old folder is only "
            "read, and your saves here stay as they are. It is safe to run "
            "twice." % root)
        if not self.app.confirm("Import old backups", message,
                                ok_label="Import"):
            return
        panel = self.app.busy("Importing old backups", "Looking ...")
        threading.Thread(target=self._import_worker, args=(panel, root),
                         daemon=True).start()

    def _import_worker(self, panel, root):
        try:
            backups, devices, games = storecheck.count_backups(root)
            if not backups:
                panel.say("No ludusavi backups found in %s." % root)
            else:
                panel.say("Found %d backups of %d games from %d devices."
                          % (backups, games, devices))
                heads = storecheck.import_folder(self.settings, root, say=panel.say)
                panel.say("Done. %d game%s on the store."
                          % (len(heads), "" if len(heads) == 1 else "s"))
        except OSError as exc:
            panel.say("Could not read the folder: %s" % exc)
        except Exception as exc:
            panel.say("Stopped: %s" % storecheck.explain(exc))
        time.sleep(2.0)
        panel.close()
        self._later(self.refresh_daemon)
