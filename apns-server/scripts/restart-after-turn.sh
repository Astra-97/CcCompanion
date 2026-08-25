#!/usr/bin/env bash
# restart-after-turn.sh <kimi-web|cc-companion>
#
# Why this exists: restarting the Kimi web process (or cc-companion.service)
# mid-reply truncates the reply streaming through it. The old habit was a
# blind fixed `sleep 150`; this watcher instead polls the live session until
# the turn is actually finished, waits a short grace for the history append,
# then restarts — typically seconds after the reply lands instead of 2 min.
#
# Launch detached:  nohup restart-after-turn.sh kimi-web >/dev/null 2>&1 &
set -u

TARGET="${1:-}"
GRACE_SECONDS=10
POLL_INTERVAL=2
POLL_TIMEOUT=900   # give up waiting for idle after 15 min, restart anyway
LOG=/root/CcCompanion/apns-server/kimi_web_restart.log

case "$TARGET" in
  kimi-web|cc-companion) ;;
  *) echo "usage: $0 <kimi-web|cc-companion>" >&2; exit 2 ;;
esac

wait_for_turn_end() {
  python3 - "$POLL_INTERVAL" "$POLL_TIMEOUT" <<'PY'
import sys, time
sys.path.insert(0, "/root/CcCompanion/apns-server")
from pathlib import Path
from kimi_web_client import KimiWebClient, KimiWebError

interval, timeout = float(sys.argv[1]), float(sys.argv[2])
client = KimiWebClient(
    state_path=Path("/root/CcCompanion/apns-server/tokens/kimi_web_session.json")
)
sid = None
try:
    import json
    sid = json.loads(client.state_path.read_text()).get("session_id")
except Exception:
    pass
deadline = time.time() + timeout
while time.time() < deadline:
    try:
        if sid:
            status = client.get_session_status(sid)
            if not status.get("busy"):
                sys.exit(0)
        else:
            sys.exit(0)
    except KimiWebError:
        pass  # server down/restarting — treat as keep waiting
    except Exception:
        pass
    time.sleep(interval)
sys.exit(1)  # timed out; caller restarts anyway
PY
}

wait_for_turn_end
sleep "$GRACE_SECONDS"

if [ "$TARGET" = "kimi-web" ]; then
  pid="$(ss -tlnp 2>/dev/null | grep ':58627' | grep -oP 'pid=\K[0-9]+' | head -1)"
  if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q kimi; then
    kill "$pid"
    echo "$(date -Is) kimi web (pid $pid) restarted after turn end (+${GRACE_SECONDS}s grace) [$TARGET]" >> "$LOG"
  else
    echo "$(date -Is) kimi web pid not found on :58627; nothing to restart" >> "$LOG"
  fi
else
  systemctl restart cc-companion.service
  echo "$(date -Is) cc-companion.service restarted after turn end (+${GRACE_SECONDS}s grace)" >> "$LOG"
fi
