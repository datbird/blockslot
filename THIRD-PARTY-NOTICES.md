# Third party notices

Blockslot uses the following third party work. Their licenses are reproduced in
full below, as those licenses require.

## ludusavi and ludusavi-manifest

Blockslot invokes `ludusavi` to perform backups and restores, and derives its
game index from `ludusavi-manifest`.

- https://github.com/mtkennerly/ludusavi
- https://github.com/mtkennerly/ludusavi-manifest

Blockslot does not contain or redistribute the ludusavi program. The Decky
plugin names the official v0.31.0 release as a download, and each device
fetches it from ludusavi's own release page. The licenses of everything
compiled into that program are published beside it, as
`ludusavi-v0.31.0-legal.zip`:

- https://github.com/mtkennerly/ludusavi/releases/tag/v0.31.0

Both are licensed as follows.

```
MIT License

Copyright (c) 2020 Matthew T. Kennerly (mtkennerly)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Blockslot's game index is derived from `data/manifest.yaml` in
`ludusavi-manifest`. It is a subset, reshaped for size and load speed. The
original data is unmodified in substance.

## PCGamingWiki

`ludusavi-manifest` is compiled from data on
[PCGamingWiki](https://www.pcgamingwiki.com), whose content is licensed
[CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0).

Blockslot takes that data through `ludusavi-manifest` under its MIT license and
does not claim rights over the underlying facts. This credit is not required by
MIT. It is here because the save file locations in that wiki were found and
recorded by people, one game at a time, and that work deserves naming.

## Garage

The BlockSlot server image (`blockslot-server`) includes
[Garage](https://garagehq.deuxfleurs.fr), the S3 store the saves live in,
copied unmodified from the official `dxflrs/garage` image. Garage is
Copyright Deuxfleurs and its contributors, licensed under the
[GNU Affero General Public License v3.0](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/LICENSE).
Its source code, for the exact version in the image (named by
`GARAGE_VERSION` in `server/Dockerfile`), is at
<https://git.deuxfleurs.fr/Deuxfleurs/garage>. BlockSlot runs Garage as a
separate program and talks to it only over its S3 and admin HTTP APIs;
BlockSlot's own code stays under the MIT license.

## Syncthing

Older BlockSlot setups moved files with [Syncthing](https://syncthing.net/),
and the Syncthing screens remain for them. BlockSlot does not bundle or modify
it. Syncthing is licensed
[MPL-2.0](https://github.com/syncthing/syncthing/blob/main/LICENSE).
