"""2026-10-10 forge 瞬间吞消息事故的回归测试（需求二/三）。

事故时间线（journalctl + chat_history 证据）：
- 03:30:15 群聊小克 @Kimi 消息在旧会话开跑；03:32:41 私聊消息排队。
- 03:34:19 服务重启：内存队列蒸发（排队私聊消息静默丢失），在飞群轮
  worker 线程死亡（群回复终态文本永远没写进 apples 历史）。
- 03:37:49 私聊「哈喽？」触发自动 forge；新会话 seed 注入是 fire-and-
  forget，首个真实用户轮排在 seed 后面跑了 2.5 分钟。排队轮没有
  idle/status_changed 边界，watcher 的 on_ready 快照看到的还是 seed 轮，
  bound_turn_id 始终绑不上：delta 全丢、只有带 promptId 的终态帧匹配，
  最终落了「Kimi 没有返回可展示内容。」——而 Kimi 侧该轮实际产出了
  完整回复（wire.jsonl 03:40:37 474 tokens 实证）。

本文件的 fake 按事故如实建模：delta 帧只带 turnId（不带 promptId）、
seed→用户轮之间没有 status_changed 边界。
"""

import json
import sys
import tempfile
import threading
import types
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _kimi_chat_test as kimi_chat_test
from _kimi_chat_test import FakeChat, FakeGroupChat, FakeWebChat
from push import PushHandler, ServerState

try:
    from push import _kimi_chat_queue_boot_reconcile, _kimi_group_turn_boot_reconcile
except ImportError:  # 修复前的 push.py 没有启动对账：相关测试 skip，其余照常跑
    _kimi_chat_queue_boot_reconcile = None
    _kimi_group_turn_boot_reconcile = None

_make_routing_handler = kimi_chat_test.KimiWebChatRoutingTest.make_handler
_wait_idle = kimi_chat_test.KimiWebChatRoutingTest.wait_idle
_wait_queue_drained = kimi_chat_test.KimiWebChatRoutingTest.wait_queue_drained


class QueuedBehindSeedWeb(FakeWebChat):
    """forge 后的新会话：seed 轮在跑，真实用户轮排在它后面。

    按事故如实建模：用户轮的 delta 只带 turnId（无 promptId），seed 轮与
    用户轮之间没有 status_changed 边界（会话一直忙），终态帧带 promptId。
    """

    def __init__(self, *, user_text, seed_session_busy=True):
        super().__init__(text=user_text)
        self.active = "old-session"
        self.phase = "seed" if seed_session_busy else "ours"
        self.submissions = []

    def start(self):
        pass

    def load_active_session_id(self):
        return self.active

    def get_session_status(self, session_id, **_kwargs):
        busy = session_id == "new-session" and self.phase == "seed"
        return {"busy": busy, "context_usage": 0.85}

    def ensure_active_session(self, *, model, thinking, **_kwargs):
        self.calls.append(("ensure", model, thinking))
        return self.active

    def create_session(self, *, title, model, thinking, permission_mode):
        self.active = "new-session"
        return "new-session"

    def submit_prompt(self, session_id, prompt, **kwargs):
        self.calls.append(("submit", session_id, prompt, dict(kwargs)))
        self.submissions.append(prompt)
        if "受控 forge" in prompt:
            return {"prompt_id": "seed-p"}
        return {"prompt_id": "user-p"}

    def get_snapshot(self, _session_id, **_kwargs):
        if self.phase == "seed":
            return {
                "current_prompt": {"id": "seed-p"},
                "in_flight_turn": {"turnId": "turn-seed", "assistant_text": "seed 输出不应出现"},
                "epoch": "e1", "as_of_seq": 100,
            }
        return {
            "current_prompt": {"id": "user-p"},
            "in_flight_turn": {"turnId": "turn-user", "assistant_text": self.text},
            "epoch": "e1", "as_of_seq": 200,
        }

    def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
        self.calls.append(("stream", session_id))
        on_ready()
        # seed 轮帧：只有 turnId 的事件必须被丢弃；终态带 seed 的 promptId。
        on_event({"type": "assistant.delta", "session_id": session_id,
                  "payload": {"turnId": "turn-seed", "delta": "seed 输出不应出现"},
                  "seq": 150, "epoch": "e1"})
        on_event({"type": "prompt.completed", "session_id": session_id,
                  "payload": {"promptId": "seed-p", "reason": "completed"},
                  "seq": 160, "epoch": "e1"})
        # 在飞轮换到本 prompt——会话始终忙，没有 status_changed 边界。
        self.phase = "ours"
        on_event({"type": "assistant.delta", "session_id": session_id,
                  "payload": {"turnId": "turn-user", "delta": self.text},
                  "seq": 210, "epoch": "e1"})
        on_event({"type": "prompt.completed", "session_id": session_id,
                  "payload": {"promptId": "user-p", "reason": "completed"},
                  "seq": 220, "epoch": "e1"})


def _forge_ready_handler(web, *, usage=0.85):
    handler, chat, web = _make_routing_handler(None, web=web)
    handler.state.kimi_auto_forge_context_threshold = 0.8
    handler.state.kimi_auto_forge_warn_threshold = 0.75
    handler.state.kimi_forge_warn_state = {}
    handler.state.kimi_forge_deferred = False
    handler.state.kimi_forge_seed_retain_messages = 0
    handler.state.kimi_sessions_root = None
    tmp = tempfile.mkdtemp()
    handler.state.token_store_path = str(Path(tmp) / "tokens" / "device_tokens.json")
    handler.state.kimi_group_turn_marker_path = str(Path(tmp) / "kimi_group_turn_inflight.json")
    handler._kimi_context_usage = lambda _session: usage
    handler._send_chat_notification = lambda *_args: None
    return handler, chat, web


class ForgeQueuedTurnRebindTest(unittest.TestCase):
    """需求三：forge 后首个真实用户轮排在 seed 后面，回复不再丢。"""

    def test_private_turn_queued_behind_forge_seed_keeps_its_answer(self):
        web = QueuedBehindSeedWeb(user_text="真正的私聊回复")
        handler, chat, web = _forge_ready_handler(web)
        with patch("push._scan_kimi_session_tasks", return_value={"finished": [], "pending": []}), \
                patch("push.KIMI_REBIND_PROBE_MIN_SECONDS", 0, create=True):
            handler._handle_kimi_chat_send({"text": "哈喽？"}, "kimi")
            _wait_idle(handler)
        # forge 发生了：seed 注入新会话。
        self.assertEqual("new-session", web.active)
        self.assertTrue(any("受控 forge" in prompt for prompt in web.submissions))
        self.assertTrue(any(row.get("source") == "system:kimi-forge" for row in chat.records))
        # 关键断言：真实回复落库，不是「没有返回可展示内容」兜底。
        finals = [row for row in chat.records
                  if row.get("role") == "assistant" and (row.get("metadata") or {}).get("turn_terminal")]
        self.assertEqual(1, len(finals))
        self.assertEqual("真正的私聊回复", finals[0]["text"])
        self.assertEqual("kimi-web", finals[0]["source"])
        # seed 轮的文本绝不混入本轮。
        self.assertNotIn("seed 输出不应出现", finals[0]["text"])

    def test_group_turn_queued_behind_forge_seed_keeps_its_answer(self):
        web = QueuedBehindSeedWeb(user_text="群里的真实回复")
        handler, _chat, web = _forge_ready_handler(web)
        group_chat = FakeGroupChat()
        handler._has_pending_group_reply = lambda: False
        user = group_chat.append(role="user", text="@Kimi 在吗", source="test:apples")
        with patch("push.KIMI_REBIND_PROBE_MIN_SECONDS", 0, create=True):
            outcome = handler._start_group_kimi_reply(
                group_chat, user["text"], sender_name="小克",
                user_ts=user["ts"], hop_count=0,
            )
            _wait_idle(handler)
        self.assertEqual("started", outcome)
        finals = [row for row in group_chat.records
                  if row.get("role") == "assistant" and row.get("source") == "group:kimi-web"]
        self.assertEqual(1, len(finals))
        self.assertEqual("群里的真实回复", finals[0]["text"])
        self.assertNotIn("seed 输出不应出现", finals[0]["text"])
        # 在飞标记已随终态清除。
        self.assertFalse(Path(handler.state.kimi_group_turn_marker_path).exists())


class ForgeThenBothWeb(FakeWebChat):
    """forge 完成后：私聊轮在跑时群消息到达排队，两轮按序各拿各的回复。"""

    TURN_IDS = {"seed-p": "turn-seed", "user-p": "turn-user", "group-p": "turn-group"}

    def __init__(self):
        super().__init__()
        self.active = "old-session"
        self.turns = []  # 非 seed 的提交：(prompt_id, prompt)
        self.mid_turn = threading.Event()
        self.release = threading.Event()
        self.stream_calls = 0

    def start(self):
        pass

    def load_active_session_id(self):
        return self.active

    def get_session_status(self, _session_id, **_kwargs):
        return {"busy": False, "context_usage": 0.85}

    def ensure_active_session(self, *, model, thinking, **_kwargs):
        self.calls.append(("ensure", model, thinking))
        return self.active

    def create_session(self, *, title, model, thinking, permission_mode):
        self.active = "new-session"
        return "new-session"

    def submit_prompt(self, session_id, prompt, **kwargs):
        self.calls.append(("submit", session_id, prompt, dict(kwargs)))
        if "受控 forge" in prompt:
            return {"prompt_id": "seed-p"}
        prompt_id = "group-p" if "苹果幼稚园" in prompt else "user-p"
        self.turns.append((prompt_id, prompt))
        return {"prompt_id": prompt_id}

    def get_snapshot(self, _session_id, **_kwargs):
        prompt_id = self.turns[-1][0] if self.turns else "seed-p"
        return {
            "current_prompt": {"id": prompt_id},
            "in_flight_turn": {"turnId": self.TURN_IDS[prompt_id]},
            "epoch": "e", "as_of_seq": 1,
        }

    def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
        self.calls.append(("stream", session_id))
        self.stream_calls += 1
        block_mid_turn = self.stream_calls == 1
        on_ready()
        prompt_id = self.turns[-1][0]
        turn_id = self.TURN_IDS[prompt_id]
        text = "私聊回复" if prompt_id == "user-p" else "群聊回复"
        on_event({"type": "assistant.delta", "session_id": session_id,
                  "payload": {"turnId": turn_id, "delta": text}})
        if block_mid_turn:
            # 第一轮在 delta 之后、终态之前挂起，让群消息在此窗口到达排队。
            self.mid_turn.set()
            self.release.wait(5)
        on_event({"type": "prompt.completed", "session_id": session_id,
                  "payload": {"promptId": prompt_id, "reason": "completed"}})


class ForgeMomentConcurrencyTest(unittest.TestCase):
    """需求二：forge 完成瞬间一条私聊在跑 + 一条群消息排队，各落各的历史。"""

    def test_private_and_queued_group_each_get_their_own_answer(self):
        web = ForgeThenBothWeb()
        handler, chat, web = _forge_ready_handler(web)
        group_chat = FakeGroupChat()
        handler.state.group_chat = group_chat
        handler._has_pending_group_reply = lambda: False
        with patch("push._scan_kimi_session_tasks", return_value={"finished": [], "pending": []}):
            handler._handle_kimi_chat_send({"text": "哈喽？"}, "kimi")
            self.assertEqual(200, handler.responses[-1][0])
            # 私聊轮在跑（delta 已到、终态未至）时，群消息到达 → 排队。
            self.assertTrue(web.mid_turn.wait(5))
            group_user = group_chat.append(role="assistant", text="@Kimi 卡拉米帮个忙", source="test:apples")
            outcome = handler._start_group_kimi_reply(
                group_chat, group_user["text"], sender_name="小克",
                user_ts=group_user["ts"], hop_count=0,
            )
            self.assertEqual("started", outcome)
            self.assertTrue(any(
                "已排队" in str(row.get("text") or "") for row in group_chat.records
            ))
            web.release.set()
            _wait_idle(handler)
            _wait_queue_drained(handler)
        kimi_finals = [row for row in chat.records
                       if row.get("role") == "assistant" and (row.get("metadata") or {}).get("turn_terminal")]
        self.assertEqual(1, len(kimi_finals))
        self.assertEqual("私聊回复", kimi_finals[0]["text"])
        group_finals = [row for row in group_chat.records
                        if row.get("role") == "assistant" and row.get("source") == "group:kimi-web"]
        self.assertEqual(1, len(group_finals))
        self.assertEqual("群聊回复", group_finals[0]["text"])
        # 两边都没有兜底文案，也没有交叉污染。
        for row in kimi_finals + group_finals:
            self.assertNotIn("没有返回可展示内容", row["text"])
        self.assertNotIn("群聊回复", kimi_finals[0]["text"])
        self.assertNotIn("私聊回复", group_finals[0]["text"])


class KimiQueuePersistenceTest(unittest.TestCase):
    """需求二：Kimi 队列随写随持久化，重启后滞留条目落可见失败说明。"""

    def _bare_state(self, tmp: str) -> ServerState:
        state = object.__new__(ServerState)
        state.kimi_chat_queue_path = Path(tmp) / "kimi_chat_queue.json"
        state.kimi_chat_queue = deque()
        return state

    def test_enqueue_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, chat, _web = _make_routing_handler(None)
            handler.state.kimi_chat_queue_path = Path(tmp) / "kimi_chat_queue.json"
            handler.state.persist_kimi_chat_queue_locked = (
                lambda: ServerState.persist_kimi_chat_queue_locked(handler.state)
            )
            handler.state.kimi_active_turn = {
                "user_ts": "busy", "cancel_event": threading.Event(), "session_id": "s",
            }
            with patch("push.threading.Thread"):  # 只断言入队，不跑 worker
                handler._handle_kimi_chat_send({"text": "重启前排队"}, "kimi")
            self.assertEqual(200, handler.responses[-1][0])
            payload = json.loads((Path(tmp) / "kimi_chat_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(payload))
            self.assertEqual("重启前排队", payload[0]["text"])
            self.assertEqual("web", payload[0]["kind"])
            # 模拟重启：新进程载入同一文件。
            state2 = self._bare_state(tmp)
            loaded = ServerState._load_kimi_chat_queue(state2)
            self.assertEqual(1, len(loaded))
            self.assertEqual("重启前排队", loaded[0]["text"])

    def test_malformed_queue_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kimi_chat_queue.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(deque(), ServerState._load_kimi_chat_queue(self._bare_state(tmp)))
            path.write_text(json.dumps([{"kind": "web", "text": "ok"}, {"no_kind": True}, "junk"]),
                            encoding="utf-8")
            loaded = ServerState._load_kimi_chat_queue(self._bare_state(tmp))
            self.assertEqual(1, len(loaded))

    @unittest.skipIf(_kimi_chat_queue_boot_reconcile is None, "boot reconcile not implemented")
    def test_boot_reconcile_fails_stranded_items_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            kimi_chat = FakeChat()
            group_chat = FakeGroupChat()
            state = types.SimpleNamespace(
                kimi_chat_queue=deque([
                    {"kind": "web", "contact_id": "kimi", "text": "私聊滞留",
                     "record": {"ts": "2026-10-10T03:32:41.548+00:00"},
                     "staged_attachments": [], "attempts": 0,
                     "queued_at": "2026-10-10T03:32:41.548+00:00"},
                    {"kind": "group", "contact_id": "apples", "text": "@Kimi 滞留",
                     "sender_name": "小克", "user_ts": "2026-10-10T03:30:15.902+00:00",
                     "hop_count": 0, "attachments": [], "attempts": 0,
                     "queued_at": "2026-10-10T03:30:15.902+00:00"},
                ]),
                kimi_turn_lock=threading.RLock(),
                kimi_chat_queue_path=Path(tmp) / "kimi_chat_queue.json",
                contact_chats={"kimi": kimi_chat},
                chat=FakeChat(),
                group_chat=group_chat,
                chat_draft_lock=threading.Lock(),
                chat_drafts={},
                chat_reply_states={},
                chat_stream_revisions={},
            )
            state.persist_kimi_chat_queue_locked = lambda: ServerState.persist_kimi_chat_queue_locked(state)
            _kimi_chat_queue_boot_reconcile(state)
            # 私聊滞留消息：可见失败说明入库，绝不静默。
            self.assertTrue(any(
                row.get("role") == "assistant" and "服务重启" in str(row.get("text") or "")
                for row in kimi_chat.records
            ))
            # 群聊滞留消息：apples 落同义说明。
            self.assertTrue(any("服务重启" in str(row.get("text") or "") for row in group_chat.records))
            # 队列清空且落盘文件删除。
            self.assertEqual(0, len(state.kimi_chat_queue))
            self.assertFalse((Path(tmp) / "kimi_chat_queue.json").exists())
            # 空队列再对账是 no-op。
            _kimi_chat_queue_boot_reconcile(state)
            self.assertEqual(2, len(kimi_chat.records) + len(group_chat.records))


class GroupTurnInflightMarkerTest(unittest.TestCase):
    """需求二：群轮在飞标记——正常终态清除，重启残留启动时落中断说明。"""

    def test_marker_cleared_only_by_owning_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, _chat, _web = _make_routing_handler(None)
            handler.state.kimi_group_turn_marker_path = str(Path(tmp) / "marker.json")
            handler._mark_kimi_group_turn_inflight("s1", "u1")
            marker = Path(handler.state.kimi_group_turn_marker_path)
            self.assertTrue(marker.exists())
            # 身份不符的清理绝不误删（异常时序下新一轮刚写的标记）。
            handler._clear_kimi_group_turn_inflight("s1", "other")
            self.assertTrue(marker.exists())
            handler._clear_kimi_group_turn_inflight("s1", "u1")
            self.assertFalse(marker.exists())

    @unittest.skipIf(_kimi_group_turn_boot_reconcile is None, "boot reconcile not implemented")
    def test_boot_reconcile_notes_interrupted_group_turn_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            group_chat = FakeGroupChat()
            marker = Path(tmp) / "kimi_group_turn_inflight.json"
            marker.write_text(json.dumps({
                "session_id": "s-old", "user_ts": "u-old", "pid": 1,
                "started_at": "2026-10-10T03:30:16+00:00",
            }), encoding="utf-8")
            state = types.SimpleNamespace(
                group_chat=group_chat,
                kimi_group_turn_marker_path=str(marker),
            )
            _kimi_group_turn_boot_reconcile(state)
            self.assertEqual(1, len(group_chat.records))
            self.assertIn("重启", group_chat.records[0]["text"])
            self.assertFalse(marker.exists())
            # 幂等：再跑一次不重复落说明。
            _kimi_group_turn_boot_reconcile(state)
            self.assertEqual(1, len(group_chat.records))


if __name__ == "__main__":
    unittest.main()
