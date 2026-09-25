#!/usr/bin/env python3
"""Build Blockslot's game index from the ludusavi manifest.

WHY THIS EXISTS

Blockslot needs four facts per game: the Steam appid, which files are saves,
whether Steam Cloud already covers it, and whether any saves live in the
Windows registry. The ludusavi manifest carries all of that, plus a great deal
Blockslot does not use.

Two measurements drove this script, both taken on a Steam Deck:

    manifest.yaml            17.6 MB     parsing it took 63.9 seconds
    this index, as JSON       ~3   MB     loading it takes 0.06 seconds

Sixty four seconds is impossible in a game launch path. Worse, Python has no
YAML parser in its standard library, and Blockslot's engine is standard library
only so it can be deployed by copying one file. So the parsing happens here, in
CI, once, and every device reads JSON.

ATTRIBUTION

The data comes from ludusavi-manifest, MIT licensed, which is itself compiled
from PCGamingWiki. MIT requires the copyright and permission notice to travel
with any copy or substantial portion, so this script writes them into the
index's own `_meta` block. Do not remove that block. See THIRD-PARTY-NOTICES.md.

USAGE

    python3 tools/build_index.py --out index/games.json

    --manifest PATH   build from a local file instead of fetching
    --check           skip the build when the index already matches upstream

The index carries no generation timestamp on purpose. It would change on every
run, so the file would always look modified and CI would commit a new copy each
week whether or not upstream had moved. `source_commit` identifies the data,
and git records when it was built.
"""

import argparse
import json
import os
import sys
import urllib.request

MANIFEST_URL = ("https://raw.githubusercontent.com/mtkennerly/"
                "ludusavi-manifest/master/data/manifest.yaml")
COMMITS_API = ("https://api.github.com/repos/mtkennerly/ludusavi-manifest"
               "/commits?path=data/manifest.yaml&per_page=1")

# MIT requires both of these in any copy or substantial portion. The index is a
# substantial portion of the manifest, so they ship inside it.
SOURCE_LICENCE = "MIT"
SOURCE_COPYRIGHT = "Copyright (c) 2020 Matthew T. Kennerly (mtkennerly)"

USER_AGENT = "blockslot-index-builder"


def fetch(url, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def upstream_commit():
    """The sha of the last commit that touched the manifest, or None.

    Pinning this is what makes a build reproducible. When a bad entry turns up,
    the sha says exactly which upstream state produced it.
    """
    try:
        data = json.loads(fetch(COMMITS_API, "application/vnd.github+json"))
        return data[0]["sha"] if data else None
    except Exception as exc:
        print("could not read the upstream commit: %s" % exc, file=sys.stderr)
        return None


def current_commit(path):
    """The upstream sha the existing index was built from, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("_meta") or {}).get("source_commit")
    except (OSError, ValueError):
        return None


def save_entries(files):
    """The save-tagged files of one game, with their OS constraints.

    Only `save` counts. Ludusavi also tags `config`, and it lists some files
    with no tags at all. On 2026-09-05 three untagged Steam screenshots made a
    live save read twenty hours newer than it was, and the wrong side nearly
    won a conflict. So an untagged file is not a save.

    The `when` constraint is kept because a path can be platform specific. A
    Windows-only path on a Linux device is not a missing save, it is a path
    that does not apply.
    """
    out = []
    for path, info in (files or {}).items():
        info = info or {}
        if "save" not in (info.get("tags") or []):
            continue
        row = {"p": path}
        oses = sorted({w.get("os") for w in (info.get("when") or [])
                       if isinstance(w, dict) and w.get("os")})
        if oses:
            row["o"] = oses
        out.append(row)
    return out


def cloud_stores(entry):
    """Which stores already sync this game's saves.

    Blockslot exists for the games this does NOT cover, so the picker uses it
    to default the list to games that need it.

    Treat it as a hint, never as proof. It is community data and it has false
    negatives: Dark Souls III is absent here while Steam's own appinfo.vdf
    declares an Auto-Cloud savefiles pattern for it. A device that can read its
    local appinfo.vdf should believe that over this field.
    """
    return sorted(k for k, v in (entry.get("cloud") or {}).items() if v)


def build(manifest):
    index = {}
    for name, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        row = {}
        steam_id = (entry.get("steam") or {}).get("id")
        if steam_id:
            row["s"] = steam_id
        saves = save_entries(entry.get("files"))
        if saves:
            row["f"] = saves
        cloud = cloud_stores(entry)
        if cloud:
            row["c"] = cloud
        if entry.get("registry"):
            # Blockslot does not read or write the registry. ludusavi does.
            # The flag is here so the UI can say so rather than implying a
            # file-only backup covered everything.
            row["r"] = 1
        # A game with neither an appid nor a save path is not addressable.
        if "s" in row or "f" in row:
            index[name] = row
    return index


def meta(commit, games, saves, cloud, registry):
    return {
        "source": "https://github.com/mtkennerly/ludusavi-manifest",
        "source_file": "data/manifest.yaml",
        "source_commit": commit,
        "licence": SOURCE_LICENCE,
        "copyright": SOURCE_COPYRIGHT,
        "notice": "THIRD-PARTY-NOTICES.md",
        "upstream_data_from": "https://www.pcgamingwiki.com",
        "derived": "Subset of the manifest, reshaped. The facts are unmodified.",
        "fields": {
            "s": "Steam application id",
            "f": "save files: p = path with ludusavi tokens, o = operating systems it applies to",
            "c": "stores whose own cloud sync already covers this game",
            "r": "1 when the game also stores saves in the Windows registry",
        },
        "counts": {
            "games": games,
            "with_saves": saves,
            "with_cloud": cloud,
            "with_registry": registry,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="index/games.json")
    ap.add_argument("--manifest", help="build from this local file instead of fetching")
    ap.add_argument("--check", action="store_true",
                    help="skip the build when the index already matches upstream")
    args = ap.parse_args(argv)

    commit = upstream_commit()
    if args.check and commit and commit == current_commit(args.out):
        # Nothing to do is not a failure. CI decides whether to commit by
        # asking git whether the file changed, not by reading an exit code.
        print("index is already at upstream %s, nothing to do" % commit[:7])
        return 0

    if args.manifest:
        raw = open(args.manifest, "rb").read()
    else:
        print("fetching %s" % MANIFEST_URL)
        raw = fetch(MANIFEST_URL)
    print("manifest: %.1f MB" % (len(raw) / 1e6))

    # PyYAML is a build-time dependency only. Nothing Blockslot ships to a
    # device is allowed to need it.
    import yaml
    manifest = yaml.safe_load(raw)
    print("parsed %d games" % len(manifest))

    index = build(manifest)
    with_saves = sum(1 for r in index.values() if "f" in r)
    with_cloud = sum(1 for r in index.values() if "c" in r)
    with_registry = sum(1 for r in index.values() if "r" in r)

    out = {"_meta": meta(commit, len(index), with_saves, with_cloud, with_registry)}
    for name in sorted(index):          # sorted so the diff is readable
        out[name] = index[name]

    directory = os.path.dirname(args.out)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False, sort_keys=False)
        f.write("\n")

    print("wrote %s  %.1f MB" % (args.out, os.path.getsize(args.out) / 1e6))
    print("  games        %d" % len(index))
    print("  with saves   %d" % with_saves)
    print("  with cloud   %d" % with_cloud)
    print("  with registry %d" % with_registry)
    print("  upstream     %s" % (commit or "unknown"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
