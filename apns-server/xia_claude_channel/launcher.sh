#!/usr/bin/env bash
set -euo pipefail

# Dedicated Xia-only Claude TUI supervisor. It never reads root's Claude HOME,
# Xiaoke's tmux socket, or CcCompanion's global configuration.
: "${XIA_CHANNEL_HOME:=/var/lib/cc-xia-relay/claude-channel-home}"
: "${XIA_CHANNEL_STATE_DIR:=/var/lib/cc-xia-relay/channel-state}"
: "${XIA_CHANNEL_WORKSPACE:=/var/lib/cc-xia-relay/workspace}"
: "${XIA_CHANNEL_INSTALL_DIR:=/opt/cc-xia-claude-channel}"
: "${XIA_CHANNEL_CLAUDE_BIN:=/usr/bin/claude}"
: "${XIA_CHANNEL_TMUX_SOCKET:=cc-xia-claude}"
: "${XIA_CHANNEL_TMUX_SESSION:=xia-claude}"
: "${XIA_CHANNEL_TMUX_TMPDIR:=$XIA_CHANNEL_STATE_DIR/tmux}"
: "${XIA_CHANNEL_START_TIMEOUT:=30}"

umask 077
test "$(id -un)" = "cc-xia-relay" || { echo "must run as cc-xia-relay" >&2; exit 70; }
for path in "$XIA_CHANNEL_HOME" "$XIA_CHANNEL_STATE_DIR" "$XIA_CHANNEL_TMUX_TMPDIR"; do
  mkdir -p "$path"
  chmod 700 "$path"
done
test -r "$XIA_CHANNEL_STATE_DIR/channel.token" || { echo "missing private channel token" >&2; exit 78; }
test -r "$XIA_CHANNEL_HOME/.claude/.credentials.json" || { echo "missing isolated Claude credential snapshot" >&2; exit 78; }
test -r "$XIA_CHANNEL_HOME/.claude/.claude.json" || { echo "missing isolated Claude onboarding state" >&2; exit 78; }
test -r "$XIA_CHANNEL_WORKSPACE/CLAUDE.md" || { echo "missing read-only Xia persona workspace" >&2; exit 78; }
test -x "$XIA_CHANNEL_CLAUDE_BIN" || {
  echo "Claude CLI is not executable by cc-xia-relay; fix trusted CLI path traversal permissions before starting" >&2
  exit 78
}
tmux_uid="$(id -u)"
tmux_socket_path="$XIA_CHANNEL_TMUX_TMPDIR/tmux-$tmux_uid/$XIA_CHANNEL_TMUX_SOCKET"
test -d "$XIA_CHANNEL_TMUX_TMPDIR/tmux-$tmux_uid" || { echo "missing private tmux socket directory" >&2; exit 78; }

health_matches() {
  /usr/bin/curl -fsS --max-time 1 -H "X-Auth-Token: $(<"$XIA_CHANNEL_STATE_DIR/channel.token")" \
    http://127.0.0.1:8821/health | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); g=int(sys.argv[1]); s=sys.argv[2]; m=sys.argv[3]; raise SystemExit(0 if d.get("ready") and int(d.get("generation",-1))==g and d.get("session_id")==s and str(d.get("model") or "")==m else 1)' "$generation" "$session_id" "$model" 2>/dev/null
}

while :; do
  # Runtime publication is gated on authoritative tmux queries, kill success,
  # captured /proc identities, and an unreachable old MCP port. The helper
  # reads control only after that gate, publishes it, rechecks it, and returns
  # the one snapshot used by this Claude command.
  snapshot="$(/usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" prepare-after-stop \
    "$tmux_socket_path" "$XIA_CHANNEL_TMUX_SESSION" "$XIA_CHANNEL_INSTALL_DIR" \
    "$XIA_CHANNEL_STATE_DIR" "$XIA_CHANNEL_WORKSPACE")"
  readarray -t values < <(/usr/bin/python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d["runtime"]); print(d["generation"]); print(d["session_id"]); print(d["model"]); print(d["bootstrap_token"])' \
    <<<"$snapshot")
  runtime=${values[0]}
  generation=${values[1]}
  session_id=${values[2]}
  model=${values[3]}
  bootstrap_token=${values[4]}
  [[ "$runtime" == "$XIA_CHANNEL_STATE_DIR/runtime" ]] || { echo "unexpected runtime path" >&2; exit 75; }

  resume=0
  [[ "$(/usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" mode "$XIA_CHANNEL_HOME" "$session_id")" == "resume" ]] && resume=1
  cmd=("$XIA_CHANNEL_CLAUDE_BIN" --setting-sources project,local --strict-mcp-config
    --mcp-config "$runtime/.mcp.json" --disable-slash-commands --tools ""
    --dangerously-load-development-channels server:xia-companion)
  if (( resume )); then
    cmd+=(--resume "$session_id")
  else
    cmd+=(--session-id "$session_id")
    [[ -n "$model" ]] && cmd+=(--model "$model")
  fi
  printf -v quoted_cmd '%q ' "${cmd[@]}"
  # No automated pane keystrokes: the one-time stable-workspace trust remains
  # an operator preflight, never a blind Enter into arbitrary TUI text.
  HOME="$XIA_CHANNEL_HOME" CLAUDE_CONFIG_DIR="$XIA_CHANNEL_HOME/.claude" \
    XDG_CONFIG_HOME="$XIA_CHANNEL_HOME/.config" \
    SHELL=/bin/bash \
    TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" \
    /usr/bin/tmux -S "$tmux_socket_path" new-session -d -s "$XIA_CHANNEL_TMUX_SESSION" -c "$runtime" "$quoted_cmd"

  deadline=$((SECONDS + XIA_CHANNEL_START_TIMEOUT))
  ready=0
  while (( SECONDS < deadline )); do
    if health_matches; then
      ready=1; break
    fi
    TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" /usr/bin/tmux -S "$tmux_socket_path" has-session -t "$XIA_CHANNEL_TMUX_SESSION" 2>/dev/null || break
    sleep 0.5
  done
  if (( ! ready )); then
    echo "Claude channel did not become ready; confirmation/credential/CLI preflight failed closed" >&2
    /usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" stop-tui \
      "$tmux_socket_path" "$XIA_CHANNEL_TMUX_SESSION" || true
    exit 75
  fi
  /usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" write-marker \
    "$XIA_CHANNEL_STATE_DIR/current-session.json" "$generation" "$session_id" "$model"
  health_failures=0
  while TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" /usr/bin/tmux -S "$tmux_socket_path" has-session -t "$XIA_CHANNEL_TMUX_SESSION" 2>/dev/null; do
    sleep 1
    if health_matches; then
      health_failures=0
    else
      health_failures=$((health_failures + 1))
      if (( health_failures >= 3 )); then
        echo "Claude channel health disappeared; terminating the dedicated TUI for a clean restart" >&2
        /usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" stop-tui \
          "$tmux_socket_path" "$XIA_CHANNEL_TMUX_SESSION" || exit 75
        break
      fi
    fi
  done
  sleep 1
done
