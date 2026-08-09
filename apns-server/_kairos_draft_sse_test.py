"""Regression coverage for provider-neutral draft lifecycle SSE.

These tests use the same in-memory state shape as the Codex bridge path.  No
socket is opened: ChatStreamBus subscribers are deliberately queue-backed, so
we can verify public event contents and locking without a timing race.
"""
from __future__ import annotations

import threading
import types
import unittest

from chat_history import ChatStreamBus
from push import PushHandler


class _ProbeBus:
    def __init__(self, draft_lock: threading.Lock):
        self._draft_lock = draft_lock
        self.records: list[dict] = []

    def publish(self, record: dict) -> None:
        # publish must run after draft mutation releases its lock.  If not,
        # this nonblocking acquire fails and turns a future slow transport into
        # a deterministic unit-test failure.
        acquired = self._draft_lock.acquire(blocking=False)
        if not acquired:
            raise AssertionError("SSE publish happened while chat_draft_lock was held")
        self._draft_lock.release()
        self.records.append(record)


class _ReorderingBus:
    """Holds revision 1 after its mutation, then publishes revision 2 first."""
    def __init__(self):
        self.first_publish_started = threading.Event()
        self.release_first = threading.Event()
        self.records: list[dict] = []
        self._lock = threading.Lock()

    def publish(self, record: dict) -> None:
        revision = int(record.get("revision") or 0)
        if revision == 1:
            self.first_publish_started.set()
            if not self.release_first.wait(2):
                raise AssertionError("test did not release delayed SSE event")
        else:
            self.release_first.set()
        with self._lock:
            self.records.append(record)


class KairosDraftSseTest(unittest.TestCase):
    def handler(self, *, probe: bool = False, bus=None) -> PushHandler:
        handler = object.__new__(PushHandler)
        lock = threading.Lock()
        handler.state = types.SimpleNamespace(
            chat_draft_lock=lock,
            chat_drafts={},
            chat_reply_states={},
            chat_stream_revisions={},
            chat_stream_bus=bus if bus is not None else (_ProbeBus(lock) if probe else ChatStreamBus()),
        )
        return handler

    @staticmethod
    def records(handler: PushHandler) -> list[dict]:
        bus = handler.state.chat_stream_bus
        if isinstance(bus, _ProbeBus):
            return list(bus.records)
        q = bus.subscribe()
        # Tests which need to observe a real bus subscribe before publishing.
        bus.unsubscribe(q)
        return []

    def test_draft_event_is_bounded_safe_and_published_after_lock(self):
        handler = self.handler(probe=True)
        private_source = "cc-app:/private/worktree --token=TOPSECRET"
        private_session = "/root/.codex/sessions/secret-thread"
        handler._set_chat_draft(
            "kairos",
            "A" * (PushHandler._CHAT_DRAFT_SSE_TEXT_LIMIT + 32),
            source=private_source,
            session_id=private_session,
            user_ts="turn-new",
            activity_text="run /private/cmd --token=TOPSECRET",
            activity_count=10000,
            activity_items=["raw prompt must not leave state"],
            worker_activity_items=[{"worker_id": "reviewer", "name": "reviewer", "status": "running", "count": 2}],
        )

        event = handler.state.chat_stream_bus.records[-1]
        self.assertEqual(event["event"], "draft")
        self.assertEqual(event["contact_id"], "kairos")
        self.assertEqual(event["turn_id"], "turn-new")
        self.assertEqual(event["reply_state"], "generating")
        self.assertTrue(event["text_truncated"])
        self.assertEqual(len(event["text"]), PushHandler._CHAT_DRAFT_SSE_TEXT_LIMIT)
        self.assertEqual(event["activity_text"], "正在处理（详情已隐藏）")
        self.assertEqual(event["activity_count"], 999)
        self.assertEqual(event["worker_activity_items"][0]["name"], "reviewer")
        self.assertNotIn("activity_items", event)
        for forbidden in ("source", "session_id", "cwd", "prompt", "task", "TOPSECRET", "/private"):
            self.assertNotIn(forbidden, repr(event))

    def test_lifecycle_order_and_terminal_history_refresh(self):
        handler = self.handler(probe=True)
        handler._set_chat_queued("kairos", user_ts="turn-1", source="secret")
        handler._set_chat_generating("kairos", user_ts="turn-1", session_id="private-thread")
        handler._set_chat_draft("kairos", "visible partial", user_ts="turn-1")
        handler._set_chat_completed(
            "kairos", user_ts="turn-1", final_ts="history-ts", source="secret", session_id="private-thread",
        )

        events = handler.state.chat_stream_bus.records
        self.assertEqual([event["reply_state"] for event in events], ["queued", "generating", "generating", "completed"])
        self.assertEqual([event["revision"] for event in events], sorted(event["revision"] for event in events))
        terminal = events[-1]
        self.assertEqual(terminal["event"], "lifecycle")
        self.assertTrue(terminal["terminal"])
        self.assertTrue(terminal["refresh_history"])
        self.assertEqual(terminal["final_ts"], "history-ts")
        self.assertNotIn("text", terminal)
        self.assertNotIn("source", terminal)
        self.assertNotIn("session_id", terminal)

    def test_interrupted_failed_and_clear_have_distinct_lifecycle_events(self):
        handler = self.handler(probe=True)
        handler._set_chat_generating("kairos", user_ts="interrupted")
        handler._set_chat_interrupted("kairos", user_ts="interrupted")
        handler._set_chat_generating("kairos", user_ts="failed")
        handler._set_chat_failed("kairos", user_ts="failed", final_ts="persisted-failure")
        handler._clear_chat_draft("kairos", user_ts="failed")

        terminals = [event for event in handler.state.chat_stream_bus.records if event["event"] == "lifecycle"]
        self.assertEqual([event["reply_state"] for event in terminals], ["interrupted", "failed", "cleared"])
        self.assertTrue(terminals[0]["refresh_history"])
        self.assertTrue(terminals[1]["refresh_history"])
        self.assertFalse(terminals[2]["terminal"])
        self.assertFalse(terminals[2]["refresh_history"])

    def test_old_turn_terminal_is_ignored_and_cannot_cover_new_draft(self):
        handler = self.handler(probe=True)
        handler._set_chat_generating("test", user_ts="new-turn")
        before = len(handler.state.chat_stream_bus.records)
        handler._set_chat_completed("test", user_ts="old-turn", final_ts="old-final")
        self.assertEqual(len(handler.state.chat_stream_bus.records), before)
        snapshot = handler._chat_draft_snapshot("test")
        self.assertEqual(snapshot["user_ts"], "new-turn")
        self.assertEqual(snapshot["reply_state"], "generating")

    def test_old_turn_interrupted_is_ignored_before_any_mutation(self):
        handler = self.handler(probe=True)
        handler._set_chat_draft("test", "new partial", user_ts="new-turn")
        before = len(handler.state.chat_stream_bus.records)
        handler._set_chat_interrupted("test", user_ts="old-turn")
        self.assertEqual(len(handler.state.chat_stream_bus.records), before)
        snapshot = handler._chat_draft_snapshot("test")
        self.assertEqual(snapshot["user_ts"], "new-turn")
        self.assertEqual(snapshot["text"], "new partial")
        self.assertEqual(snapshot["reply_state"], "generating")

    def test_same_turn_late_draft_or_activity_cannot_resurrect_terminal_state(self):
        for terminal_name, terminal_setter in (
            ("completed", lambda handler: handler._set_chat_completed("test", user_ts="turn-1")),
            ("interrupted", lambda handler: handler._set_chat_interrupted("test", user_ts="turn-1")),
            ("failed", lambda handler: handler._set_chat_failed("test", user_ts="turn-1")),
        ):
            with self.subTest(terminal=terminal_name):
                handler = self.handler(probe=True)
                handler._set_chat_draft("test", "initial partial", user_ts="turn-1")
                terminal_setter(handler)
                before = len(handler.state.chat_stream_bus.records)

                handler._set_chat_draft("test", "late partial", user_ts="turn-1")
                handler._set_chat_activity(
                    "test",
                    user_ts="turn-1",
                    activity_text="运行命令（参数与输出已隐藏）",
                    activity_count=1,
                )

                self.assertEqual(len(handler.state.chat_stream_bus.records), before)
                snapshot = handler._chat_draft_snapshot("test")
                self.assertEqual(snapshot["reply_state"], terminal_name)
                self.assertNotEqual(snapshot["text"], "late partial")

    def test_revisions_remain_reliable_when_lock_outside_publish_reorders_events(self):
        bus = _ReorderingBus()
        handler = self.handler(bus=bus)
        first = threading.Thread(
            target=lambda: handler._set_chat_generating("test", user_ts="turn-1"),
        )
        first.start()
        self.assertTrue(bus.first_publish_started.wait(1), "first mutation did not reach publish")
        handler._set_chat_draft("test", "second event", user_ts="turn-1")
        first.join(1)
        self.assertFalse(first.is_alive())

        # Queue delivery is intentionally [2, 1], proving callers must use
        # revision rather than arrival order.  Each event still carries the
        # monotonic revision allocated under chat_draft_lock.
        self.assertEqual([event["revision"] for event in bus.records], [2, 1])
        self.assertTrue(all("revision" in event for event in bus.records))
        self.assertEqual(handler.state.chat_drafts["test"]["stream_revision"], 2)

    def test_polling_snapshot_exposes_only_public_turn_identity_and_revision(self):
        handler = self.handler(probe=True)
        handler._set_chat_queued("test", user_ts="turn-1", source="cc-app:private", session_id="private-session")
        queued = handler._chat_draft_snapshot("test")
        handler._set_chat_draft("test", "partial", user_ts="turn-1")
        draft = handler._chat_draft_snapshot("test")
        handler._set_chat_completed("test", user_ts="turn-1", final_ts="history-final")
        terminal = handler._chat_draft_snapshot("test")

        self.assertEqual(queued["turn_id"], "turn-1")
        self.assertEqual(draft["turn_id"], "turn-1")
        self.assertEqual(terminal["turn_id"], "turn-1")
        self.assertLess(queued["revision"], draft["revision"])
        self.assertLess(draft["revision"], terminal["revision"])
        for snapshot in (queued, draft, terminal):
            self.assertIsInstance(snapshot["revision"], int)
            self.assertGreater(snapshot["revision"], 0)
            self.assertNotIn("cwd", snapshot)
            self.assertNotIn("prompt", snapshot)
            self.assertNotIn("task", snapshot)

    def test_shared_bus_filter_is_strictly_contact_scoped(self):
        handler = self.handler()
        kairos = {"event": "draft", "contact_id": "kairos"}
        xiaoke = {"event": "chunk", "contact_id": "xiaoke"}
        malformed = {"event": "draft"}
        self.assertTrue(handler._chat_stream_event_matches_contact(kairos, "kairos"))
        self.assertFalse(handler._chat_stream_event_matches_contact(xiaoke, "kairos"))
        self.assertFalse(handler._chat_stream_event_matches_contact(malformed, "kairos"))
        self.assertFalse(handler._chat_stream_event_matches_contact("not-a-record", "kairos"))


if __name__ == "__main__":
    unittest.main()
