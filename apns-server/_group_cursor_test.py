"""Function-level test of group unread cursors + 3s dedupe (offline; mocks Popen).

Covers:
1. dedupe — 同 sender 同 text 3s 内重复: 只落库一条, 返回已落库那条 + deduped 标记
2. cursor — AI 被 @ 时只注入它游标之后的未读 (不再是共享最后 20 条)
3. advance — AI 发言后游标推进到它自己那条
4. fallback — 游标缺失/失效/指向消息已删 → 回退最近 4 条
5. cap — 未读超过 50 条时取最近 50 条并在上下文首行明示省略
6. persist — 游标写 group_state.json, 新实例读回
"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TEST_JSONL = HERE / "tokens" / "_test_group_cursor.jsonl"
TEST_STATE = HERE / "tokens" / "_test_group_cursor_state.json"
for p in (TEST_JSONL, TEST_STATE):
    if p.exists():
        p.unlink()

import push  # noqa: E402
from group_chat import GroupChatStore, UNREAD_CAP, UNREAD_FALLBACK  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


class Stub:
    pass


def make_handler(store):
    stub = Stub()
    stub.state = SimpleNamespace(group_chat=store, bus_send_path="/nonexistent/bus_send.py")
    stub._send_json = MagicMock()
    stub._source_for_request = lambda suffix="": "test"
    stub._group_online_agents = lambda: {"opia", "sonnet", "shu"}
    stub._group_tmux_session_exists = lambda s: True
    stub._handle_group_send = push.PushHandler._handle_group_send.__get__(stub)
    stub._infer_group_task_owner = push.PushHandler._infer_group_task_owner.__get__(stub)
    return stub


store = GroupChatStore(TEST_JSONL, TEST_STATE)

# ---------- 1. dedupe: 3s 窗口内同 sender 同 text 只落一条 ----------
handler = make_handler(store)
with patch("subprocess.Popen") as popen:
    handler._handle_group_send({"text": "hello @opia", "sender_id": "amian", "mentions": ["opia"]})
    status, payload = handler._send_json.call_args[0]
    check("dedupe:first send 200 + routed", status == 200 and payload.get("ok") and payload.get("targets") == ["opia"])
    first_id = payload["record"]["id"]
    check("dedupe:dispatched with --to opia only", popen.call_count == 1 and "--to" in popen.call_args[0][0] and "opia" in popen.call_args[0][0])

    handler._send_json.reset_mock()
    handler._handle_group_send({"text": "hello @opia", "sender_id": "amian", "mentions": ["opia"]})
    status, payload = handler._send_json.call_args[0]
    check("dedupe:dup returns 200 + deduped flag", status == 200 and payload.get("deduped") is True)
    check("dedupe:dup returns original record", payload.get("record", {}).get("id") == first_id)
    check("dedupe:dup not re-dispatched", popen.call_count == 1 and payload.get("targets") == [])

lines = [l for l in TEST_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
check("dedupe:only one row persisted", len(lines) == 1, f"rows={len(lines)}")

# ---------- 2. cursor: AI 被 @ 只拿到它游标之后的未读 ----------
# opia 发言 → 它的游标推进到自己这条; 之后再发两条 @opia, context 只含新消息
with patch("subprocess.Popen") as popen:
    handler._handle_group_send({"text": "first question", "sender_id": "amian", "mentions": ["opia"]})
    store.append("opia", "opia 的回复", source="tmux:opia")
    cursor = store.get_read_cursor("opia")
    check("cursor:advanced on own speak", bool(cursor) and cursor.get("msg_id") and "opia 的回复" not in str(cursor))
    handler._handle_group_send({"text": "second question", "sender_id": "amian", "mentions": ["opia"]})
    handler._handle_group_send({"text": "third question", "sender_id": "amian", "mentions": ["opia"]})
    # 最后一次 Popen 的 --context
    ctx = popen.call_args[0][0][popen.call_args[0][0].index("--context") + 1]
    check("cursor:context has new unread", "third question" in ctx)
    check("cursor:context excludes pre-cursor", "first question" not in ctx and "hello @opia" not in ctx and "opia 的回复" not in ctx)

# sonnet 从未发言也无游标 → fallback 最近 4 条
records, omitted, used_fallback = store.unread_records("sonnet")
check("fallback:no cursor -> last 4", used_fallback and len(records) == UNREAD_FALLBACK and omitted == 0)

# ---------- 3. fallback: 游标指向已删除/不存在的消息 ----------
store.set_read_cursor("sonnet", "grp_nonexistent_xxxx", "2026-01-01T00:00:00.000000+08:00")
records, omitted, used_fallback = store.unread_records("sonnet")
check("fallback:bogus cursor id -> last 4", used_fallback and len(records) == UNREAD_FALLBACK)
ctx_lines = store.context_lines_for("sonnet")
check("fallback:context_lines_for renders 4", len(ctx_lines) == UNREAD_FALLBACK)

# 游标指向真实消息后被 delete → 也回退
victim = store.append("amian", "即将被删除的消息", source="test")
store.set_read_cursor("sonnet", victim["id"], victim["ts"])
store.delete(victim["id"])
records, omitted, used_fallback = store.unread_records("sonnet")
check("fallback:deleted cursor target -> last 4", used_fallback and len(records) == UNREAD_FALLBACK)

# ---------- 4. cap: 未读 > 50 截断 + 省略提示 ----------
rows = store._iter_records()
store.set_read_cursor("shu", rows[0]["id"], rows[0]["ts"])
for i in range(60):
    store.append("amian", f"flood {i}", source="test")
records, omitted, used_fallback = store.unread_records("shu")
check("cap:unread truncated to 50", not used_fallback and len(records) == UNREAD_CAP and omitted > 0, f"len={len(records)} omitted={omitted}")
check("cap:latest kept", records[-1]["text"] == "flood 59")
ctx_lines = store.context_lines_for("shu")
check("cap:omission note first line", ctx_lines[0].startswith("[更早的") and "已省略]" in ctx_lines[0])

# ---------- 5. persistence: 新实例读回游标 ----------
store2 = GroupChatStore(TEST_JSONL, TEST_STATE)
cur = store2.get_read_cursor("shu")
check("persist:cursor survives reload", bool(cur) and cur.get("msg_id") == rows[0]["id"])

# ---------- 6. 正常连续发言不被误伤 ----------
handler2 = make_handler(store2)
with patch("subprocess.Popen"):
    handler2._handle_group_send({"text": "连续发言 A", "sender_id": "amian", "mentions": []})
    handler2._handle_group_send({"text": "连续发言 B", "sender_id": "amian", "mentions": []})
    s1, p1 = handler2._send_json.call_args_list[0][0]
    s2, p2 = handler2._send_json.call_args_list[1][0]
    check("dedupe:normal distinct msgs both stored", s1 == 200 and s2 == 200 and not p1.get("deduped") and not p2.get("deduped") and p1["record"]["id"] != p2["record"]["id"])

# 同一句话隔 3 秒以上再发 → 正常落库
with patch("subprocess.Popen"):
    handler2._handle_group_send({"text": "repeat later", "sender_id": "amian", "mentions": []})
    time.sleep(3.1)
    handler2._handle_group_send({"text": "repeat later", "sender_id": "amian", "mentions": []})
    last = handler2._send_json.call_args_list[-1][0]
    check("dedupe:same text after 3s window stored", last[0] == 200 and not last[1].get("deduped"))

print()
print(f"total: {len(PASS)} pass, {len(FAIL)} fail")
for p in (TEST_JSONL, TEST_STATE):
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
