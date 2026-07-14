#!/usr/bin/env bash
set -euo pipefail
# Read-only operator view. It intentionally offers no attach/send-keys path.
export TMUX_TMPDIR="${XIA_CHANNEL_TMUX_TMPDIR:-/var/lib/cc-xia-relay/channel-state/tmux}"
exec /usr/bin/tmux -L "${XIA_CHANNEL_TMUX_SOCKET:-cc-xia-claude}" \
  capture-pane -p -t "${XIA_CHANNEL_TMUX_SESSION:-xia-claude}" -S -200
