from collections import deque
from pathlib import Path
import threading
import types
import unittest

import push
from push import PushHandler, _CodexRunRegistry


class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        record.setdefault("ts", f"a-{len(self.records)}")
        self.records.append(record)
        return record


class FakeBridge:
    def __init__(self):
        self.interrupt_calls = 0

    def interrupt_active(self):
        self.interrupt_calls += 1
        return True


class KairosCancelTest(unittest.TestCase):
    def handler(self, *, queue=(), active=None, active_event=None):
        state = types.SimpleNamespace(
            kairos_queue_lock=threading.Lock(),
            kairos_queue=deque(queue),
            kairos_active_task=active,
            kairos_active_task_cancel=active_event,
            codex_app_bridge=FakeBridge(),
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            contact_chats={"kairos": FakeChat()},
        )
        state.persist_kairos_queue_locked = lambda: None
        state.mark_kairos_pending_run = lambda *_args: None
        state.clear_kairos_pending_run = lambda *_args: None
        state.update_kairos_pending_draft = lambda *_args: None
        handler = object.__new__(PushHandler)
        handler.state = state
        handler.interrupted = []
        handler.typing = []
        handler.responses = []
        handler._set_chat_interrupted = lambda contact_id, **kwargs: handler.interrupted.append(
            (contact_id, kwargs)
        )
        handler._set_chat_queued = lambda *_args, **_kwargs: None
        handler._set_typing_for_contact = lambda contact_id, value: handler.typing.append(
            (contact_id, value)
        )
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._chat_for_contact = lambda contact_id: state.contact_chats[contact_id]
        return handler

    def test_exact_latest_queued_task_is_removed_without_touching_other(self):
        handler = self.handler(queue=[
            {"contact_id": "kairos", "user_ts": "u1", "text": "first"},
            {"contact_id": "kairos", "user_ts": "u2", "text": "second"},
        ])

        cancelled = handler._cancel_kairos_pending_task("kairos", "u2")

        self.assertEqual(cancelled["cancel_kind"], "queued_task")
        self.assertEqual([item["user_ts"] for item in handler.state.kairos_queue], ["u1"])

    def test_pre_register_active_task_sets_its_own_event(self):
        event = threading.Event()
        active = {"contact_id": "kairos", "user_ts": "u-active", "text": "hello"}
        handler = self.handler(active=active, active_event=event)

        cancelled = handler._cancel_kairos_pending_task("kairos", "u-active")

        self.assertEqual(cancelled["cancel_kind"], "active_task")
        self.assertTrue(event.is_set())

    def test_only_queued_task_lands_interrupted_state(self):
        handler = self.handler(queue=[
            {"contact_id": "kairos", "user_ts": "u-only", "text": "only"},
        ])

        handler._cancel_kairos_pending_task("kairos", "u-only")

        self.assertEqual(list(handler.state.kairos_queue), [])
        self.assertEqual(handler.interrupted[-1][1]["user_ts"], "u-only")
        self.assertFalse(handler.typing[-1][1]["is_typing"])

    def test_cancelling_queued_target_does_not_clear_different_active_task(self):
        event = threading.Event()
        handler = self.handler(
            queue=[{"contact_id": "kairos", "user_ts": "u-queued", "text": "later"}],
            active={"contact_id": "kairos", "user_ts": "u-active", "text": "now"},
            active_event=event,
        )

        cancelled = handler._cancel_kairos_pending_task("kairos", "u-queued")

        self.assertEqual(cancelled["cancel_kind"], "queued_task")
        self.assertFalse(cancelled["mark_interrupted"])
        self.assertFalse(event.is_set())
        self.assertEqual(handler.interrupted, [])
        self.assertEqual(handler.typing, [])

    def test_scoped_registry_cancel_does_not_cancel_unrelated_run(self):
        registry = _CodexRunRegistry()
        run = registry.start(
            source="cc-app:codex:forge",
            session_id="forge",
            cwd=Path("/tmp"),
            contact_id="",
        )
        self.assertIsNotNone(run)
        self.assertIsNone(registry.cancel_latest(source="cc-app:kairos", contact_id="kairos"))
        self.assertFalse(run[1].is_set())
        registry.finish(run[0])

    def test_http_200_ok_false_when_nothing_matches_and_bridge_is_untouched(self):
        handler = self.handler()
        handler._handle_codex_abort({"contact_id": "kairos", "cancel_pending": True, "user_ts": "missing"})

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(handler.state.codex_app_bridge.interrupt_calls, 0)

    def test_scoped_endpoint_cancels_matching_active_run_and_bridge(self):
        event = threading.Event()
        active = {"contact_id": "kairos", "user_ts": "u-run", "text": "hello"}
        handler = self.handler(active=active, active_event=event)
        run = push.CODEX_RUNS.start(
            source="cc-app:kairos",
            session_id="session-k",
            cwd=Path("/tmp"),
            cancel_event=event,
            contact_id="kairos",
            user_ts="u-run",
        )
        self.assertIsNotNone(run)
        try:
            handler._handle_codex_abort({
                "contact_id": "kairos",
                "cancel_pending": True,
                "user_ts": "u-run",
            })
            self.assertTrue(handler.responses[-1][1]["ok"])
            self.assertEqual(handler.responses[-1][1]["action"], "active_run")
            self.assertTrue(event.is_set())
            self.assertEqual(handler.state.codex_app_bridge.interrupt_calls, 1)
        finally:
            push.CODEX_RUNS.finish(run[0])

    def test_cancelled_before_registration_never_loads_or_runs_codex(self):
        handler = self.handler()
        event = threading.Event()
        event.set()
        handler._load_codex_target = lambda: self.fail("cancelled task reached target loading")
        task = {"contact_id": "kairos", "user_ts": "u-pre", "text": "hello"}

        handler._process_kairos_task(task, task_cancel_event=event)

        self.assertEqual(handler.state.contact_chats["kairos"].records[-1]["role"], "assistant")
        self.assertTrue(handler.interrupted)


if __name__ == "__main__":
    unittest.main()
