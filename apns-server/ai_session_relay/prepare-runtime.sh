#!/usr/bin/env bash
set -euo pipefail

# Prepare pinned source and private directory layout. It never installs,
# enables, or starts a service.
UPSTREAM_URL="https://github.com/blanchexxxxx/ai-session-relay.git"
UPSTREAM_COMMIT="783e01455e2719962d09839d08a9254441cae478"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-}"
CODEX_SOURCE_BIN="${2:-}"
CLAUDE_CREDENTIAL_SOURCE="${3:-}"
CODEX_AUTH_SOURCE="${4:-}"
SERVICE_USER="${AI_RELAY_SERVICE_USER:-cc-xia-relay}"
BACKEND_WORKSPACE_SOURCE="${AI_RELAY_BACKEND_WORKSPACE_SOURCE:-/root/CcCompanion/apns-server/state/ai_relay_workspace}"
INSTANCE_ROOT="${AI_RELAY_INSTANCE_ROOT:-/var/lib/cc-xia-relay}"

if [[ -z "$DEST" || "$DEST" != /* || -z "$CODEX_SOURCE_BIN" \
      || -z "$CLAUDE_CREDENTIAL_SOURCE" || -z "$CODEX_AUTH_SOURCE" ]]; then
  echo "usage: $0 /absolute/empty/runtime /absolute/codex-ELF /absolute/claude-credentials /absolute/codex-auth" >&2
  exit 2
fi
if [[ -e "$DEST" && -n "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "destination must be empty: $DEST" >&2
  exit 2
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "dedicated service user does not exist: $SERVICE_USER" >&2
  exit 2
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

validate_private_source() {
  local path="$1" label="$2" mode owner
  [[ "$path" == /* && -f "$path" && ! -L "$path" ]] || {
    echo "$label source must be an absolute regular non-symlink file" >&2; return 1;
  }
  owner="$(stat -c %u "$path")"
  mode="$(stat -c %a "$path")"
  [[ "$owner" == 0 && "$mode" == 600 ]] || {
    echo "$label source must be root-owned mode 0600" >&2; return 1;
  }
}

[[ "$(basename "$CLAUDE_CREDENTIAL_SOURCE")" == ".credentials.json" ]] || {
  echo "Claude credential source basename must be .credentials.json" >&2; exit 2;
}
[[ "$(basename "$CODEX_AUTH_SOURCE")" == "auth.json" ]] || {
  echo "Codex auth source basename must be auth.json" >&2; exit 2;
}
validate_private_source "$CLAUDE_CREDENTIAL_SOURCE" "Claude credential"
validate_private_source "$CODEX_AUTH_SOURCE" "Codex auth"
python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert isinstance(v,dict) and v' \
  "$CLAUDE_CREDENTIAL_SOURCE" >/dev/null 2>&1 || {
  echo "Claude credential source is not a non-empty JSON object" >&2; exit 2;
}
python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert isinstance(v,dict) and v' \
  "$CODEX_AUTH_SOURCE" >/dev/null 2>&1 || {
  echo "Codex auth source is not a non-empty JSON object" >&2; exit 2;
}
[[ "$CODEX_SOURCE_BIN" == /* && -f "$CODEX_SOURCE_BIN" && ! -L "$CODEX_SOURCE_BIN" ]] || {
  echo "Codex source must be an absolute regular non-symlink file" >&2; exit 2;
}
CODEX_SOURCE_RESOLVED="$(readlink -f "$CODEX_SOURCE_BIN")"
CODEX_SOURCE_UID="$(stat -c %u "$CODEX_SOURCE_BIN")"
[[ -x "$CODEX_SOURCE_BIN" ]] || {
  echo "Codex source must be executable" >&2; exit 2;
}
if [[ "$CODEX_SOURCE_UID" != 0 ]]; then
  [[ "$CODEX_SOURCE_RESOLVED" == /root/.codex/packages/standalone/releases/*/bin/codex \
        && "$(stat -c %u /root)" == 0 && "$(stat -c %a /root)" == 700 ]] || {
    echo "non-root-owned Codex source is allowed only below root-confined standalone releases" >&2
    exit 2
  }
fi
[[ "$(od -An -tx1 -N4 "$CODEX_SOURCE_BIN" | tr -d ' \n')" == "7f454c46" ]] || {
  echo "Codex source is not an ELF executable" >&2; exit 2;
}
file -Lb "$CODEX_SOURCE_BIN" | grep -q "static-pie linked" || {
  echo "Codex source must be a standalone static-pie executable" >&2; exit 2;
}

find_service_cli() {
  local name="$1" candidate resolved owner
  for candidate in "/usr/local/bin/$name" "/usr/bin/$name" "/bin/$name"; do
    [[ -e "$candidate" ]] || continue
    resolved="$(readlink -f "$candidate")"
    [[ "$resolved" != /root/* ]] || continue
    owner="$(stat -c %u "$resolved")"
    [[ "$owner" == 0 ]] || continue
    if runuser -u "$SERVICE_USER" -- test -x "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "no root-owned globally accessible $name CLI for $SERVICE_USER" >&2
  return 1
}

CLAUDE_BIN="$(find_service_cli claude)"

install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 \
  "$BACKEND_WORKSPACE_SOURCE" \
  "$INSTANCE_ROOT" \
  "$INSTANCE_ROOT/workspace" \
  "$INSTANCE_ROOT/state" \
  "$INSTANCE_ROOT/state/claude-home" \
  "$INSTANCE_ROOT/state/codex-home" \
  "$INSTANCE_ROOT/state/runtime-home"
for persona in CLAUDE.md AGENTS.md; do
  if [[ ! -e "$BACKEND_WORKSPACE_SOURCE/$persona" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 /dev/null \
      "$BACKEND_WORKSPACE_SOURCE/$persona"
  else
    chown "$SERVICE_USER:$SERVICE_GROUP" "$BACKEND_WORKSPACE_SOURCE/$persona"
    chmod 0600 "$BACKEND_WORKSPACE_SOURCE/$persona"
  fi
done
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
  "$HERE/empty-mcp.json" "$INSTANCE_ROOT/state/empty-mcp.json"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
  "$CLAUDE_CREDENTIAL_SOURCE" "$INSTANCE_ROOT/state/claude-home/.credentials.json"
install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
  "$CODEX_AUTH_SOURCE" "$INSTANCE_ROOT/state/codex-home/auth.json"

mkdir -p "$DEST"
chown root:root "$DEST"
chmod 0755 "$DEST"
install -d -o root -g root -m 0755 "$DEST/bin"
install -o root -g root -m 0755 "$CODEX_SOURCE_BIN" "$DEST/bin/codex"
cmp -s "$CODEX_SOURCE_BIN" "$DEST/bin/codex"
install -o root -g root -m 0755 "$HERE/smoke-codex-app-server.py" \
  "$DEST/bin/smoke-codex-app-server.py"
git clone --no-checkout "$UPSTREAM_URL" "$DEST/source"
git -C "$DEST/source" checkout --detach "$UPSTREAM_COMMIT"
test "$(git -C "$DEST/source" rev-parse HEAD)" = "$UPSTREAM_COMMIT"
git -C "$DEST/source" apply --check "$HERE/upstream-chat-only.patch"
git -C "$DEST/source" apply "$HERE/upstream-chat-only.patch"
find "$DEST/source" -type d -exec chmod 0755 {} +
find "$DEST/source" -type f -exec chmod 0644 {} +
chmod 0755 "$DEST/source/run.sh" "$DEST/source/install.sh"
install -o root -g root -m 0644 "$HERE/security_policy.py" "$DEST/source/security_policy.py"

python3 -m venv "$DEST/source/.venv"
"$DEST/source/.venv/bin/python" -m pip install --upgrade pip
"$DEST/source/.venv/bin/python" -m pip install -r "$DEST/source/requirements.txt"
chown -R root:root "$DEST/source"
find "$DEST/source" -type d -exec chmod a+rx {} +
find "$DEST/source" -type f -exec chmod a+r {} +
find "$DEST/source/.venv/bin" -type f -exec chmod a+rx {} +

runuser -u "$SERVICE_USER" -- env \
  HOME="$INSTANCE_ROOT/state/runtime-home" \
  CLAUDE_CONFIG_DIR="$INSTANCE_ROOT/state/claude-home" \
  "$CLAUDE_BIN" --version >/dev/null
runuser -u "$SERVICE_USER" -- env \
  HOME="$INSTANCE_ROOT/state/runtime-home" \
  CLAUDE_CONFIG_DIR="$INSTANCE_ROOT/state/claude-home" \
  "$CLAUDE_BIN" auth status >/dev/null 2>&1
runuser -u "$SERVICE_USER" -- env \
  HOME="$INSTANCE_ROOT/state/runtime-home" \
  CODEX_HOME="$INSTANCE_ROOT/state/codex-home" \
  "$DEST/bin/codex" --version >/dev/null
runuser -u "$SERVICE_USER" -- env \
  HOME="$INSTANCE_ROOT/state/runtime-home" \
  CODEX_HOME="$INSTANCE_ROOT/state/codex-home" \
  "$DEST/bin/codex" login status >/dev/null 2>&1
runuser -u "$SERVICE_USER" -- env \
  PATH="$DEST/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME="$INSTANCE_ROOT/state/runtime-home" \
  CODEX_HOME="$INSTANCE_ROOT/state/codex-home" \
  /usr/bin/python3 "$DEST/bin/smoke-codex-app-server.py" \
    "$DEST/source/security_policy.py" "$DEST/bin/codex"

echo "Prepared pinned restricted source at $DEST/source"
echo "Prepared private relay layout for $SERVICE_USER under $INSTANCE_ROOT"
echo "Verified isolated Claude auth and provisioned Codex app-server access"
echo "No service was installed or started. Follow README.md for isolated auth and verification."
