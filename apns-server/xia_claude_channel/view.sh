#!/usr/bin/env bash
set -euo pipefail
# Read-only operator view. It intentionally offers no attach/send-keys path.
tmux_root="${XIA_CHANNEL_TMUX_TMPDIR:-/var/lib/cc-xia-relay/channel-state/tmux}"
tmux_uid="$(id -u cc-xia-relay)"
tmux_socket="${XIA_CHANNEL_TMUX_SOCKET:-cc-xia-claude}"
exec /usr/bin/tmux -S "$tmux_root/tmux-$tmux_uid/$tmux_socket" \
  capture-pane -p -t "${XIA_CHANNEL_TMUX_SESSION:-xia-claude}" -S -200
