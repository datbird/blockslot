# Blockslot on a Linux desktop

The same daemon as on Windows and the Deck, run by systemd as a user unit, and
the same launch option, which has to work when Steam is the snap.

Probed on a test laptop (Ubuntu 26.04, systemd 259, `steam` snap 1.0.0.87 rev
271 on base core24) on 2026-09-28. The unit's lifecycle was run for real on a
second Ubuntu machine (systemd user manager).

## Finding Steam

Three installs, three places. `gui/core/steamdir.py` looks in all of them:

| Install | Data folder | Notes |
|---|---|---|
| Distribution package | `~/.local/share/Steam` | `~/.steam/steam` and `~/.steam/root` point at it. |
| Snap (Ubuntu's App Center) | `~/snap/steam/common/.local/share/Steam` | The snap's own `~/.steam` is inside the snap (`~/snap/steam/common/.steam`), so the real `~/.steam` does not exist at all. |
| Flatpak | `~/.var/app/com.valvesoftware.Steam/.local/share/Steam` | |

`userdata/<id>/config/localconfig.vdf`, `shortcuts.vdf` and
`appcache/appinfo.vdf` are under that folder in every case.

With more than one installed, the Steam that is running wins (its client is
`<root>/ubuntu12_32/steam` in `/proc/<pid>/cmdline`, snap included), then the
one whose `config/loginusers.vdf` was written last. `--check` prints which
kind it found (`Install: snap`).

Closing and starting Steam goes through `/snap/bin/steam` for the snap and
`flatpak run com.valvesoftware.Steam` for the flatpak, so a second install on
the PATH is never the one asked to shut down.

**Proven on the test laptop** (this code, fed in on stdin, nothing installed):
the running snap client was found in `/proc`, `find_root()` answered
`~/snap/steam/common/.local/share/Steam`, kind `snap`, the signed-in user, with its
`localconfig.vdf` and `appcache/appinfo.vdf` present; `steam_command()` was
`/snap/bin/steam` and the launch option's python `/usr/bin/python3`.

## The daemon as a systemd user unit

    python3 gui/blockslot.py --install-service      write, enable, start
    python3 gui/blockslot.py --uninstall-service    stop, disable, remove

No root. `--install-service` writes `~/.config/systemd/user/blockslot.service`:

    ExecStart="<python>" "<checkout>/gui/blockslot.py" "--daemon" "--config" "<savepick.json>"
    Restart=on-failure, RestartSec=10, at most 5 starts in 5 minutes
    RestartPreventExitStatus=2      the store settings are wrong; restarting cannot fix it
    UMask=0077
    StandardOutput=append:~/.local/state/blockslot/daemon.log
    WantedBy=default.target         starts with the user's session

| What | Where |
|---|---|
| Queue, `daemon.json`, `bases.json` | `$XDG_STATE_HOME/blockslot/store` (`~/.local/state/blockslot/store`), mode 0700 |
| Daemon log | `$XDG_STATE_HOME/blockslot/daemon.log`, beside `savepick.log` |
| Settings and store secrets | `~/.config/savepick.json`, mode 0600 (tightened at install if it was not) |

Secrets are plain text in a 0600 file, as on the Deck; DPAPI is Windows only.

The unit names the python and the checkout that installed it. Moving the
checkout means installing again; `--check` says so (`Daemon unit: ... names
another copy`).

When the unit is installed, the picker and the Store screen start the daemon
with `systemctl --user start blockslot.service` instead of forking it. A
daemon forked by a game that snap Steam started would live in the snap's
sandbox and cgroup, with the snap's private `/tmp`.

**Proven on the second Ubuntu machine, for real:** install wrote and enabled the unit and the
daemon answered `slotd.py --status`; `kill -9` of the daemon was restarted by
systemd (NRestarts 1) and answered again; `/stop` left it inactive, not
restarted; the picker's `connect()` then started it through systemctl
(`how == "started"`); a bad store type exited 2 and systemd left it failed
instead of looping; uninstall removed the unit and the wants link.

## Snap Steam: what a game's launch option runs in

A game started from snap Steam runs inside the snap's confinement. What that
means for Blockslot, each point probed with `snap run --shell steam` on the test laptop:

| Question | Answer |
|---|---|
| Which `/usr`? | The snap's base, core24. `/usr/bin/python3` is core24's **3.12.3**, not the host's 3.14. A python from a venv, `/usr/local` or `/opt` does not exist in there. |
| Does that python run the engine? | Yes, also with Steam's runtime `LD_LIBRARY_PATH` in front (json, ssl, http.client, hashlib, sqlite3, pyexpat, zlib all import). |
| HOME | `~/snap/steam/common`. `XDG_CONFIG_HOME` and `XDG_DATA_HOME` point in there too. The real home is in `SNAP_REAL_HOME`. |
| Can it read `~/.local/bin/savepick.py`? | Yes. The `home` interface alone denies hidden folders, but the snap's `steam-support` interface adds `allow all` to its AppArmor profile ("to avoid steam constantly breaking with every update"). This is what lets the engine live in `~/.local/bin`. If Canonical ever narrows that, hidden folders stop being reachable. |
| Can it reach a daemon on 127.0.0.1? | Yes: the snap has `network` and shares the host's network namespace. |
| `/tmp` | Private to the snap. `TMPDIR` is set to `/run/user/<uid>/snap.steam`, which the host can see, but Blockslot does not rely on that. |
| `systemctl --user` | Works from inside (the session bus is `/run/user/<uid>/bus`). |

So the launch option Blockslot writes for a snap Steam game is

    /usr/bin/python3 /home/<you>/.local/bin/savepick.py -- %command%

`/usr/bin/python3` because it is the one interpreter that exists on both
sides. A game wrapped earlier with any other python is wrapped again.

Inside the snap the engine uses the real home for its own files and leaves
HOME alone for ludusavi:

- `savepick.json`, the log, the vault, the daemon's queue and `daemon.json`,
  and the ludusavi binary: under `SNAP_REAL_HOME`. Otherwise the picker would
  find no settings and a daemon started from there would queue where the real
  one never looks.
- Folders handed to the daemon (the exit backup, a fetched snapshot): under
  `~/.local/state/blockslot/handoff`, not the snap's `/tmp`.
- ludusavi keeps the snap's HOME, because that is where the game writes. A
  Unity game such as Getting Over It saves to
  `~/snap/steam/common/.config/unity3d/...` under snap Steam, not
  `~/.config/unity3d/...`.

**Proven on the test laptop, end to end but without Steam starting a game:** from
`snap run --shell steam`, with Steam's runtime `LD_LIBRARY_PATH`, the engine
(savepick, slotd, slotstore, fed in on stdin, nothing installed) resolved
`~/.config/savepick.json`, `~/.local/state/blockslot/store` and
`~/.local/bin/ludusavi` under the real home, reached a daemon running outside
the snap on 127.0.0.1, staged a save from a folder outside the snap's `/tmp`,
and the daemon committed it to its store.

**Proven with a real game, later on 2026-09-28:** on the test laptop, BlockSlot
turned Getting Over It on, which closed snap Steam, wrote the launch option and
started Steam again. Steam then ran the game through the wrap, and the exit
backup went up to the store. Its save under snap Steam is where the snap's
`XDG_CONFIG_HOME` puts it,
`~/snap/steam/common/.config/unity3d/Bennett Foddy/Getting Over It/`, and that
is the path the store recorded.

ludusavi ran inside the snap with the snap's HOME, so it read its config and
manifest from `~/snap/steam/common/.config/ludusavi`. Settings puts the
manifest there. A ludusavi config made by hand outside the snap is not the one
the picker's ludusavi sees.

## Each OS keeps its own history

That same save is a Unity prefs file on Linux and a preferences plist on a
Mac, so neither can be restored where the other belongs. Every snapshot
records the OS of the game build that made it, and a device restores and is
offered only its own OS's snapshots. A native Linux game on this desktop
shares its history with the Deck's native builds. A Windows game under Proton
keeps its save in the Wine prefix, so it shares its history with Windows.

## Remote Play from this desktop

When another device streams a game from this desktop, the game runs here and
snap Steam starts it with this desktop's launch option, so the wrap and the
exit upload run here, as for a local launch (see "Steam Remote Play" in
`engine/README.md`). No dialog is shown here during a stream.

What was seen on the test laptop on 2026-09-28: a stream from the Mac VM
seemed to start Getting Over It with no line in `savepick.log`. The Steam
logs showed no streamed launch at all. The game was already running,
orphaned by a `kill -TERM` of savepick at 10:33:29 that stopped only
steam-launch-wrapper (fixed since, see the engine README), and a client's
Play button turns into "Stream from" a host where the game runs. A second try
at 11:04 reached Steam's pairing step: `remote_connections.txt` logged
"Received authorization request" from the VM, which needs a PIN typed on
this desktop's screen, and it was cancelled there.

## Still unproven

- The engine's dialogs under snap Steam. The snap carries its own zenity
  (`/snap/steam/271/usr/bin/zenity`), but no dialog was shown through it.
- Flatpak Steam: discovery only. Its manifest grants no access to the real
  home, as far as is known, so a launch option naming
  `~/.local/bin/savepick.py` would likely not start there. Not probed: there
  is no flatpak Steam on either test bed.
- Blockslot's own non-Steam shortcut under snap Steam: the window needs
  tkinter, and core24's python has none (`No module named 'tkinter'`, seen).
- The unit on the test laptop itself (not installed there, by instruction).
- A real Remote Play stream from this desktop. The Mac VM was never paired
  with it (pairing needs a PIN typed on this screen), so no streamed launch
  has run here. That the host applies its own launch option and sets
  `SteamStreaming=1` is from other people's reports, not seen here.
