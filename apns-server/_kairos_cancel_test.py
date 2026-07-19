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

    def test_group_and_legacy_runner_observer_never_retains_raw_activity(self):
        registry = _CodexRunRegistry()
        sensitive = "调用 /bin/sh --token=TOPSECRET /private/user/file"
        for source in ("cc-app:apples:kairos", "cc-app:kairos"):
            run = registry.start(
                source=source,
                session_id="private-session",
                cwd=Path("/private/workspace"),
            )
            self.assertIsNotNone(run)
            run_id = run[0]
            self.assertTrue(registry.set_observer_phase(run_id, "running"))
            self.assertTrue(registry.publish_runner_activity(run_id, sensitive))
            self.assertFalse(
                registry.publish_runner_activity(run_id, "思考摘要：private user prompt")
            )

            snapshot = registry.observer_snapshot()
            rendered = repr(snapshot)
            self.assertTrue(snapshot["busy"])
            self.assertEqual(snapshot["phase"], "正在处理")
            self.assertIn("调用工具（名称与参数已隐藏）", rendered)
            for forbidden in (
                "TOPSECRET",
                "/bin/sh",
                "/private",
                "private-session",
                "private user prompt",
            ):
                self.assertNotIn(forbidden, rendered)
            registry.finish(run_id)
            self.assertFalse(registry.observer_snapshot()["busy"])

    def test_generic_observer_is_thread_safe_and_bounded(self):
        registry = _CodexRunRegistry()
        run = registry.start(
            source="cc-app:apples:kairos",
            session_id="session",
            cwd=Path("/tmp"),
        )
        self.assertIsNotNone(run)
        run_id = run[0]

        threads = [
            threading.Thread(
                target=registry.publish_observer_event,
                args=(run_id, "commandExecution"),
            )
            for _ in range(80)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = registry.observer_snapshot()
        self.assertEqual(len(snapshot["events"]), 40)
        self.assertTrue(all(
            event["label"] == "运行命令（参数与输出已隐藏）"
            for event in snapshot["events"]
        ))
        registry.finish(run_id)

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
            self.assertEqual(
                handler.interrupted,
                [],
                "the abort handler must leave the live draft for the worker to persist",
            )
        finally:
            push.CODEX_RUNS.finish(run[0])

    def test_cancelled_before_registration_never_loads_or_runs_codex(self):
        handler = self.handler()
        event = threading.Event()
        event.set()
        handler._load_codex_target = lambda: self.fail("cancelled task reached target loading")
        task = {"contact_id": "kairos", "user_ts": "u-pre", "text": "hello"}

        handler._process_kairos_task(task, task_cancel_event=event)

        final = handler.state.contact_chats["kairos"].records[-1]
        self.assertEqual(final["role"], "assistant")
        self.assertEqual(final["metadata"]["kairos_user_ts"], "u-pre")
        self.assertTrue(handler.interrupted)

    def test_interrupted_snapshot_freezes_draft_until_short_expiry(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            chat_draft_lock=threading.Lock(),
            chat_drafts={"test": {"is_active": True, "text": "already visible", "user_ts": "u1"}},
            chat_reply_states={},
        )

        handler._set_chat_interrupted("test", user_ts="u1", final_ts="a-final", ttl_sec=15)
        snapshot = handler._chat_draft_snapshot("test")

        self.assertFalse(snapshot["is_active"])
        self.assertEqual(snapshot["text"], "already visible")
        self.assertEqual(snapshot["reply_state"], "interrupted")
        self.assertEqual(snapshot["final_ts"], "a-final")

        handler.state.chat_drafts["test"]["expires_at"] = 0
        handler.state.chat_reply_states["test"]["expires_at"] = 0
        expired = handler._chat_draft_snapshot("test")
        self.assertEqual(expired["reply_state"], "idle")
        self.assertEqual(expired["text"], "")

    def test_interrupted_state_never_relabels_a_different_turn_draft(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            chat_draft_lock=threading.Lock(),
            chat_drafts={"test": {"is_active": True, "text": "older", "user_ts": "old"}},
            chat_reply_states={},
        )

        handler._set_chat_interrupted("test", user_ts="new")

        self.assertNotIn("test", handler.state.chat_drafts)
        self.assertEqual(handler.state.chat_reply_states["test"]["user_ts"], "new")

    def test_new_queued_turn_drops_previous_interrupted_draft(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            chat_draft_lock=threading.Lock(),
            chat_drafts={"test": {"is_active": True, "text": "stopped text", "user_ts": "old"}},
            chat_reply_states={},
        )
        handler._set_chat_interrupted("test", user_ts="old")

        handler._set_chat_queued("test", user_ts="new")
        snapshot = handler._chat_draft_snapshot("test")

        self.assertEqual(snapshot["reply_state"], "queued")
        self.assertEqual(snapshot["user_ts"], "new")
        self.assertEqual(snapshot["text"], "")

    def test_foreground_reply_state_is_active_before_first_text_delta(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
        )

        handler._set_chat_queued("test", user_ts="queued-turn")
        queued = handler._chat_draft_snapshot("test")
        self.assertTrue(queued["is_active"])
        self.assertEqual(queued["text"], "")
        self.assertEqual(queued["activity_count"], 0)

        handler._set_chat_generating("test", user_ts="generating-turn")
        generating = handler._chat_draft_snapshot("test")
        self.assertTrue(generating["is_active"])
        self.assertEqual(generating["text"], "")
        self.assertEqual(generating["activity_count"], 0)

    def test_terminal_reply_state_is_not_active_even_when_interrupted_text_is_retained(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            chat_draft_lock=threading.Lock(),
            chat_drafts={
                "test": {
                    "is_active": True,
                    "text": "already visible",
                    "user_ts": "u1",
                }
            },
            chat_reply_states={},
        )

        handler._set_chat_interrupted("test", user_ts="u1", ttl_sec=15)
        interrupted = handler._chat_draft_snapshot("test")
        self.assertFalse(interrupted["is_active"])
        self.assertEqual(interrupted["text"], "already visible")

        handler._set_chat_completed("test", user_ts="u1", ttl_sec=15)
        completed = handler._chat_draft_snapshot("test")
        self.assertFalse(completed["is_active"])


if __name__ == "__main__":
    unittest.main()
