# BlockSlot for Decky

Save sync for the games Steam Cloud does not cover. A [Decky](https://decky.xyz)
plugin for the Steam Deck and anything else that runs SteamOS or Bazzite.

Open the Quick Access menu, see which of your games Steam Cloud misses, and
turn syncing on for the one you are about to play. The save then follows you:
play on the Deck, stop, and pick up on your PC.

This is the Deck half of [BlockSlot](https://github.com/datbird/blockslot).

## What it needs

**A BlockSlot server.** Saves go to the `blockslot-server` container on a NAS
or any machine that is always on. It is a small S3 store with a web page for
your games, devices and settings. There is no account with anyone: the server
is yours. See [the server](https://github.com/datbird/blockslot/tree/main/server).

**ludusavi.** It knows where each game keeps its saves and does every copy.
Decky downloads the official ludusavi v0.31.0 release when the plugin installs,
and checks its hash. BlockSlot never replaces a ludusavi that is already there.

**One limit, today.** ludusavi v0.31.0 cannot match a Windows save path to the
same game's path inside a Proton prefix. The fix is in ludusavi's unreleased
changes. Until it ships, a save moves between SteamOS devices with the stock
install. Moving one between SteamOS and Windows needs a ludusavi build that
has the fix.

## Install

The plugin is not in the Decky store yet. Until it is, install the zip.

### From a release (easiest)

1. In Decky, open Settings, turn on **Developer mode**, then open
   **Developer**.
2. Choose **Install Plugin from URL** and give it the `...-decky.zip` link
   from the newest release on
   [BlockSlot's releases page](https://github.com/datbird/blockslot/releases/latest).
   Or download the zip and use **Install Plugin from ZIP file**.

### Install from source

On any PC with Node 20, pnpm 9 and Python 3:

```
git clone https://github.com/datbird/blockslot-decky
cd blockslot-decky
pnpm install
pnpm build
python3 scripts/package.py
```

That writes `out/Blockslot.zip`. Copy it to the Deck, then in Decky choose
Settings, Developer, **Install Plugin from ZIP file**.

## Setting it up

1. On the server's web page, open **Devices** and add this Deck. The page
   shows the server's address and a pairing code, like `K7QX-4MPA`. The code
   works once, for 15 minutes.
2. On the Deck, open BlockSlot from the Quick Access menu, choose
   **Open BlockSlot**, then **Server**. Enter the address and the code, and
   choose **Pair with the server**.
3. **Settings:** install the engine, and ludusavi if it is missing.
4. **Games:** turn a game on. It syncs from its next launch.

An emulator (RetroArch, RetroDECK, shadPS4) has no Steam app id that says
which saves are its own. Add it on the server's Settings page. Then, on
**Emulator games**, point it at its save folder on this Deck.

## How it works

Turning a game on sets its Steam launch option, so Steam starts BlockSlot's
engine in place of the game. Before the game opens, the engine asks the store
for the newest save of that game.

- Another device saved it last: the engine restores that save first.
- Two devices both played from the same save: it asks which one to keep, and
  shows both dates.

When you quit, the engine backs the save up and uploads it. With no network,
the save waits in a queue and goes up later. A save is never restored over a
newer one without asking.

The plugin never edits Steam's files. It asks Steam to set the launch option,
through `SteamClient.Apps.SetAppLaunchOptions`, and Steam writes it.

## What it touches

| Path | Why |
|---|---|
| `~/.config/savepick.json` | the settings. The engine reads this same file at launch, so there is one copy |
| `~/.local/bin/savepick.py` | the engine, which Steam starts. It must outlive the plugin's own folder |
| `~/.local/bin/ludusavi` | only when it was missing |
| `~/.local/state/blockslot/` | the upload queue and the engine's log |

The plugin runs without root.

## Building

`main.py` is the backend. `py_modules/blockslot_core` holds every rule about
which save wins, and the Windows app runs that same code. `defaults/` holds
the engine, the game index and the third-party notices; its contents land in
the plugin's root when it is packaged.

This repository is generated from BlockSlot's main source tree, so a change
made here by hand is overwritten by the next release. Open an issue or a pull
request on [BlockSlot](https://github.com/datbird/blockslot) instead.

## License and credit

MIT. See [LICENSE](LICENSE).

BlockSlot drives [ludusavi](https://github.com/mtkennerly/ludusavi) for every
backup and restore, and its game index is derived from
[ludusavi-manifest](https://github.com/mtkennerly/ludusavi-manifest), which is
compiled from [PCGamingWiki](https://www.pcgamingwiki.com). The full notices
ship with the plugin as `THIRD-PARTY-NOTICES.md`.
