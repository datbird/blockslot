"""The savepick launch option: build it, recognise it, take it off again.

A wrapped Steam game looks like this in localconfig.vdf:

    "LaunchOptions"  "<python> <savepick.py> -- %command%"

or, from the built Windows exe, which carries the engine inside it so that a
PC needs no python at all:

    "LaunchOptions"  "<Blockslot.exe> --pick -- %command%"

Steam replaces %command% with everything it would have run, so savepick gets
the real command line and starts it itself. That is the whole hook, and it is
the only point in a launch that is early enough to be safe: the restore has to
finish before the game opens its save file.

KEEPING WHAT WAS ALREADY THERE

A launch option is the player's, not ours. Three cases, and all three keep it:

    ""                       ->  <wrap> -- %command%
    "-windowed"              ->  <wrap> -- %command% -windowed
    "DXVK_HUD=1 %command%"   ->  <wrap> -- DXVK_HUD=1 %command%

The third is passed through untouched because the player put %command% exactly
where they meant it. Removing the wrap gives back what was there before.

Both heads are recognised whichever one this install writes, so a game wrapped
by an older install with python is noticed, unwrapped and wrapped again in the
new form, and the other way round.

TWO JOBS, EACH ITS OWN SWITCH

Save sync is one job. Borderless is the other: take a windowed game's title
bar off and fit it to its monitor, for a game like Dark Souls II that offers
only exclusive fullscreen or a framed window. They are independent, so a game
Steam Cloud already covers can be borderless without Blockslot touching a
save. The head carries the answer:

    <wrap> -- %command%                          sync only
    <wrap> --borderless -- %command%             sync and borderless
    <wrap> --borderless --no-sync -- %command%   borderless only
"""

import re

COMMAND_TOKEN = "%command%"
SEPARATOR = "--"
# Blockslot.exe --pick runs the engine it carries. It stands where the .py
# path stands in the python form, so build() takes it as the engine.
PICK = "--pick"
BORDERLESS_FLAG = "--borderless"
NO_SYNC_FLAG = "--no-sync"


def _parts(value):
    """Split a wrapped option into (head, args, rest), or None.

    Recognising our own work cannot be done by looking for one file name. The
    engine is deployed under whatever name and path a device uses, and an
    option written by an older install names a path that has since moved. What
    every wrap does have is a head that runs a .py file, or a program started
    with --pick, then ` -- `, then the thing being launched.
    """
    if not value:
        return None
    at = _separator_at(value)
    if at is None:
        return None
    head = value[:at].strip()
    rest = value[at + 4:].strip()
    tokens = _split(head)
    # One token is enough: a wrapped SHORTCUT puts the interpreter in its exe
    # field, so its launch option starts with the engine itself.
    if not tokens:
        return None
    if not _is_pick(tokens) and not any(token.lower().endswith(".py")
                                        for token in tokens):
        return None
    return head, head, rest


def _is_pick(tokens):
    """True for an exe-hosted head: `<something.exe> --pick ...`, or a
    shortcut's `--pick ...`, whose exe field holds the program. Only the
    Windows build writes this form, so the program always ends in .exe."""
    if not tokens:
        return False
    if tokens[0] == PICK:
        return True
    return (len(tokens) > 1 and tokens[1] == PICK
            and tokens[0].lower().endswith(".exe"))


def _separator_at(value):
    """Where ` -- ` sits, ignoring one inside quotes."""
    quoted = False
    for index in range(len(value) - 3):
        char = value[index]
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if value[index:index + 4] == " -- ":
            return index
    return None


def quote(path):
    """Quote a path for a launch option only when it needs it."""
    text = str(path)
    if not text or any(space in text for space in " \t"):
        return '"%s"' % text
    return text


def _flags(tree=None, borderless=False, sync=True):
    """The switches after the engine's path, in one fixed order."""
    out = ""
    if tree and sync:
        out += " --tree %s" % tree
    if borderless:
        out += " " + BORDERLESS_FLAG
    if not sync:
        out += " " + NO_SYNC_FLAG
    return out


def build(python, engine, existing="", tree=None, borderless=False, sync=True):
    """The launch option that wraps `existing` with savepick.

    `tree` names a save set in savepick.json for whole-tree mode, which is what
    a retro frontend needs. A normal Steam game leaves it out and savepick works
    the game out from the Steam app id. A save set means nothing without sync,
    so it is dropped when sync is off.
    """
    existing = (existing or "").strip()
    head = "%s %s" % (quote(python), quote(engine))
    head += _flags(tree, borderless, sync)
    if not existing:
        return "%s %s %s" % (head, SEPARATOR, COMMAND_TOKEN)
    if COMMAND_TOKEN in existing:
        return "%s %s %s" % (head, SEPARATOR, existing)
    return "%s %s %s %s" % (head, SEPARATOR, COMMAND_TOKEN, existing)


def build_shortcut(python, engine, exe, options="", tree=None,
                   borderless=False, sync=True):
    """Wrap a non-Steam shortcut. Returns the new (exe, launch options).

    A shortcut cannot use %command%: on Windows it expands to the entry's own
    exe, so the wrapper would be handed its own path and nothing would start.
    The target is named after the separator instead, which is the form already
    proven on both operating systems.
    """
    head = quote(engine) + _flags(tree, borderless, sync)
    rest = quote(exe)
    options = (options or "").strip()
    if options:
        rest += " " + options
    return str(python), "%s %s %s" % (head, SEPARATOR, rest)


def unwrap_shortcut(exe, options):
    """Give back the (exe, launch options) a wrapped shortcut started as."""
    found = _parts(options)
    if found is None:
        return exe, options or ""
    parts = _split_first(found[2])
    if not parts[0]:
        return exe, options or ""
    return parts[0], parts[1]


def _split_first(text):
    """The first token of a command line, and everything after it."""
    text = text.strip()
    if not text:
        return "", ""
    if text.startswith('"'):
        end = text.find('"', 1)
        if end < 0:
            return text.strip('"'), ""
        return text[1:end], text[end + 1:].strip()
    parts = text.split(" ", 1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


def is_wrapped(value):
    """True when this launch option already runs through savepick."""
    return _parts(value) is not None


def borderless_of(value):
    """True when a wrapped option asks for the game's window to go borderless."""
    parts = _parts(value)
    return parts is not None and BORDERLESS_FLAG in _split(parts[1])


def syncs(value):
    """True when a wrapped option carries saves. Borderless only does not."""
    parts = _parts(value)
    return parts is not None and NO_SYNC_FLAG not in _split(parts[1])


def tree_of(value):
    """The save set named in a wrapped option, or None."""
    parts = _parts(value)
    if parts is None:
        return None
    found = re.search(r"--tree\s+(\S+)", parts[1])
    return found.group(1) if found else None


def engine_of(value, exe=None):
    """What a wrapped option runs the engine with, or None.

    The savepick.py path for the python form, and the program for the exe
    form. A wrapped shortcut in the exe form has only `--pick` in its launch
    option: its program is in the entry's exe field, passed as `exe`, and
    without that the answer is PICK itself.

    This is how the GUI notices a launch option left behind by an older
    install: it names a savepick.py, or an exe, that is no longer the one
    being deployed.
    """
    parts = _parts(value)
    if parts is None:
        return None
    head = _split(parts[0])
    if _is_pick(head):
        if head[0] != PICK:
            return head[0]
        return str(exe).strip().strip('"') if exe else PICK
    tokens = [token for token in head if token.lower().endswith(".py")]
    return tokens[-1] if tokens else None


def strip(value):
    """The launch option with the wrap removed.

    An option that was only ever the wrap comes back empty, not as a bare
    %command%, because Steam treats those two the same and an empty box reads
    as "nothing set" to the player.
    """
    parts = _parts(value)
    if parts is None:
        return value or ""
    rest = parts[2]
    if rest == COMMAND_TOKEN:
        return ""
    return rest


def _split(text):
    """Split a command head on spaces, honouring double quotes."""
    out = []
    current = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
            continue
        if char == " " and not quoted:
            if current:
                out.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        out.append("".join(current))
    return out
