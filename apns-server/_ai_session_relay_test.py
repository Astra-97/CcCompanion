import json
import io
import tempfile
import threading
import urllib.error
import uuid
import unittest
import os
from unittest import mock
from pathlib import Path

from ai_chat import AIChatManager
from ai_session_relay_proxy import (
    AISessionRelayProxy,
    RelayBusyError,
    RelayError,
    RelayRequestUncertain,
    RelayRequestTerminal,
    build_authoritative_handoff,
    map_relay_event,
    normalize_provider,
    validate_request_id,
    validate_loopback_url,
)


class RelayProtocolTest(unittest.TestCase):
    def test_provider_and_loopback_validation(self):
        self.assertEqual(normalize_provider("cc"), "claude")
        self.assertEqual(validate_loopback_url("http://127.0.0.1:8900/"), "http://127.0.0.1:8900")
        with self.assertRaises(ValueError):
            normalize_provider("openai")
        with self.assertRaises(ValueError):
            validate_loopback_url("https://example.com/relay")
        with self.assertRaises(ValueError):
            validate_loopback_url("http://token@127.0.0.1:8900")
        self.assertEqual(validate_request_id("local_123_-45"), "local_123_-45")
        with self.assertRaises(ValueError):
            validate_request_id("bad request/id")
        with self.assertRaises(ValueError):
            validate_request_id("x" * 201)

    def test_event_mapping_is_delta_public_summary_activity_and_done(self):
        self.assertEqual(map_relay_event({"delta": "夏"}, "claude"), {"type": "delta", "text": "夏"})
        self.assertEqual(
            map_relay_event({"codex_thinking_delta": "公开摘要"}, "codex"),
            {"type": "thinking_delta", "text": "公开摘要"},
        )
        # Claude relay can reconstruct raw transcript thinking. Never expose it.
        self.assertIsNone(map_relay_event({"cc_thinking_delta": "private chain"}, "claude"))
        self.assertIsNone(map_relay_event({
            "provider": "codex", "codex_thinking_delta": "spoofed summary",
        }, "claude"))
        done = map_relay_event({
            "done": True,
            "full": "final",
            "provider": "claude",
            "cc_thinking_full": "private full chain",
            "codex_thinking_full": "",
        }, "claude")
        self.assertEqual(done["reply"], "final")
        self.assertEqual(done["thinking"], "")
        spoofed_done = map_relay_event({
            "done": True, "provider": "codex", "full": "final",
            "codex_thinking_full": "spoofed full summary",
        }, "claude")
        self.assertEqual(spoofed_done["thinking"], "")
        activity = map_relay_event({
            "activity": {"id": "1", "title": "Read", "status": "running"}
        }, "claude")
        self.assertEqual(activity["type"], "activity")

    def test_handoff_is_bounded_and_keeps_authoritative_roles(self):
        handoff = build_authoritative_handoff([
            {"role": "user", "text": "你好"},
            {"role": "assistant", "text": "我在"},
            {"role": "system", "text": "must not copy"},
        ])
        self.assertIn("User: 你好", handoff)
        self.assertIn("Assistant: 我在", handoff)
        self.assertNotIn("must not copy", handoff)
        self.assertLessEqual(len(build_authoritative_handoff([
            {"role": "user", "text": "x" * 40_000}
        ])), 24_000)

    def test_codex_model_failure_never_spawns_backend_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            proxy = AISessionRelayProxy(temp)
            with mock.patch.object(proxy, "_json_request", side_effect=RelayError("relay down")), \
                    mock.patch("subprocess.run") as run:
                result = proxy.list_models("http://127.0.0.1:8900", "codex")
        self.assertEqual(result, {"models": [], "dynamic": False})
        run.assert_not_called()


class FakeRelay:
    def __init__(self):
        self.turn_active = False
        self.turns = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.provider = "claude"
        self.last_model = None
        self.last_request_id = None
        self.refreshes = 0

    def sync_persona(self, _prompt, _mode="chat_only"):
        pass

    def refresh_sessions(self, _url):
        self.refreshes += 1
        return {"ok": True, "provider": self.provider, "epoch": self.refreshes}

    def status(self, _url):
        return {
            "ok": True,
            "provider": self.provider,
            "epoch": 0,
            "turn_active": self.turn_active,
            "claude_available": True,
            "codex_available": True,
        }

    def switch_provider(self, _url, provider):
        self.provider = provider
        return {"ok": True, "provider": provider, "epoch": 1, "changed": True, "turn_active": False}

    def list_models(self, _url, provider):
        if provider == "codex":
            return {"models": [{"id": "codex-test", "label": "Codex Test"}], "dynamic": True}
        return {"models": [], "dynamic": False}

    def stream_turn(self, _url, *, provider, text, handoff, execution_mode, model, request_id, emit):
        self.last_model = model
        self.last_request_id = request_id
        self.turns += 1
        self.turn_active = True
        self.started.set()
        self.release.wait(3)
        emit({"type": "delta", "text": "答"})
        emit({"type": "activity", "activity": {"id": "tool-1", "title": "测试", "status": "completed"}})
        self.turn_active = False
        return {
            "type": "done",
            "reply": "答案",
            "thinking": "公开摘要",
            "provider": provider,
            "activities": [],
            "error": "",
        }


class RelayManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = AIChatManager(Path(self.temp.name))
        self.fake = FakeRelay()
        self.manager._relay = self.fake
        self.manager.configure_relay({
            "enabled": True,
            "relay_enabled": True,
            "relay_execution_mode": "chat_only",
            "relay_url": "http://127.0.0.1:8900",
            "system_prompt": "You are Xia.",
        })

    def tearDown(self):
        self.fake.release.set()
        self.temp.cleanup()

    def test_switch_rejected_while_turn_active_and_retry_is_idempotent(self):
        events = []
        result_box = {}

        def run_turn():
            result_box["result"] = self.manager.send_message_stream(
                "你好", events.append, client_message_id="client-1"
            )

        thread = threading.Thread(target=run_turn)
        thread.start()
        self.assertTrue(self.fake.started.wait(1))
        with self.assertRaises(RelayBusyError):
            self.manager.switch_relay_provider("codex")
        with self.assertRaises(RelayBusyError):
            self.manager.select_relay_model("claude", "alias")
        with self.assertRaises(RelayBusyError):
            self.manager.apply_persona_composition([], "locked")
        self.fake.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result_box["result"]["ok"])
        self.assertEqual(self.fake.turns, 1)

        replay_events = []
        duplicate = self.manager.send_message_stream(
            "你好", replay_events.append, client_message_id="client-1"
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.fake.turns, 1)
        history = self.manager.read_history()
        self.assertEqual([record["role"] for record in history], ["user", "assistant"])
        self.assertEqual(json.loads(history[-1]["tools"])[0]["id"], "tool-1")

    def test_provider_switch_and_mode_validation(self):
        result = self.manager.switch_relay_provider("codex")
        self.assertEqual(result["provider"], "codex")
        selected = self.manager.select_relay_model("codex", "codex-test")
        self.assertEqual(selected["current_model"], "codex-test")
        with self.assertRaises(ValueError):
            self.manager.select_relay_model("codex", "invented-version")
        claude = self.manager.select_relay_model("claude", "my-safe-alias")
        self.assertEqual(claude["current_model"], "my-safe-alias")
        self.manager.switch_relay_provider("claude")
        self.fake.release.set()
        sent = self.manager.send_message("model check", client_message_id="model-check")
        self.assertTrue(sent["ok"])
        self.assertEqual(self.fake.last_model, "my-safe-alias")
        with self.assertRaises(ValueError):
            self.manager.configure_relay({"relay_execution_mode": "unsafe-ish"})

        aliases = {item["id"] for item in self.manager.relay_model_status("claude")["models"]}
        self.assertTrue({"", "fable", "opus", "sonnet", "my-safe-alias"}.issubset(aliases))

    def test_attachment_is_rejected_without_creating_false_history(self):
        self.fake.release.set()
        result = self.manager.send_attachment(
            "看这个", "/attachments/example.jpg", "image", "example.jpg", "/private/example.jpg"
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["unsupported"])
        self.assertEqual(self.manager.read_history(), [])

    def _local_persona_relay(self):
        relay = AISessionRelayProxy(self.temp.name)
        relay.refresh_sessions = mock.Mock(return_value={
            "ok": True, "provider": "claude", "epoch": 1,
        })
        return relay

    def test_persona_composition_is_ordered_private_atomic_and_syncs_both_engines(self):
        self.manager._relay = self._local_persona_relay()
        status = self.manager.apply_persona_composition(
            [
                {"filename": "base.md", "content": "第一层"},
                {"filename": "detail.txt", "content": "第二层"},
            ],
            "最后覆盖",
        )
        self.assertEqual([item["filename"] for item in status["files"]], ["base.md", "detail.txt"])
        persona_path = Path(self.temp.name) / "ai_persona" / "current" / "manifest.json"
        self.assertEqual(persona_path.stat().st_mode & 0o777, 0o600)
        workspace = Path(self.temp.name) / "ai_relay_workspace"
        self.assertEqual((workspace / "CLAUDE.md").read_text(), (workspace / "AGENTS.md").read_text())
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
        self.assertEqual((workspace / "CLAUDE.md").stat().st_mode & 0o777, 0o600)
        compiled = (workspace / "CLAUDE.md").read_text()
        self.assertLess(compiled.index("第一层"), compiled.index("第二层"))
        self.assertLess(compiled.index("第二层"), compiled.index("最后覆盖"))
        existing_ids = [item["id"] for item in status["files"]]
        reordered = self.manager.apply_persona_composition(
            [{"id": existing_ids[1]}, {"id": existing_ids[0]}], "最后覆盖"
        )
        self.assertEqual([item["id"] for item in reordered["files"]], existing_ids[::-1])
        with self.assertRaises(ValueError):
            self.manager.apply_persona_composition(
                [{"filename": "bad.pdf", "content": "bad"}], ""
            )
        self.assertEqual(self.manager.persona_status()["files"], reordered["files"])
        self.assertEqual(self.manager._relay.refresh_sessions.call_count, 2)

    def test_persona_accepts_yaml_as_plain_text_without_parsing_or_execution(self):
        self.manager._relay = self._local_persona_relay()
        yaml_text = "name: 夏以昼\ninstructions:\n  - 保持克制\n"
        status = self.manager.apply_persona_composition(
            [
                {"filename": "base.yaml", "content": yaml_text},
                {"filename": "override.YML", "content": "tone: gentle\n"},
            ],
            "最后覆盖",
        )
        self.assertEqual(
            [item["filename"] for item in status["files"]],
            ["base.yaml", "override.YML"],
        )
        compiled = (Path(self.temp.name) / "ai_relay_workspace" / "CLAUDE.md").read_text()
        self.assertIn(yaml_text.strip(), compiled)
        self.assertLess(compiled.index("name: 夏以昼"), compiled.index("tone: gentle"))
        self.assertLess(compiled.index("tone: gentle"), compiled.index("最后覆盖"))
        with self.assertRaisesRegex(ValueError, "binary data"):
            self.manager.apply_persona_composition(
                [{"filename": "binary.yaml", "content": "name: ok\u0001payload"}], ""
            )
        with self.assertRaisesRegex(ValueError, "binary data"):
            self.manager.apply_persona_composition(
                [{"filename": "unicode-control.yml", "content": "name: ok\u0085payload"}], ""
            )
        with self.assertRaisesRegex(ValueError, "must be"):
            self.manager.apply_persona_composition(
                [{"filename": "persona.json", "content": "{}"}], ""
            )

    def test_persona_journal_recovery_finishes_staged_atomic_apply(self):
        self.manager._relay = self._local_persona_relay()
        self.manager.apply_persona_composition(
            [{"filename": "old.md", "content": "old"}], ""
        )
        persona_root = Path(self.temp.name) / "ai_persona"
        txid = uuid.uuid4().hex
        stage = persona_root / f".stage-{txid}"
        backup = persona_root / f".backup-{uuid.uuid4().hex}"
        (stage / "files").mkdir(parents=True)
        file_id = uuid.uuid4().hex
        (stage / "files" / f"{file_id}.txt").write_text("new")
        (stage / "manifest.json").write_text(json.dumps({
            "files": [{"id": file_id, "filename": "new.md", "size": 3}],
            "custom_text": "override", "updated_at": "now",
            "transaction_id": txid,
        }))
        (persona_root / ".apply-journal.json").write_text(json.dumps({
            "version": 1, "transaction_id": txid, "phase": "refresh_inflight",
            "stage": stage.name, "backup": backup.name,
        }))
        self.manager._recover_persona_transaction()
        status = self.manager.persona_status()
        self.assertEqual(status["files"][0]["filename"], "new.md")
        self.assertEqual(status["custom_text"], "override")
        self.assertFalse((persona_root / ".apply-journal.json").exists())

    def test_persona_has_no_file_count_cap_but_enforces_text_byte_budgets(self):
        self.manager._relay = self._local_persona_relay()
        many = [
            {"filename": f"part-{index}.txt", "content": "x"}
            for index in range(64)
        ]
        status = self.manager.apply_persona_composition(many, "last")
        self.assertEqual(len(status["files"]), 64)
        with self.assertRaisesRegex(ValueError, "256 KiB"):
            self.manager.apply_persona_composition(
                [{"filename": "huge.txt", "content": "x" * (256 * 1024 + 1)}], ""
            )
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            self.manager.apply_persona_composition([], "x" * (512 * 1024 + 1))

    def test_persona_precommit_failure_rolls_back_manifest_and_workspace(self):
        self.manager._relay = self._local_persona_relay()
        self.manager.apply_persona_composition([{"filename": "old.md", "content": "old"}], "")
        old_status = self.manager.persona_status()
        workspace = Path(self.temp.name) / "ai_relay_workspace" / "CLAUDE.md"

        def fault(point):
            if point == "after_workspace_sync":
                raise RuntimeError("precommit")

        with self.assertRaisesRegex(RuntimeError, "precommit"):
            self.manager.apply_persona_composition(
                [{"filename": "new.md", "content": "new"}], "", _fault=fault
            )
        self.assertEqual(self.manager.persona_status()["files"], old_status["files"])
        self.assertIn("old", workspace.read_text())
        self.assertNotIn("new", workspace.read_text())
        self.assertFalse((Path(self.temp.name) / "ai_persona" / ".apply-journal.json").exists())

        self.manager._relay.refresh_sessions.side_effect = RuntimeError("refresh failed")
        with self.assertRaisesRegex(RuntimeError, "refresh failed"):
            self.manager.apply_persona_composition(
                [{"filename": "new.md", "content": "new"}], ""
            )
        self.assertEqual(self.manager.persona_status()["files"], old_status["files"])
        self.assertIn("old", workspace.read_text())

    def test_postcommit_cleanup_faults_do_not_report_failure_or_rollback(self):
        self.manager._relay = self._local_persona_relay()
        self.manager.apply_persona_composition([{"filename": "old.md", "content": "old"}], "")

        def backup_fault(point):
            if point == "before_backup_cleanup":
                raise RuntimeError("cleanup")

        status = self.manager.apply_persona_composition(
            [{"filename": "new.md", "content": "new"}], "", _fault=backup_fault
        )
        self.assertEqual(status["files"][0]["filename"], "new.md")
        journal = Path(self.temp.name) / "ai_persona" / ".apply-journal.json"
        self.assertTrue(journal.exists())
        self.manager._recover_persona_transaction()
        self.assertFalse(journal.exists())

        def journal_fault(point):
            if point == "before_journal_unlink":
                raise RuntimeError("journal")

        status = self.manager.apply_persona_composition(
            [{"filename": "newer.md", "content": "newer"}], "", _fault=journal_fault
        )
        self.assertEqual(status["files"][0]["filename"], "newer.md")
        self.assertTrue(journal.exists())
        self.assertIn("newer", (Path(self.temp.name) / "ai_relay_workspace" / "CLAUDE.md").read_text())

        def pointer_fault(point):
            if point == "after_backup_rename":
                raise RuntimeError("pointer crash")

        status = self.manager.apply_persona_composition(
            [{"filename": "final.md", "content": "final"}], "", _fault=pointer_fault
        )
        self.assertEqual(status["files"][0]["filename"], "final.md")
        self.assertIn("final", (Path(self.temp.name) / "ai_relay_workspace" / "AGENTS.md").read_text())

    def test_zero_output_failure_retries_same_id_but_visible_failure_is_terminal(self):
        class FailingRelay(FakeRelay):
            def __init__(self, visible):
                super().__init__()
                self.visible = visible
                self.request_ids = []
                self.release.set()

            def stream_turn(self, _url, *, provider, text, handoff, execution_mode, model, request_id, emit):
                self.turns += 1
                self.last_request_id = request_id
                self.request_ids.append(request_id)
                if self.turns == 1:
                    if self.visible:
                        emit({"type": "delta", "text": "半"})
                    raise RuntimeError("broken")
                return {"type": "done", "reply": "恢复", "thinking": "", "provider": provider,
                        "activities": [], "error": ""}

        zero = FailingRelay(False)
        self.manager._relay = zero
        first = self.manager.send_message("retry", client_message_id="zero")
        self.assertTrue(first["retryable"])
        second = self.manager.send_message("retry", client_message_id="zero")
        self.assertTrue(second["ok"])
        self.assertEqual(zero.request_ids, ["zero", "zero"])
        self.assertEqual([r["role"] for r in self.manager.read_history()], ["user", "assistant"])

        visible = FailingRelay(True)
        self.manager._relay = visible
        first = self.manager.send_message_stream("visible", lambda _event: None, client_message_id="visible")
        self.assertTrue(first["terminal"])
        second = self.manager.send_message("visible", client_message_id="visible")
        self.assertTrue(second["terminal"])
        self.assertEqual(visible.turns, 1)

    def test_terminal_ambiguous_request_never_retries_same_id(self):
        class UncertainRelay(FakeRelay):
            def __init__(self):
                super().__init__()
                self.release.set()

            def stream_turn(self, _url, *, provider, text, handoff, execution_mode, model, request_id, emit):
                self.turns += 1
                raise RelayRequestUncertain("admitted without recoverable final")

        relay = UncertainRelay()
        self.manager._relay = relay
        first = self.manager.send_message("maybe", client_message_id="uncertain-id")
        self.assertFalse(first["ok"])
        self.assertEqual(first["code"], "request_uncertain")
        self.assertTrue(first["terminal"])
        self.assertFalse(first["retryable"])
        self.assertIn("作为一条新消息发送", first["error"])
        second = self.manager.send_message("maybe", client_message_id="uncertain-id")
        self.assertTrue(second["terminal"])
        self.assertFalse(second["retryable"])
        self.assertEqual(relay.turns, 1)

    def test_completed_without_cached_final_is_terminal_and_never_retried(self):
        class TerminalRelay(FakeRelay):
            def __init__(self):
                super().__init__()
                self.release.set()

            def stream_turn(self, _url, *, provider, text, handoff, execution_mode, model, request_id, emit):
                self.turns += 1
                raise RelayRequestTerminal("cached completed error")

        relay = TerminalRelay()
        self.manager._relay = relay
        first = self.manager.send_message("cached", client_message_id="terminal-id")
        self.assertEqual(first["code"], "request_terminal")
        self.assertTrue(first["terminal"])
        self.assertFalse(first["retryable"])
        second = self.manager.send_message("cached", client_message_id="terminal-id")
        self.assertTrue(second["terminal"])
        self.assertEqual(relay.turns, 1)

    def test_authoritative_done_is_durable_terminal_before_history_append(self):
        class SilentDoneRelay(FakeRelay):
            def __init__(self):
                super().__init__()
                self.release.set()

            def stream_turn(self, _url, *, provider, text, handoff, execution_mode, model, request_id, emit):
                self.turns += 1
                self.last_request_id = request_id
                return {"type": "done", "reply": "done", "thinking": "", "provider": provider,
                        "activities": [], "error": ""}

        relay = SilentDoneRelay()
        self.manager._relay = relay
        original_append = self.manager._append_history

        def fail_assistant(role, text, *args, **kwargs):
            if role == "assistant":
                raise OSError("disk full")
            return original_append(role, text, *args, **kwargs)

        with mock.patch.object(self.manager, "_append_history", side_effect=fail_assistant):
            first = self.manager.send_message("once", client_message_id="done-window")
        self.assertTrue(first["terminal"])
        second = self.manager.send_message("once", client_message_id="done-window")
        self.assertTrue(second["terminal"])
        self.assertFalse(second["retryable"])
        self.assertEqual(relay.turns, 1)

    def test_pending_refresh_recovery_fails_closed_then_recovers_before_turn(self):
        self.manager._relay = self._local_persona_relay()
        self.manager.apply_persona_composition([{"filename": "old.md", "content": "old"}], "")
        persona_root = Path(self.temp.name) / "ai_persona"
        txid = uuid.uuid4().hex
        stage = persona_root / f".stage-{txid}"
        backup = persona_root / f".backup-{uuid.uuid4().hex}"
        (stage / "files").mkdir(parents=True)
        file_id = uuid.uuid4().hex
        (stage / "files" / f"{file_id}.txt").write_text("new")
        (stage / "manifest.json").write_text(json.dumps({
            "files": [{"id": file_id, "filename": "new.md", "size": 3}],
            "custom_text": "", "updated_at": "now", "transaction_id": txid,
        }))
        (persona_root / ".apply-journal.json").write_text(json.dumps({
            "version": 1, "transaction_id": txid, "phase": "refresh_inflight",
            "stage": stage.name, "backup": backup.name,
        }))

        class RecoveringRelay(FakeRelay):
            def __init__(self):
                super().__init__()
                self.available = False

            def refresh_sessions(self, _url):
                if not self.available:
                    raise RuntimeError("relay down")
                return super().refresh_sessions(_url)

        relay = RecoveringRelay()
        relay.release.set()
        self.manager._relay = relay
        self.manager._recover_persona_transaction()  # same best-effort attempt made during init
        blocked = self.manager.send_message("blocked", client_message_id="blocked")
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["retryable"])
        self.assertEqual(relay.turns, 0)
        self.assertTrue((persona_root / ".apply-journal.json").exists())

        relay.available = True
        sent = self.manager.send_message("after recovery", client_message_id="after-recovery")
        self.assertTrue(sent["ok"])
        self.assertEqual(relay.turns, 1)
        self.assertFalse((persona_root / ".apply-journal.json").exists())
        self.assertEqual(self.manager.persona_status()["files"][0]["filename"], "new.md")


class RelayOperationLockTest(unittest.TestCase):
    def test_http_200_streamed_uncertain_and_cached_completed_are_terminal(self):
        class Response:
            def __init__(self, event): self.event = event
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self):
                return iter([(json.dumps(self.event) + "\n").encode()])

        cases = [
            ({"done": True, "full": "", "error": "terminal_uncertain",
              "request_status": "uncertain", "retryable": False}, RelayRequestUncertain),
            ({"done": True, "full": "", "error": "cached failure",
              "request_status": "completed", "retryable": False}, RelayRequestTerminal),
            ({"done": True, "full": "", "error": "",
              "request_status": "completed", "retryable": False}, RelayRequestTerminal),
        ]
        for index, (event, expected) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp:
                proxy = AISessionRelayProxy(temp)
                with mock.patch.object(proxy, "_prime_first_handoff"), \
                        mock.patch("ai_session_relay_proxy.urllib.request.urlopen", return_value=Response(event)):
                    with self.assertRaises(expected):
                        proxy.stream_turn(
                            "http://127.0.0.1:8900", provider="claude", text="hello",
                            handoff="history", request_id=f"terminal-{index}", emit=lambda _event: None,
                        )

    def test_http_409_request_uncertain_maps_to_distinct_terminal_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            proxy = AISessionRelayProxy(temp)
            body = io.BytesIO(json.dumps({
                "ok": False, "code": "request_uncertain", "error": "unknown final",
            }).encode())
            conflict = urllib.error.HTTPError(
                "http://127.0.0.1:8900/chat_stream", 409, "Conflict", {}, body
            )
            with mock.patch.object(proxy, "_prime_first_handoff"), \
                    mock.patch("ai_session_relay_proxy.urllib.request.urlopen", side_effect=conflict):
                with self.assertRaises(RelayRequestUncertain):
                    proxy.stream_turn(
                        "http://127.0.0.1:8900", provider="claude", text="hello",
                        handoff="history", request_id="uncertain-id", emit=lambda _event: None,
                    )

    def test_stream_payload_carries_stable_validated_request_id(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self):
                return iter([(json.dumps({"done": True, "full": "ok", "provider": "claude"}) + "\n").encode()])

        with tempfile.TemporaryDirectory() as temp:
            proxy = AISessionRelayProxy(temp)
            captured = {}

            def open_request(request, timeout):
                captured.update(json.loads(request.data.decode("utf-8")))
                return Response()

            with mock.patch.object(proxy, "_prime_first_handoff"), \
                    mock.patch("ai_session_relay_proxy.urllib.request.urlopen", side_effect=open_request):
                result = proxy.stream_turn(
                    "http://127.0.0.1:8900", provider="claude", text="hello",
                    handoff="history", request_id="local_123_-45", emit=lambda _event: None,
                )
            self.assertEqual(result["reply"], "ok")
            self.assertEqual(captured["request_id"], "local_123_-45")

    def test_switch_fails_instead_of_waiting_behind_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            proxy = AISessionRelayProxy(temp)
            proxy._operation_lock.acquire()
            try:
                with self.assertRaises(RelayBusyError):
                    proxy.switch_provider("http://127.0.0.1:8900", "codex")
            finally:
                proxy._operation_lock.release()

    def test_workspace_second_replace_failure_restores_both_files_durably(self):
        with tempfile.TemporaryDirectory() as temp:
            proxy = AISessionRelayProxy(temp)
            proxy.sync_persona("old")

            def fault(point):
                if point == "after_first_workspace_replace":
                    raise RuntimeError("second-file window")

            with self.assertRaisesRegex(RuntimeError, "second-file"):
                proxy.sync_persona("new", _fault=fault)
            claude = (Path(temp) / "ai_relay_workspace" / "CLAUDE.md").read_text()
            agents = (Path(temp) / "ai_relay_workspace" / "AGENTS.md").read_text()
            self.assertEqual(claude, agents)
            self.assertIn("old", claude)
            self.assertNotIn("new", claude)
            self.assertEqual((Path(temp) / "ai_relay_workspace").stat().st_mode & 0o777, 0o700)
            self.assertFalse((Path(temp) / "ai_relay_state").exists())

    def test_workspace_owner_is_fixed_name_or_current_process_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                AISessionRelayProxy(temp, workspace_owner="arbitrary-user")
            with mock.patch("ai_session_relay_proxy.pwd.getpwnam", side_effect=KeyError):
                proxy = AISessionRelayProxy(temp)
                proxy.sync_persona("fallback")
            self.assertEqual((Path(temp) / "ai_relay_workspace" / "CLAUDE.md").stat().st_uid, os.geteuid())

            account = mock.Mock(pw_uid=1234, pw_gid=5678)
            with mock.patch("ai_session_relay_proxy.pwd.getpwnam", return_value=account), \
                    mock.patch("ai_session_relay_proxy.os.chown") as chown:
                proxy = AISessionRelayProxy(temp)
                proxy.sync_persona("owned")
            chowned_names = [Path(call.args[0]).name for call in chown.call_args_list]
            self.assertIn("ai_relay_workspace", chowned_names)
            self.assertIn("CLAUDE.md.tmp", chowned_names)
            self.assertIn("AGENTS.md.tmp", chowned_names)
            self.assertTrue(all(call.args[1:] == (1234, 5678) for call in chown.call_args_list))

    def test_workspace_owner_is_resolved_again_if_user_appears_after_init(self):
        with tempfile.TemporaryDirectory() as temp:
            account = mock.Mock(pw_uid=2468, pw_gid=1357)
            with mock.patch(
                "ai_session_relay_proxy.pwd.getpwnam",
                side_effect=[KeyError, account, account, account],
            ), mock.patch("ai_session_relay_proxy.os.chown") as chown:
                proxy = AISessionRelayProxy(temp)
                self.assertIsNone(proxy._workspace_identity)
                proxy.sync_persona("late owner")
            self.assertEqual(proxy._workspace_identity, (2468, 1357))
            self.assertGreaterEqual(chown.call_count, 3)
            self.assertTrue(all(call.args[1:] == (2468, 1357) for call in chown.call_args_list))

    def test_refresh_marks_next_turn_uninitialized_and_pending_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            proxy = AISessionRelayProxy(temp)
            proxy._save_proxy_state({"initialized": True, "pending_switch": False, "provider": "claude"})
            with mock.patch.object(proxy, "_json_request", return_value={
                "ok": True, "provider": "claude", "epoch": 4,
            }) as request:
                result = proxy.refresh_sessions("http://127.0.0.1:8900")
            self.assertEqual(result["provider"], "claude")
            self.assertEqual(request.call_args.args[0], "http://127.0.0.1:8900/refresh")
            state = proxy._load_proxy_state()
            self.assertFalse(state["initialized"])
            self.assertTrue(state["pending_switch"])


if __name__ == "__main__":
    unittest.main()
