#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
install -d -o root -g root -m 0755 /opt/cc-xia-claude-channel
install -d -o cc-xia-relay -g cc-xia-relay -m 0700 \
  /var/lib/cc-xia-relay/channel-state /var/lib/cc-xia-relay/claude-channel-home
install -d -o cc-xia-relay -g cc-xia-relay -m 0700 /var/lib/cc-xia-relay/channel-state/tmux
install -d -o cc-xia-relay -g cc-xia-relay -m 0700 /var/lib/cc-xia-relay/claude-channel-home/.claude
install -d -o root -g root -m 0755 /var/lib/cc-xia-relay/workspace
if [[ ! -s /var/lib/cc-xia-relay/channel-state/channel.token ]]; then
  /usr/bin/openssl rand -hex 32 > /var/lib/cc-xia-relay/channel-state/channel.token
fi
chown cc-xia-relay:cc-xia-relay /var/lib/cc-xia-relay/channel-state/channel.token
chmod 0600 /var/lib/cc-xia-relay/channel-state/channel.token
echo "Runtime prepared. Copy an isolated Claude credential snapshot manually; no root/Xiaoke config is imported by this script."
