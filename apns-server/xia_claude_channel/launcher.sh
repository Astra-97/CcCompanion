#!/usr/bin/env bash
set -euo pipefail

# Dedicated Xia-only Claude TUI supervisor. It never reads root's Claude HOME,
# Xiaoke's tmux socket, or CcCompanion's global configuration.
: "${XIA_CHANNEL_HOME:=/var/lib/cc-xia-relay/claude-channel-home}"
: "${XIA_CHANNEL_STATE_DIR:=/var/lib/cc-xia-relay/channel-state}"
: "${XIA_CHANNEL_WORKSPACE:=/var/lib/cc-xia-relay/workspace}"
: "${XIA_CHANNEL_INSTALL_DIR:=/opt/cc-xia-claude-channel}"
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
test -r "$XIA_CHANNEL_WORKSPACE/CLAUDE.md" || { echo "missing read-only Xia persona workspace" >&2; exit 78; }

while :; do
  readarray -t values < <(/usr/bin/python3 - "$XIA_CHANNEL_STATE_DIR/control.json" <<'PY'
import json, os, pathlib, sys, uuid
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text()) if p.exists() else {}
generation = int(d.get("generation") or 1)
session_id = str(d.get("session_id") or uuid.uuid4())
model = str(d.get("model") or "")
if not p.exists() or not d.get("session_id"):
    d = {"version": 1, "generation": generation, "session_id": session_id, "model": model,
         "requires_fresh": False, "draining": False, "bootstrap_token": ""}
    tmp = p.with_suffix(".tmp")
    with tmp.open("w") as f:
        f.write(json.dumps(d) + "\n"); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp, 0o600); os.replace(tmp, p)
    fd = os.open(p.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(fd)
    finally: os.close(fd)
requires_fresh = bool(d.get("requires_fresh", False)); draining = bool(d.get("draining", False))
bootstrap_token = str(d.get("bootstrap_token") or "")
print(generation); print(session_id); print(model); print(bootstrap_token)
PY
  )
  generation=${values[0]}
  session_id=${values[1]}
  model=${values[2]}
  bootstrap_token=${values[3]}
  runtime="$XIA_CHANNEL_STATE_DIR/runtime-$generation"
  mkdir -p "$runtime"
  chmod 700 "$runtime"
  sed \
    -e "s|@INSTALL_DIR@|$XIA_CHANNEL_INSTALL_DIR|g" \
    -e "s|@STATE_DIR@|$XIA_CHANNEL_STATE_DIR|g" \
    -e "s|@TOKEN_FILE@|$XIA_CHANNEL_STATE_DIR/channel.token|g" \
    -e "s|@GENERATION@|$generation|g" \
    -e "s|@SESSION_ID@|$session_id|g" \
    -e "s|@MODEL@|$model|g" \
    -e "s|@BOOTSTRAP_TOKEN@|$bootstrap_token|g" \
    "$XIA_CHANNEL_INSTALL_DIR/.mcp.json.in" > "$runtime/.mcp.json"
  chmod 600 "$runtime/.mcp.json"
  mkdir -p "$runtime/.claude"
  install -m 600 "$XIA_CHANNEL_INSTALL_DIR/settings.json" "$runtime/.claude/settings.json"
  ln -sfn "$XIA_CHANNEL_WORKSPACE/CLAUDE.md" "$runtime/CLAUDE.md"

  resume=0
  [[ "$(/usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" mode "$XIA_CHANNEL_HOME" "$session_id")" == "resume" ]] && resume=1
  cmd=(/usr/bin/claude --setting-sources project,local --strict-mcp-config
    --mcp-config "$runtime/.mcp.json" --disable-slash-commands --tools ""
    --dangerously-load-development-channels server:xia-companion)
  if (( resume )); then
    cmd+=(--resume "$session_id")
  else
    cmd+=(--session-id "$session_id")
    [[ -n "$model" ]] && cmd+=(--model "$model")
  fi
  printf -v quoted_cmd '%q ' "${cmd[@]}"
  # No automated pane keystrokes: a changed Claude confirmation screen is a
  # fail-closed readiness timeout, never a blind Enter into arbitrary TUI text.
  TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" /usr/bin/tmux -L "$XIA_CHANNEL_TMUX_SOCKET" kill-session -t "$XIA_CHANNEL_TMUX_SESSION" 2>/dev/null || true
  HOME="$XIA_CHANNEL_HOME" XDG_CONFIG_HOME="$XIA_CHANNEL_HOME/.config" \
    TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" \
    /usr/bin/tmux -L "$XIA_CHANNEL_TMUX_SOCKET" new-session -d -s "$XIA_CHANNEL_TMUX_SESSION" -c "$runtime" "$quoted_cmd"

  deadline=$((SECONDS + XIA_CHANNEL_START_TIMEOUT))
  ready=0
  while (( SECONDS < deadline )); do
    if /usr/bin/curl -fsS --max-time 1 -H "X-Auth-Token: $(<"$XIA_CHANNEL_STATE_DIR/channel.token")" \
      http://127.0.0.1:8821/health | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); g=int(sys.argv[1]); s=sys.argv[2]; m=sys.argv[3]; raise SystemExit(0 if d.get("ready") and int(d.get("generation",-1))==g and d.get("session_id")==s and str(d.get("model") or "")==m else 1)' "$generation" "$session_id" "$model" 2>/dev/null; then
      ready=1; break
    fi
    TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" /usr/bin/tmux -L "$XIA_CHANNEL_TMUX_SOCKET" has-session -t "$XIA_CHANNEL_TMUX_SESSION" 2>/dev/null || break
    sleep 0.5
  done
  if (( ! ready )); then
    echo "Claude channel did not become ready; confirmation/credential/CLI preflight failed closed" >&2
    TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" /usr/bin/tmux -L "$XIA_CHANNEL_TMUX_SOCKET" kill-session -t "$XIA_CHANNEL_TMUX_SESSION" 2>/dev/null || true
    exit 75
  fi
  /usr/bin/python3 "$XIA_CHANNEL_INSTALL_DIR/runtime_state.py" write-marker \
    "$XIA_CHANNEL_STATE_DIR/current-session.json" "$generation" "$session_id" "$model"
  while TMUX_TMPDIR="$XIA_CHANNEL_TMUX_TMPDIR" /usr/bin/tmux -L "$XIA_CHANNEL_TMUX_SOCKET" has-session -t "$XIA_CHANNEL_TMUX_SESSION" 2>/dev/null; do sleep 1; done
  sleep 1
done
