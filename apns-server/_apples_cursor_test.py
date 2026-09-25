"""Function-level test of apples 群未读游标 + 3s 去重 (offline; mocks inject/kimi/kairos).

Covers:
1. first-enable — 首次启用 (无任何游标) 视为全部已读, 不倒历史, 只推触发消息
2. unread — AI 被 @ 时注入它游标之后的未读 (标明未读分段), 不含触发消息本身/旧消息
3. advance-timing — 游标在派发时推进: 回复生成期间新到的消息保持未读, 下次再注入
4. cap — 未读超 50 条取最近 50 条, 首行注明「更早的 N 条未读已省略」
5. fallback — 游标缺失/失效 (指向比最新还新的 ts) 回退最近 4 条
6. persist — 游标写 apples_read_cursors.json, 新实例读回
7. dedupe — 3s 内同文本重复只落库/派发一次; 连续不同发言不误伤; 隔 3s 同文本正常落库
8. prompt — kimi prompt 里未读块在 [群聊消息] 之前
"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TEST_CHAT_PATH = HERE / "tokens" / "_test_apples_cursor_history.jsonl"
TEST_STATE_PATH = HERE / "tokens" / "_test_apples_read_cursors.json"
for p in (TEST_CHAT_PATH, TEST_STATE_PATH):
    if p.exists():
        p.unlink()

import push  # noqa: E402
from apples_unread import ApplesCursorStore, UNREAD_CAP  # noqa: E402
from chat_history import ChatHistory  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


class Stub:
    pass


def make_handler(chat):
    stub = Stub()
    stub.state = SimpleNamespace(
        contact_chats={"xiaoke": chat, "kairos": chat, "kimi": chat, "apples": chat},
        contact_catalog=[],
        contact_routes={
            "xiaoke": {"send_handler": "xiaoke", "capabilities": ["chat", "history", "group_member", "group_reply"], "group_dispatcher": "xiaoke"},
            "kairos": {"send_handler": "kairos", "capabilities": ["chat", "history", "group_member", "group_reply"], "group_dispatcher": "kairos"},
            "kimi": {"send_handler": "kimi", "capabilities": ["chat", "history", "group_member", "group_reply"], "group_dispatcher": "kimi"},
            "apples": {"send_handler": "apples", "capabilities": ["chat", "history", "group_chat"]},
        },
        group_reply_lock=__import__("threading").Lock(),
        group_reply_pending=[],
    )
    stub._apples_cursors = ApplesCursorStore(TEST_STATE_PATH)
    stub._send_json = MagicMock()
    stub.kimi_group_calls = []
    stub.kairos_group_calls = []
    stub.xiaoke_injects = []

    def _chat_for_contact(self, contact_id):
        return chat

    def _set_typing_for_contact(self, contact_id, state):
        pass

    def _source_for_request(self, contact_id=""):
        return "test"

    def _inject_to_session(self, session, text, source="ios-app", sender="iphone"):
        self.xiaoke_injects.append(text)
        return True, None

    def _start_group_kairos_reply(self, chat, text, sender_name="Astra", hop_count=0, **kwargs):
        self.kairos_group_calls.append({"text": text, "sender_name": sender_name, **kwargs})

    def _start_group_kimi_reply(self, chat, text, sender_name="Astra", hop_count=0, **kwargs):
        self.kimi_group_calls.append({"text": text, "sender_name": sender_name, **kwargs})

    def _link_context_from_record(self, rec):
        return ""

    def _remember_group_reply(self, member_id, ts, source_member=None):
        return None

    def _enrich_user_links(self, text):
        return SimpleNamespace(previews=[], prompt_context="")

    def _discard_uncommitted_staged_attachments(self, staged):
        pass

    def _kimi_bqb_protocol(self):
        return ""

    import types
    local = {
        "_chat_for_contact": _chat_for_contact,
        "_set_typing_for_contact": _set_typing_for_contact,
        "_source_for_request": _source_for_request,
        "_inject_to_session": _inject_to_session,
        "_start_group_kairos_reply": _start_group_kairos_reply,
        "_start_group_kimi_reply": _start_group_kimi_reply,
        "_link_context_from_record": _link_context_from_record,
        "_remember_group_reply": _remember_group_reply,
        "_enrich_user_links": _enrich_user_links,
        "_discard_uncommitted_staged_attachments": _discard_uncommitted_staged_attachments,
        "_kimi_bqb_protocol": _kimi_bqb_protocol,
    }
    H = push.PushHandler
    real = {}
    for name in [
        "_handle_apples_chat_send",
        "_dispatch_apples_mentions",
        "_apples_unread_block",
        "_apples_cursor_store",
        "_kimi_group_prompt",
        "_group_reply_marker",
        "_apples_dispatch_allowed",
        "_apples_sender_global_allowed",
        "_apples_room_global_allowed",
        "_apples_record_global",
        "_apples_is_human_sender",
        "_apples_emit_drop_system_msg",
        "_apples_member_name",
        "_apples_members",
        "_apples_self_id",
        "_apples_member_ids",
        "_apples_replyable_member_ids",
        "_invalid_explicit_apples_mentions",
        "_normalize_mentioned_member_ids",
        "_detect_apples_mentions",
        "_chat_contact_directory",
    ]:
        real[name] = getattr(H, name)
    for name, fn in {**local, **real}.items():
        setattr(stub, name, types.MethodType(fn, stub))
    for const in [
        "APPLES_HOP_LIMIT",
        "APPLES_HOP_LIMIT_DEBUG",
        "APPLES_PAIR_RATE_LIMIT_SEC",
        "APPLES_SENDER_GLOBAL_LIMIT",
        "APPLES_ROOM_GLOBAL_LIMIT",
        "APPLES_GLOBAL_WINDOW_SEC",
        "APPLES_AGENT_SENDERS",
    ]:
        setattr(stub, const, getattr(H, const))
    return stub


def reset_dedupe_cache(handler):
    for tgt in (type(handler), push.PushHandler):
        if hasattr(tgt, "_apples_dedupe_cache"):
            delattr(tgt, "_apples_dedupe_cache")


def send(handler, text, mentions):
    return handler._handle_apples_chat_send(
        {"text": text, "metadata": {"mentioned_member_ids": mentions}}, "apples",
    )


def last_payload(handler):
    return handler._send_json.call_args[0][1]


chat = ChatHistory(TEST_CHAT_PATH)
handler = make_handler(chat)
reset_dedupe_cache(handler)

# ---------- 1. first-enable: 首次启用不倒历史 ----------
chat.append(role="user", text="历史消息甲", source="test", sender_id="astra", sender_name="Astra")
chat.append(role="assistant", text="历史消息乙", source="group:kairos", sender_id="kairos", sender_name="Kairos")
send(handler, "kimi 在吗", ["kimi"])
status, payload = handler._send_json.call_args[0]
check("first:200 + routed kimi", status == 200 and payload.get("routed") == ["kimi"])
check("first:no unread injected", handler.kimi_group_calls[-1].get("unread_context") == "")
store = handler._apples_cursor_store()
check("first:store initialized", store.is_initialized())
check("first:cursor = trigger ts", store.get_cursor("kimi") == payload["record"]["ts"])

# ---------- 2. unread: 注入游标之后的未读 ----------
time.sleep(0.011)  # ChatHistory ts 为毫秒精度, 隔开避免同毫秒 ts 碰撞
chat.append(role="user", text="给小克的第一条", source="test", sender_id="astra", sender_name="Astra")
time.sleep(0.011)
chat.append(role="assistant", text="kairos 插话", source="group:kairos", sender_id="kairos", sender_name="Kairos")
time.sleep(0.011)
send(handler, "小克 看上面", ["xiaoke"])
injected = handler.xiaoke_injects[-1]
check("unread:block marker present", "[未读群消息" in injected)
check("unread:has post-cursor msgs", "给小克的第一条" in injected and "kairos 插话" in injected)
check("unread:excludes pre-cursor history", "历史消息甲" not in injected and "历史消息乙" not in injected)
# 未读分段里不含触发消息本身 (触发消息由 text_for_agent 单独附上, 允许在分段外出现)
block_section = injected.split("[未读群消息", 1)[1].split("小克 看上面", 1)[0]
check("unread:trigger not inside block", "看上面" not in block_section)
check("unread:cursor advanced to trigger", store.get_cursor("xiaoke") == last_payload(handler)["record"]["ts"])

# ---------- 3. advance-timing: 派发时推进, 回复期间新消息保持未读 ----------
send(handler, "kairos 新问题", ["kairos"])
t1_ctx = handler.kairos_group_calls[-1].get("unread_context") or ""
t1_cursor = store.get_cursor("kairos")
check("timing:cursor advanced at dispatch", t1_cursor == last_payload(handler)["record"]["ts"])
# 模拟 kairos 回复生成期间 (游标已推进后) 人类又发一条
time.sleep(0.011)  # 避免与触发消息同毫秒 ts 碰撞 (同 ts 会被游标比较视为已读)
chat.append(role="user", text="回复期间来的消息", source="test", sender_id="astra", sender_name="Astra")
time.sleep(0.011)
send(handler, "kairos 再来", ["kairos"])
t2_ctx = handler.kairos_group_calls[-1].get("unread_context") or ""
check("timing:new msg during reply stays unread", "回复期间来的消息" in t2_ctx)
check("timing:prev trigger not replayed", "kairos 新问题" not in t2_ctx)
check("timing:own earlier unread not double-injected", "给小克的第一条" not in t2_ctx)
check("timing:kairos dispatched twice", len(handler.kairos_group_calls) == 2)
_ = t1_ctx  # t1 上下文内容不做断言 (含给小克的第一条等, 取决于 kairos 游标初始位置)

# ---------- 4. cap: 未读 > 50 截断 + 省略提示 ----------
time.sleep(0.011)
early_ts = chat.append(role="user", text="cap-anchor", source="test", sender_id="astra", sender_name="Astra")["ts"]
store.set_cursor("kimi", early_ts)
time.sleep(0.011)
flood_recs = [
    chat.append(role="user", text=f"flood-{i:02d}", source="test", sender_id="astra", sender_name="Astra")
    for i in range(60)
]
block, advance_ts = handler._apples_unread_block("kimi", chat, "")
check("cap:omission note with count", "[更早的 10 条未读消息已省略]" in block)
check("cap:kept latest 50", "flood-59" in block and "flood-10" in block)
check("cap:dropped oldest 10", "flood-09" not in block and "flood-00" not in block)
check("cap:advance = latest unread", advance_ts == flood_recs[-1]["ts"])

# ---------- 5. fallback: 游标失效 (指向比最新还新的 ts) 回退最近 4 条 ----------
store.set_cursor("xiaoke", "9999-01-01T00:00:00.000000+08:00")
block, _ = handler._apples_unread_block("xiaoke", chat, "")
check("fallback:invalid cursor -> last 4", "flood-59" in block and "flood-56" in block)
check("fallback:older excluded", "flood-55" not in block)

# ---------- 6. persist: 新实例读回游标 ----------
store2 = ApplesCursorStore(TEST_STATE_PATH)
check("persist:initialized survives reload", store2.is_initialized())
check("persist:cursor survives reload", store2.get_cursor("kimi") == early_ts)
# 单调推进: 旧 ts 不覆盖新 ts
store2.set_cursor("kimi", "2020-01-01T00:00:00.000000+08:00")
check("persist:cursor never regresses", store2.get_cursor("kimi") == early_ts)

# ---------- 7. dedupe: 3s 内同文本只落一条, 不重复派发 ----------
reset_dedupe_cache(handler)
handler._send_json.reset_mock()
kimi_calls_before = len(handler.kimi_group_calls)
send(handler, "去重测试同一句话", ["kimi"])
first = last_payload(handler)
send(handler, "去重测试同一句话", ["kimi"])
second = last_payload(handler)
check("dedupe:dup 200 + deduped flag", second.get("deduped") is True and second.get("ok"))
check("dedupe:dup returns first record", second.get("record", {}).get("ts") == first["record"]["ts"])
check("dedupe:dup not re-dispatched", len(handler.kimi_group_calls) == kimi_calls_before + 1)
rows = [l for l in TEST_CHAT_PATH.read_text(encoding="utf-8").splitlines() if "去重测试同一句话" in l]
check("dedupe:only one row persisted", len(rows) == 1, f"rows={len(rows)}")

# 连续不同发言不误伤
send(handler, "连续发言甲", [])
send(handler, "连续发言乙", [])
p_a = handler._send_json.call_args_list[-2][0][1]
p_b = handler._send_json.call_args_list[-1][0][1]
check("dedupe:distinct msgs both stored", not p_a.get("deduped") and not p_b.get("deduped"))
rows = [l for l in TEST_CHAT_PATH.read_text(encoding="utf-8").splitlines() if "连续发言甲" in l or "连续发言乙" in l]
check("dedupe:distinct msgs both persisted", len(rows) == 2, f"rows={len(rows)}")

# 同文本隔 3 秒以上再发 → 正常落库
send(handler, "隔窗重复句", [])
time.sleep(3.1)
send(handler, "隔窗重复句", [])
last = last_payload(handler)
check("dedupe:same text after 3s window stored", last.get("ok") and not last.get("deduped"))
rows = [l for l in TEST_CHAT_PATH.read_text(encoding="utf-8").splitlines() if "隔窗重复句" in l]
check("dedupe:after-window dup persisted twice", len(rows) == 2, f"rows={len(rows)}")

# ---------- 8. prompt: kimi 未读块在 [群聊消息] 之前 ----------
prompt = handler._kimi_group_prompt("触发消息", sender_name="Astra", unread_context="[未读群消息]\n[01:30] Astra: 旧消息")
check("prompt:unread before trigger", prompt.index("[未读群消息]") < prompt.index("[群聊消息]"))

print()
print(f"total: {len(PASS)} pass, {len(FAIL)} fail")
for p in (TEST_CHAT_PATH, TEST_STATE_PATH):
    if p.exists():
        p.unlink()
sys.exit(1 if FAIL else 0)
