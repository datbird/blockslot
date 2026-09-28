<p align="center">
  <img src="assets/banner.png" alt="BlockSlot" width="100%">
</p>

<p align="center">
  <b>Your game saves, on every device you play on.</b><br>
  Save sync for the games Steam Cloud does not cover, on a server you own.
</p>

<p align="center">
  <a href="https://github.com/datbird/blockslot/releases/latest">Download</a> ·
  <a href="#get-started">Get started</a> ·
  <a href="server/README.md">Server</a> ·
  <a href="decky/README.md">Steam Deck</a> ·
  <a href="gui/README.md">Windows, Mac and Linux</a>
</p>

---

You play on the PC, pick up the Steam Deck later, and the save is not there.
Steam Cloud covers some games and not others. Emulators, games from other
stores and anything added to Steam by hand are left to you. A sync folder that
copies files both ways will happily put the older save over the newer one.

BlockSlot fixes that for the games Steam misses:

- **Before a game starts,** it restores the newest save from any of your
  devices.
- **When the game closes,** it uploads the save. Offline, the save waits in a
  queue and goes up later.
- **Every save is kept** as a snapshot with its history. Nothing is ever
  overwritten.
- **Two devices played from the same save?** It asks which one to keep. It
  never guesses.
- **Emulators too.** A RetroArch, RetroBat or RetroDECK saves folder is split
  into one save per game, and each game keeps its own history.
- **Each OS keeps its own history.** A Mac never restores a Linux save of the
  same game, because the two builds save different files in different places.
  A Windows game under Proton on the Deck shares its history with Windows.

<p align="center">
  <img src="server/docs/images/01-games.png" alt="Every game on the store, in the web UI" width="100%">
</p>

## How it fits together

```
  Windows PC                Steam Deck               Mac, Linux desktop
  BlockSlot.exe             BlockSlot for Decky       Blockslot.app, a checkout
        \                         |                         /
         \  upload on exit, restore before start          /
          `---------------.       |       .--------------'
                           v      v      v
                 blockslot-server  (your NAS, one container)
                 S3 store (Garage)  +  web UI on :8761
```

- **The server** holds every save and has a web page for your games, devices
  and the settings they share. One Docker container. Unraid has a template.
- **Each device** runs a small daemon that uploads and restores. On Windows it
  is a service with a tray icon. On the Deck it lives inside the Decky plugin.
  On a Mac it is a LaunchAgent, and on a Linux desktop a systemd user unit.
- **Steam starts the game through BlockSlot.** Turning sync on for a game sets
  its launch option, and BlockSlot then runs the game itself. Steam's files
  are never edited by hand.

### Streaming with Steam Remote Play

A streamed game runs on the machine that streams it (the host), not on the
one in your hands, and the host's Steam starts it with the host's own launch
option. So where BlockSlot is installed on the host and the game is turned on
there, a stream syncs exactly like a local launch: the newest save is restored
before the game starts and uploaded after it quits.

- **No dialogs on the host.** Nobody sits there during a stream, so nothing is
  asked and nothing waits. Two saves that were both played are left for the
  next launch in front of a screen to decide, and warnings go to the log.
- **A game already running on the host is joined, not started.** Steam turns
  the Play button into "Stream from" that machine and nothing is restored.
  Quit it there first if you played somewhere else since.
- The client needs BlockSlot only if you also play on it locally.

## Get started

### 1. The server

On Unraid, search for **BlockSlot** in the Apps tab. Anywhere else:

```
docker run -d --name blockslot-server --restart unless-stopped \
  -p 3900:3900 -p 8761:8761 \
  -v /path/to/blockslot:/data \
  ghcr.io/datbird/blockslot-server:latest
```

Open `http://<server>:8761`. A short guide asks for what it needs: an admin
account, the address devices use, and your emulators if you have any. Then it
adds your first device. More in [the server's README](server/README.md).

### 2. Windows

1. Download `BlockSlot-<version>-windows-x64.zip` from the
   [latest release](https://github.com/datbird/blockslot/releases/latest).
2. Unzip it to a folder of its own and run `BlockSlot.exe`. It needs nothing
   else installed. Settings offers to fetch ludusavi.
3. On the server's **Devices** page, add this PC. In BlockSlot, open
   **Store**, enter the address and pairing code it shows, and choose
   **Pair with the server**.
4. On **Games**, turn sync on for the games you play.

### 3. Steam Deck

The plugin is not in the Decky store yet. Install the release's zip:

1. In Decky, open Settings and turn on **Developer mode**.
2. Under **Developer**, choose **Install Plugin from URL** and give it the
   `...-decky.zip` link from the
   [latest release](https://github.com/datbird/blockslot/releases/latest).
   To build it yourself instead, see
   [install from source](https://github.com/datbird/blockslot-decky#install-from-source).
3. On the server's **Devices** page, add the Deck. In the plugin, open
   **Server**, enter the address and the code, and pair.
4. On **Games**, turn a game on.

### Mac (Apple silicon)

1. Download `BlockSlot-<version>-mac-arm64.zip` from the
   [latest release](https://github.com/datbird/blockslot/releases/latest)
   (from 1.0.3 on) and unzip it.
2. Put `Blockslot.app` in `/Applications` and open it from there. Launch
   options and the LaunchAgent name the app by its path, so a copy run from
   Downloads or a disk image is refused. The app is signed ad hoc, not
   notarized. A copy your browser downloaded opens the first time only
   through System Settings, Privacy & Security, **Open Anyway**.
3. Pair it on **Store**, as on Windows. Secrets go into the login Keychain.
4. In **Settings**, install ludusavi if it is missing.
5. Install the daemon as a LaunchAgent:
   `/Applications/Blockslot.app/Contents/MacOS/Blockslot --install-service`.
6. On **Games**, turn sync on. A game gets the launch option
   `/Applications/Blockslot.app/Contents/MacOS/Blockslot --pick -- %command%`.
   The app carries the engine, so the Mac needs no python.

### Linux desktop

Run BlockSlot from a checkout with `python3 gui/blockslot.py` (the window needs
tkinter, `python3-tk` on Debian and Ubuntu), and install the daemon as a
systemd user unit with `python3 gui/blockslot.py --install-service`. Pair it on
**Store**, then in **Settings** install the engine (it goes to
`~/.local/bin/savepick.py`) and ludusavi. Games get the python form of the
launch option, which names that engine.
Steam from the snap, the flatpak or the distribution is found. What works
under snap Steam and why is in [docs/linux-desktop.md](docs/linux-desktop.md).

## Screenshots

### The server

| | |
|---|---|
| ![A game's history](server/docs/images/02-game-history.png) | ![Two saves: pick the one to keep](server/docs/images/03-two-saves.png) |
| ![Emulator library games](server/docs/images/04-library.png) | ![Settings every device shares](server/docs/images/05-settings.png) |
| ![Devices and pairing](server/docs/images/06-devices.png) | ![Storage and clean-up](server/docs/images/07-storage.png) |

### Windows

| | |
|---|---|
| ![Games](docs/images/windows-games.png) | ![Store](docs/images/windows-store.png) |
| ![Emulator games](docs/images/windows-emulator-games.png) | ![Activity](docs/images/windows-activity.png) |

### Steam Deck

| | |
|---|---|
| ![Games](docs/images/deck-games.png) | ![Emulator games](docs/images/deck-emulator-games.png) |
| ![Settings](docs/images/deck-settings.png) | ![Activity](docs/images/deck-activity.png) |

<p align="center">
  <img src="docs/images/deck-quick-access.png" alt="The Quick Access panel" width="40%">
</p>

## What it is not

- **Not a replacement for Steam Cloud.** Games Steam Cloud covers can stay
  with Steam. BlockSlot hides them by default.
- **Not a cloud service.** There is no account and nothing leaves your
  network unless you put the server behind something like Cloudflare Access.
  The server supports that, for sign-in and for device tokens.
- **Not a backup of your whole PC.** It carries game saves, and only for the
  games you turn on.

## Building from source

Everything on a device is the Python standard library. Nothing is installed
with pip to run it.

| Part | Where | Build |
|---|---|---|
| Server | `server/` | `docker build -f server/Dockerfile -t blockslot-server .` |
| Windows app | `gui/` | `python tools/build_exe.py` (PyInstaller, build time only) |
| Mac app | `gui/` | `python3 tools/build_exe.py --zip` on an Apple silicon Mac (see below) |
| Decky plugin | `decky/` | `pnpm install && pnpm build && python3 scripts/package.py` |
| Engine | `engine/` | nothing to build: one file per job, shared by every device |

The Mac build needs python.org's Python (universal2, with its own Tcl/Tk) and
Apple's Command Line Tools (`xcode-select --install`), because PyInstaller
calls `lipo` and `install_name_tool`. It writes `dist/Blockslot.app`, a
self-contained arm64 folder bundle signed ad hoc, and `--zip` packs it into
`dist/Blockslot-mac-arm64.zip`. Only the build needs any of this. The app runs
on a Mac with no python and no developer tools.

```
engine/     the save picker Steam starts, the store client, the device daemon
gui/        the desktop app. gui/core holds every rule; gui/ui draws them
decky/      the SteamOS plugin. It carries a staged copy of gui/core
server/     the container: Garage and the web UI
index/      games.json: which files are saves, which games Steam Cloud covers
tools/      the index generator and the Windows and Mac builds
```

Tests run on every push, on Windows, macOS and Linux:

```
python engine/test_savepick.py
python gui/tests/run.py
python -m unittest discover server/tests
```

The store, the daemon and the save splitter have suites of their own, run
from `engine/`: `python3 -m unittest test_slotstore test_slotd test_saveunits`.

## Status

In daily use by its author on a Windows PC and a Steam Deck. On 2026-09-28 a
Mac (macOS 26.6, running the release's Mac app) and an Ubuntu 26.04 laptop
with snap Steam each ran a game through BlockSlot, each exit backup reached
the store, each kept its own save history, and a Remote Play stream hosted by
the laptop synced like a local launch. The server is new. One gap is known: the **Emulator games** screen still keys each folder by
the older Syncthing device folder, so a device set up with only a store cannot
give an emulator its folder yet. Bug reports and pull requests are welcome in
[issues](https://github.com/datbird/blockslot/issues).

## License and credit

MIT. See [LICENSE](LICENSE).

BlockSlot drives [ludusavi](https://github.com/mtkennerly/ludusavi) for every
backup and restore. Its game index is derived from
[ludusavi-manifest](https://github.com/mtkennerly/ludusavi-manifest), which is
compiled from [PCGamingWiki](https://www.pcgamingwiki.com). The server stores
saves with [Garage](https://garagehq.deuxfleurs.fr). Full notices are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
