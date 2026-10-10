"""apples 群聊 typing 载荷契约测试（offline；fake handler 风格，参考 _apples_cursor_test.py）。

钉住 2026-10-10 群聊「…」气泡头像/终止键修复的服务端契约：
1. member_id 已带 → 保留，且暴露 turn_user_ts（= since，即触发群消息的 ts）
2. member_id 缺失 → 兜底一：group_reply_pending 最后一条（xiaoke 派发路径）
3. member_id 缺失且 pending 空 → 兜底二：kimi_active_turn 里的群轮
   （user_ts 与 typing.since 一致才认领，私聊轮/错配轮绝不冒领）
4. 非 typing 状态原样返回，不补 member_id/turn_user_ts
5. 源码契约：_start_group_kimi_reply 真正开始流式时的 apples typing True
   写入必须带 member_id="kimi"（覆盖派发写入点就是本次事故根因）
6. 源码契约：_handle_apples_chat_send 的初始 typing 写入认 kimi 分支
7. 源码契约：do_GET 的 /chat/typing 对 apples 走 _apples_typing_payload
"""
import inspect
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import push  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def make_handler(*, pending=None, kimi_active_turn=None):
    stub = SimpleNamespace()
    stub.state = SimpleNamespace(
        group_reply_lock=threading.Lock(),
        group_reply_pending=list(pending or []),
        kimi_turn_lock=threading.Lock(),
        kimi_active_turn=dict(kimi_active_turn or {}),
    )
    return stub


def payload(handler, ts):
    return push.PushHandler._apples_typing_payload(handler, ts)


# 1. member_id 已带 → 保留 + turn_user_ts
out = payload(make_handler(), {"is_typing": True, "since": "ts-1", "member_id": "kimi"})
check("kept:member_id preserved", out.get("member_id") == "kimi")
check("kept:turn_user_ts exposed", out.get("turn_user_ts") == "ts-1")

# 2. pending 兜底（xiaoke 路径）
out = payload(
    make_handler(pending=[{"member_id": "xiaoke", "user_ts": "ts-2"}]),
    {"is_typing": True, "since": "ts-2"},
)
check("pending:member_id from last pending", out.get("member_id") == "xiaoke")
check("pending:turn_user_ts exposed", out.get("turn_user_ts") == "ts-2")

# 3. kimi_active_turn 群轮兜底
out = payload(
    make_handler(kimi_active_turn={"user_ts": "ts-3", "session_id": "s", "group": True}),
    {"is_typing": True, "since": "ts-3"},
)
check("kimi_active:member_id kimi", out.get("member_id") == "kimi")
check("kimi_active:turn_user_ts exposed", out.get("turn_user_ts") == "ts-3")

# 3b. user_ts 错配 → 不认领
out = payload(
    make_handler(kimi_active_turn={"user_ts": "ts-other", "group": True}),
    {"is_typing": True, "since": "ts-3"},
)
check("kimi_active:mismatch not claimed", "member_id" not in out)
check("kimi_active:mismatch no turn_user_ts", "turn_user_ts" not in out)

# 3c. 私聊轮（无 group 标记）→ 不认领
out = payload(
    make_handler(kimi_active_turn={"user_ts": "ts-4"}),
    {"is_typing": True, "since": "ts-4"},
)
check("kimi_active:private turn not claimed", "member_id" not in out)

# 3d. active turn 为空 → 不认领
out = payload(make_handler(), {"is_typing": True, "since": "ts-5"})
check("kimi_active:empty not claimed", "member_id" not in out)
check("kimi_active:empty no turn_user_ts", "turn_user_ts" not in out)

# 4. 非 typing 原样返回
original = {"is_typing": False, "since": None}
out = payload(
    make_handler(kimi_active_turn={"user_ts": "ts-6", "group": True}),
    original,
)
check("idle:unchanged", out == original)
check("idle:no member_id", "member_id" not in out)
check("idle:no turn_user_ts", "turn_user_ts" not in out)

# 4b. typing 但 since 缺失 → member_id 仍兜底，turn_user_ts 不造空值
out = payload(
    make_handler(pending=[{"member_id": "xiaoke", "user_ts": "x"}]),
    {"is_typing": True, "since": None},
)
check("no_since:member_id still resolved", out.get("member_id") == "xiaoke")
check("no_since:no empty turn_user_ts", "turn_user_ts" not in out)

# 5. 源码契约：群轮真正开始流式时 typing True 写入带 member_id="kimi"
src = inspect.getsource(push.PushHandler._start_group_kimi_reply)
anchor = '"is_typing": True, "since": user_ts, "transport": "kimi-web",'
idx = src.find(anchor)
check(
    "source:group kimi typing write carries member_id",
    idx != -1 and '"member_id": "kimi"' in src[idx:idx + 300],
)

# 6. 源码契约：群消息入口的初始 typing 写入认 kimi 分支
src = inspect.getsource(push.PushHandler._handle_apples_chat_send)
check("source:send entry typing_target knows kimi", 'elif "kimi" in targets:' in src)

# 7. 源码契约：do_GET 的 /chat/typing 对 apples 走 _apples_typing_payload
src = inspect.getsource(push.PushHandler.do_GET)
check("source:do_GET apples typing uses payload helper", "self._apples_typing_payload(ts)" in src)

print(f"\ntotal: {len(PASS)} pass, {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
