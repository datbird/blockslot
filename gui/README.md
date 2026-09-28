# Blockslot, the window

One window that turns save syncing on for the games Steam Cloud does not
cover. Windows, macOS and Linux, including the Steam Deck in Game Mode, from
this one tree.

```
python3 gui/blockslot.py                 open the window
python3 gui/blockslot.py --check         print what it can see, no window
python3 gui/blockslot.py --fullscreen    for Game Mode
python3 gui/blockslot.py --install-service   the daemon, started with the session
python3 gui/blockslot.py --uninstall-service stop and remove it

python3 tools/build_exe.py               build one executable file
python3 tools/build_exe.py --zip         on a Mac, also dist/Blockslot-mac-arm64.zip
```

The build produces `dist/Blockslot.exe` on Windows, `dist/Blockslot` on Linux
and `dist/Blockslot.app` on macOS, carrying the game index and the engine
inside it. The Windows exe is about 12 MB. That one file (one bundle on a Mac)
is the whole install: nothing else has to be copied and no python has to be
there. A Linux desktop is set up from a checkout instead, with the python
form of the launch option below.

The Mac build is a PyInstaller onedir bundle for arm64, `com.datbird.blockslot`,
signed ad hoc, with `assets/blockslot.icns` (written by `tools/make_icon.py`).
`--zip` packs it with `ditto`, which keeps the bundle's signature intact.
Building it needs two things on the Mac, and running it needs neither:

- python.org's Python, whose installer is universal2 and carries its own
  Tcl/Tk. The build targets arm64, so the python doing it has to include it.
- Apple's Command Line Tools (`xcode-select --install`). PyInstaller calls
  `lipo` and `install_name_tool` while it assembles the bundle, and without
  the tools the build stops there.

The Windows build has no console of its own, on purpose, so it never flashes a
black window. `Blockslot.exe --check` still prints, by attaching to the
terminal that started it.

## The Windows exe and the Mac app need no python and no ludusavi

On Windows the exe is also the engine's host, and on a Mac the app is. A game
either one turns on gets

```
"C:\path\to\Blockslot.exe" --pick [--tree NAME] [--borderless] [--no-sync] -- %command%
/Applications/Blockslot.app/Contents/MacOS/Blockslot --pick [...] -- %command%
```

and `--pick` runs the savepick it carries, exactly as
`pythonw savepick.py ... -- %command%` does: same switches, same dialogs, same
log, same exit code. Settings says the engine is built in and has nothing to
install. A source install, Linux and the Deck keep the python form, and games
wrapped by either form are recognised, taken off and put back on in the
current one. On a Linux desktop that form is

```
/usr/bin/python3 /home/<you>/.local/bin/savepick.py -- %command%
```

and beside snap Steam it is always `/usr/bin/python3`, the one python that
exists both inside and outside the snap.

On a Mac this matters more than convenience: without Apple's command line
tools, `/usr/bin/python3` is only a stub that opens "install the developer
tools?", so a launch option naming it would show that dialog in place of the
game. A source install on such a Mac refuses to wrap a game, and the
LaunchAgent refuses to install, rather than name the stub.

Each wrapped game names the exe by its full path, so keep `Blockslot.exe`
somewhere it can stay. Run from `%TEMP%`, or straight out of a zip without
extracting it, BlockSlot warns first. Moved later, turn the games on again.
On a Mac, an app opened from Downloads still quarantined runs from a random
App Translocation copy, and one opened from a disk image runs from `/Volumes`;
BlockSlot warns the same way, the LaunchAgent install refuses, and the answer
is to move `Blockslot.app` into `/Applications` first and open it from there.

If ludusavi is missing, Settings, ludusavi, `Install ludusavi` downloads the
official v0.31.0 release for the machine (the version the Decky plugin pins):
`win64.zip` on Windows, `linux.tar.gz` on x86-64 Linux, `mac.tar.gz` on
Apple silicon. It checks the SHA-256 against the one pinned in
`gui/core/engine.py` and puts ludusavi in `~/.local/bin` (`%USERPROFILE%` on
Windows). One that is already there is never replaced. Upstream publishes no
Intel Mac or ARM Linux build, so there Settings says to install it by hand.
It then downloads ludusavi's manifest, its list of where each game saves:
the engine always runs ludusavi with `--no-manifest-update`, and without the
manifest ludusavi knows no game. A device that never got it fetches it once,
on the first launch.

Beside Ubuntu's Steam snap, the engine, ludusavi and `savepick.json` stay in
the real home: the games it starts see `~/snap/steam/common` as home, but the
snap's `steam-support` interface lets them read the real one, and the engine
finds it through `SNAP_REAL_HOME` (see `docs/linux-desktop.md`). ludusavi
itself runs with the snap's home, so its config and manifest are the ones in
`~/snap/steam/common/.config/ludusavi`, and that is where the manifest goes.

Standard library only, python 3.9 and up. The window is tkinter. Nothing is
installed or downloaded unless you ask (ludusavi, above), and `--check` works
on a machine with no display at all.

## What it does

**Games.** Every installed game, with Steam's own answer to whether it has
cloud saves, the ludusavi index's answer to whether it has saves at all,
whether Blockslot is on for it, and what the hub is holding: how old the newest
backup is and which device made it. Cloud games are hidden by default. Pick as
many as you like and turn them on in one pass.

**Store.** Where saves go: the server's S3 store, an SSH server or a folder.
Pair with the server's address and code, or paste a setup code, then test it.
The test runs on a worker thread, and only the newest test's answer is shown.
The screen also says whether the daemon answers, and imports old Syncthing
backups.

**Sync.** Shown only when no store is set up. Whether Syncthing is up, whether
the hub is connected, and whether the shared folder is behind. `Find
Syncthing` reads the API key out of Syncthing's own config file, so nobody has
to copy a 32 character key by hand.

**Emulator games.** One entry per emulator library (RetroBat's or RetroDECK's
saves folder) or per emulator game (Bloodborne in shadPS4): a name, and the
folder that holds its saves on this device. Every device fills in its own, so
nobody types another machine's paths. With a store, the right side lists the
games the store holds for that library. One gap: the folder is still keyed by
the Syncthing device folder (`device_dir`). This screen asks for it on the
Sync screen, which a device set up with only a store does not show, so such a
device cannot set its folder here yet.

**Settings.** This device, the store or Syncthing, the engine, ludusavi, and
whether Blockslot is in your Steam library. Pick a row on the left and
everything about that one thing, including what to do about it, is on the
right. With a store, the device is named by `store.device` and the Sync row
describes the store and whether its uploader answers. Rows are required or
optional. The Steam library entry is optional: it is drawn faint and never
counts as a problem. The header says everything is set only when every
required row is, and otherwise names the first one that is not. `--check`
prints the same answers.

**Activity.** The engine's log, followed live, coloured for the lines that
matter.

**Slow work never touches the window.** Testing a store, pairing, an import, a
batch of games: each runs on a worker thread and hands its result back through
`App.post`, which puts it on a queue the Tk thread empties every 40 ms. A job
that worked closes its panel by itself. One that failed keeps its reason up.

**HiDPI.** Fonts and every pixel size grow with the display's scale. On X11 and
Xwayland the factor is `Xft.dpi / 96`; on Windows it follows the system DPI; on
a Mac it is 1, because points are already Retina. `BLOCKSLOT_SCALE=1.5` (1 to
4) overrides it for a desktop that gets it wrong. See `gui/core/uiscale.py`.

## The daemon

The daemon owns the store connection and the upload queue. `--install-service`
makes it start with the machine or the session:

| OS | What it installs | Queue | Log |
|---|---|---|---|
| Windows | the `Blockslot` service (admin), plus `Blockslot.exe --tray` for the icon | `C:\ProgramData\Blockslot\store` | `C:\ProgramData\Blockslot\service.log` |
| Linux | `~/.config/systemd/user/blockslot.service`, no root | `~/.local/state/blockslot/store` | `~/.local/state/blockslot/daemon.log` |
| macOS | `~/Library/LaunchAgents/com.datbird.blockslot.plist` | `~/Library/Application Support/Blockslot/store` | `~/Library/Logs/BlockSlot/` |

Without it, the picker and the Store screen start a daemon when none answers.
Store secrets are sealed with DPAPI on Windows and kept in the login Keychain
on a Mac (the file then holds `keychain:<field>`). On Linux and the Deck they
stay in `savepick.json`, mode 0600. The Linux unit is described in full in
`docs/linux-desktop.md`.

## The part that will surprise you

**Steam has to close before a launch option can change.** Steam keeps
`localconfig.vdf` in memory and writes it out when it exits, so an edit made
while it is running is discarded without a word. Blockslot asks Steam to close,
waits for the file to settle, writes, and starts Steam again. It says so before
it does it, and it does it once for a whole batch rather than once per game.
On a Mac it quits Steam through `steam://exit` and starts it with `open -a
Steam`; beside snap or flatpak Steam it goes through `/snap/bin/steam` or
`flatpak run`. On 2026-09-28 the whole sequence ran for real on a Mac and on
a Linux desktop with snap Steam.

This is also why Blockslot is not a Steam release. A tool shipped on Steam runs
under Steam by definition, so it could never make the edit stick.

## Where things live

| What | Path |
|---|---|
| The engine it configures | `~/.local/bin/savepick.py`, or inside `Blockslot.exe` on Windows and `Blockslot.app` on a Mac |
| ludusavi | `~/.local/bin/ludusavi`, `%USERPROFILE%\.local\bin\ludusavi.exe` |
| Its settings | `%APPDATA%\savepick.json`, or `~/.config/savepick.json` |
| Its log | `%TEMP%\savepick.log`, or `~/.local/state/blockslot/savepick.log` |
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
  tests/           python3 gui/tests/run.py
```

`core` never imports tkinter, so every decision it makes can be tested without
a display. That matters: a headless run of a GUI proves nothing, and the rules
about which save wins are the part that must not be wrong.
