"""blockslot_server - the BlockSlot server's web app and its helpers.

The design is docs/superpowers/specs/2026-09-25-server-design.md.

The server reads and writes the store with engine/slotstore.py, the same file
every device runs. In the image that file is copied next to this package at
build time; in the repo it is found in engine/. It is never forked, so the
server can never disagree with a device about what a snapshot is.
"""

import os
import sys

__version__ = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))


def _engine_on_path():
    """Make slotstore and saveunits importable in the image and in the repo."""
    for folder in (os.path.dirname(HERE),                      # the image: /app
                   os.path.join(os.path.dirname(os.path.dirname(HERE)), "engine")):
        if os.path.isfile(os.path.join(folder, "slotstore.py")):
            if folder not in sys.path:
                sys.path.insert(0, folder)
            return folder
    return None


ENGINE_DIR = _engine_on_path()


def asset_path(name):
    """A brand file: static/ in the image, assets/ in the repo."""
    inside = os.path.join(HERE, "static", name)
    if os.path.isfile(inside):
        return inside
    return os.path.join(os.path.dirname(os.path.dirname(HERE)), "assets", name)
