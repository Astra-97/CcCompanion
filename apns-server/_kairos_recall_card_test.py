from pathlib import Path
import json
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_history import ChatHistory
from push import KairosRecallIndex, PushHandler, ServerState


class FakeRecall:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.queries = []

    def recall_result(self, query, *, exclude_memory_keys=()):
        self.queries.append((query, tuple(exclude_memory_keys)))
        if self.error:
            raise self.error
        return self.result


class KairosRecallCardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatHistory(Path(self.tmp.name) / "chat.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def handler(self, recall):
        state = types.SimpleNamespace(
            kairos_semantic_memory_recall_enabled=True,
            kairos_semantic_memory_recall_timeout_sec=0.2,
            kairos_semantic_memory_recall=recall,
            kairos_semantic_memory_recall_init_attempted=True,
            kairos_semantic_memory_recall_lock=threading.Lock(),
            kairos_recall_card_lock=threading.Lock(),
            kairos_recall_index=KairosRecallIndex(Path(self.tmp.name) / "recall-index.json"),
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler._apples_member_name = lambda member_id: "Kairos" if member_id == "kairos" else member_id
        return handler

    @staticmethod
    def result():
        return types.SimpleNamespace(
            context="【记忆浮现·自动检索】\n安全的模型上下文",
            items=(
                {"date": "2026-07-19", "title": "午饭", "snippet": "Astra 喜欢番茄"},
            ),
            memory_keys=("v1:" + "a" * 64,),
        )

    def test_hit_appends_one_card_between_user_and_reply(self):
        handler = self.handler(FakeRecall(self.result()))
        user = self.chat.append(role="user", text="今天吃什么", source="cc-app:kairos")

        result = handler._kairos_semantic_recall("今天吃什么")
        self.assertIsNotNone(result)
        self.assertTrue(handler._append_kairos_recall_card(
            self.chat,
            result,
            user_ts=user["ts"],
            source="memory-recall:kairos",
            session_id="session-a",
        ))
        self.assertFalse(handler._append_kairos_recall_card(
            self.chat,
            result,
            user_ts=user["ts"],
            source="memory-recall:kairos",
            session_id="session-a",
        ))
        self.chat.append(role="assistant", text="吃番茄吧", source="codex:kairos")

        records = self.chat.read_since(limit=20, include_hidden=True)
        self.assertEqual([record["role"] for record in records], ["user", "assistant", "assistant"])
        card = records[1]
        self.assertGreater(card["ts"], user["ts"])
        self.assertEqual(card["source"], "memory-recall:kairos")
        self.assertTrue(card["metadata"]["recall_card"])
        self.assertEqual(card["metadata"]["kairos_user_ts"], user["ts"])
        self.assertFalse(card["metadata"]["turn_terminal"])
        self.assertEqual(card["metadata"]["turn_message_kind"], "auxiliary_recall")
        self.assertEqual(card["metadata"]["items"], [
            {"date": "2026-07-19", "title": "午饭", "snippet": "Astra 喜欢番茄"},
        ])
        self.assertEqual(card["metadata"]["recall_session_id"], "session-a")
        self.assertEqual(card["metadata"]["recall_memory_keys"], ["v1:" + "a" * 64])

    def test_seen_keys_are_persistent_session_scoped_and_do_not_scan_chat_history(self):
        recall = FakeRecall(self.result())
        handler = self.handler(recall)
        group_chat = ChatHistory(Path(self.tmp.name) / "group.jsonl")
        group_user = group_chat.append(role="user", text="群聊第一轮", source="cc-app:apples")
        self.assertTrue(handler._append_kairos_recall_card(
            group_chat,
            self.result(),
            user_ts=group_user["ts"],
            source="memory-recall:group:kairos",
            group=True,
            session_id="session-a",
        ))
        # Rendering the UI card alone must not mark a memory seen before Codex
        # confirms that turn/start accepted the prompt.
        self.assertEqual(handler._kairos_seen_memory_keys("session-a"), ())
        self.assertTrue(handler._commit_kairos_recall(self.result(), "session-a"))

        # Simulate a service restart: a fresh handler/index recovers from the
        # bounded ledger and does not need any chat JSONL scan.
        restarted = self.handler(recall)
        restarted.state.contact_chats = {
            "kairos": types.SimpleNamespace(read_since=lambda **_kwargs: self.fail("history scanned")),
            "apples": types.SimpleNamespace(read_since=lambda **_kwargs: self.fail("history scanned")),
        }

        self.assertIsNotNone(restarted._kairos_semantic_recall("私聊第二轮", session_id="session-a"))
        self.assertEqual(recall.queries[-1], ("私聊第二轮", ("v1:" + "a" * 64,)))
        self.assertIsNotNone(restarted._kairos_semantic_recall("同会话第三轮", session_id="session-a"))
        self.assertEqual(recall.queries[-1], ("同会话第三轮", ("v1:" + "a" * 64,)))

        self.assertIsNotNone(restarted._kairos_semantic_recall("另一个会话", session_id="session-b"))
        self.assertEqual(recall.queries[-1], ("另一个会话", ()))

        self.assertIsNotNone(restarted._kairos_semantic_recall("无会话边界"))
        self.assertEqual(recall.queries[-1], ("无会话边界", ()))

    def test_new_session_first_recall_binds_only_after_real_thread_is_accepted(self):
        recall = FakeRecall(self.result())
        handler = self.handler(recall)
        user = self.chat.append(role="user", text="新会话首轮", source="cc-app:kairos")

        first = handler._kairos_semantic_recall("新会话首轮", session_id=None)
        self.assertIsNotNone(first)
        self.assertFalse(handler._append_kairos_recall_card(
            self.chat,
            first,
            user_ts=user["ts"],
            source="memory-recall:kairos",
            session_id=None,
        ))
        self.assertEqual(handler._kairos_seen_memory_keys("thread-new"), ())

        # Mirrors run_turn's order: thread preparation resolves a real ID, the
        # UI card binds to it, then accepted turn/start commits the index.
        self.assertTrue(handler._append_kairos_recall_card(
            self.chat,
            first,
            user_ts=user["ts"],
            source="memory-recall:kairos",
            session_id="thread-new",
        ))
        self.assertEqual(handler._kairos_seen_memory_keys("thread-new"), ())
        self.assertTrue(handler._commit_kairos_recall(first, "thread-new"))

        second = handler._kairos_semantic_recall("新会话第二轮", session_id="thread-new")
        self.assertIsNotNone(second)
        self.assertEqual(recall.queries[-1], ("新会话第二轮", ("v1:" + "a" * 64,)))

    def test_index_persist_failure_does_not_mutate_seen_state(self):
        index = KairosRecallIndex(Path(self.tmp.name) / "failing-index.json")
        key = "v1:" + "b" * 64
        with mock.patch.object(index, "_persist_locked", return_value=False):
            self.assertFalse(index.add("session-fail", (key,)))
        self.assertEqual(index.keys("session-fail"), ())

    def test_empty_or_exception_is_fail_open_and_appends_nothing(self):
        empty = types.SimpleNamespace(context="", items=())
        for recall in (FakeRecall(empty), FakeRecall(error=RuntimeError("secret detail"))):
            with self.subTest(error=bool(recall.error)):
                handler = self.handler(recall)
                self.assertIsNone(handler._kairos_semantic_recall("查询原文不能出现在卡片"))
        self.assertEqual(self.chat.read_since(limit=20, include_hidden=True), [])

    def test_group_card_uses_kairos_identity(self):
        handler = self.handler(FakeRecall(self.result()))
        user = self.chat.append(role="user", text="@Kairos 还记得吗", source="cc-app:apples")
        self.assertTrue(handler._append_kairos_recall_card(
            self.chat,
            self.result(),
            user_ts=user["ts"],
            source="memory-recall:group:kairos",
            group=True,
            session_id="session-group",
        ))
        card = self.chat.read_since(since_ts=user["ts"], limit=20, include_hidden=True)[0]
        self.assertEqual(card["sender_id"], "kairos")
        self.assertEqual(card["sender_name"], "Kairos")
        self.assertNotIn("@Kairos 还记得吗", card["text"])

    def test_group_recall_boundary_allows_human_but_not_agent_hops(self):
        handler = self.handler(FakeRecall(self.result()))
        captured = []
        handler._chat_for_contact = lambda _contact_id: self.chat
        handler._set_typing_for_contact = lambda *_args, **_kwargs: None
        handler._start_group_kairos_reply = lambda *args, **kwargs: captured.append(kwargs)
        handler._apples_dispatch_allowed = lambda *_args: True
        handler._apples_sender_global_allowed = lambda *_args: True
        handler._apples_room_global_allowed = lambda *_args: True
        handler._apples_record_global = lambda *_args: None
        handler._apples_emit_drop_system_msg = lambda *_args, **_kwargs: None

        handler._dispatch_apples_mentions(
            {"ts": "human-turn", "text": "@Kairos 还记得吗"},
            "apples",
            {"kairos"},
            "Astra",
            sender_id="astra",
        )
        handler._dispatch_apples_mentions(
            {"ts": "agent-turn", "text": "@Kairos 验收一下"},
            "apples",
            {"kairos"},
            "小克",
            sender_id="xiaoke",
        )
        handler._dispatch_apples_mentions(
            {"ts": "unknown-turn", "text": "@Kairos 还记得吗"},
            "apples",
            {"kairos"},
            "未知成员",
            sender_id="unknown",
        )
        handler._dispatch_apples_mentions(
            {"ts": "empty-turn", "text": "@Kairos 还记得吗"},
            "apples",
            {"kairos"},
            "未知成员",
            sender_id="",
        )

        self.assertEqual(
            [call["semantic_recall_allowed"] for call in captured],
            [True, False, False, False],
        )
        self.assertEqual(
            [call["user_ts"] for call in captured],
            ["human-turn", "agent-turn", "unknown-turn", "empty-turn"],
        )

    def test_concurrent_same_turn_append_is_atomic(self):
        handler = self.handler(FakeRecall(self.result()))
        user = self.chat.append(role="user", text="并发测试", source="cc-app:kairos")
        barrier = threading.Barrier(3)
        outcomes = []
        outcomes_lock = threading.Lock()

        def worker():
            barrier.wait()
            outcome = handler._append_kairos_recall_card(
                self.chat,
                self.result(),
                user_ts=user["ts"],
                source="memory-recall:kairos",
                session_id="session-concurrent",
            )
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=1.0)

        self.assertEqual(sorted(outcomes), [False, True])
        cards = [
            record for record in self.chat.tail(20)
            if isinstance(record.get("metadata"), dict)
            and record["metadata"].get("recall_card") is True
        ]
        self.assertEqual(len(cards), 1)

    def test_recovery_does_not_treat_recall_card_as_final_reply(self):
        user = self.chat.append(role="user", text="还记得吗", source="cc-app:kairos")
        handler = self.handler(FakeRecall(self.result()))
        handler._append_kairos_recall_card(
            self.chat,
            self.result(),
            user_ts=user["ts"],
            source="memory-recall:kairos",
            session_id="session-recovery",
        )
        pending = Path(self.tmp.name) / "pending.json"
        pending.write_text(json.dumps({
            "contact_id": "kairos",
            "user_ts": user["ts"],
            "draft_text": "",
        }), encoding="utf-8")
        state = object.__new__(ServerState)
        state.kairos_pending_run_path = pending
        state.contact_chats = {"kairos": self.chat}
        state.clear_kairos_pending_run = lambda *_args: pending.unlink(missing_ok=True)

        state._recover_pending_kairos_run()

        records = self.chat.tail(20)
        self.assertEqual(records[-1]["source"], "codex:kairos:recovered")


if __name__ == "__main__":
    unittest.main()
