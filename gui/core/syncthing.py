"""Syncthing, read over its REST API and found on disk when it is not set up.

The engine already talks to Syncthing during a launch, to wait for the hub's
saves to land. The GUI needs the same server for a different reason: to say
whether the thing is working, in a sentence a person can act on.

Nothing here ever changes a folder or a device. Blockslot configures its own
settings and reports Syncthing's, because a tool that rewrites another
program's config is a tool that can take a folder away from you.
"""

import json
import socket
from pathlib import Path

from . import paths

# urllib and xml are imported where they are used, not here.
#
# Decky Loader ships a frozen python and its bundle carries only the standard
# library modules Decky itself needs. `xml.etree` is not one of them, and an
# import at module level took the whole plugin down on load. A feature that
# cannot run has to degrade to a message, not to a dead plugin.

TIMEOUT = 8


class Result(object):
    """An answer, or the reason there is not one. Never an exception."""

    def __init__(self, ok, value=None, error=None, status=None):
        self.ok = ok
        self.value = value
        self.error = error
        self.status = status

    def __bool__(self):
        return self.ok

    __nonzero__ = __bool__


def get(config, endpoint, timeout=TIMEOUT):
    """GET a Syncthing endpoint. Returns a Result holding parsed JSON."""
    try:
        import urllib.error
        import urllib.request
    except ImportError as exc:
        return Result(False, error="no HTTP support in this python (%s)" % exc)
    url = (config.get("url") or "").rstrip("/")
    key = config.get("apikey") or ""
    if not url:
        return Result(False, error="no server address is set")
    if not key:
        return Result(False, error="no API key is set")
    request = urllib.request.Request(url + endpoint, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        return Result(True, json.loads(body.decode("utf-8")))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return Result(False, error="the API key was refused", status=403)
        if exc.code == 404:
            return Result(False, error="no such folder or endpoint", status=404)
        return Result(False, error="the server answered %s" % exc.code,
                      status=exc.code)
    except urllib.error.URLError as exc:
        return Result(False, error="cannot reach %s (%s)" % (url, exc.reason))
    except (socket.timeout, TimeoutError):
        return Result(False, error="%s did not answer in %ds" % (url, timeout))
    except (ValueError, OSError) as exc:
        return Result(False, error=str(exc))


def status(config):
    """Who this device is, as Syncthing knows it."""
    return get(config, "/rest/system/status")


def version(config):
    return get(config, "/rest/system/version")


def connections(config):
    return get(config, "/rest/system/connections")


def folder_status(config, folder=None):
    folder = folder or config.get("folder")
    if not folder:
        return Result(False, error="no folder id is set")
    return get(config, "/rest/db/status?folder=%s" % folder)


def folder_config(config, folder=None):
    folder = folder or config.get("folder")
    if not folder:
        return Result(False, error="no folder id is set")
    return get(config, "/rest/config/folders/%s" % folder)


def devices(config):
    return get(config, "/rest/config/devices")


def check(config):
    """One pass over everything, in the order a person would debug it.

    Returns a list of (label, ok, detail). The first failure explains itself
    and the rest are skipped, because "folder not found" after "key refused"
    is noise.
    """
    steps = []
    if not (config.get("url") or ""):
        steps.append(("Server address", False, "not set"))
        return steps
    if not (config.get("apikey") or ""):
        steps.append(("API key", False, "not set"))
        return steps

    answer = status(config)
    if not answer.ok:
        steps.append(("Syncthing", False, answer.error))
        return steps
    steps.append(("Syncthing", True, "this device is %s"
                  % _short_id(answer.value.get("myID", ""))))

    folder = config.get("folder")
    if not folder:
        steps.append(("Folder", False, "no folder id is set"))
        return steps
    conf = folder_config(config, folder)
    if not conf.ok:
        steps.append(("Folder %s" % folder, False, conf.error))
        return steps
    steps.append(("Folder %s" % folder, True, conf.value.get("path") or ""))

    state = folder_status(config, folder)
    if not state.ok:
        steps.append(("Folder state", False, state.error))
        return steps
    steps.append(("Folder state", state.value.get("state") == "idle",
                  describe_folder(state.value)))

    hub = config.get("hub_id")
    if hub:
        seen = connections(config)
        if not seen.ok:
            steps.append(("Hub", False, seen.error))
        else:
            entry = (seen.value.get("connections") or {}).get(hub) or {}
            connected = bool(entry.get("connected"))
            label = config.get("hub_name") or _short_id(hub)
            steps.append(("Hub %s" % label, connected,
                          "connected" if connected else "not connected"))
    return steps


def describe_folder(state):
    """One line about a folder: idle, or what it is still doing."""
    if not isinstance(state, dict):
        return "no answer"
    name = state.get("state") or "unknown"
    need_files = int(state.get("needFiles") or 0)
    need_bytes = int(state.get("needBytes") or 0)
    errors = int(state.get("errors") or 0)
    parts = [name]
    if need_files:
        parts.append("%d file%s to go (%s)"
                     % (need_files, "" if need_files == 1 else "s",
                        human_bytes(need_bytes)))
    if errors:
        parts.append("%d error%s" % (errors, "" if errors == 1 else "s"))
    return ", ".join(parts)


def human_bytes(count):
    count = float(count or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return "%.0f %s" % (count, unit) if unit == "B" else "%.1f %s" % (count, unit)
        count /= 1024
    return "%.1f TB" % count


def _short_id(device_id):
    return (device_id or "").split("-")[0]


# ------------------------------------------------------------ finding it


def config_locations():
    """Every place Syncthing keeps config.xml on this operating system."""
    home = Path.home()
    if paths.is_windows():
        import os
        base = os.environ.get("LOCALAPPDATA") or str(home)
        return [Path(base) / "Syncthing" / "config.xml"]
    if paths.is_mac():
        return [home / "Library" / "Application Support" / "Syncthing" / "config.xml"]
    return [
        home / ".local" / "state" / "syncthing" / "config.xml",
        home / ".config" / "syncthing" / "config.xml",
        home / ".var" / "app" / "me.kozec.syncthingtk" / "config" / "syncthing" / "config.xml",
    ]


def read_local_config(locations=None):
    """The API key and address Syncthing is using on this machine, or None.

    Typing an API key by hand is the step people get wrong, and the file is
    right there. Reading it is not a privilege escalation: it is this user's
    own config, readable by this user.
    """
    try:
        import xml.etree.ElementTree as ElementTree
    except ImportError:
        # Decky's frozen python has no xml. Reading Syncthing's own config is a
        # convenience, and the settings can still be typed in by hand.
        return None
    for path in (locations if locations is not None else config_locations()):
        try:
            tree = ElementTree.parse(str(path))
        except (OSError, ElementTree.ParseError):
            continue
        root = tree.getroot()
        gui = root.find("gui")
        if gui is None:
            continue
        key = (gui.findtext("apikey") or "").strip()
        address = (gui.findtext("address") or "127.0.0.1:8384").strip()
        scheme = "https" if (gui.get("tls") or "").lower() == "true" else "http"
        if address.startswith("0.0.0.0"):
            address = "127.0.0.1" + address[len("0.0.0.0"):]
        folders = []
        for folder in root.findall("folder"):
            folders.append({
                "id": folder.get("id"),
                "label": folder.get("label") or folder.get("id"),
                "path": folder.get("path"),
            })
        devices_found = []
        for device in root.findall("device"):
            devices_found.append({
                "id": device.get("id"),
                "name": device.get("name") or _short_id(device.get("id") or ""),
            })
        return {
            "path": str(path),
            "apikey": key,
            "url": "%s://%s" % (scheme, address),
            "folders": folders,
            "devices": devices_found,
        }
    return None
