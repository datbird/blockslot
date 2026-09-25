"""What the hub is actually holding, asked of ludusavi rather than guessed.

The one question this product exists to answer is "is my save carried". The
answer is in the shared folder: one directory per device, a directory per game
inside it, and ludusavi's own index of every backup in there.

ludusavi is asked, not the filesystem, for the same reason the engine asks it:
the layout inside a backup directory is ludusavi's business and it has changed
before. `backups --api` is its supported answer.

NOTHING HERE DECIDES ANYTHING. It reports times. Which save wins at launch is
the engine's decision, made with the live save in hand, and a second opinion
computed here would eventually disagree with it.
"""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import paths

TIMEOUT = 90


def ask(binary, args, timeout=TIMEOUT):
    """Run ludusavi and parse its JSON. None when it cannot be run.

    `binary` may be a list, which is how a test points this at a stand-in
    without needing an executable file on every operating system.
    """
    head = [str(part) for part in binary] if isinstance(binary, (list, tuple)) \
        else [str(binary)]
    command = head + ["--no-manifest-update"] + list(args) + ["--api"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              **paths.no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    for payload in (proc.stdout, proc.stderr):
        if not payload:
            continue
        try:
            return json.loads(payload)
        except ValueError:
            continue
    return None


def backups(binary, path=None):
    """{game: [{name, when}]} for one device's backup directory."""
    args = ["backups"]
    if path:
        args += ["--path", str(path)]
    return _entries(ask(binary, args))


def _entries(data):
    if not isinstance(data, dict):
        return {}
    found = {}
    for game, row in (data.get("games") or {}).items():
        if not isinstance(row, dict):
            continue
        entries = [entry for entry in (row.get("backups") or [])
                   if isinstance(entry, dict) and entry.get("when")]
        if entries:
            found[game] = entries
    return found


def own_directory(binary):
    """This device's own backup directory, as ludusavi has it configured.

    Read from a backup path it reports rather than from its config file: the
    config has moved between versions and a reported path is what it is
    actually using.
    """
    return _directory(ask(binary, ["backups"]))


def _directory(data):
    games = data.get("games") if isinstance(data, dict) else None
    for row in (games or {}).values():
        path = (row or {}).get("backupPath")
        if path:
            return Path(path).parent
    return None


def peer_directories(own):
    """The other devices' directories, which are this one's siblings."""
    if own is None:
        return []
    own = Path(own)
    try:
        entries = sorted(item for item in own.parent.iterdir() if item.is_dir())
    except OSError:
        return []
    return [item for item in entries
            if item.name != own.name and not item.name.startswith(".")]


def newest_everywhere(binary, own=None):
    """{game: (when, device directory name)} across this device and its peers.

    A game the hub has never seen is simply absent, which is what the games
    screen shows as a blank rather than as a zero.
    """
    # One answer from ludusavi gives both this device's backups and where it
    # keeps them, so it is asked once.
    mine = ask(binary, ["backups"])
    own = own or _directory(mine)
    newest = {}

    def fold(rows, device):
        for game, entries in rows.items():
            when = max(entry["when"] for entry in entries)
            current = newest.get(game)
            if current is None or when > current[0]:
                newest[game] = (when, device)

    if own is not None:
        fold(_entries(mine), Path(own).name)
        peers = peer_directories(own)
        # Each peer is its own read-only ludusavi process, so they run at once.
        for peer, rows in zip(peers, _together(
                lambda peer: backups(binary, peer), peers)):
            fold(rows, peer.name)
    return newest


def _together(work, items):
    if len(items) < 2:
        return [work(item) for item in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(work, items))


def when_text(stamp, now=None):
    """An ISO timestamp as something a person reads at a glance."""
    if not stamp:
        return ""
    try:
        text = stamp.replace("Z", "+0000")
        # ludusavi writes fractional seconds of varying length.
        if "." in text:
            head, rest = text.split(".", 1)
            digits = "".join(ch for ch in rest if ch.isdigit())[:6]
            tail = rest[len(digits):] if rest[len(digits):].startswith("+") \
                else "+0000"
            text = "%s.%s%s" % (head, digits.ljust(6, "0"), tail)
        seconds = _parse(text)
    except (ValueError, TypeError):
        return ""
    if seconds is None:
        return ""
    return ago_text((now if now is not None else time.time()) - seconds)


def ago_text(delta):
    """How long ago, from a number of seconds."""
    if delta < 0:
        return "just now"
    if delta < 3600:
        return "%dm ago" % max(1, int(delta // 60))
    if delta < 86400:
        return "%dh ago" % max(1, int(round(delta / 3600.0)))
    # Rounded, not truncated. A backup two days old to the second is "2d" to
    # a person, and truncation calls it 1d for the whole first second.
    days = max(1, int(round(delta / 86400.0)))
    if days < 30:
        return "%dd ago" % days
    if days < 365:
        return "%dmo ago" % (days // 30)
    return "%dy ago" % (days // 365)


def _parse(text):
    for shape in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, shape).timestamp()
        except ValueError:
            continue
    return None
