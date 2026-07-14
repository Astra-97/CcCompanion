#!/usr/bin/env bash
set -euo pipefail

# Refresh only the two isolated auth snapshots. The relay must be stopped so
# neither CLI can race the atomic replacements or rewrite a stale refresh token.
CLAUDE_SOURCE="${1:-}"
CODEX_SOURCE="${2:-}"
SERVICE_USER="${AI_RELAY_SERVICE_USER:-cc-xia-relay}"
INSTANCE_ROOT="${AI_RELAY_INSTANCE_ROOT:-/var/lib/cc-xia-relay}"
UNIT="${AI_RELAY_SYSTEMD_UNIT:-cc-xia-ai-session-relay.service}"

if [[ -z "$CLAUDE_SOURCE" || -z "$CODEX_SOURCE" ]]; then
  echo "usage: $0 /absolute/.credentials.json /absolute/auth.json" >&2
  exit 2
fi
if systemctl is-active --quiet "$UNIT"; then
  echo "stop the isolated relay before refreshing credentials" >&2
  exit 2
fi
id "$SERVICE_USER" >/dev/null 2>&1 || {
  echo "dedicated service user does not exist" >&2; exit 2;
}
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

validate() {
  local path="$1" basename="$2" label="$3"
  [[ "$path" == /* && "$(basename "$path")" == "$basename" \
      && -f "$path" && ! -L "$path" \
      && "$(stat -c %u "$path")" == 0 && "$(stat -c %a "$path")" == 600 ]] || {
    echo "$label source contract failed" >&2; return 1;
  }
  python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert isinstance(v,dict) and v' \
    "$path" >/dev/null 2>&1 || {
    echo "$label source is not a non-empty JSON object" >&2; return 1;
  }
}

validate "$CLAUDE_SOURCE" ".credentials.json" "Claude credential"
validate "$CODEX_SOURCE" "auth.json" "Codex auth"

install_one() {
  local source="$1" destination="$2" directory temp
  directory="$(dirname "$destination")"
  [[ -d "$directory" && ! -L "$directory" ]] || {
    echo "isolated credential directory is missing" >&2; return 1;
  }
  temp="$directory/.credential-refresh.$$"
  trap 'rm -f -- "$temp"' RETURN
  install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 "$source" "$temp"
  mv -fT -- "$temp" "$destination"
  sync -f "$directory"
  trap - RETURN
}

install_one "$CLAUDE_SOURCE" "$INSTANCE_ROOT/state/claude-home/.credentials.json"
install_one "$CODEX_SOURCE" "$INSTANCE_ROOT/state/codex-home/auth.json"
echo "Refreshed two isolated credential snapshots; the relay remains stopped."
