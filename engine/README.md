# Blockslot engine

Decides whether restoring a save is safe, and refuses when it is not. Then makes
sure the save you just made actually reaches the other devices.

This is the engine of [Blockslot](../README.md). It still ships as `savepick.py`,
which is the name it was born with and the name in the Steam launch options.

## Why it exists

`ludusavi wrap` restores another device's backup on every game launch. It never
checks whether that backup is older than the local save. On 2026-09-03 that rolled a
Dark Souls II save back by 14 hours on DESKTOP, because the Steam Deck's newest backup
was from 09-02 1:28 AM while the live save was from 09-02 3:36 PM.

## How the whole thing fits together

Three parts, one job each. There is no SSH anywhere in the running system. Each
machine only ever reads its own local disk.

**Syncthing** moves one shared folder of backups, never the live saves. The hub is
the NAS, a server that is always on. Every device, hub included, holds a full copy of
the same `gamesaves` folder, and inside it each device owns exactly one subdirectory
and writes only there, because ludusavi keeps a `mapping.yaml` index inside and two
writers would conflict on it:

```
gamesaves/
  deck/        <- the Steam Deck writes here, and nowhere else
  desktop/     <- DESKTOP writes here, and nowhere else
```

Nothing enforces that one-writer-per-directory rule at the Syncthing layer. It holds by
convention, and savepick never writes outside its own directory. A new device just
means a new subdirectory; the folder count never grows with the number of devices.

**ludusavi** copies files between the live save and a backup folder, and translates
Windows paths into the Deck's Proton prefix. `backup.path` is this device's own
subdirectory. `restore.path` stays pointed at that same subdirectory, so a bare
`ludusavi restore` typed by hand does something harmless, but savepick never relies on
it: it always passes `--path` to read one specific peer directory by name.

**savepick** decides. On launch it first waits for Syncthing, then reads every peer
subdirectory under the shared folder and compares two timestamps, both on local disk:

```
mtime of the live save file
        vs
mtime of the save file inside the newest backup, across every peer directory
```

Adding a third device costs nothing extra here: the newest backup anywhere wins, and
the dialog names which device it came from.

| Comparison | Action |
|---|---|
| No backup exists | Launch, no restore |
| Backup newer than the live save | Restore, no prompt |
| Live save newer | Show the conflict dialog |
| Within 2 seconds of each other | Launch, no restore (already in sync) |

Timestamps travel correctly between machines because ludusavi preserves mtime through a
restore, so "when was this save last written" survives the trip.

**Only files ludusavi tags `save` count.** Ludusavi lists more than saves. For Dark
Souls II it lists Steam screenshots, and it gives those no tags at all. On 2026-09-05
three screenshots taken at 1:56 PM made the Deck's live save look 20 hours newer than
it was, and the dialog offered to keep it over a real save from another device. The
one fallback: if no file in the set carries any tag, take every file, because returning
nothing would read as "fresh install" and that restores.

## Waiting for Syncthing first

savepick will not compare anything until Syncthing says this device is caught up with
the hub. This is Syncthing's own answer from `/rest/db/status`, not a guess:

```
state == "idle"  and  needBytes == 0  and  needTotalItems == 0  and  no errors
```

Why it exists: on 2026-09-05 the Deck woke, Syncthing started pulling, and savepick
compared timestamps at 13:56:04. Syncthing finished at 13:57:15. For those 71 seconds
the newest backup on disk was the one from the afternoon before, so savepick offered to
keep a save 20 hours older than the real one. It was right about the disk. The disk was
stale. The hub being always on does not remove this window; it only means the wait
almost always ends on its own rather than depending on a second gaming machine being
awake.

What it does, in order:

1. Resumes the hub device and the shared folder if either is paused. **It asks for no
   scan.** See below.
2. Waits for the hub to connect. It never reads the folder status before that. A
   folder reads idle with nothing needed right after a wake, because the hub has not
   sent its index yet.
3. Polls the folder every 1.5 s and says what Syncthing is actually doing: a transfer
   shows its percentage and how much is left, a scan says it is a scan, weightless
   changes say how many.
4. Requires the folder to stay ready for `SYNC_SETTLE_SECONDS` (6 s) before saying yes,
   which covers the index arriving a moment after the connection. The bar fills across
   those 6 seconds, because it is the one part of the wait whose length is known.

### The scan is at exit, not at launch

This wait used to post `/rest/db/scan` for the whole folder before doing anything else,
and `state == "idle"` cannot be true while the folder is scanning. So every launch paid
for a full walk of the shared folder.

Measured on the Windows PC on 2026-09-21, with nothing to transfer and nothing changed:
**51.6 seconds**, for 28,889 files and 14,087 directories holding every retained backup
of every game for every device. His own log shows the same wait running 58 to 126
seconds on ordinary launches, with the window reading `Getting saves from nas
... 100%` for all of it. The percentage counts bytes left to transfer, there were none
left, and the window was not waiting on a transfer at all.

A scan tells Syncthing what changed **on this device**. Nothing here writes to the
shared folder except the exit backup, and it writes to exactly one directory, this
device's own. Every other device's directory is written by Syncthing, which indexes
what it writes. So the scan was never capable of changing a single answer in this wait.

It now runs where the write happens: `backup_on_exit` posts
`/rest/db/scan?folder=<folder>&sub=<this device>/<this game>` after ludusavi returns,
and waits for the folder to go idle before asking the hub anything. That fixes a real
hole as well. The hub is asked from the index, so a backup that has not been scanned
yet is a backup the hub does not know it needs, and the exit wait would have called
that "up to date".

### The exit wait asks two questions, and never asks `/rest/db/completion`

    the hub HOLDS a file from this backup   /rest/db/file, availability
    the hub NEEDS nothing else from it      /rest/db/remoteneed, this game's path

The first alone would pass while the rest of a save set is still moving. The second
alone would pass in the moment before the hub has heard of the backup at all.

**`/rest/db/completion` is not trustworthy for this, measured 2026-09-21.** On the
Deck, running Syncthing v2.1.2, it reported 99.85% with 8.2 MB, 20 items and 28
deletes outstanding. `/rest/db/remoteneed` said the hub needed nothing, and the hub
already held the save on disk. savepick ran its full 180 seconds on that number and
told him NOT SYNCED for a save that had arrived. DESKTOP, on v1.30.0, reported 100
and agreed with the disk, so the two Syncthing versions do not even answer alike.

The percentage is of THIS backup's outstanding bytes, not the folder's. Against the
folder, one save moved the number by a hundredth of a percent, so the window read
"Sending to nas ... 99%" from the first second and sometimes went DOWN as new
work was indexed.

A Syncthing with no `/rest/db/remoteneed` falls back to the old folder-completion
wait. Both machines have it: v1.30.0 and v2.1.2 both answer `{files, page, perpage}`.

Measured the same day: the pre-launch wait went from 58 to 126 seconds down to
**7.5 seconds**, for both a small game and the RetroBat save set.

**There is no time cap on a healthy transfer**, his decision. The wait ends when
Syncthing says it is done, when you cancel, when the folder itself stops, or when a
persistent pull error has had a full retry cycle to clear (below).

### Two kinds of error, and only one of them is fatal

Syncthing reports these separately, and savepick treats them separately.

| Field | Meaning | savepick |
|---|---|---|
| `error`, a string | the folder itself stopped: path missing, marker gone, no space | give up, no restore |
| `errors`, a count | individual items failed this pull, Syncthing retries about once a minute | keep waiting, then give up if it does not clear |

On 2026-09-10 at 22:13:59 the Deck refused to restore and said it could not get an
answer from Syncthing. It could. DESKTOP's ludusavi retention had deleted two old
backups, Syncthing carried the deletes to the Deck, and its first pass could not remove
the directories because it tried the parents before the children. Nine pull errors, and
Syncthing's own log said `will be retried (wait=1m1s)`. savepick sampled 20 seconds into
that minute, read a nonzero count, and gave up. Syncthing finished the deletes at
22:14:39, 40 seconds later. Nothing was wrong.

Retention prunes on every spoke now, so this shape recurs on ordinary sessions.

**There is no repair anymore.** The old code reverted the incoming folder once pull
errors would not clear on their own. That was safe only because that folder was
receive-only and held nothing this device owned. Under the shared folder, this
device's own subdirectory holds saves nobody else has written yet, so a revert could
throw away a session that had not reached the hub. If pull errors are still there
after `SYNC_HEAL_AFTER_SECONDS` (90 s), Syncthing's own retry has had a full cycle and
is not going to clear them, so the wait simply ends and returns not-confirmed. The
2026-09-10 event above cleared in 40 seconds, well inside that window.

### The window

One line of status, a bar, and a log pane that stays hidden until you ask for it. The
status line is written as `<text>|<percent>`, or `<text>|-` where there is no honest
number to show. A half-written line matches neither and is ignored, so the window
keeps what it has rather than flickering.

**A Marquee progress bar is drawn by comctl32 v6 and by nothing else.** Without visual
styles WinForms falls back to the classic control, which draws an empty box and never
animates it. That is what the window had been showing. `EnableVisualStyles` is asked
for now, and where `RenderWithVisualStyles` is still false, as it is on the Windows PC,
the bar is swept by hand instead: one pass every four seconds. It always moves.

### Putting the game in front

Windows hands the foreground to whoever had it last. savepick is started by Steam, not
by a click, so the game opened BEHIND Steam and he alt-tabbed to it on every launch.

`SetForegroundWindow` on its own is refused, and it returns success while doing
nothing. What lifts the lock is `AttachThreadInput`: with savepick's input queue
attached to the foreground window's thread, savepick counts as the foreground process
and the call is honoured. The window is found by walking the process tree under the
game, because the window often belongs to a grandchild: RetroBat starts its own
frontend and the shadPS4 launcher is a python script. An owned window is a dialog and
a window with no title is a helper, so neither is ever raised.

It runs on a daemon thread beside the game, gives up the moment it succeeds, and stops
after 40 seconds. A player who alt-tabs away a second later keeps what they chose.

On Linux it asks `xdotool` instead, and only where the compositor says which window is
active. **gamescope does not**, measured on the Deck on 2026-09-21:

    xdotool getactivewindow
    XGetWindowProperty[_NET_ACTIVE_WINDOW] failed (code=1)

Game Mode picks the focused window itself, from what Steam tells it, so savepick logs
one line and leaves it alone. A desktop session answers, and gets the raise.

**Two ways to cancel**, because the Deck's dialogs do not always draw under gamescope.
The spinner has a Cancel button, and B on any gamepad ends the wait. Without the pad, a
no-cap wait behind an invisible window would mean the game never starts.

**Anything but a confirmed sync means no restore.** Cancelled, unreadable Syncthing, a
persistent pull error past the heal window, or no Syncthing settings at all: savepick
shows a warning saying so, then launches the game on the save already on this device.
It never restores and never shows the conflict dialog on an unconfirmed folder, because
both would present a stale folder as current.

**A known limit.** savepick can only see what the hub has announced. If a peer device
wrote a backup and the hub's Syncthing has not indexed it yet, this device is genuinely
current with a stale index and savepick says yes. In normal use the exit backup covers
this: savepick does not finish until Syncthing confirms the hub holds the new backup.

## The one promise: the game always starts

savepick owns the save decision. It must not also hold a veto over the game
running. Every path through `main()` now ends with the game launched, and the
tests enforce it. Only a call with no game command after `--` does not launch,
and there is nothing to launch there.

Getting to that meant taking `ludusavi wrap` out of the launch path entirely.
Once savepick did the restore and the backup itself, wrap had no job left
except spawning the same command savepick can spawn, and it carried three ways
to stop the game:

- **It returns nonzero when any entry of a save job fails.** 2026-09-11: three
  Steam screenshots in the Deck's backup had no home on Windows, so wrap
  reported a restore that had actually worked as a failure and never ran DS2.
- **With `--infer steam` and no SteamAppId it blocks on a GUI prompt, forever.**
  Proven on the Deck: the command never ran and the process had to be killed.
  savepick's own no-SteamAppId path fed straight into it, so the one case where
  savepick gave up early was also the one case where the game never started.
- **Its exit codes say nothing useful.** A -2 could be a cancelled dialog or a
  signal, and savepick could not tell a failed restore from a finished session.

So savepick runs the game with `subprocess.call` and returns its exit code.

### Closing stdin, which was a hang all by itself

`ludusavi find` takes game names on stdin as an alternative to arguments, so it
reads stdin when stdin is a pipe. savepick never set it, and the child
inherited whatever the parent had.

Measured on the Windows PC on 2026-09-11: the same `find --steam-id` that takes
1 second from a console took the full 120 second timeout from Python, for a
known id and an unknown one alike, because it sat waiting on an inherited pipe.
With `stdin=subprocess.DEVNULL` it is 0.7 seconds. Every ludusavi call closes
its stdin now.

## Who performs the restore

savepick runs the everyday restore itself, then starts the game directly. It
does not hand that restore to `wrap`, and `wrap` is not in the launch path at
all (see "The one promise" above).

On 2026-09-11 at 00:27 the Windows PC pulled the Deck's newest backup. The save
restored perfectly and was byte-identical afterwards. But that backup also held
three Steam screenshots at Deck-only paths under `/home/deck`, which have no
home on Windows. ludusavi returns nonzero when **any** entry fails, so `wrap`
exited 1, put up "Failed to restore save data", and never started the game.

So savepick asks the disk, not the exit code. After the restore it checks
whether the live save now matches the backup it compared. If it does, the game
starts, whatever else failed. If it does not, savepick says so plainly and the
game starts on the save that was already here.

Running the restore itself buys one more thing: savepick passes `--backup` and
pins it to the exact backup it compared. `wrap` always takes the newest, so a
sync landing between the comparison and the launch would restore something
savepick never looked at.

**Answering B gets a notice, not a second prompt.** `--ask-downgrade` was
ludusavi's second "are you sure" on a downgrade, and it caught a wrong answer
on 2026-09-03. Only `wrap` has that flag, and wrap is no longer in the path. In
its place savepick names the vault snapshot holding the newer save you just
replaced. That is the same information the conflict dialog already showed, plus
the one thing that turns a mis-pressed B from a lost session into a file copy.

### Store screenshots do not belong in a save backup

The root cause of that night was screenshots being backed up at all. ludusavi
has a setting for it and it was off on both machines:

```yaml
backup:
  filter:
    excludeStoreScreenshots: true
```

It is on now. **`backup --preview --api` reports the pre-filter scan**, so the
screenshots still appear there and the preview is not how you check this. Run a
real backup into a throwaway directory instead:

```sh
ludusavi --no-manifest-update backup --force --path /tmp/check "<game>"
```

`ignoredPaths` does not work for these. It is applied to manifest-discovered
files, and store screenshots are not one, so even the exact `.jpg` path in
`ignoredPaths` leaves them in.

## The conflict dialog

Shown only on a real conflict. Identical wording on every device.

```
Dark Souls II: Scholar of the First Sin

The backup is OLDER than the save on this device.

This device:         Sep 3, 2026  4:13 PM   (newest)
Windows PC backup:   Sep 2, 2026  3:36 PM

A = keep this device.   B = restore the backup.
Keeping the newest in 30 seconds.
```

Both rows are padded to a common width so the dates start in the same column.
Comparing them at a glance is the only thing this dialog is for. Padding needs a
monospace font, so Windows sets Consolas on the label and the Deck's text goes to
zenity inside a `<tt>` span. zenity renders Pango markup by default, which means a
game name holding `&` or `<` has to be escaped or it breaks the dialog. kdialog, the
fallback, gets the plain unescaped text, because it does not render markup.

The peer row's label comes from `device_names` in config, keyed by that peer's
subdirectory name (`"deck": "Steam Deck"`). An unknown directory falls back to the
directory name itself. No product name is ever guessed from an operating system: that
guess could only ever describe two devices, and it called any Linux peer a Steam Deck.

It auto-continues with the newest save after 30 seconds, so a run of sessions on one
device never becomes a click-through habit.

## On exit

The backup always runs. There is no question, because backing up only reads the save,
and declining is the one action that leaves the hub stale.

A spinner stays up through the whole thing, showing the transfer percentage, and it does
not finish until Syncthing reports the hub has actually received the backup. "Synced"
now means the server holds it, not that some other console does. If the hub cannot be
reached, a warning appears telling you the save is safe here but the server does not
have it, and not to play the game on another device until it does.

## The vault: the last 10 live saves

ludusavi's own retention keeps the last 10 **backups** per game per machine
(`backup.retention.full: 10`, raised from 3 on 2026-09-03). Restore any of them by
hand with `ludusavi restore --backup <ID>`.

The vault keeps the last 10 **live saves**. In the moment before a restore
overwrites the save on this device, savepick copies it aside first. That covers
the one case retention cannot: progress that no backup ever captured, after a
crash or a failed exit backup.

```
~/.local/share/savepick/vault/<game>/<UTC stamp>/     Linux and the Deck
%LOCALAPPDATA%\savepick\vault\<game>\<UTC stamp>\    Windows
```

The vault sits outside the synced folder, so Syncthing never sees it and it never
reaches another device. Copies use `copy2`, which preserves mtime, because
mtime is what this whole system uses to tell saves apart.

Each snapshot holds the save files under their real names plus a `manifest.json`
giving the original absolute path of each one. Recovery is a plain copy back.

**savepick only writes to the vault.** It never reads it and never restores from
it. That stays a deliberate manual act.

A snapshot is taken on every restore, including one you asked for by answering B
in the conflict dialog. A fresh install with no live save is the exception: there
is nothing to protect, so nothing blocks the restore.

**If the snapshot fails, the restore does not happen.** Same rule as everywhere
else here: a skipped restore costs one manual sync, a lost live save costs hours.
A partial snapshot is deleted rather than kept, because half a snapshot still
looks like a recovery point.

### The vault and hub versioning

There is a second layer, and it lives entirely on the hub. The hub's copy of
`gamesaves` runs Syncthing's staggered file versioning, kept up to 365 days, while
every device stays set to none. That protects against the case the vault does not
cover: a re-imaged or restored device pushing an empty or stale subdirectory up, and
the deletions or overwrites propagating out to every other device, or ludusavi
retention pruning something that turns out to have been wanted after all.

`.stversions` lives inside the shared folder path on the hub, and Syncthing never
syncs it, so no spoke device ever sees it and it costs them nothing. Recovery from it
is a manual copy on the hub, in the same spirit as the vault. **savepick never reads
it**, on any device, hub included.

## The fail-safe rule

Every failure resolves to "do not restore". No `SteamAppId`, ludusavi errors, no dialog
program, a crash, an unreadable gamepad: it launches on the live save. A skipped restore
costs one manual sync. A wrong restore costs hours of play.

## Controllers

Reading the pad directly, because in Game Mode the controller drives Steam rather than a
stray dialog window.

- **Linux and the Deck**: every joystick under `/dev/input/js*` whose name looks like a
  gamepad, rescanned every 2 seconds so a pad connected mid-dialog still works. Matching
  by name matters: `js0` on the Deck is "Mouse passthrough (absolute)" and the real pad
  is `js1`, "Microsoft X-Box 360 pad 0".
- **Windows**: XInput, all four slots.

Both wait for every button to be released once before accepting input, so a press left
over from quitting the game cannot answer the dialog instantly. If A and B are somehow
both held, A wins, because keeping the newest save is the safe outcome.

## Install

Copy `savepick.py` next to the ludusavi binary, then set the Steam launch option. Steam
must be **closed**, because it holds `localconfig.vdf` in memory and rewrites it on exit.
Use forward slashes on Windows, since VDF treats `\` as an escape.

DESKTOP, at `C:/Users/player/.local/bin/savepick.py`:

```
C:/Python313/pythonw.exe C:/Users/player/.local/bin/savepick.py -- %command%
```

Steam Deck, at `/home/deck/.local/bin/savepick.py`:

```
/usr/bin/python3 /home/deck/.local/bin/savepick.py -- %command%
```

`pythonw.exe` on Windows avoids a console window for the whole play session.

### Optional config

`~/.config/savepick.json` on Linux, `%APPDATA%\savepick.json` on Windows. Without it
savepick still works, it just does not wait for the sync.

```json
{
  "syncthing": {
    "url": "http://127.0.0.1:8384",
    "apikey": "<this device's Syncthing API key>",
    "folder": "gamesaves",
    "hub_id": "<the hub's Syncthing device id>",
    "hub_name": "nas",
    "device_dir": "deck",
    "device_names": { "deck": "Steam Deck", "desktop": "Windows PC" }
  }
}
```

`folder` is the one shared Syncthing folder every device holds a full copy of.
`device_dir` is this device's own subdirectory inside it, spelled out explicitly
rather than derived from the hostname, so renaming a machine can never make it
silently adopt a different directory and orphan its own backup history.
`device_names` labels every peer's subdirectory for the dialog and the log; an
unlisted directory falls back to its own name.

This is a clean break from the old two-folder shape. `incoming_folder`, `peer_id`
and `peer_name` are gone, and a config file still in that shape fails the required-key
check, which already resolves to "not configured": no restore, and the game still
launches on the local save. Mode 600, it holds an API key.

## Dialog rendering, the hard-won part

On the Steam Deck in Game Mode, the only combination that draws a visible window is the
host's own `/usr/bin/zenity` with **Steam's environment left completely alone**.

- Stripping `LD_LIBRARY_PATH` and friends produced a process that ran its full timeout
  with nothing on screen.
- Steam's bundled zenity in `ubuntu12_32/steam-runtime` cannot run at all: it is a GTK2
  build and the Deck has no GTK2, so it dies on `libgtk-x11-2.0.so.0`.
- zenity's list widget renders as one squashed unreadable line under gamescope. The
  plain question box is fine.

## Logging

`%TEMP%\savepick.log` on Windows, `/tmp/savepick.log` on Linux. One line per launch with
the game, both timestamps and the decision, plus the gamepad and dialog outcomes.

## Tests

```
python3 -m unittest test_savepick -v
```

206 tests over the decision logic, the mtime scan, the save-tag filter, the Syncthing
wait, picking the newest backup across every peer directory, the restore and its check,
the launch path, the vault, joystick and XInput parsing, the config loader, the dialog
wording and every fail-safe path. They need no ludusavi install, no Syncthing and no
GUI. Several are regression tests for bugs that actually shipped, including a kdialog
error code once read as consent to restore, `js0` being a mouse rather than the pad, a
Linux-only `import fcntl` at module scope that stopped savepick starting on Windows at
all, Steam screenshots counting as saves, comparing timestamps before Syncthing had
finished, a retryable pull error read as a dead Syncthing, and a screenshot that could
not be restored stopping the game from starting. Newer ones cover the hub-and-spoke
shape directly: a config still in the old two-folder shape reads as not configured, a
folder stuck on pull errors past the heal window ends the wait instead of repairing
itself, the newest backup is chosen across three or more peer directories rather than
just the first one found, and no user-visible string carries a hardcoded product name
guessed from an operating system.

## Status

Live on both machines since 2026-09-03. Full round trip proven: conflict dialog answered
from the Deck's controller, unprompted exit backup, Syncthing confirmed, and DESKTOP
holding a byte-identical copy.

2026-09-05: the save-tag filter and the Syncthing wait shipped to both machines. The
wait is proven on real hardware. With 320 MB of incompressible data staged on DESKTOP
and the Deck's folder paused, savepick resumed the folder, reported 73 through 98
percent, and returned true only after the data had fully landed, 26.6 s. It also
resumed a folder that was paused on purpose, and it correctly said "current" when
ludusavi had deduped a forced backup and there was genuinely nothing to send.

2026-09-10: pull errors no longer read as a dead Syncthing, and a stuck folder now
repairs itself. Syncthing v2.1.2 on the Deck and v1.30.0 on DESKTOP both answer
`/rest/db/revert` (405 on GET, against 404 for a route that does not exist), so the
repair is available on both.

2026-09-11: savepick runs the everyday restore itself and checks the save rather
than the exit code, so a file it does not care about can no longer stop the game.
`excludeStoreScreenshots` is on now on both machines, proven by a real backup into
a throwaway directory on each.

2026-09-13: hub and spoke went live. One shared Syncthing folder on a server,
`gamesaves`, holding one subdirectory per device, replaced the two one-way
device-to-device folders. The wait now targets the always-on hub rather than
whichever console happens to be awake, and "synced" on exit means the server has
it. The old `ludusavi-deck` and `ludusavi-windows` folders were removed from all
three nodes after verifying every file was duplicated. Staggered versioning at
365 days now runs on the hub copy only.

The same release removed every product name inferred from the operating system,
and settled on **device** as the one word for a machine that holds saves.

2026-09-11, later: ludusavi wrap is out of the launch path. savepick starts the
game itself, so nothing between the save decision and the game can refuse to run
it. Every ludusavi call closes stdin, which on its own was a 120 second hang
before every launch on the Windows PC. Proven on both machines: no SteamAppId,
an unknown SteamAppId, an unconfirmed sync and a full end to end run all start
the game.
