# Blockslot, the window

One window that turns save syncing on for the games Steam Cloud does not
cover. Windows, macOS and Linux, including the Steam Deck in Game Mode, from
this one tree.

```
python3 gui/blockslot.py                 open the window
python3 gui/blockslot.py --check         print what it can see, no window
python3 gui/blockslot.py --fullscreen    for Game Mode

python3 tools/build_exe.py               build one executable file
```

The build produces `dist/Blockslot.exe` on Windows, `dist/Blockslot` on Linux
and `dist/Blockslot.app` on macOS, about 12 MB, carrying the game index and the
engine inside it. That one file is the whole install: nothing else has to be
copied and no python has to be there.

The Windows build has no console of its own, on purpose, so it never flashes a
black window. `Blockslot.exe --check` still prints, by attaching to the
terminal that started it.

## The Windows exe needs no python and no ludusavi

On Windows the exe is also the engine's host. A game it turns on gets

```
"C:\path\to\Blockslot.exe" --pick [--tree NAME] [--borderless] [--no-sync] -- %command%
```

and `--pick` runs the savepick it carries, exactly as
`pythonw savepick.py ... -- %command%` does: same switches, same dialogs, same
log, same exit code. Settings says the engine is built in and has nothing to
install. A source install, macOS, Linux and the Deck keep the python form, and
games wrapped by either form are recognised, taken off and put back on in the
current one.

Each wrapped game names the exe by its full path, so keep `Blockslot.exe`
somewhere it can stay. Run from `%TEMP%`, or straight out of a zip without
extracting it, BlockSlot warns first. Moved later, turn the games on again.

If ludusavi is missing, Settings, ludusavi, `Install ludusavi` downloads the
official `ludusavi-v0.31.0-win64.zip` (the version the Decky plugin pins),
checks its SHA-256 against the one pinned in `gui/core/engine.py`, and puts
`ludusavi.exe` in `%USERPROFILE%\.local\bin`. One that is already there is
never replaced.

Standard library only, python 3.9 and up. The window is tkinter. Nothing is
installed or downloaded unless you ask (ludusavi, above), and `--check` works
on a machine with no display at all.

## What it does

**Games.** Every installed game, with Steam's own answer to whether it has
cloud saves, the ludusavi index's answer to whether it has saves at all,
whether Blockslot is on for it, and what the hub is holding: how old the newest
backup is and which device made it. Cloud games are hidden by default. Pick as
many as you like and turn them on in one pass.

**Sync.** Whether Syncthing is up, whether the hub is connected, and whether the
shared folder is behind. `Find Syncthing` reads the API key out of Syncthing's
own config file, so nobody has to copy a 32 character key by hand.

**Save sets.** One per launcher or non-Steam game: a name, and the folder that
holds its saves on this device. Every device fills in its own, so nobody types
another machine's paths.

**Settings.** This device, the engine, ludusavi, and whether Blockslot is in
your Steam library. Pick a row on the left and everything about that one thing,
including what to do about it, is on the right.

**Activity.** The engine's log, followed live, coloured for the lines that
matter.

## The part that will surprise you

**Steam has to close before a launch option can change.** Steam keeps
`localconfig.vdf` in memory and writes it out when it exits, so an edit made
while it is running is discarded without a word. Blockslot asks Steam to close,
waits for the file to settle, writes, and starts Steam again. It says so before
it does it, and it does it once for a whole batch rather than once per game.

This is also why Blockslot is not a Steam release. A tool shipped on Steam runs
under Steam by definition, so it could never make the edit stick.

## Where things live

| What | Path |
|---|---|
| The engine it configures | `~/.local/bin/savepick.py`, or inside `Blockslot.exe` on Windows |
| ludusavi | `~/.local/bin/ludusavi`, `%USERPROFILE%\.local\bin\ludusavi.exe` |
| Its settings | `%APPDATA%\savepick.json`, or `~/.config/savepick.json` |
| Its log | `%TEMP%\savepick.log`, or `/tmp/savepick.log` |
| Blockslot's own cache | `%LOCALAPPDATA%\Blockslot`, `~/Library/Application Support/Blockslot`, `~/.local/state/blockslot` |

## Game Mode

Settings has an `Add to Steam` button that puts Blockslot in the library as a
non-Steam entry, pointing at this install and starting fullscreen. Press it
once on the Deck, then open Blockslot from Game Mode like any other game.
Pressing it again on an entry that already exists corrects the paths and keeps
the id, so the artwork and the playtime stay attached. The pad is
read directly, so it works whatever controller layout Steam has applied:

| Pad | Does |
|---|---|
| d-pad, left stick | move |
| A | choose |
| B | back, then quit |
| X | tick the row under the cursor |
| Y | tick everything shown |
| LB, RB | previous and next screen |
| Start | refresh |

## Layout

```
gui/
  blockslot.py     the entry point and the controller the screens share
  core/            no UI imports, every rule about Steam and sync, all tested
  ui/              tkinter: one file per screen, plus the widgets and the pad
  tests/           python3 gui/tests/test_core.py
```

`core` never imports tkinter, so every decision it makes can be tested without
a display. That matters: a headless run of a GUI proves nothing, and the rules
about which save wins are the part that must not be wrong.
