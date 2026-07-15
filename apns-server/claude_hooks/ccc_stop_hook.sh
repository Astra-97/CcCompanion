#!/bin/bash
# CcCompanion Claude Code Stop hook
#
# Trigger: Claude Code 自动在每个 chain turn 结束时调一次. 这里读 transcript
# 抓最近这一 turn 的 assistant 文本, POST 给本地 apns-server /chat/append,
# server 再 push 到 iPhone.
#
# 配置方式 (一次性):
#   1. cp 这一份到 ~/.claude/hooks/ccc_stop_hook.sh
#   2. chmod +x ~/.claude/hooks/ccc_stop_hook.sh
#   3. 编辑 ~/.claude/settings.json 加 hook 引用 (注意 nested hooks array):
#      {
#        "hooks": {
#          "Stop": [
#            {
#              "matcher": "",
#              "hooks": [
#                {
#                  "type": "command",
#                  "command": "$HOME/.claude/hooks/ccc_stop_hook.sh"
#                }
#              ]
#            }
#          ]
#        }
#      }
#   4. 重启 Claude Code (退出 tmux session 重进, 让 hook config 生效)
#
# 验证 hook 跑通:
#   iPhone 端 ccc 发一条 "hi"; Mac 上 cc 回复后, 看
#   tail -f /tmp/ccc_stop_hook.log
#   应该看到 "posted to /chat/append ok"
#
# Env:
#   CCC_SERVER_URL  default http://127.0.0.1:8795
#   CCC_AUTH_TOKEN  shared_secret 跟 server config.toml 对齐 (写接口必须)

set -uo pipefail

SERVER_URL="${CCC_SERVER_URL:-http://127.0.0.1:8795}"
AUTH_TOKEN="${CCC_AUTH_TOKEN:-}"
# 兜底从 server 自动生成的 secret 文件读
if [ -z "$AUTH_TOKEN" ] && [ -f "$HOME/.ots/secret" ]; then
    AUTH_TOKEN=$(cat "$HOME/.ots/secret" 2>/dev/null)
fi

LOG_PATH="/tmp/ccc_stop_hook.log"
log() { echo "[$(date +%Y-%m-%d\ %H:%M:%S)] $*" >> "$LOG_PATH"; }

# Claude Code 通过 stdin 传 {session_id, transcript_path, cwd, hook_event_name,
# stop_hook_active, last_assistant_message?}.
# 新版 Claude Code 直接传 last_assistant_message; 没有时 fallback 反扫 transcript.
INPUT=$(cat 2>/dev/null || echo "{}")

# 一次性 parse 出 transcript_path 加 last_assistant_message
PARSED=$(echo "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
    print(d.get("transcript_path") or "")
    print(d.get("last_assistant_message") or "")
except Exception:
    print("")
    print("")
' 2>/dev/null)
TRANSCRIPT_PATH=$(echo "$PARSED" | sed -n '1p')
DIRECT_LAST=$(echo "$PARSED" | sed -n '2,$p')

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
    log "no transcript path (stdin=$INPUT)"
    exit 0
fi

# Correlate this callback's assistant text with its own real user turn.  A
# delayed callback must never borrow the newest marker from a later turn.  If
# the transcript does not contain one unique match, omit identity fail-closed:
# history may still be appended, but it cannot complete a newer active turn.
TURN_IDENTITY=$(printf '%s' "$DIRECT_LAST" | python3 -c '
import json, re, sys, time

direct = sys.stdin.read().replace("\r\n", "\n").strip()
if not direct:
    sys.exit(0)

path = sys.argv[1]
pattern = re.compile(r"\[CCC_APP_TURN:([0-9a-f]{32}):([A-Za-z0-9_.:-]{1,80})\]")

def texts(content):
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]

def tool_result_only(content):
    return (
        isinstance(content, list)
        and bool(content)
        and all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    )

def normalized(value):
    return value.replace("\r\n", "\n").strip()

def resolve(lines):
    current = None
    assistant_parts = []
    matches = set()
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        kind = obj.get("type")
        content = obj.get("message", {}).get("content", [])
        if kind == "user":
            if tool_result_only(content):
                continue
            match = pattern.search("\n".join(texts(content)))
            current = (match.group(1), match.group(2)) if match else None
            assistant_parts = []
            continue
        if kind != "assistant" or current is None:
            continue
        event = normalized("\n".join(texts(content)))
        if not event:
            continue
        assistant_parts.append(event)
        candidates = {
            event,
            "".join(assistant_parts),
            "\n".join(assistant_parts),
            "\n\n".join(assistant_parts),
        }
        if any(normalized(candidate) == direct for candidate in candidates):
            matches.add(current)
    return matches

# Give Claude time to flush the assistant row.  Re-read instead of trusting a
# potentially stale snapshot; ambiguity after a later flush remains fail-closed.
lines = []
for attempt in range(5):
    try:
        with open(path, encoding="utf-8") as transcript:
            lines = transcript.readlines()
    except Exception:
        sys.exit(0)
    if attempt < 4:
        time.sleep(0.2)

matches = resolve(lines)
if len(matches) == 1:
    token, session = next(iter(matches))
    print(token)
    print(session)
' "$TRANSCRIPT_PATH" 2>/dev/null)
TURN_TOKEN=$(echo "$TURN_IDENTITY" | sed -n '1p')
TURN_SESSION=$(echo "$TURN_IDENTITY" | sed -n '2p')

# Prefer stdin last_assistant_message (新版 Claude Code 直接给), fallback transcript
if [ -n "$DIRECT_LAST" ]; then
    LAST_ASSISTANT="$DIRECT_LAST"
    log "using stdin last_assistant_message (chars=${#LAST_ASSISTANT})"
else
    # Claude Code transcript flush 慢 — 等 file size 稳定 (连续两次相等 或最长 ~3 秒)
    LAST_SIZE=-1
    STABLE_COUNT=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.3
        CUR_SIZE=$(stat -f '%z' "$TRANSCRIPT_PATH" 2>/dev/null \
                || stat -c '%s' "$TRANSCRIPT_PATH" 2>/dev/null \
                || echo "0")
        if [ "$CUR_SIZE" = "$LAST_SIZE" ]; then
            STABLE_COUNT=$((STABLE_COUNT + 1))
            [ "$STABLE_COUNT" -ge 2 ] && break
        else
            STABLE_COUNT=0
        fi
        LAST_SIZE=$CUR_SIZE
    done

    # transcript 是 JSONL 一行一条 message
    # 倒着读 抓自上次 user 以来的所有 assistant text part 然后 join
    # Linux 没 tail -r 用 tac
    REVERSE_CAT="tail -r"
    if ! command -v tail >/dev/null 2>&1 || ! tail -r /dev/null 2>/dev/null; then
        REVERSE_CAT="tac"
    fi
    LAST_ASSISTANT=$($REVERSE_CAT "$TRANSCRIPT_PATH" | python3 -c '
import json, sys
collected = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    t = obj.get("type")
    if t == "user":
        break
    if t == "assistant":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        text_parts = [
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
        ]
        if text_parts:
            collected.append("\n".join(text_parts))
collected.reverse()
print("\n\n".join(collected))
' 2>/dev/null)
fi

if [ -z "$LAST_ASSISTANT" ]; then
    log "empty assistant text — skip"
    exit 0
fi

# POST 到 /chat/append
PAYLOAD=$(ASSISTANT_TEXT="$LAST_ASSISTANT" TURN_TOKEN="$TURN_TOKEN" TURN_SESSION="$TURN_SESSION" python3 -c '
import json, os, datetime
ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
payload = {
    "role": "assistant",
    "text": os.environ["ASSISTANT_TEXT"],
    "source": "ccc-stop-hook",
    "ts": ts,
}
token = os.environ.get("TURN_TOKEN", "").strip()
session = os.environ.get("TURN_SESSION", "").strip()
if token and session:
    payload["turn_token"] = token
    payload["session_id"] = session
    payload["metadata"] = {
        "xiaoke_turn_token": token,
        "xiaoke_session_id": session,
    }
print(json.dumps(payload))
')

# retry transient network errors (000/502/503/504), don't retry 401 (auth)
attempt=0
while [ $attempt -lt 3 ]; do
    HTTP_CODE=$(curl -s -o /tmp/ccc_stop_hook.curlout -w "%{http_code}" \
        -X POST "$SERVER_URL/chat/append" \
        -H "Content-Type: application/json" \
        -H "X-Auth-Token: $AUTH_TOKEN" \
        --data "$PAYLOAD" \
        --max-time 8 2>>"$LOG_PATH")
    case "$HTTP_CODE" in
        200) break ;;
        000|502|503|504)
            attempt=$((attempt + 1))
            log "POST retry $attempt http=$HTTP_CODE"
            sleep 1
            ;;
        401)
            log "POST 401 unauthorized — check CCC_AUTH_TOKEN or ~/.ots/secret matches server config.toml shared_secret"
            break
            ;;
        *)
            break
            ;;
    esac
done

if [ "$HTTP_CODE" = "200" ]; then
    log "posted to /chat/append ok (chars=${#LAST_ASSISTANT})"
else
    log "POST /chat/append failed http=$HTTP_CODE body=$(cat /tmp/ccc_stop_hook.curlout 2>/dev/null | head -c 200)"
fi

exit 0
