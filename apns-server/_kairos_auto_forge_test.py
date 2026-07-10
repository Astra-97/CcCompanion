import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from codex_app_bridge import (
    CodexThreadTokenUsage,
    CodexTokenUsageBreakdown,
    CodexTurnResult,
)
from push import (
    AUTO_FORGE_SEEN_BITS,
    AutoForgeClaimStore,
    PushHandler,
    _should_auto_forge,
)


def token_usage(
    input_tokens: int,
    window: int = 100_000,
    *,
    total_tokens: int | None = None,
) -> CodexThreadTokenUsage:
    resolved_total = input_tokens + 25 if total_tokens is None else total_tokens
    breakdown = CodexTokenUsageBreakdown(
        input_tokens=input_tokens,
        cached_input_tokens=100,
        output_tokens=20,
        reasoning_output_tokens=5,
        total_tokens=resolved_total,
    )
    return CodexThreadTokenUsage(
        total=breakdown,
        last=breakdown,
        model_context_window=window,
    )


class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        self.records.append(record)
        return record


class FakeBridge:
    def __init__(self, result, on_run=None):
        self.result = result
        self.on_run = on_run
        self.calls = []

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_run:
            self.on_run()
        return self.result


class FakeSessionStore:
    marked = []

    def __init__(self, _root):
        pass

    def get_history(self, _session_id, limit):
        return object(), [("user", f"retained {limit}"), ("assistant", "ready")]

    def mark_as_desktop_session(self, session_id):
        self.marked.append(session_id)


class KairosAutoForgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_threshold_boundary_below_and_compaction_override(self):
        self.assertTrue(_should_auto_forge(
            token_usage(79_500, total_tokens=80_000),
            threshold_percent=80.0,
            context_compacted=False,
        ))
        self.assertFalse(_should_auto_forge(
            token_usage(79_500, total_tokens=79_999),
            threshold_percent=80.0,
            context_compacted=False,
        ))
        self.assertFalse(_should_auto_forge(
            token_usage(80_000, window=0),
            threshold_percent=80.0,
            context_compacted=False,
        ))
        self.assertTrue(_should_auto_forge(
            None,
            threshold_percent=80.0,
            context_compacted=True,
        ))

    def test_claim_is_persistent_redacted_and_bounded(self):
        path = self.root / "codex_auto_forge.json"
        first = AutoForgeClaimStore(path, history_limit=16)
        second = AutoForgeClaimStore(path, history_limit=16)
        old_session_id = "sensitive-session-id"
        self.assertTrue(first.claim(old_session_id))
        self.assertFalse(second.claim(old_session_id))
        first.finish(old_session_id, "completed")
        self.assertFalse(second.claim(old_session_id))
        self.assertNotIn(old_session_id, path.read_text(encoding="utf-8"))

        for index in range(20):
            self.assertTrue(first.claim(f"session-{index}"))
        claims = json.loads(path.read_text(encoding="utf-8"))["claims"]
        self.assertEqual(len(claims), 16)
        self.assertFalse(second.claim(old_session_id))

    def test_claim_recovers_when_previous_owner_process_is_gone(self):
        path = self.root / "codex_auto_forge.json"
        first = AutoForgeClaimStore(path)
        self.assertTrue(first.claim("recover-session"))

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["claims"][-1]["owner_pid"] = 999_999_999
        path.write_text(json.dumps(payload), encoding="utf-8")

        recovered = AutoForgeClaimStore(path)
        self.assertTrue(recovered.claim("recover-session"))
        recovered.finish("recover-session", "failed")
        self.assertFalse(AutoForgeClaimStore(path).claim("recover-session"))

    def test_v2_claim_file_migration_recovers_in_progress_owner(self):
        path = self.root / "codex_auto_forge.json"
        store = AutoForgeClaimStore(path)
        key = store._session_key("v2-recover-session")
        seen = bytearray(AUTO_FORGE_SEEN_BITS // 8)
        store._seen_add(seen, key)
        path.write_text(json.dumps({
            "version": 2,
            "seen": seen.hex(),
            "claims": [{
                "key": key,
                "status": "claimed",
                "owner_pid": 999_999_999,
            }],
        }), encoding="utf-8")

        self.assertTrue(store.claim("v2-recover-session"))

    def test_concurrent_claim_has_one_winner(self):
        path = self.root / "codex_auto_forge.json"
        barrier = threading.Barrier(8)
        results = []

        def claim():
            barrier.wait()
            results.append(AutoForgeClaimStore(path).claim("same-session"))

        workers = [threading.Thread(target=claim) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(results.count(True), 1)

    def _write_pointer(self, session_id: str):
        state_path = self.root / "bot_state.json"
        state_path.write_text(json.dumps({
            "shared_sessions": {
                "kairos": {
                    "active_session_id": session_id,
                    "active_cwd": str(self.root),
                },
            },
            "users": {
                "astra": {
                    "active_session_id": session_id,
                    "active_cwd": str(self.root),
                },
            },
        }), encoding="utf-8")
        return state_path

    def _handler(self, old_session_id: str, bridge: FakeBridge):
        state_path = self._write_pointer(old_session_id)
        chat = FakeChat()
        state = types.SimpleNamespace(
            contact_chats={"kairos": chat},
            codex_auto_forge_claims=AutoForgeClaimStore(self.root / "claims.json"),
            codex_auto_forge_retain_messages=80,
            codex_home=str(self.root / "codex-home"),
            codex_model="gpt-test",
            codex_reasoning_effort="high",
            codex_app_bridge=bridge,
            codex_bot_state_path=str(state_path),
            codex_shared_session_name="kairos",
            codex_user_id="astra",
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler._codex_allowed_cwd = lambda cwd: Path(cwd)
        return handler, chat, state_path

    def _run(self, handler, old_session_id):
        fake_module = types.SimpleNamespace(SessionStore=FakeSessionStore)
        with mock.patch.dict(sys.modules, {"codex_common": fake_module}):
            return handler._run_kairos_auto_forge(
                old_session_id=old_session_id,
                cwd=self.root,
                token_usage=token_usage(80_000),
                context_compacted=False,
                cancel_event=threading.Event(),
            )

    def test_success_uses_new_app_server_thread_then_cas_and_notifies(self):
        old_session_id = "old-session-1234"
        new_session_id = "new-session-5678"
        bridge = FakeBridge(CodexTurnResult(
            thread_id=new_session_id,
            turn_id="handoff-turn",
            text="forge-ready",
            status="completed",
        ))
        handler, chat, state_path = self._handler(old_session_id, bridge)

        self.assertTrue(self._run(handler, old_session_id))

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["shared_sessions"]["kairos"]["active_session_id"],
            new_session_id,
        )
        self.assertEqual(bridge.calls[0]["thread_id"], None)
        self.assertNotIn("on_thread", bridge.calls[0])
        notice = chat.records[-1]["text"]
        self.assertIn("80.0%", notice)
        self.assertIn(old_session_id[:8], notice)
        self.assertIn(new_session_id[:8], notice)

    def test_handoff_failure_retains_old_pointer_and_notifies(self):
        old_session_id = "old-session-1234"
        bridge = FakeBridge(CodexTurnResult(
            thread_id="new-session-5678",
            turn_id="handoff-turn",
            text="",
            status="failed",
            error="failed",
        ))
        handler, chat, state_path = self._handler(old_session_id, bridge)

        self.assertFalse(self._run(handler, old_session_id))

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["shared_sessions"]["kairos"]["active_session_id"],
            old_session_id,
        )
        self.assertIn("失败", chat.records[-1]["text"])
        self.assertIn("仍保持 active", chat.records[-1]["text"])

    def test_cas_conflict_keeps_external_pointer_and_notifies(self):
        old_session_id = "old-session-1234"
        external_session_id = "external-session"

        def switch_externally():
            self._write_pointer(external_session_id)

        bridge = FakeBridge(CodexTurnResult(
            thread_id="new-session-5678",
            turn_id="handoff-turn",
            text="forge-ready",
            status="completed",
        ), on_run=switch_externally)
        handler, chat, state_path = self._handler(old_session_id, bridge)

        self.assertFalse(self._run(handler, old_session_id))

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["shared_sessions"]["kairos"]["active_session_id"],
            external_session_id,
        )
        self.assertIn("CAS", chat.records[-1]["text"])
        self.assertIn(external_session_id[:8], chat.records[-1]["text"])

    def test_cas_rejects_diverged_shared_and_user_pointers(self):
        old_session_id = "old-session-1234"
        external_session_id = "external-session"
        bridge = FakeBridge(CodexTurnResult(
            thread_id="new-session-5678",
            turn_id="handoff-turn",
            text="forge-ready",
            status="completed",
        ))
        handler, chat, state_path = self._handler(old_session_id, bridge)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["users"]["astra"]["active_session_id"] = external_session_id
        state_path.write_text(json.dumps(state), encoding="utf-8")

        self.assertFalse(self._run(handler, old_session_id))

        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["shared_sessions"]["kairos"]["active_session_id"],
            old_session_id,
        )
        self.assertEqual(
            state["users"]["astra"]["active_session_id"],
            external_session_id,
        )
        self.assertIn("CAS", chat.records[-1]["text"])


if __name__ == "__main__":
    unittest.main()
