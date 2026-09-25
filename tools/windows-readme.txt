BlockSlot for Windows
=====================

BlockSlot keeps game saves in step between your PCs and your Steam Deck.
It uploads a game's save when the game closes, and restores the newest
save before the game starts.

You need a BlockSlot server first: the blockslot-server container, on a
NAS or any machine that is always on. See
https://github.com/datbird/blockslot

Start
-----

1. Put BlockSlot.exe in a folder of its own, for example
   C:\Program Files\BlockSlot or a folder in your user profile.
   Keep it there: Steam launch options and the service point at this path.
2. Run BlockSlot.exe.
3. On the server's web page, open Devices and add this PC. It shows an
   address and a pairing code.
4. In BlockSlot, open Store, enter the address and the code, and press
   "Pair with the server".
5. Open Games and turn sync on for the games you play.

Windows SmartScreen may warn that the app is unrecognised, because the
exe is not code-signed. Choose "More info", then "Run anyway".

Uninstall: in BlockSlot, turn sync off for your games, then delete the
folder. Settings are in %APPDATA%\savepick.json.

License: MIT, see LICENSE.txt. Third-party notices are in
THIRD-PARTY-NOTICES.md.
