#!/usr/bin/env python3
"""Blockslot: turn save syncing on for the games Steam Cloud does not cover.

    python3 gui/blockslot.py                 the window
    python3 gui/blockslot.py --check         what it can see, as text
    python3 gui/blockslot.py --fullscreen    for Game Mode
    python3 gui/blockslot.py --daemon        the store daemon (a tray icon
                                             on Windows), started at login
    Blockslot.exe --pick [switches] -- CMD   the engine, as a launch option
                                             runs it (savepick.py's switches)

Standard library only, python 3.9 and up, one tree for Windows, macOS and
Linux. The engine it configures is `engine/savepick.py`, which does the actual
syncing at launch time.
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from gui.core import engine, launchopts, model, paths  # noqa: E402
from gui.core import settings as settings_mod, shortcuts, steamdir  # noqa: E402


class Controller(object):
    """What the screens share: the library, the settings, and where things are."""

    def __init__(self, steam_root=None, user_id=None, config_path=None,
                 index_path=None):
        self.settings = settings_mod.Settings.load(config_path)
        self.library = model.Library.discover(steam_root, user_id)
        self.library.settings = self.settings
        self.index_path = index_path

    def prepare(self):
        """Read the index and Steam's cloud answer. Slow enough to say so."""
        self.library.load_catalog(self.index_path)
        return self.library

    def entry_point(self):
        """What a shortcut should run: the exe when built, else this script."""
        if paths.is_frozen():
            return Path(sys.executable)
        return HERE / "blockslot.py"

    def steam_shortcut(self):
        """Blockslot's own non-Steam shortcut, or None.

        In Game Mode nothing runs that Steam does not launch, so the window
        has to be in the library before it can be opened at all.
        """
        if not self.library.root or self.library.user_id is None:
            return None
        entries = shortcuts.read(
            steamdir.shortcuts_path(self.library.root, self.library.user_id))
        found = shortcuts.find_own(entries, self.entry_point())
        return found[0] if found else None

    def build_steam_shortcut(self):
        """The entry that would be added for this install.

        A built exe is its own launcher and takes only its arguments. The
        source tree needs an interpreter named in front of it.
        """
        target = self.entry_point()
        if paths.is_frozen():
            return shortcuts.new_entry(
                "BlockSlot", str(target), start_dir=str(target.parent),
                options="--fullscreen")
        return shortcuts.new_entry(
            "BlockSlot",
            str(paths.python_for_launch()),
            start_dir=str(HERE),
            options='"%s" --fullscreen' % target)

    def add_steam_shortcut(self):
        """Add it, or update the one that is there. Steam must be closed."""
        if self.library.steam_running():
            raise launchopts.SteamBusy(
                "Steam is running. It would overwrite this on exit.")
        path = steamdir.shortcuts_path(self.library.root, self.library.user_id)
        entries = shortcuts.read(path)
        fresh = self.build_steam_shortcut()
        existing = shortcuts.find_own(entries, self.entry_point())
        if existing:
            # Keep the id it already has, so its artwork and playtime stay
            # attached, and only correct what moved.
            entry = existing[0]
            entry.exe = fresh.exe
            entry.launch_options = fresh.launch_options
            entry.fields["StartDir"] = fresh.start_dir
        else:
            entries.append(fresh)
        shortcuts.write(path, entries)
        return len(entries)

    def engine_source(self):
        """The savepick.py that ships with this copy of Blockslot."""
        return engine.source()

    def summary_lines(self):
        library = self.library
        counts = library.counts()
        lines = [
            "Steam:        %s" % (library.root or "not found"),
            "User:         %s" % (library.user_id or "none"),
            "Settings:     %s" % self.settings.path,
            _engine_line(),
            "Games:        %d, %d on BlockSlot, %d covered by Steam Cloud"
            % (counts["total"], counts["wrapped"], counts["cloud"]),
            "Candidates:   %d" % counts["candidates"],
        ]
        missing = self.settings.missing_sync_keys()
        if missing:
            lines.append("Sync:         not set up (%s)" % ", ".join(missing))
        else:
            lines.append("Sync:         %s, folder %s"
                         % (self.settings.sync.get("url"),
                            self.settings.sync.get("folder")))
        return lines


def _engine_line():
    if paths.engine_in_exe():
        return "Engine:       built into %s%s" % (
            paths.launch_program(),
            "   (a temporary folder: move it first)"
            if paths.in_temporary_place() else "")
    return "Engine:       %s%s" % (paths.engine_path(),
                                   "" if paths.engine_path().is_file()
                                   else "   (not installed)")


def attach_console():
    """Write to the terminal that started us, when there is one.

    The Windows build has no console of its own, on purpose: one that owns a
    console flashes a black window on every start, which is the complaint this
    whole evening began with. A GUI program can still borrow the console of
    whatever started it, which is what makes `Blockslot.exe --check` print in
    cmd and stay silent from Explorer.
    """
    if not paths.is_windows() or not paths.is_frozen():
        return
    try:
        import ctypes
        ATTACH_PARENT = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT):
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except Exception:
        pass


def run_check(controller):
    controller.prepare()
    controller.library.load()
    for line in controller.summary_lines():
        print(line)
    print("")
    rows = controller.library.visible(hide_cloud=True)
    print("%-44s %-13s %-15s %s" % ("GAME", "CLOUD", "SAVES", "BLOCKSLOT"))
    for row in rows:
        print("%-44s %-13s %-15s %s" % (
            row.name[:44],
            {True: "steam cloud", False: "no cloud", None: "unknown"}[row.cloud],
            {True: "known", False: "none known", None: "unknown"}[row.saves],
            "on" if row.syncing else ""))
    return 0


def run_gui(controller, fullscreen=False, size=(1280, 800)):
    from gui.ui import (activity, app as app_mod, games, savesets, setup,
                        store, sync)

    window = app_mod.App(controller, fullscreen=fullscreen, size=size)
    controller.prepare()
    controller.library.load()
    screens = [("games", "Games", games.GamesScreen)]
    if not controller.settings.store().get("type"):
        # With a store set up, Syncthing is not used, and its screen would
        # only invite someone to fix settings nothing reads.
        screens.append(("sync", "Sync", sync.SyncScreen))
    screens.append(("store", "Store", store.StoreScreen))
    window.add_screens(screens + [
        ("sets", "Emulator games", savesets.SaveSetsScreen),
        ("settings", "Settings", setup.SetupScreen),
        ("activity", "Activity", activity.ActivityScreen),
    ])
    try:
        from gui.ui import pad
        pad.attach(window)
    except Exception:
        pass
    window.mainloop()
    return 0


def run_service_command(args):
    """--service, --install-service, --uninstall-service."""
    from gui import tray, winservice
    slotd = tray.load_slotd()
    config = args.config or slotd.default_config_path()
    if args.service:
        return winservice.run_as_service(config, slotd)
    attach_console()
    try:
        if args.install_service:
            lines = winservice.install(config)
            from gui.core import autostart
            autostart.enable(tray=True)
            lines.append("The tray icon starts at login")
        else:
            lines = winservice.uninstall()
            from gui.core import autostart
            autostart.enable()
            lines.append("Login starts the plain daemon again")
    except (RuntimeError, OSError) as exc:
        print("Stopped: %s" % exc, file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


PICK = "--pick"


def load_savepick():
    """The engine that ships with this copy of Blockslot, imported.

    Found the way the daemon is found (tray.load_slotd): the exe carries it
    under engine/ in its bundle, a checkout has it beside gui/. Its folder
    goes on sys.path, so it imports slotd, slotstore and saveunits from
    beside itself exactly as it does when python runs it as a file.
    """
    found = paths.resource("engine/" + paths.ENGINE_NAME)
    folder = found.parent if found else HERE.parent / "engine"
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    import savepick
    return savepick


def run_pick(argv):
    """`Blockslot.exe --pick [switches] -- <game>`: savepick, in this process.

    This is what a launch option runs on a Windows PC with no python. It is
    the same as `pythonw savepick.py [switches] -- <game>`: savepick.main is
    handed everything after --pick, the same list it gets from sys.argv[1:]
    when run as a file, and its answer is the exit code. The build has no
    console, as pythonw has none, so the log file is the only output, and
    no console is borrowed from whatever started it.
    """
    return load_savepick().main(list(argv))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == PICK:
        # Before argparse: everything after -- is the game's command line,
        # which is not ours to parse.
        return run_pick(argv[1:])
    parser = argparse.ArgumentParser(description="BlockSlot save sync")
    parser.add_argument("--check", action="store_true",
                        help="print what BlockSlot can see and exit")
    parser.add_argument("--fullscreen", action="store_true",
                        help="for Game Mode")
    parser.add_argument("--daemon", action="store_true",
                        help="run the store daemon; a tray icon on Windows")
    parser.add_argument("--tray", action="store_true",
                        help="the tray icon for the BlockSlot service (Windows)")
    parser.add_argument("--service", action="store_true",
                        help="run as the Windows service (the service manager starts this)")
    parser.add_argument("--install-service", action="store_true",
                        help="install and start the Windows service (admin)")
    parser.add_argument("--uninstall-service", action="store_true",
                        help="stop and remove the Windows service (admin)")
    parser.add_argument("--steam-root", help="read this Steam folder instead")
    parser.add_argument("--user", type=int, help="Steam3 account id to use")
    parser.add_argument("--config", help="savepick.json to read and write")
    parser.add_argument("--index", help="games.json to read")
    parser.add_argument("--size", default="1280x800",
                        help="window size, WIDTHxHEIGHT")
    args = parser.parse_args(argv)

    if args.daemon:
        # No Steam library and no window: the daemon needs only the store.
        from gui import tray
        return tray.run_daemon(args.config)
    if args.tray:
        from gui import tray
        return tray.run_tray(args.config)
    if args.service or args.install_service or args.uninstall_service:
        return run_service_command(args)
    if args.check:
        attach_console()
    controller = Controller(args.steam_root, args.user, args.config, args.index)
    if args.check:
        return run_check(controller)
    try:
        width, height = (int(part) for part in args.size.lower().split("x"))
    except ValueError:
        width, height = 1280, 800
    try:
        return run_gui(controller, args.fullscreen, (width, height))
    except ImportError as exc:
        print("BlockSlot needs tkinter for its window: %s" % exc,
              file=sys.stderr)
        print("Install it (python3-tk on Debian and Arch) or run --check.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
