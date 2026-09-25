"""saveunits - which game each file in an emulator's saves folder belongs to.

A frontend such as RetroBat or RetroDECK keeps every system's saves in one
folder. Blockslot treats every game as one save, so that folder is split
into one save per game, and each keeps its own history and is restored or
asked about on its own, exactly like a Steam game.

The split is by the layout each emulator writes, measured on 2026-09-24 from
his two devices (RetroBat on Windows, RetroDECK on the Deck):

    snes/Super Mario World (USA).srm              one game per file name
    psx/Xenogears (USA).1.mcr                     memory card slot suffix dropped
    <Game>.m3u/scd_U.brm                          one game per content folder
    psp/PPSSPP-SA/ULES01248Diabolik/SAVE1.SAV     one game per game id folder
    3ds/.../title/00040000/<title id>/...         one game per title id
    dolphin/User/GC/USA/01-GFEE-NAME.gci          one game per disc code
    mame/<core>/hi/005.hi, .../nvram/<rom>/...    one game per ROM
    segacd/scd_U.brm, *_shared.smpc               the system's shared memory
    anything else nested                          one save per emulator folder

A unit is identified by (system, name). The system is the canonical name of
its alias group, so sg-1000 and sg1000 are the same system on both devices.

The label says what owns the save: the emulator, never the frontend. The
frontend differs per device and owns no saves.

Standard library only; Python 3.9.
"""

import re

# Systems as the frontends name them. A top-level folder that is not one of
# these is a content folder: RetroArch's "sort saves by content" writes one
# per game.
SYSTEMS = frozenset("""
3do 3ds amiga amiga1200 amiga500 amstradcpc apple2 arcade atari2600 atari5200
atari7800 atari800 atarijaguar atarilynx atomiswave c64 cdi colecovision
cps1 cps2 cps3 dolphin dos dreamcast fbneo fds gamecube gamegear gb gba gbc
gc genesis intellivision jaguar lynx mame mame-sa mastersystem megacd
megadrive msx msx2 n3ds n64 naomi nds neogeo neogeocd nes ngp ngpc pc88
pc98 pcengine pcenginecd pcfx pico8 pico-8 ps2 ps3 ps4 psp psvita psx
saturn scummvm sega32x segacd sfc sg-1000 sg1000 snes supergrafx switch
tg16 tgcd vb vectrex virtualboy wii wiiu wonderswan wonderswancolor x68000
xbox zxspectrum fmtowns
""".split())

# Display only: the emulator that owns a system's saves in the usual setups.
LABELS = {
    "gc": "Dolphin", "gamecube": "Dolphin", "dolphin": "Dolphin", "wii": "Dolphin",
    "ps2": "PCSX2", "ps3": "RPCS3", "ps4": "shadPS4", "psp": "PPSSPP",
    "psvita": "Vita3K", "3ds": "Citra / Lime3DS", "n3ds": "Citra / Lime3DS",
    "wiiu": "Cemu", "switch": "Ryujinx", "xbox": "xemu",
    "mame": "MAME", "mame-sa": "MAME", "scummvm": "ScummVM", "dos": "DOSBox",
}
SYSTEM_NAMES = {
    "snes": "SNES", "sfc": "SNES", "nes": "NES", "fds": "Famicom Disk", "n64": "N64",
    "gb": "Game Boy", "gbc": "Game Boy Color", "gba": "GBA", "nds": "DS",
    "psx": "PS1", "segacd": "Sega CD", "megacd": "Sega CD", "saturn": "Saturn",
    "dreamcast": "Dreamcast", "genesis": "Genesis", "megadrive": "Mega Drive",
    "sega32x": "32X", "tg16": "TurboGrafx-16", "pcengine": "PC Engine",
    "pcenginecd": "PC Engine CD", "neogeocd": "Neo Geo CD", "jaguar": "Jaguar",
    "atarijaguar": "Jaguar", "sg-1000": "SG-1000", "sg1000": "SG-1000",
    "3do": "3DO", "mastersystem": "Master System", "gamegear": "Game Gear",
}

# System-wide memory: one file holds many games' saves, so it cannot belong
# to any one game.
SHARED = re.compile(r"(^scd_|_cart\.|_shared\.|^blank\.|^bios|^cart\.)", re.I)
GAME_ID = re.compile(r"^[A-Z]{4}\d{5}")          # PSP, PS3, Vita
GCI = re.compile(r"^\d\d-([A-Z0-9]{4})-(.+)\.gci$", re.I)
HEX_TITLE = re.compile(r"^[0-9a-fA-F]{8}$")
CONTENT_SUFFIX = re.compile(r"\.(m3u|cue|chd|iso|zip|7z|bin)$", re.I)


def label_for(system):
    """What owns saves for this system, as a person reads it.

    No system is a content folder, which only RetroArch writes (its "sort
    saves by content" option), so RetroArch owns it.
    """
    if not system:
        return "RetroArch"
    if system in LABELS:
        return LABELS[system]
    return "RetroArch (%s)" % SYSTEM_NAMES.get(system, system)


def canonical(system, aliases=()):
    """The one name an alias group is known by: its first entry."""
    for group in aliases or ():
        if system in group:
            return group[0]
    return system


def stem(filename):
    """A save file's game name: extension and memory card slot dropped.

    "Super Mario Bros. 3.srm" keeps its inner dot; "Xenogears (USA).1.mcr"
    and "Night Trap (USA) (Disc 2).0.srm" lose the slot number.
    """
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"\.\d+$", "", name)


def unit_of(rel, aliases=()):
    """(system, unit name) for a path relative to the saves folder.

    system is "" for a content folder, which holds one game whatever its
    system is.
    """
    parts = rel.split("/")
    top = parts[0]
    if len(parts) == 1:
        return "", stem(top)
    if top not in SYSTEMS:
        return "", CONTENT_SUFFIX.sub("", top)
    system = canonical(top, aliases)
    inner = parts[1:]
    filename = inner[-1]
    if len(inner) == 1:
        if SHARED.search(filename):
            return system, "shared memory"
        return system, stem(filename)
    found = GCI.match(filename)
    if found:
        return system, found.group(1).upper()
    for index, part in enumerate(inner[:-1]):
        if GAME_ID.match(part):
            return system, part[:9]
        if part.lower() == "title" and index + 2 < len(inner):
            return system, inner[index + 2].lower()
        if part.lower() == "nvram" and index + 2 < len(inner):
            return system, inner[index + 1]
        if part.lower() in ("per_game", "memcards", "hi", "nvram", "diff", "inp", "cfg"):
            return system, stem(filename)
        if part.lower() == "backup" and index + 1 < len(inner) - 1:
            return system, inner[index + 1].split(".")[0]
    for index, part in enumerate(inner[:-1]):
        # Wii U (cemu): <system>/cemu/00050000/<title>/...
        if HEX_TITLE.match(part) and index + 1 < len(inner) - 1 \
                and HEX_TITLE.match(inner[index + 1]):
            return system, inner[index + 1].lower()
    if len(inner) == 2:
        # <system>/<emulator folder>/<file>: the file names the game.
        if SHARED.search(filename):
            return system, "shared memory"
        return system, stem(filename)
    if len(inner) == 3 and not SHARED.search(filename):
        # <system>/<emulator>/<core or kind>/<file>, as picodrive and
        # kega-fusion write: the file names the game.
        return system, stem(filename)
    # A structure nothing here knows. One save for the emulator folder:
    # honest, and never two games folded into one by a guess.
    return system, "%s (all saves)" % inner[0]


def unit_id(system, name):
    return "%s/%s" % (system or "content", name)


def display(system, name):
    if not system:
        return name
    return "%s (%s)" % (name, SYSTEM_NAMES.get(system, system.upper() if len(system) <= 4 else system))


def split(rels, aliases=()):
    """{unit id: [rel, ...]} for every path."""
    units = {}
    for rel in rels:
        system, name = unit_of(rel, aliases)
        units.setdefault(unit_id(system, name), []).append(rel)
    return units
