"""Blockslot GUI tests. See run.py."""

import os

# A test run on a Mac must never write the login Keychain (slotd.protect).
os.environ["BLOCKSLOT_NO_KEYCHAIN"] = "1"
