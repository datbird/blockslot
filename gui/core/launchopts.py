"""Read and write Steam launch options, and survive Steam doing the same.

THE RULE THAT SHAPES THIS FILE

Steam holds localconfig.vdf in memory and writes it out when it exits. An edit
made while Steam is running is discarded at that moment, silently, and the only
symptom is that Blockslot appears not to work. So a write is always:

    stop Steam, wait for the file to settle, edit it, start Steam again

The edit itself is surgical. The file is 400 KB of settings that have nothing
to do with games, written by a program that keeps adding new blocks. Re-writing
it from a parsed tree would drop anything this parser does not model. Editing
the bytes between two offsets cannot.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths, steamdir, vdf

APPS_PATH = ["UserLocalConfigStore", "Software", "Valve", "Steam", "apps"]
LAUNCH_KEY = "LaunchOptions"

# How long Steam is given to write localconfig.vdf out and exit.
STEAM_STOP_SECONDS = 60
# The file has to stop changing for this long before it counts as settled.
SETTLE_SECONDS = 2.0


class SteamBusy(RuntimeError):
    """Steam is running, so a write now would be thrown away."""


def read_all(text):
    """Every app id that has a launch option, to its value."""
    span = vdf.find_block(text, APPS_PATH)
    if span is None:
        return {}
    found = {}
    for appid, block in _app_blocks(text, span):
        pair = _find_pair(text, block, LAUNCH_KEY)
        if pair is not None:
            found[appid] = pair[2]
    return found


def read_apps(text, keys=("LaunchOptions", "LastPlayed", "Playtime")):
    """Selected fields of every app Steam has a record for.

    One pass over the file for the whole games list: which apps you have
    touched, when you last played them, and what their launch option is.
    """
    span = vdf.find_block(text, APPS_PATH)
    if span is None:
        return {}
    found = {}
    for appid, block in _app_blocks(text, span):
        row = {}
        for key in keys:
            pair = _find_pair(text, block, key)
            if pair is not None:
                row[key] = pair[2]
        found[appid] = row
    return found


def read_one(text, appid):
    return read_all(text).get(int(appid))


def write_all(text, changes):
    """Apply {appid: value or None} and return the new text.

    None removes the launch option. An app with no block yet gets one, because
    a game you have never started has no entry and is exactly the game you want
    to set up before the first launch.
    """
    span = vdf.find_block(text, APPS_PATH)
    if span is None:
        raise vdf.VdfError("this localconfig.vdf has no Steam apps block")
    blocks = dict(_app_blocks(text, span))
    edits = []
    appends = []
    for appid, value in changes.items():
        appid = int(appid)
        block = blocks.get(appid)
        if block is None:
            if value is not None:
                appends.append((appid, value))
            continue
        pair = _find_pair(text, block, LAUNCH_KEY)
        if pair is None:
            if value is not None:
                indent = _indent_inside(text, block)
                line = '\n%s"%s"\t\t"%s"' % (indent, LAUNCH_KEY, vdf.escape(value))
                edits.append((block[0] + 1, block[0] + 1, line))
            continue
        if value is None:
            edits.append((_line_start(text, pair[0]), _line_end(text, pair[3]), ""))
        else:
            edits.append((pair[1], pair[3], '"%s"' % vdf.escape(value)))

    if appends:
        indent = _indent_inside(text, span)
        # Sit the new blocks on their own lines, in front of the whitespace
        # that already indents the closing brace.
        at = span[1]
        while at > 0 and text[at - 1] in " \t":
            at -= 1
        addition = []
        for appid, value in appends:
            addition.append('%s"%d"\n%s{\n%s\t"%s"\t\t"%s"\n%s}\n'
                            % (indent, appid, indent, indent, LAUNCH_KEY,
                               vdf.escape(value), indent))
        edits.append((at, at, "".join(addition)))

    out = text
    for start, stop, replacement in sorted(edits, reverse=True):
        out = out[:start] + replacement + out[stop:]
    return out


def _app_blocks(text, apps_span):
    """(appid, (open, close)) for every app inside the apps block."""
    inner = text[apps_span[0] + 1:apps_span[1]]
    base = apps_span[0] + 1
    depth = 0
    pending = None
    for token, quoted, start, stop in vdf.iter_spans(inner):
        if not quoted and token == "{":
            depth += 1
            if depth == 1 and pending is not None and pending.isdigit():
                close = _matching_close(inner, start)
                if close is not None:
                    yield int(pending), (base + start, base + close)
            pending = None
            continue
        if not quoted and token == "}":
            depth -= 1
            pending = None
            continue
        if depth == 0:
            pending = token


def _matching_close(text, open_at):
    depth = 0
    for token, quoted, start, stop in vdf.iter_spans(text[open_at:]):
        if quoted:
            continue
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                return open_at + start
    return None


def _find_pair(text, block, key):
    """A direct child key of a block: (key_start, value_start, value, value_end)."""
    inner_start = block[0] + 1
    inner = text[inner_start:block[1]]
    depth = 0
    pending = None
    pending_at = None
    for token, quoted, start, stop in vdf.iter_spans(inner):
        if not quoted and token == "{":
            depth += 1
            pending = None
            continue
        if not quoted and token == "}":
            depth -= 1
            pending = None
            continue
        if depth != 0:
            continue
        if pending is None:
            pending = token
            pending_at = start
            continue
        if pending.lower() == key.lower():
            return (inner_start + pending_at, inner_start + start, token,
                    inner_start + stop)
        pending = None
    return None


def _indent_inside(text, block):
    """The whitespace that starts the first line inside a block."""
    line = text.find("\n", block[0])
    if line < 0:
        return "\t"
    after = line + 1
    end = after
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[after:end] or "\t"


def _line_start(text, offset):
    start = text.rfind("\n", 0, offset)
    return 0 if start < 0 else start


def _line_end(text, offset):
    end = text.find("\n", offset)
    return len(text) if end < 0 else end


# ------------------------------------------------------------ the file


def load(path):
    with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def save(path, text, backup=True):
    """Write localconfig.vdf, keeping one copy of what was there before.

    The temporary file is written beside the real one so the replace happens on
    the same filesystem, which is what makes it atomic.
    """
    path = Path(path)
    if backup:
        try:
            shutil.copy2(str(path), str(path) + ".blockslot.bak")
        except OSError:
            pass
    temp = str(path) + ".blockslot.tmp"
    with open(temp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, str(path))


# ------------------------------------------------------------ the process


def steam_running():
    """True when a Steam client process is up on this machine."""
    return bool(_steam_pids())


def steam_process_name(system=None):
    """The client's process name: steam_osx on a Mac, steam on Linux."""
    return "steam_osx" if (system or sys.platform) == "darwin" else "steam"


def _steam_pids(run=None, system=None):
    run = run or subprocess.run
    system = system or sys.platform
    if system == "win32":
        try:
            out = run(
                ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                capture_output=True, text=True, timeout=20,
                **paths.no_window()).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in out.splitlines() if "steam.exe" in line.lower()]
    # Only this user's Steam: another account's on the same machine is not
    # the one holding this localconfig.vdf, and waiting for it to close would
    # time out. On a Mac the client is steam_osx: a search for "steam" finds
    # nothing there, and a write would go ahead with Steam open and be lost.
    # It runs from inside ~/Library/Application Support/Steam/Steam.AppBundle,
    # not from /Applications/Steam.app, which only bootstraps it.
    try:
        out = run(["pgrep", "-x", "-u", str(os.getuid()),
                   steam_process_name(system)],
                  capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.split() if line.strip()]


SNAP_STEAM = "/snap/bin/steam"
FLATPAK_STEAM = ["flatpak", "run", "com.valvesoftware.Steam"]


def steam_binary(root=None):
    """The program that starts Steam on this machine, or None."""
    if sys.platform == "darwin":
        app = mac_steam_app()
        return str(Path(app) / "Contents" / "MacOS" / "steam_osx") if app else None
    command = steam_command(root)
    return command[0] if command else None


def steam_command(root=None, which=None, exists=None, system=None):
    """The command that runs the Steam in use at `root`, as a list, or None.

    A root inside the Steam snap or the flatpak is started through them: the
    steam on PATH may be a different install, or none, and running the
    client's own files from outside its sandbox does not work.
    """
    which = which or shutil.which
    exists = exists or os.path.exists
    system = system or sys.platform
    if system == "win32":
        if root:
            candidate = Path(root) / "steam.exe"
            if candidate.is_file():
                return [str(candidate)]
        found = which("steam.exe")
        return [found] if found else None
    if system == "darwin":
        # Through LaunchServices, as the Dock would, so Steam is not a child
        # of this window and does not go when it closes.
        app = mac_steam_app()
        return ["/usr/bin/open", "-a", app] if app else None
    kind = steamdir.install_kind(root) if root else None
    if kind == steamdir.SNAP and exists(SNAP_STEAM):
        # With the distribution's steam installed as well, `steam` on PATH
        # is that one, and it would ask a Steam that is not running to shut
        # down. The snap's own launcher always lives here.
        return [SNAP_STEAM]
    if kind == steamdir.FLATPAK and which("flatpak"):
        # The flatpak is started through `flatpak run`, not by a file of
        # its own.
        return list(FLATPAK_STEAM)
    found = which("steam") or (SNAP_STEAM if exists(SNAP_STEAM) else None)
    return [found] if found else None


def shutdown_command(root=None, system=None, **found):
    """The command that asks the running Steam to exit cleanly, or None.

    `steam -shutdown` on Windows and Linux. A Mac's steam_osx is started by
    Launch Services, not by hand, so it is asked through Steam's own URL,
    which the running client handles the same way.
    """
    if (system or sys.platform) == "darwin":
        return ["/usr/bin/open", "steam://exit"]
    command = steam_command(root, system=system, **found)
    return command + ["-shutdown"] if command else None


def _detached():
    """Popen arguments that leave Steam running after this window closes."""
    if sys.platform == "win32":
        return paths.no_window()
    # Its own session, so closing the terminal Blockslot was started from, or
    # Blockslot itself, does not take a restarted Steam down with it.
    return {"start_new_session": True}


def mac_steam_app(exists=os.path.isdir):
    """Steam.app, in /Applications or the user's own Applications, or None."""
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        if exists(str(folder / "Steam.app")):
            return str(folder / "Steam.app")
    return None


def stop_command(root=None, platform=None):
    """The command that asks Steam to exit cleanly, or None.

    On a Mac that is the steam://exit link, opened by LaunchServices: it
    reaches the Steam that is running and needs no permission to script
    another app. Elsewhere, `steam -shutdown` (shutdown_command).
    """
    return shutdown_command(root, system=platform)


def start_command(root=None, platform=None):
    """The command that starts Steam, or None (steam_command).

    A Mac starts it through LaunchServices, as the Dock would, so Steam is
    not a child of this window and does not go when it closes.
    """
    return steam_command(root, system=platform)


def stop_steam(root=None, timeout=STEAM_STOP_SECONDS):
    """Ask Steam to exit and wait for it. True once nothing is left running.

    `steam -shutdown` is Steam's own clean exit. It flushes localconfig.vdf on
    the way out, which is the whole point of asking rather than killing.
    """
    if not steam_running():
        return True
    command = shutdown_command(root)
    if command:
        try:
            subprocess.Popen(command,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, **_detached())
        except (OSError, subprocess.SubprocessError):
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not steam_running():
            return True
        time.sleep(1.0)
    return not steam_running()


def start_steam(root=None):
    command = start_command(root)
    if not command:
        return False
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         **_detached())
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def wait_until_settled(path, timeout=30, quiet=SETTLE_SECONDS):
    """Wait for a file to stop changing, so a write lands after Steam's own."""
    path = Path(path)
    deadline = time.monotonic() + timeout
    last = None
    steady_since = None
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            signature = None
        now = time.monotonic()
        if signature != last:
            last = signature
            steady_since = now
        elif steady_since is not None and now - steady_since >= quiet:
            return True
        time.sleep(0.3)
    return False


class SteamStayedOpen(Exception):
    """Steam was asked to close and did not, so nothing was changed."""


def with_steam_closed(root, settle_path, work, say=lambda text: None):
    """Run `work` with Steam shut, which is the only time a write survives.

    Stop Steam, wait for its own write of `settle_path` to land, do the work,
    and start Steam again if it was running. Steam comes back even when the
    work fails: a failed write is put right by the caller's own backup, and a
    person left without Steam has a second problem on top of the first.
    """
    was_running = steam_running()
    if was_running:
        say("Asking Steam to close ...")
        if not stop_steam(root):
            raise SteamStayedOpen("Steam did not close. Nothing was changed.")
        say("Steam closed. Waiting for its settings file to settle ...")
        wait_until_settled(settle_path)
    try:
        return work()
    finally:
        if was_running:
            say("Starting Steam again ...")
            start_steam(root)
