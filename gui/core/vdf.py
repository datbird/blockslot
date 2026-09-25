"""Valve KeyValues, the three shapes Blockslot has to read.

Steam stores the same tree in three encodings and Blockslot needs all of them:

    localconfig.vdf     text, and it holds the launch options
    shortcuts.vdf       binary, and it holds the non-Steam games
    appinfo.vdf         binary with a string table, and it holds Steam's own
                        answer to "does this game use Steam Cloud"

There is no parser for any of them in the standard library, and the engine's
rule is standard library only, so they live here. Reading is what matters:
Blockslot writes a text file back by editing the bytes it read, never by
re-serialising a tree it only half understands. See `launchopts.py`.
"""

import struct

# Binary KeyValues type bytes. Steam has never used them all.
BIN_MAP = 0x00
BIN_STRING = 0x01
BIN_INT32 = 0x02
BIN_FLOAT32 = 0x03
BIN_POINTER = 0x04
BIN_WSTRING = 0x05
BIN_COLOR = 0x06
BIN_UINT64 = 0x07
BIN_END = 0x08
BIN_INT64 = 0x0A
BIN_ALT_END = 0x0B


class VdfError(ValueError):
    """The bytes are not the KeyValues they claimed to be."""


# ---------------------------------------------------------------- text


def parse_text(text):
    """A text KeyValues document as nested dicts.

    Duplicate keys keep the last value, which is what Steam itself does.
    Comments and the conditional suffixes ([$WINDOWS]) are skipped.
    """
    tokens = _tokens(text)
    root = {}
    stack = [root]
    key = None
    for token, quoted in tokens:
        if not quoted and token == "{":
            if key is None:
                raise VdfError("a block with no name")
            child = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif not quoted and token == "}":
            if len(stack) == 1:
                raise VdfError("one closing brace too many")
            stack.pop()
            key = None
        elif not quoted and token.startswith("["):
            # A conditional, [$WIN32]. It qualifies the pair before it, and
            # Blockslot reads files Steam wrote for THIS machine, so it is
            # always true here.
            continue
        elif key is None:
            key = token
        else:
            stack[-1][key] = token
            key = None
    if len(stack) != 1:
        raise VdfError("%d block(s) left open" % (len(stack) - 1))
    return root


_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "r": "\r"}


def _tokens(text):
    """(token, was_quoted) pairs. Quoted matters: "{" is a string, { is not."""
    i = 0
    end = len(text)
    while i < end:
        char = text[i]
        if char in " \t\r\n":
            i += 1
            continue
        if char == "/" and i + 1 < end and text[i + 1] == "/":
            while i < end and text[i] != "\n":
                i += 1
            continue
        if char == '"':
            i += 1
            out = []
            while i < end and text[i] != '"':
                if text[i] == "\\" and i + 1 < end:
                    out.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i >= end:
                raise VdfError("a quoted string never closes")
            i += 1
            yield "".join(out), True
            continue
        if char in "{}":
            i += 1
            yield char, False
            continue
        start = i
        while i < end and text[i] not in ' \t\r\n"{}':
            i += 1
        yield text[start:i], False


def iter_spans(text):
    """Every token with the offsets it occupies: (token, quoted, start, end).

    This is what makes a surgical edit possible. Blockslot changes one launch
    option inside a 400 KB file Steam wrote, and rewriting the whole tree from
    a parse would silently drop anything the parser does not model. Editing the
    bytes between two offsets cannot.
    """
    i = 0
    end = len(text)
    while i < end:
        char = text[i]
        if char in " \t\r\n":
            i += 1
            continue
        if char == "/" and i + 1 < end and text[i + 1] == "/":
            while i < end and text[i] != "\n":
                i += 1
            continue
        if char == '"':
            start = i
            i += 1
            while i < end and text[i] != '"':
                if text[i] == "\\" and i + 1 < end:
                    i += 2
                    continue
                i += 1
            if i >= end:
                raise VdfError("a quoted string never closes")
            i += 1
            yield unescape(text[start + 1:i - 1]), True, start, i
            continue
        if char in "{}":
            i += 1
            yield char, False, i - 1, i
            continue
        start = i
        while i < end and text[i] not in ' \t\r\n"{}':
            i += 1
        yield text[start:i], False, start, i


def unescape(text):
    if "\\" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out.append(_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def escape(text):
    """Quote a value the way Steam writes one back."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def find_block(text, path):
    """The span of the block at a key path, as (open_brace, close_brace).

    Key matching ignores case, because Steam is not consistent about it and a
    miss here would mean writing a second `apps` block beside the real one.
    Returns None when the path is not in the file.
    """
    wanted = [key.lower() for key in path]
    stack = []
    key = None
    opened = []
    for token, quoted, start, stop in iter_spans(text):
        if not quoted and token == "{":
            stack.append(key.lower() if key else "")
            opened.append(start)
            key = None
            if stack == wanted:
                depth = len(stack)
                open_at = start
                # Walk on until this block closes.
                for token2, quoted2, start2, stop2 in iter_spans(text[stop:]):
                    if not quoted2 and token2 == "{":
                        depth += 1
                    elif not quoted2 and token2 == "}":
                        depth -= 1
                        if depth == len(stack) - 1:
                            return open_at, stop + start2
                return None
        elif not quoted and token == "}":
            if stack:
                stack.pop()
                opened.pop()
            key = None
        elif not quoted and token.startswith("["):
            continue
        elif key is None:
            key = token
        else:
            key = None
    return None


def load_text(path, encoding="utf-8"):
    """Parse a text KeyValues file. Steam writes UTF-8 with the odd stray byte."""
    with open(str(path), "r", encoding=encoding, errors="replace") as handle:
        return parse_text(handle.read())


# ---------------------------------------------------------------- binary


def parse_binary(data, offset=0, string_table=None):
    """One binary KeyValues map. Returns (tree, offset after it).

    With a string_table the keys are uint32 indexes into it, which is what
    appinfo.vdf v29 does. Without one they are null terminated strings, which
    is what shortcuts.vdf does.
    """
    tree = {}
    while True:
        if offset >= len(data):
            raise VdfError("the map never ends")
        kind = data[offset]
        offset += 1
        if kind in (BIN_END, BIN_ALT_END):
            return tree, offset
        if string_table is None:
            key, offset = _cstring(data, offset)
        else:
            index = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            try:
                key = string_table[index]
            except IndexError:
                raise VdfError("string %d is not in the table" % index)
        if kind == BIN_MAP:
            value, offset = parse_binary(data, offset, string_table)
        elif kind == BIN_STRING:
            value, offset = _cstring(data, offset)
        elif kind == BIN_WSTRING:
            value, offset = _wstring(data, offset)
        elif kind == BIN_INT32 or kind == BIN_POINTER or kind == BIN_COLOR:
            value = struct.unpack_from("<i", data, offset)[0]
            offset += 4
        elif kind == BIN_FLOAT32:
            value = struct.unpack_from("<f", data, offset)[0]
            offset += 4
        elif kind == BIN_UINT64:
            value = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
        elif kind == BIN_INT64:
            value = struct.unpack_from("<q", data, offset)[0]
            offset += 8
        else:
            raise VdfError("unknown value type 0x%02x at %d" % (kind, offset - 1))
        tree[key] = value


def _cstring(data, offset):
    end = data.find(b"\x00", offset)
    if end < 0:
        raise VdfError("a string never ends")
    return data[offset:end].decode("utf-8", "replace"), end + 1


def _wstring(data, offset):
    end = offset
    while True:
        if end + 1 >= len(data):
            raise VdfError("a wide string never ends")
        if data[end] == 0 and data[end + 1] == 0:
            break
        end += 2
    return data[offset:end].decode("utf-16-le", "replace"), end + 2


def dump_binary(tree, string_table=None):
    """Serialise a map the way shortcuts.vdf stores it.

    Only the types Steam writes into shortcuts.vdf are supported, because
    writing a type Steam does not expect there is how you lose a library.
    """
    out = bytearray()
    for key, value in tree.items():
        if isinstance(value, dict):
            out.append(BIN_MAP)
            out += _key_bytes(key, string_table)
            out += dump_binary(value, string_table)
        elif isinstance(value, bool):
            out.append(BIN_INT32)
            out += _key_bytes(key, string_table)
            out += struct.pack("<i", 1 if value else 0)
        elif isinstance(value, int):
            out.append(BIN_INT32)
            out += _key_bytes(key, string_table)
            out += struct.pack("<i", value)
        elif isinstance(value, str):
            out.append(BIN_STRING)
            out += _key_bytes(key, string_table)
            out += value.encode("utf-8") + b"\x00"
        else:
            raise VdfError("cannot write %r into a binary KeyValues" % type(value))
    out.append(BIN_END)
    return bytes(out)


def _key_bytes(key, string_table):
    if string_table is None:
        return key.encode("utf-8") + b"\x00"
    return struct.pack("<I", string_table.index(key))
