# Adding a launcher to Blockslot tree mode

Tree mode is not written for RetroBat and RetroDECK. They are the two configured so far.
Adding another launcher is a config change, not a code change, as long as the launcher
owns a directory of save data.

## First: does the launcher own any saves?

Most do not. This is the thing to settle before anything else.

| Launcher | Owns saves? | Why |
|---|---|---|
| RetroBat | yes | Imposes `saves/<system>/` and configures every emulator to write there. |
| RetroDECK | yes | Same idea, `<retrodeck>/saves/<system>/`. |
| EmuDeck | yes | Imposes `Emulation/saves/`, but nests by emulator. See below. |
| Batocera, Recalbox | yes | `/userdata/saves/`. |
| Lakka | yes | RetroArch only, one save directory. |
| muOS, Knulli, AmberELEC, ArkOS | yes | Batocera-style, a single save tree. |
| **ES-DE (standalone)** | **no** | A launcher only. It scans ROMs and runs a command. Saves land wherever each emulator is configured, which is usually that emulator's own default. |
| **EmulationStation (original), Pegasus** | **no** | Same. A menu in front of emulators. |
| **LaunchBox, BigBox, Playnite** | **no** | Same. Windows front ends over emulators and store games. |
| **Steam, Heroic, Lutris** | **no** | Steam Cloud covers its own; the rest is per game. That is the single game path, not tree mode. |

For a launcher that owns nothing, there is no tree to add. Point a save set at the
**emulator's** save directory instead, and the launcher is irrelevant to Blockslot.

## Finding the root

Do not trust a wiki path, including the ones above. Read the live tree:

```sh
find <install dir> -maxdepth 3 -type d -iname "save*"
```

Then confirm it holds saves rather than configuration, and check the file count against
what the frontend claims.

**Follow every symlink before you settle on a root.** EmuDeck's own wiki warns that its
save directories are symlinks and that a backup must use the target. RetroDECK does the
same thing in reverse: `saves/switch/ryujinx/nand/system/Contents` points at
`bios/switch/firmware`, and ludusavi followed it into 322 MB of Switch firmware that has
nothing to do with saves. Check with:

```sh
find <root> -type l -printf "%p -> %l\n"
```

## The config block

Each save set is one entry under `trees` in `savepick.json`, shipped identically to every
device, plus a matching ludusavi custom game on each device.

```json
"trees": {
  "RetroFrontend": {
    "roots": {
      "deck": "/run/media/deck/<uuid>/retrodeck/saves",
      "desktop": "C:/Users/player/Apps/RetroBat/saves"
    },
    "extensions": ["srm", "sav", "mcr", "brm", "bkr", "bcr", "smpc", "ldci",
                   "gci", "dsv", "ps2", "sram"],
    "always_dirs": ["mame", "mame-sa"],
    "system_aliases": [["gc", "gamecube"], ["mame", "mame-sa"], ["3ds", "n3ds"]]
  }
}
```

`roots` is required. Every device gets an entry, including this one, because a peer's
root is a path on another operating system and cannot be resolved locally. Use the real
path, not a symlink: ludusavi records the resolved path inside the backup, and a mismatch
produces an empty peer index with no error.

### `extensions`

Which file types may cross devices. The default is the battery save and memory card list.

Set `"*"` to turn the check off. That is right for a save set holding one game's save
directory and nothing else, and wrong for a frontend tree, which mixes saves with save
states and screenshots.

A save state is tied to the exact emulator build that wrote it. Two launchers almost never
ship the same build, so states stay off the list unless you have checked.

### `always_dirs`

Top level directories where every file counts as save data, whatever it is called.

This exists because MAME defeats an extension list. It names each nvram dump after the
chip it came from: `at28c16`, `ioasic`, `nov0`, `smpc_smem`, `0_eagle1_bram`. The list is
unbounded. Any launcher that keeps a whole subtree of save data wants this instead of a
longer extension list.

### `system_aliases`

Groups of directory names that mean the same system. Every launcher names its systems
differently, and one character is enough to stop a save crossing for years:

| RetroDECK | RetroBat | Evidence they are the same |
|---|---|---|
| `sg1000` | `sg-1000` | `Ys - The Vanished Omens (UE) [!].srm` in both |
| `gc` | `gamecube` | `Metroid Prime (USA).s01` in both |
| `jaguar` | `atarijaguar` | Doom, Rayman, Wolfenstein `.srm` |
| `mame-sa` | `mame` | `blank.fmtowns` in both |
| `n3ds` | `3ds` | about 500 files each |

**Verify a pair by content before adding it.** Name normalisation finds `sg-1000` and
`sg1000`, and misses `gc` and `gamecube` entirely. Worse, a normaliser confident enough to
catch `gc` would also fold real systems together, and folding two systems means one save
overwrites another. So the list is written by hand, from evidence.

An alias only ever redirects into a directory this device already uses. If the device uses
none of a group's names, the file is refused rather than written somewhere the launcher
will never read.

## Layout differences the engine already handles

Two launchers rarely agree on shape, and tree mode refuses a foreign shape rather than
building directories nothing reads.

- **Nesting.** RetroBat writes `3do/opera/per_game/<rom>.0.srm` where RetroDECK writes
  `3do/<rom>.srm`. EmuDeck nests by emulator: `saves/retroarch/saves/gba`,
  `saves/MAME/saves`. A path deeper than `<system>/<file>` is only taken when its parent
  directory already exists here.
- **Sort by content.** RetroArch can write `<game>.m3u/<game>.srm`. A top directory with a
  dot in its name is not treated as a system directory.

Measured on the first real merge: those two rules refused 142 files one way and 714 the
other, all of them dead paths.

## Checklist

1. Confirm the launcher owns saves at all.
2. Find the root on each device, resolving symlinks.
3. Add a ludusavi custom game with the same name on every device.
4. Exclude what is not a save. A frontend `saves` directory is not only saves: RetroBat's
   `saves/dos` holds whole zipped DOS games, one of them 634 MB.
5. Add the `trees` block to every device's `savepick.json`.
6. Back up once on each device and let Syncthing settle.
7. **Dry run before it writes.** Read the plan. Hundreds of files means a layout clash or
   a wrong root, not a first sync.
8. Add the aliases the dry run reveals, and dry run again.

## Wiring one emulator into a frontend, rather than the frontend itself

Tree mode syncs a frontend's whole `saves` directory. An emulator the frontend does not
bundle is a separate job: the frontend has no system entry for it, so nothing launches it
and nothing wraps it. shadPS4 is the worked example, on both devices.

Add the system, and put savepick in that system's launch command. Where the file goes:

| Frontend | File | Loaded how |
| --- | --- | --- |
| ES-DE, RetroDECK | `ES-DE/custom_systems/es_systems.xml` under the app's **config** dir | Merged with the bundled systems |
| RetroBat, Batocera ES | `es_systems_<name>.cfg` beside `es_systems.cfg` | Every `es_systems_*.cfg` in that folder is read |

Both survive a frontend update, which the main systems file does not.

Three things bite:

- **The emulator path moves.** A self-updating build (an AppImage kept current, or
  `shadPS4QtLauncher` on Windows) has a new path after every update. Point the command at
  a small resolver, not at the exe: a symlink on Linux, or on Windows read the launcher's
  own selected version out of its ini.
- **Windows wants no console.** Run savepick under `pythonw.exe`. It already handles the
  missing `sys.stderr`.
- **ES-DE takes several labelled commands, Batocera ES takes one.** Only ES-DE can offer a
  "no save sync" alternative in the same system.

A per-game emulator needs a per-game tree name, because the save root is one game's save
directory. A second game on the same emulator needs its own `trees` entry and its own
command.
