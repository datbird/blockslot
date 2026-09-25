#!/usr/bin/env bash
# Build Blockslot's plugin here and install it on the Deck.
#
#     scripts/deploy-deck.sh [ssh-host]
#
# The Deck needs nothing but ssh. The bundle is built on this machine, because
# SteamOS has no node, and the shared code is staged in first so the plugin
# carries the same core as the desktop window.
#
# The plugin directory has to be owned by root, which is what Decky's loader
# expects, so this asks for the Deck's sudo password once.
set -euo pipefail

HOST="${1:-deck}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="Blockslot"
REMOTE_STAGE="/home/deck/blockslot-plugin"
REMOTE_PLUGIN="/home/deck/homebrew/plugins/${NAME}"

cd "$HERE"

echo "staging the shared code"
python3 scripts/stage.py

echo "building the panel"
pnpm install --silent
pnpm build

echo "shipping to ${HOST}"
# The same shape the store's build gives: what is under defaults/ lands in the
# plugin's root.
tar czf - --exclude node_modules --exclude __pycache__ \
    main.py plugin.json package.json LICENSE README.md dist py_modules \
    -C defaults engine index THIRD-PARTY-NOTICES.md \
  | ssh "$HOST" "rm -rf ${REMOTE_STAGE} && mkdir -p ${REMOTE_STAGE} && tar xzf - -C ${REMOTE_STAGE}"

echo "installing (the Deck will ask for its sudo password)"
ssh -t "$HOST" "sudo bash -c '
  rm -rf ${REMOTE_PLUGIN}
  mkdir -p ${REMOTE_PLUGIN}
  cp -r ${REMOTE_STAGE}/. ${REMOTE_PLUGIN}/
  chown -R root:root ${REMOTE_PLUGIN}
  find ${REMOTE_PLUGIN} -type d -exec chmod 755 {} +
  find ${REMOTE_PLUGIN} -type f -exec chmod 644 {} +
  systemctl restart plugin_loader
'"

echo "waiting for the loader"
sleep 8
ssh "$HOST" "journalctl -u plugin_loader --since '-1 min' --no-pager | grep -i blockslot | tail -5"
