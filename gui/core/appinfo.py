"""Steam's own appinfo.vdf, read for one fact: does this game use Steam Cloud.

WHY NOT THE MANIFEST

The ludusavi manifest carries a cloud field and it is right most of the time.
Dark Souls III is the counter-example: the manifest said no cloud, Steam's own
cache said yes. Steam is the authority on its own service, so when this file is
readable its answer wins.

WHAT COUNTS AS CLOUD

Two different features wear the name:

    ufs.savefiles       Auto-Cloud, where Steam itself matches file patterns
    common.cloud*       the quota an SDK game gets when it calls the API

Auto-Cloud is visible here as real patterns. An SDK game is visible only as a
quota, because the paths live in the game's code. Both mean Steam is already
syncing that game, which is all Blockslot needs to know to leave it alone.

FORMAT

    magic uint32        0x07564429 v29, 0x07564428 v28, 0x07564427 v27
    universe uint32
    string table offset int64                       (v29 only)
    per app:
        appid uint32                                0 ends the file
        size uint32                                 bytes after this field
        infoState, lastUpdated uint32
        picsToken uint64
        text sha1 20 bytes
        changeNumber uint32
        binary sha1 20 bytes                        (v28 and later)
        binary KeyValues, string table keys in v29

The string table is at the recorded offset: a uint32 count then that many null
terminated strings.
"""

import struct

from . import vdf

MAGIC_V27 = 0x07564427
MAGIC_V28 = 0x07564428
MAGIC_V29 = 0x07564429

SUPPORTED = (MAGIC_V27, MAGIC_V28, MAGIC_V29)


class AppInfoError(ValueError):
    """appinfo.vdf is not in a shape this knows how to read."""


def read_string_table(data, offset):
    count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    table = []
    for _ in range(count):
        end = data.find(b"\x00", offset)
        if end < 0:
            raise AppInfoError("the string table runs off the end")
        table.append(data[offset:end].decode("utf-8", "replace"))
        offset = end + 1
    return table


def iter_apps(data):
    """Yield (appid, tree) for every app in the file.

    A single unreadable app stops the walk, because the entries are laid out
    end to end and a wrong length means every later offset is wrong. Whatever
    was read before that point is still good and is already yielded.
    """
    if len(data) < 8:
        raise AppInfoError("too short to be appinfo.vdf")
    magic, _universe = struct.unpack_from("<II", data, 0)
    if magic not in SUPPORTED:
        raise AppInfoError("unknown appinfo version 0x%08x" % magic)
    offset = 8
    table = None
    if magic == MAGIC_V29:
        table_offset = struct.unpack_from("<q", data, offset)[0]
        offset += 8
        table = read_string_table(data, table_offset)
    header = 4 + 4 + 8 + 20 + 4 + (20 if magic != MAGIC_V27 else 0)
    while offset + 8 <= len(data):
        appid, size = struct.unpack_from("<II", data, offset)
        offset += 8
        if appid == 0:
            return
        body = offset + header
        following = offset + size
        if following > len(data) or body > following:
            return
        try:
            tree, _ = vdf.parse_binary(data, body, table)
        except vdf.VdfError:
            return
        yield appid, tree
        offset = following


def cloud_appids(data):
    """Every appid in this file that Steam Cloud already covers.

    Auto-Cloud shows as ufs.savefiles. An SDK game shows as a cloud quota on
    the common block. Either way Steam is syncing it.
    """
    found = set()
    for appid, tree in iter_apps(data):
        if uses_cloud(tree):
            found.add(appid)
    return found


def uses_cloud(tree):
    app = tree.get("appinfo", tree)
    ufs = app.get("ufs") or {}
    if isinstance(ufs, dict):
        if ufs.get("savefiles"):
            return True
        if _positive(ufs.get("quota")) and _positive(ufs.get("maxnumfiles")):
            return True
    common = app.get("common") or {}
    if isinstance(common, dict):
        for key in ("cloudavailable", "clouddisabled"):
            value = common.get(key)
            if key == "cloudavailable" and _positive(value):
                return True
    return False


def _positive(value):
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def scan(data):
    """One pass for everything appinfo.vdf is read for."""
    cloud = set()
    types = {}
    names = {}
    for appid, tree in iter_apps(data):
        app = tree.get("appinfo", tree)
        common = app.get("common") or {}
        if uses_cloud(tree):
            cloud.add(appid)
        if isinstance(common, dict):
            kind = common.get("type")
            if kind:
                types[appid] = str(kind).lower()
            name = common.get("name")
            if name:
                names[appid] = name
    return {"cloud": cloud, "types": types, "names": names}

