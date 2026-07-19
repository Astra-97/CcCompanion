from pathlib import Path
import json
import sys
import tempfile
import threading
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_history import ChatHistory
from push import PushHandler, ServerState


class FakeRecall:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.queries = []

    def recall_result(self, query):
        self.queries.append(query)
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
        ))
        self.assertFalse(handler._append_kairos_recall_card(
            self.chat,
            result,
            user_ts=user["ts"],
            source="memory-recall:kairos",
        ))
        self.chat.append(role="assistant", text="吃番茄吧", source="codex:kairos")

        records = self.chat.read_since(limit=20, include_hidden=True)
        self.assertEqual([record["role"] for record in records], ["user", "assistant", "assistant"])
        card = records[1]
        self.assertGreater(card["ts"], user["ts"])
        self.assertEqual(card["source"], "memory-recall:kairos")
        self.assertTrue(card["metadata"]["recall_card"])
        self.assertEqual(card["metadata"]["kairos_user_ts"], user["ts"])
        self.assertEqual(card["metadata"]["items"], [
            {"date": "2026-07-19", "title": "午饭", "snippet": "Astra 喜欢番茄"},
        ])

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
