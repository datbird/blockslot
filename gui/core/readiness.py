"""Is this device ready: what the Settings screen and --check say.

Both used to read only the Syncthing block, so a device set up with a store
was told it had no name and no sync, next to a header that said everything
was set. The answers live here, once, and depend on which kind of sync the
settings file actually uses: the store when it has a "store" section, and
Syncthing otherwise.

Every answer is (text, ok). ok is True or False, or None while it is still
being found out (the daemon is asked on a worker thread).
"""

from urllib.parse import urlsplit

from . import storecheck

# The daemon has not answered yet. Not None: None means nobody answered.
CHECKING = object()


def using_store(settings):
    return storecheck.using_store(settings)


def device_state(settings, daemon=None):
    """This device's name, as the other devices will see it."""
    if using_store(settings):
        status = daemon if isinstance(daemon, dict) else {}
        # The running daemon's own name is the one in use. Without one, the
        # name it will use (slotd.load_settings reads the same keys).
        return status.get("device") or settings.store_device(), True
    device_dir = settings.device_dir()
    if not device_dir:
        return "not named yet", False
    return settings.device_label(device_dir), True


def describe_store(settings):
    """Where the store is, in a few words: "s3 at host, bucket saves"."""
    block = settings.store()
    kind = settings.store_type()
    if kind == "s3":
        endpoint = block.get("endpoint") or ""
        where = urlsplit(endpoint).netloc or endpoint or "no endpoint"
        text = "S3 at %s" % where
        if block.get("bucket"):
            text += ", bucket %s" % block["bucket"]
        return text
    if kind == "ssh":
        host = block.get("host") or "no host"
        if block.get("user"):
            host = "%s@%s" % (block["user"], host)
        return "SSH to %s:%s" % (host, block.get("root") or "")
    if kind == "local":
        return "the folder %s" % (block.get("root") or "")
    return "a store of kind %s" % (kind or "none")


def sync_state(settings, daemon=CHECKING):
    """How saves travel: the store and its uploader, or Syncthing."""
    if using_store(settings):
        missing = settings.missing_store_keys()
        if missing:
            return "store not set up (%s)" % ", ".join(missing), False
        where = describe_store(settings)
        if daemon is CHECKING:
            return "%s, asking the uploader ..." % where, None
        if not daemon:
            return "%s, the uploader is not running" % where, False
        return "%s, the uploader is running" % where, True
    missing = settings.missing_sync_keys()
    if missing:
        return "Syncthing not set up (%s)" % ", ".join(missing), False
    return ("Syncthing at %s, folder %s"
            % (settings.sync.get("url"), settings.sync.get("folder")), True)


def headline(rows):
    """(text, kind) for the top of the Settings screen.

    rows have label, state, ok and required. "Everything is set" is only said
    when every required row is ok; an optional row that is off never stops
    it, and never counts as a problem.
    """
    problems = [row for row in rows if row.required and row.ok is False]
    if problems:
        first = problems[0]
        text = "%s: %s." % (first.label, first.state)
        if len(problems) > 1:
            text += " And %d more to look at below." % (len(problems) - 1)
        return text, "warn"
    waiting = [row for row in rows if row.required and row.ok is None]
    if waiting:
        return "Checking %s ..." % waiting[0].label.lower(), "info"
    return "Everything BlockSlot needs is set.", "good"


def check_lines(settings, daemon_status=None):
    """The sync part of `blockslot.py --check`, as aligned lines.

    daemon_status is the daemon's own answer (storecheck.daemon_status), or
    None when none answered. Only asked for with a store.
    """
    lines = []
    if using_store(settings):
        missing = settings.missing_store_keys()
        if missing:
            lines.append("Store:        not set up (%s)" % ", ".join(missing))
        else:
            lines.append("Store:        %s" % describe_store(settings))
        lines.append("Device:       %s" % device_state(settings, daemon_status)[0])
        if daemon_status:
            lines.append("Daemon:       running. %s"
                         % storecheck.describe_daemon(daemon_status))
        else:
            lines.append("Daemon:       not running (saves still upload when "
                         "a game exits)")
        return lines
    missing = settings.missing_sync_keys()
    if missing:
        lines.append("Sync:         not set up (%s)" % ", ".join(missing))
    else:
        lines.append("Sync:         %s, folder %s"
                     % (settings.sync.get("url"), settings.sync.get("folder")))
    return lines
