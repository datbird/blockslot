"""Reading the engine's log, which is its only record of a launch.

The words that mark a line are the engine's own wording. They are listed once,
here, so a new engine message is classified in one place for both faces.
"""

import os
import re

TAIL_BYTES = 64 * 1024

BAD = "bad"
WARN = "warn"
GOOD = "good"

_WORDS = (
    (BAD, ("failed", "cannot", "could not", "refus", "error", "killed",
           "giving up")),
    # "may not finish its exit backup" is the engine saying a backup did NOT
    # run. It mentions a backup, and must not be coloured as one.
    (WARN, ("no snapshot", "not confirmed", "timed out", "skip", "may not")),
    (GOOD, ("restored", "backup", "up to date", "copied")),
)

# A count of nothing is not a failure: "tree: 3 copied, 0 failed" is the good
# outcome, and the word alone would colour it as the worst one.
_NOTHING_FAILED = re.compile(r"(?<!\d)0 failed\b")


def classify(line):
    """"bad", "warn", "good" or "" for one line of the log."""
    lowered = _NOTHING_FAILED.sub("", line.lower())
    for kind, words in _WORDS:
        for word in words:
            if word in lowered:
                return kind
    return ""


def tail(path, lines, max_bytes=TAIL_BYTES):
    """The last `lines` lines. Raises OSError when the log cannot be read.

    The log only grows and nothing rotates it, so the end is read and the rest
    is never touched.
    """
    with open(str(path), "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - max_bytes))
        found = handle.read().decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        found = found[1:]       # the first line was cut in half
    return found[-int(lines):]
