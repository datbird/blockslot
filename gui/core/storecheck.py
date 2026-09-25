"""Is the store set up right: the Store screen's test, and what the daemon says.

Kept apart from the screen so it can be tested without a window. Every answer
is a list of plain lines, because the person reading them is looking at a
handheld across a room, not at a traceback.

The test writes and deletes a real object. Listing alone proves the key can
read, and a key that can read but not write would pass it and then fail on
the first save after a game, which is the worst moment to find out.
"""

import base64
import binascii
import datetime
import json
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path

from . import paths
from . import settings as settings_mod

PROBE_BODY = b"blockslot probe\n"
SETUP_FIELDS = ("endpoint", "bucket", "region", "access_key", "secret_key",
                "device", "cf_client_id", "cf_client_secret")


def parse_setup_code(text):
    """The store section a BlockSlot server's setup code holds.

    The server's Devices page shows the code once, when it makes a device's
    key: base64 of a JSON object with the S3 endpoint, bucket, region, key,
    the device name and, when Cloudflare is set up, the service token.
    ValueError says, in words a person can act on, why a code will not do.
    """
    compact = "".join((text or "").split())
    if not compact:
        raise ValueError("Copy the setup code from the BlockSlot server first.")
    try:
        body = json.loads(base64.b64decode(compact, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValueError("That is not a BlockSlot setup code. Copy the whole "
                         "line from the server's Devices page.")
    return _setup_values(body)


def _setup_values(body, what="That setup code"):
    if not isinstance(body, dict) or body.get("type") != "s3":
        raise ValueError("%s is not for an S3 store." % what)
    missing = [key for key in ("endpoint", "bucket", "access_key", "secret_key")
               if not body.get(key)]
    if missing:
        raise ValueError("%s has no %s." % (what, ", ".join(missing)))
    values = {"type": "s3"}
    values.update((key, str(body[key])) for key in SETUP_FIELDS if body.get(key))
    return values


PAIR_TIMEOUT = 15


def server_address(text):
    """The web address of a BlockSlot server, as a person typed it.

    "192.168.1.20:8761" and "http://nas:8761/" both come out as a base URL
    with no trailing slash. No scheme means http, because a server on the
    LAN usually has no certificate.
    """
    text = (text or "").strip().rstrip("/")
    if not text:
        raise ValueError("Enter the BlockSlot server's address, as the "
                         "Devices page shows it.")
    if "://" not in text:
        text = "http://" + text
    return text


def pair(address, code, opener=None):
    """The store section for this device, fetched with a pairing code.

    The server's Devices page shows a short code (XXXX-XXXX) when it adds a
    device. The code is good once, for 15 minutes, and a handheld can type
    it, which a 300-character setup code is not. ValueError says why pairing
    did not work, in words a person can act on.
    """
    base = server_address(address)
    cleaned = "".join(ch for ch in (code or "") if ch.isalnum()).upper()
    if len(cleaned) != 8:
        raise ValueError("A pairing code has 8 letters and digits, like K7QX-4MPA.")
    request = urllib.request.Request(
        base + "/api/pair", data=json.dumps({"code": cleaned}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "BlockSlot/1"},
        method="POST")
    context = None
    if base.startswith("https://"):
        try:
            # Decky's frozen python has no CA store of its own.
            context = _engine().tls_context()
        except Exception:
            context = None
    try:
        if opener is not None:
            response = opener(request, PAIR_TIMEOUT)
        else:
            response = urllib.request.urlopen(request, timeout=PAIR_TIMEOUT,
                                              context=context)
        with response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error")
        except (ValueError, AttributeError, OSError):
            message = None
        raise ValueError(message or "The server refused the code (HTTP %d)." % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError("Cannot reach %s: %s. Check the address, and that this "
                         "device is on the same network." % (base, reason))
    except ValueError:
        raise ValueError("%s answered, but not like a BlockSlot server. Check "
                         "the address and port." % base)
    return _setup_values((body or {}).get("setup"), "The server's answer")


def _engine():
    return settings_mod.engine_module("slotstore")


def _section(settings):
    """A Settings object or a bare store section, either way opened."""
    if hasattr(settings, "store_for_engine"):
        return settings.store_for_engine()
    return dict(settings or {})


def _missing(section):
    kind = (section.get("type") or "").lower()
    if kind not in settings_mod.STORE_REQUIRED:
        return ["type"]
    return [key for key in settings_mod.STORE_REQUIRED[kind] if not section.get(key)]


def explain(exc, section=None):
    """A store error in words a person can act on."""
    ss = _engine()
    reason = str(exc).strip()
    if isinstance(exc, ss.StoreOffline):
        return "Could not reach the server: %s" % reason
    if isinstance(exc, ss.NotFound):
        return "The server does not have it: %s" % reason
    if isinstance(exc, ss.StoreRefused):
        text = "The server said no: %s" % reason
        lower = reason.lower()
        has_token = bool(section and section.get("cf_client_id"))
        from_cloudflare = "cloudflare" in lower or (
            has_token and any(code in lower for code in ("401", "403", "forbidden")))
        if from_cloudflare and "missing, wrong or expired" not in lower:
            text += ". The Cloudflare token is missing, wrong or expired."
        return text
    return "Something went wrong: %s" % (reason or exc.__class__.__name__)


def test_store(settings, device=None, store=None):
    """Try the store the way a save will use it. [(label, ok, detail)].

    Stops at the first step that fails, since every later step would fail
    for the same reason and only bury it.
    """
    lines = []
    try:
        section = _section(settings)
    except Exception as exc:
        return [("Read the settings", False, explain(exc))]
    missing = _missing(section)
    if store is None and missing:
        if missing == ["type"]:
            return [("Settings", False, "Pick S3, SSH or Folder first.")]
        return [("Settings", False, "Still needed: %s." % ", ".join(missing))]

    ss = _engine()
    if store is None:
        try:
            store = ss.store_from_settings(section)
        except KeyError as exc:
            return [("Settings", False, "Still needed: %s." % exc.args[0])]
        except Exception as exc:
            return [("Settings", False, explain(exc, section))]
    name = ss.store_name(section) if section else getattr(store, "label", "the store")
    lines.append(("Settings", True, "Using %s" % name))

    if device is None:
        device = section.get("device") or _hostname()
    probe = "%sprobe/%s-%s" % (ss.PREFIX, ss.device_key(device), secrets.token_hex(4))

    try:
        found = store.list(ss.PREFIX)
    except Exception as exc:
        lines.append(("Reach the store", False, explain(exc, section)))
        return lines
    lines.append(("Reach the store", True,
                  "%d object%s under %s" % (len(found), "" if len(found) == 1 else "s",
                                            ss.PREFIX)))

    try:
        store.put(probe, PROBE_BODY)
    except Exception as exc:
        lines.append(("Write a test file", False, explain(exc, section)))
        return lines
    lines.append(("Write a test file", True, probe))

    try:
        back = store.get(probe)
    except Exception as exc:
        lines.append(("Read it back", False, explain(exc, section)))
        _quiet_delete(store, probe)
        return lines
    if back != PROBE_BODY:
        lines.append(("Read it back", False,
                      "The file came back different from what was written."))
        _quiet_delete(store, probe)
        return lines
    lines.append(("Read it back", True, "It matches"))

    try:
        store.delete(probe)
    except Exception as exc:
        lines.append(("Delete it", False, explain(exc, section)))
        return lines
    lines.append(("Delete it", True, "The store is ready for saves"))
    return lines


def _quiet_delete(store, key):
    """Tidy up a probe after a failure. A second failure says nothing new."""
    try:
        store.delete(key)
    except Exception:
        pass


def _hostname():
    import socket
    return socket.gethostname()


# ------------------------------------------------------------------ import


def default_import_folder(settings, syncthing_config=None):
    """Where the old Syncthing gamesaves folder probably is, or "".

    savepick.json names only this device's folder INSIDE the share, and
    Syncthing is the one that knows where the share is, so its own config is
    asked. `syncthing_config` is what syncthing.read_local_config returned, for
    tests; None reads this machine's.
    """
    block = settings.sync if hasattr(settings, "sync") else {}
    device_dir = block.get("device_dir") or ""
    if device_dir and os.path.isabs(device_dir):
        return str(Path(device_dir).parent)
    folder_id = block.get("folder")
    if not folder_id:
        return ""
    if syncthing_config is None:
        from . import syncthing
        syncthing_config = syncthing.read_local_config() or {}
    for folder in syncthing_config.get("folders") or []:
        if folder.get("id") == folder_id and folder.get("path"):
            return str(Path(folder["path"]).expanduser())
    return ""


def count_backups(root):
    """(backups, devices, games) under a gamesaves folder."""
    found = _engine().find_ludusavi_backups(root)
    return (len(found), len(set(row[0] for row in found)),
            len(set(row[1] for row in found)))


def import_folder(settings, root, say=None):
    """Put every old backup under root on the store. Returns {game: head}."""
    ss = _engine()
    section = _section(settings)
    store = ss.store_from_settings(section)

    def progress(done, total, game, device):
        if say:
            say("%d of %d: %s %s" % (done, total, device, game))

    return ss.import_backups(store, root, progress=progress)


# ------------------------------------------------------------------ daemon


def daemon_state_dir(settings):
    slotd = settings_mod.engine_module("slotd")
    return settings.store().get("state_dir") or slotd.default_state_dir()


def daemon_status(settings):
    """What the daemon says, or None when no daemon answers."""
    slotd = settings_mod.engine_module("slotd")
    client = slotd._client_from_info(daemon_state_dir(settings))
    if client is None:
        return None
    try:
        return client.status()
    except Exception:
        return None


def describe_daemon(status):
    """One line about the daemon, from Client.status() or None."""
    if not status:
        return "The daemon is not running."
    parts = []
    queued = status.get("queued") or []
    if queued:
        parts.append("%d save%s waiting to upload." % (
            len(queued), "" if len(queued) == 1 else "s"))
    else:
        parts.append("Nothing waiting to upload.")
    if status.get("paused"):
        parts.append("Uploads are paused.")
    if status.get("last_ok"):
        parts.append("Last reached the store %s." % local_time(status["last_ok"]))
    error = status.get("error")
    if error:
        parts.append("Last error: %s" % (error.get("message") or error.get("kind")))
    return " ".join(parts)


def local_time(text):
    """An ISO UTC time from the engine, as this machine's clock shows it."""
    try:
        when = datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return text or ""
    when = when.replace(tzinfo=datetime.timezone.utc).astimezone()
    return when.strftime("%Y-%m-%d %H:%M")


def restart_daemon(settings, wait=5.0, starter=None):
    """Start the daemon on the settings just saved. Returns a short line.

    A running daemon read its settings when it started, so it is asked to
    stop first. On Windows the daemon is Blockslot's own --daemon host, with
    its tray icon, and it is also set to start at login; elsewhere it is
    slotd on its own. Nothing here waits on the store.
    """
    import subprocess
    import time as _time
    from . import autostart
    slotd = settings_mod.engine_module("slotd")
    state_dir = daemon_state_dir(settings)
    if settings.store().get("service"):
        # The service watches savepick.json and reloads on its own, and a
        # restart would need an admin token the window does not have.
        deadline = _time.monotonic() + 30
        while _time.monotonic() < deadline:
            client = slotd._client_from_info(state_dir)
            if client is not None:
                return "The BlockSlot service picks up the new settings by itself."
            _time.sleep(1)
        return "The BlockSlot service is not answering. Is it running?"
    client = slotd._client_from_info(state_dir)
    if client is not None:
        try:
            client.stop()
        except Exception:
            pass
        deadline = _time.monotonic() + wait
        while _time.monotonic() < deadline and slotd._client_from_info(state_dir):
            _time.sleep(0.2)
    if starter is not None:
        starter()
    elif paths.is_windows():
        autostart.enable()
        subprocess.Popen(autostart.command_argv(), close_fds=True,
                         creationflags=0x00000008 | 0x00000200 | 0x08000000)
    else:
        slotd.start_detached()
    deadline = _time.monotonic() + wait
    while _time.monotonic() < deadline:
        if slotd._client_from_info(state_dir):
            return "The uploader is running%s." % (
                " and starts at login" if paths.is_windows() else "")
        _time.sleep(0.2)
    return "The uploader did not start. Saves still upload when a game exits."


def newest_on_store(settings):
    """{game_key: (when, device)} from the store, and the key function to
    match a game name against it. Raises the store's own error."""
    slotstore = settings_mod.engine_module("slotstore")
    store = slotstore.store_from_settings(settings.store_for_engine())
    return slotstore.newest_on_store(store), slotstore.game_key


def using_store(settings):
    """True when saves go to the store rather than through Syncthing."""
    return bool(settings.store().get("type"))
