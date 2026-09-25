"""Non-Steam shortcuts: the games Steam did not sell you.

shortcuts.vdf is binary KeyValues, a map keyed by the entry's index as a
string. Blockslot cares about it for two reasons: a retro frontend is normally
a non-Steam shortcut, and so is any launcher whose saves are worth syncing.

Steam rewrites this file when it exits, exactly like localconfig.vdf, so the
same rule applies: write it with Steam closed or the edit is discarded.

%command% DOES NOT WORK HERE ON WINDOWS. It expands to the entry's own `exe`,
so a wrapper written that way hands itself its own path and nothing starts.
A wrapped shortcut therefore names its target after `--` instead.
"""

import binascii
import os
import shutil
from pathlib import Path

from . import vdf, wrap

ROOT_KEY = "shortcuts"


class Shortcut(object):
    """One entry, kept as the raw fields so nothing unknown is dropped."""

    def __init__(self, index, fields):
        self.index = index
        self.fields = fields

    # Steam has used both spellings over the years.
    def _get(self, *names):
        for name in names:
            for key, value in self.fields.items():
                if key.lower() == name.lower():
                    return value
        return None

    def _set(self, value, *names):
        for name in names:
            for key in list(self.fields):
                if key.lower() == name.lower():
                    self.fields[key] = value
                    return
        self.fields[names[0]] = value

    @property
    def appid(self):
        value = self._get("appid")
        if value is None:
            return None
        return value & 0xFFFFFFFF if value < 0 else value

    @property
    def name(self):
        return self._get("AppName", "appname") or ""

    @property
    def exe(self):
        return self._get("Exe", "exe") or ""

    @exe.setter
    def exe(self, value):
        self._set(value, "Exe", "exe")

    @property
    def launch_options(self):
        return self._get("LaunchOptions") or ""

    @launch_options.setter
    def launch_options(self, value):
        self._set(value, "LaunchOptions")

    @property
    def start_dir(self):
        return self._get("StartDir") or ""

    def __repr__(self):
        return "Shortcut(%r)" % self.name


def read(path):
    """Every shortcut in the file, in order. A missing file is an empty list."""
    try:
        with open(str(path), "rb") as handle:
            data = handle.read()
    except OSError:
        return []
    if not data:
        return []
    try:
        tree, _ = vdf.parse_binary(data, 0)
    except vdf.VdfError:
        return []
    block = None
    for key, value in tree.items():
        if key.lower() == ROOT_KEY and isinstance(value, dict):
            block = value
            break
    if block is None:
        return []
    found = []
    for index, fields in block.items():
        if isinstance(fields, dict):
            found.append(Shortcut(index, fields))
    found.sort(key=lambda entry: _as_int(entry.index))
    return found


def write(path, entries, backup=True):
    """Write the whole file back, keeping a copy of the old one.

    Every field that was read is written again, including the ones Blockslot
    does not understand, because Steam put them there for a reason.
    """
    path = Path(path)
    block = {}
    for position, entry in enumerate(entries):
        block[str(position)] = entry.fields
    payload = vdf.dump_binary({ROOT_KEY: block})
    if backup and path.is_file():
        try:
            shutil.copy2(str(path), str(path) + ".blockslot.bak")
        except OSError:
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    temp = str(path) + ".blockslot.tmp"
    with open(temp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, str(path))


# Every field current Steam writes for a shortcut. A new entry carries all of
# them, in this order, because an entry missing a field Steam expects can make
# the client drop the whole list rather than one row.
TEMPLATE = (
    ("appid", 0),
    ("AppName", ""),
    ("exe", ""),
    ("StartDir", ""),
    ("icon", ""),
    ("ShortcutPath", ""),
    ("LaunchOptions", ""),
    ("IsHidden", 0),
    ("AllowDesktopConfig", 1),
    ("AllowOverlay", 1),
    ("OpenVR", 0),
    ("Devkit", 0),
    ("DevkitGameID", ""),
    ("DevkitOverrideAppID", 0),
    ("LastPlayTime", 0),
    ("FlatpakAppID", ""),
)


def new_entry(name, exe, start_dir="", options="", icon=""):
    """A shortcut Steam will accept, with its id already worked out.

    The id is written in on purpose. Steam derives one for an entry that has
    none, and that derivation includes the exe, so an entry whose exe later
    changes loses its artwork and its playtime. Ours will change: the
    interpreter moves when python is upgraded.
    """
    exe = exe if str(exe).startswith('"') else '"%s"' % exe
    fields = {}
    for key, default in TEMPLATE:
        fields[key] = default
    fields["appid"] = as_signed32(generated_appid(exe, name))
    fields["AppName"] = name
    fields["exe"] = exe
    fields["StartDir"] = str(start_dir or "")
    fields["LaunchOptions"] = options or ""
    fields["icon"] = str(icon or "")
    fields["tags"] = {}
    return Shortcut("0", fields)


def as_signed32(value):
    """Steam stores an appid as a signed int32, and ours has the top bit set."""
    value = int(value) & 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


# Blockslot's own entry runs one of these: the script from a source install,
# or the built program, which has no script to name.
OWN_SCRIPT = "blockslot.py"
OWN_PROGRAMS = ("blockslot.exe", "blockslot")


def find_own(entries, entry_point=None):
    """Blockslot's own entries, whichever way this copy was installed.

    A source install names the script in its options. A built program IS the
    exe and names nothing, so looking for the script alone never finds it,
    and every "Add to Steam" would append another copy. Both forms are
    matched, so moving from one install to the other updates the entry that
    is there and keeps its artwork and playtime.

    The program is matched by its file name, not by a word anywhere in the
    path: a game kept in a folder called blockslot is not ours.

    A game wrapped by the Windows exe also names Blockslot.exe, with
    `--pick ... -- <game>` as its options. That is the game's entry, not
    Blockslot's, so a wrapped entry is never counted as our own.
    """
    programs = set(OWN_PROGRAMS)
    if entry_point is not None:
        programs.add(_file_name(entry_point))
    return [entry for entry in entries
            if not wrap.is_wrapped(entry.launch_options or "")
            and (OWN_SCRIPT in (entry.launch_options or "").lower()
                 or _file_name(entry.exe) in programs)]


def _file_name(path):
    text = str(path or "").strip().strip('"').replace("\\", "/")
    return text.rsplit("/", 1)[-1].lower()


def generated_appid(exe, name):
    """The id Steam derives for a shortcut that carries none.

    crc32 of the exe and the name, with the top bit set. Entries written by
    current Steam carry an explicit appid field and keep it; this is for the
    ones that do not, and for working out where the grid art went.
    """
    key = ('"%s"' % exe if not exe.startswith('"') else exe) + name
    crc = binascii.crc32(key.encode("utf-8")) & 0xFFFFFFFF
    return crc | 0x80000000


def run_id(appid):
    """The steam://rungameid number for a non-Steam shortcut."""
    return (int(appid) << 32) | 0x02000000


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
