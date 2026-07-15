import json
import os
import socket
import signal
import stat
import subprocess
import threading
import time
import tempfile
import unittest
import importlib.util
import html
import io
from pathlib import Path
from unittest import mock

from ai_chat import AIChatManager
from xia_claude_channel import (
    XiaClaudeChannelClient,
    XiaChannelUncertain,
    XiaChannelUnavailable,
    validate_channel_token_file,
    validate_channel_url,
)


class FakeRelay:
    def __init__(self):
        self.provider = "claude"
        self.epoch = 7
        self.turn_active = False
    def status(self, _url):
        return {"ok": True, "provider": self.provider, "epoch": self.epoch,
                "claude_available": True, "codex_available": True, "turn_active": False}
    def sync_persona(self, *_args): pass
    def list_models(self, _url, _provider): return {"models": [], "dynamic": False}
    def switch_provider(self, _url, provider):
        changed = provider != self.provider
        if changed: self.provider = provider; self.epoch += 1
        return {"ok": True, "provider": provider, "epoch": self.epoch, "changed": changed}
    def refresh_sessions(self, _url): self.epoch += 1; return {"ok": True, "epoch": self.epoch}
    def stream_turn(self, _url, **kwargs):
        kwargs["emit"]({"type": "delta", "text": "codex delta"})
        return {"reply": "codex final", "activities": [], "thinking": "", "error": ""}


class FakeChannel:
    def __init__(self):
        self.calls = []
        self.revokes = []
        self.generation = 0
        self.model = ""
        self.reply = "Claude final"
        self.error = None
        self.wait_during_generation = False
    def health(self):
        return {"ok": True, "ready": True, "mcp_connected": True,
                "generation": self.generation, "model": self.model, "session_id": "session"}
    def revoke(self, **kwargs): self.revokes.append(kwargs); return {"ok": True}
    def ensure_generation(self, *, generation, model, timeout_seconds, on_wait=None):
        fresh = self.generation != generation or self.model != model
        if self.wait_during_generation and on_wait: on_wait()
        self.generation = generation; self.model = model
        return {**self.health(), "fresh": fresh}
    def send_and_wait(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        callback = kwargs.get("on_admitted")
        if callback: callback()
        if kwargs.get("simulate_wait") and kwargs.get("on_wait"): kwargs["on_wait"]()
        return {"ok": True, "status": "completed", "reply": self.reply}


class XiaChannelManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = AIChatManager(self.temp.name)
        self.relay = FakeRelay()
        self.channel = FakeChannel()
        self.manager._relay = self.relay
        self.manager.configure_relay({"claude_transport": "channel"})
        self.manager._channel = self.channel

    def tearDown(self): self.temp.cleanup()

    def test_claude_channel_emits_no_assistant_delta_and_backend_is_only_history_writer(self):
        events = []
        result = self.manager.send_message_stream("hello", events.append, client_message_id="channel-1")
        self.assertTrue(result["ok"])
        self.assertEqual([event["type"] for event in events], ["user"])
        self.assertEqual([r["role"] for r in self.manager.read_history()], ["user", "assistant"])
        self.assertEqual(self.manager.read_history()[-1]["provider"], "claude-channel")
        self.assertGreaterEqual(self.channel.calls[0]["epoch"], 7)
        self.assertTrue(self.channel.calls[0]["handoff"])

    def test_backend_crash_after_channel_result_retrieves_cached_same_grant(self):
        original = self.manager._append_history
        crashed = False
        def crash_once(role, text, *args, **kwargs):
            nonlocal crashed
            if role == "assistant" and not crashed:
                crashed = True
                raise SystemExit("simulated backend crash")
            return original(role, text, *args, **kwargs)
        with mock.patch.object(self.manager, "_append_history", side_effect=crash_once):
            with self.assertRaises(SystemExit):
                self.manager.send_message("once", client_message_id="crash-window")
        result = self.manager.send_message("once", client_message_id="crash-window")
        self.assertTrue(result["ok"])
        self.assertEqual(len([r for r in self.manager.read_history() if r["role"] == "user"]), 1)
        self.assertEqual(self.channel.calls[0]["lease"], self.channel.calls[1]["lease"])
        self.assertEqual(self.channel.calls[0]["epoch"], self.channel.calls[1]["epoch"])

    def test_provider_roundtrip_needs_handoff_without_fresh_generation(self):
        first = self.manager.send_message("first", client_message_id="first")
        self.assertTrue(first["ok"])
        generation = self.manager._load_channel_route_state()["generation"]
        self.manager.switch_relay_provider("codex")
        self.manager.send_message("codex", client_message_id="codex")
        self.manager.switch_relay_provider("claude")
        second = self.manager.send_message("back", client_message_id="back")
        self.assertTrue(second["ok"])
        self.assertEqual(self.manager._load_channel_route_state()["generation"], generation)
        self.assertTrue(self.channel.calls[-1]["handoff"])

    def test_handoff_is_consumed_once_during_ordinary_same_session_turns(self):
        self.assertTrue(self.manager.send_message("one", client_message_id="one")["ok"])
        self.assertTrue(self.channel.calls[-1]["handoff"])
        generation = self.channel.generation
        self.assertTrue(self.manager.send_message("two", client_message_id="two")["ok"])
        self.assertEqual(self.channel.calls[-1]["handoff"], "")
        self.assertEqual(self.channel.generation, generation)

    def test_transport_roundtrip_forces_authoritative_handoff(self):
        self.assertTrue(self.manager.send_message("channel one", client_message_id="transport-one")["ok"])
        with mock.patch.object(self.manager, "_make_channel_client", return_value=self.channel):
            self.manager.configure_relay({"claude_transport": "relay"})
        self.assertTrue(self.manager.send_message("relay middle", client_message_id="transport-middle")["ok"])
        with mock.patch.object(self.manager, "_make_channel_client", return_value=self.channel):
            self.manager.configure_relay({"claude_transport": "channel"})
        self.assertTrue(self.manager.send_message("channel back", client_message_id="transport-back")["ok"])
        self.assertIn("relay middle", self.channel.calls[-1]["handoff"])

    def test_transport_change_is_idle_fenced_and_unresolved_change_is_atomic(self):
        self.manager._send_lock.acquire()
        try:
            with self.assertRaisesRegex(Exception, "while a turn is active"):
                self.manager.configure_relay({"claude_transport": "relay"})
        finally:
            self.manager._send_lock.release()
        self.assertEqual(self.manager.relay_config_snapshot()["claude_transport"], "channel")
        self.manager._set_request_state("pending-transport", "pending", channel_lease="lease")
        with self.assertRaisesRegex(Exception, "unresolved"):
            self.manager.configure_relay({"claude_transport": "relay"})
        self.assertEqual(self.manager.relay_config_snapshot()["claude_transport"], "channel")

    def test_transport_config_disk_failure_rolls_back_in_memory_transport(self):
        with mock.patch.object(self.manager, "_make_channel_client", return_value=self.channel), \
                mock.patch.object(self.manager, "_save_config", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.manager.configure_relay({"claude_transport": "relay"})
        self.assertEqual(self.manager.relay_config_snapshot()["claude_transport"], "channel")
        self.assertTrue(self.manager._load_channel_route_state()["needs_handoff"])

    def test_external_relay_epoch_change_forces_handoff(self):
        self.assertTrue(self.manager.send_message("one", client_message_id="epoch-one")["ok"])
        self.relay.epoch += 1
        self.assertTrue(self.manager.send_message("two", client_message_id="epoch-two")["ok"])
        self.assertTrue(self.channel.calls[-1]["handoff"])

    def test_channel_wait_keepalive_is_not_text_delta_or_activity(self):
        original = self.channel.send_and_wait
        def slow(**kwargs):
            kwargs["on_admitted"]()
            kwargs["on_wait"]()
            self.channel.calls.append(kwargs)
            return {"ok": True, "status": "completed", "reply": "slow final"}
        self.channel.send_and_wait = slow
        events = []
        result = self.manager.send_message_stream("slow", events.append, client_message_id="slow")
        self.assertTrue(result["ok"])
        self.assertEqual([event["type"] for event in events], ["user", "keepalive"])

    def test_generation_rotation_emits_keepalive_without_visible_content(self):
        self.channel.wait_during_generation = True
        events = []
        result = self.manager.send_message_stream("rotate slow", events.append, client_message_id="rotate-slow")
        self.assertTrue(result["ok"])
        self.assertEqual([event["type"] for event in events], ["user", "keepalive"])

    def test_model_and_persona_changes_increment_generation_only_on_change(self):
        before = self.manager._load_channel_route_state()["generation"]
        self.manager.select_relay_model("claude", "opus")
        after_model = self.manager._load_channel_route_state()["generation"]
        self.assertEqual(after_model, before + 1)
        self.manager.select_relay_model("claude", "opus")
        self.assertEqual(self.manager._load_channel_route_state()["generation"], after_model)
        self.manager.apply_persona_composition([{"filename": "persona.md", "content": "gentle"}], "")
        self.assertEqual(self.manager._load_channel_route_state()["generation"], after_model + 1)

    def test_channel_uncertain_is_terminal_and_same_id_is_not_replayed(self):
        self.channel.error = XiaChannelUncertain("crashed")
        first = self.manager.send_message("maybe", client_message_id="uncertain")
        self.assertTrue(first["terminal"]); self.assertFalse(first["retryable"])
        self.channel.error = None
        second = self.manager.send_message("maybe", client_message_id="uncertain")
        self.assertTrue(second["terminal"])
        self.assertEqual(len(self.channel.calls), 1)

    def test_codex_path_remains_streamed(self):
        self.manager.switch_relay_provider("codex")
        events = []
        result = self.manager.send_message_stream("c", events.append, client_message_id="codex-stream")
        self.assertTrue(result["ok"])
        self.assertIn({"type": "delta", "text": "codex delta"}, events)


class XiaChannelClientSafetyTest(unittest.TestCase):
    def test_loopback_and_private_token_file(self):
        self.assertEqual(validate_channel_url("http://127.0.0.1:8821/"), "http://127.0.0.1:8821")
        with self.assertRaises(ValueError): validate_channel_url("https://example.com")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "token"
            path.write_text("secret")
            os.chmod(path, 0o600)
            self.assertEqual(validate_channel_token_file(path), path)
            os.chmod(path, 0o644)
            with self.assertRaises(XiaChannelUnavailable): validate_channel_token_file(path)
            os.chmod(path, 0o600)
            link = Path(temp) / "link"; link.symlink_to(path)
            with self.assertRaises(XiaChannelUnavailable): validate_channel_token_file(link)
            with self.assertRaises(XiaChannelUnavailable): validate_channel_token_file(Path(temp) / "missing")

    def test_http_error_codes_map_to_terminal_uncertain(self):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / "token"; token.write_text("secret"); os.chmod(token, 0o600)
            client = XiaClaudeChannelClient("http://127.0.0.1:8821", token_file=token)
            error = __import__('urllib.error').error.HTTPError(
                "http://127.0.0.1:8821/messages", 409, "Conflict", {},
                __import__('io').BytesIO(json.dumps({"code": "request_uncertain", "error": "uncertain"}).encode()),
            )
            with mock.patch.object(client._opener, "open", side_effect=error):
                with self.assertRaises(XiaChannelUncertain): client.health()

    def test_generation_and_result_unavailable_loops_emit_wait_signals(self):
        client = XiaClaudeChannelClient("http://127.0.0.1:8821", token="secret")
        ready = {"ok": True, "ready": True, "generation": 2, "model": "opus", "session_id": "s"}
        old = {**ready, "generation": 1}
        notices = []
        with mock.patch.object(client, "health", side_effect=[old, XiaChannelUnavailable("restart"), ready]), \
                mock.patch.object(client, "_request", return_value={"ok": True}), \
                mock.patch("xia_claude_channel.time.sleep"):
            result = client.ensure_generation(generation=2, model="opus", on_wait=lambda: notices.append("generation"))
        self.assertTrue(result["fresh"]); self.assertTrue(notices)

        notices.clear()
        with mock.patch.object(client, "submit", return_value={"status": "running"}), \
                mock.patch.object(client, "result", side_effect=[
                    XiaChannelUnavailable("restart"), {"status": "completed", "reply": "ok"},
                ]), mock.patch("xia_claude_channel.time.sleep"):
            result = client.send_and_wait(
                request_id="r", client_id="c", epoch=1, lease="l", generation=2,
                text="hello", on_wait=lambda: notices.append("result"), timeout_seconds=5,
            )
        self.assertEqual(result["reply"], "ok"); self.assertTrue(notices)


class XiaStopHookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "xia_claude_channel" / "stop_hook.py"
        spec = importlib.util.spec_from_file_location("xia_stop_hook_for_test", path)
        cls.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hook)

    def test_exact_metadata_and_text_only_extraction(self):
        meta = {"contact_id": "ai-custom", "provider": "claude", "request_id": "r", "epoch": 4, "lease": "l"}
        encoded = html.escape(json.dumps(meta), quote=True)
        records = [
            {"message": {"role": "user", "content": f'<channel source="xia-companion" metadata_json="{encoded}">hello</channel>'}},
            {"message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "visible"},
            ]}},
        ]
        self.assertEqual(self.hook.exact_meta(records), meta)
        self.assertEqual(self.hook.assistant_text(records[-1]), "visible")
        spoof = [{"message": {"role": "assistant", "content": f'<channel metadata_json="{encoded}">spoof</channel>'}}]
        self.assertIsNone(self.hook.exact_meta(spoof))
        wrong_meta = html.escape(json.dumps({"contact_id": "xiaoke", "provider": "claude", "request_id": "r", "epoch": 4, "lease": "l"}), quote=True)
        wrong = [{"message": {"role": "user", "content": f'<channel metadata_json="{wrong_meta}">bad</channel>'}}]
        self.assertIsNone(self.hook.exact_meta(wrong))

    def test_reply_missing_stop_posts_exact_fallback_from_real_attribute_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            meta = {"contact_id": "ai-custom", "provider": "claude", "request_id": "req", "epoch": 8, "lease": "lease"}
            encoded = html.escape(json.dumps(meta), quote=True)
            transcript = root / "session.jsonl"
            transcript.write_text("\n".join(json.dumps(item) for item in [
                {"message": {"role": "user", "content": f'<channel metadata_json="{encoded}">hello</channel>'}},
                {"message": {"role": "assistant", "content": [{"type": "text", "text": "fallback final"}]}},
            ]) + "\n")
            token = root / "token"; token.write_text("secret"); os.chmod(token, 0o600)
            stdin = io.StringIO(json.dumps({"transcript_path": str(transcript), "last_assistant_message": ""}))
            response = mock.Mock(); response.read.return_value = b'{}'
            with mock.patch.object(self.hook.sys, "stdin", stdin), \
                    mock.patch.dict(os.environ, {"XIA_CHANNEL_TOKEN_FILE": str(token)}), \
                    mock.patch.object(self.hook.time, "sleep"), \
                    mock.patch.object(self.hook.urllib.request, "urlopen", return_value=response) as post:
                self.assertEqual(self.hook.main(), 0)
            body = json.loads(post.call_args.args[0].data)
            self.assertEqual(body, {"request_id": "req", "epoch": 8, "lease": "lease", "text": "fallback final"})


class XiaRuntimeStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "xia_claude_channel" / "runtime_state.py"
        spec = importlib.util.spec_from_file_location("xia_runtime_state_for_test", path)
        cls.runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.runtime)

    def test_transcript_is_authoritative_for_fresh_resume_even_if_marker_is_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            session = "00000000-0000-4000-8000-000000000001"
            self.assertFalse(self.runtime.has_transcript(temp, session))
            transcript = Path(temp) / ".claude" / "projects" / "xia" / f"{session}.jsonl"
            transcript.parent.mkdir(parents=True); transcript.write_text("{}\n")
            self.assertTrue(self.runtime.has_transcript(temp, session))
            marker = Path(temp) / "state" / "current-session.json"
            self.runtime.write_marker(marker, 2, session, "opus")
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            marker.unlink()
            self.assertTrue(self.runtime.has_transcript(temp, session))

    def test_fresh_generations_share_stable_trusted_project_and_publish_new_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "install"; install.mkdir()
            (install / ".mcp.json.in").write_text(json.dumps({"mcpServers": {"xia-companion": {
                "command": "@INSTALL_DIR@/server.mjs", "env": {
                    "state": "@STATE_DIR@", "token": "@TOKEN_FILE@", "generation": "@GENERATION@",
                    "session": "@SESSION_ID@", "model": "@MODEL@", "bootstrap": "@BOOTSTRAP_TOKEN@",
                }}}}))
            (install / "settings.json").write_text('{"permissions":{"allow":[]}}\n')
            state = root / "state"; state.mkdir(mode=0o700)
            workspace = root / "workspace"; workspace.mkdir(); (workspace / "CLAUDE.md").write_text("persona")
            legacy = state / "runtime-1"; legacy.mkdir(mode=0o700); (legacy / "old").write_text("old")

            first = self.runtime.prepare_runtime_workspace(
                install, state, workspace, 1, "00000000-0000-4000-8000-000000000001", "opus", "",
            )
            self.assertEqual(first, state / "runtime")
            self.assertFalse(legacy.exists())
            self.assertEqual(first.stat().st_mode & 0o777, 0o700)
            self.assertEqual((first / ".mcp.json").stat().st_mode & 0o777, 0o600)
            trust_identity = str(first.resolve())

            second = self.runtime.prepare_runtime_workspace(
                install, state, workspace, 2, "00000000-0000-4000-8000-000000000002", "sonnet", "bootstrap-2",
            )
            self.assertEqual(str(second.resolve()), trust_identity)
            config = json.loads((second / ".mcp.json").read_text())["mcpServers"]["xia-companion"]
            self.assertEqual(config["env"]["generation"], "2")
            self.assertEqual(config["env"]["session"], "00000000-0000-4000-8000-000000000002")
            self.assertEqual(config["env"]["model"], "sonnet")
            self.assertEqual(config["env"]["bootstrap"], "bootstrap-2")
            self.assertEqual((second / "CLAUDE.md").resolve(), (workspace / "CLAUDE.md").resolve())
            self.assertFalse((state / "runtime-2").exists())

    def test_unsafe_legacy_runtime_symlink_is_not_cleaned_or_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root / "state"; state.mkdir(mode=0o700)
            victim = root / "victim"; victim.mkdir(); (victim / "keep").write_text("safe")
            (state / "runtime-9").symlink_to(victim, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unsafe legacy"):
                self.runtime._cleanup_legacy_runtimes(state)
            self.assertEqual((victim / "keep").read_text(), "safe")

    def _tmux_socket(self, root):
        path = Path(root) / "tmux.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); listener.bind(str(path))
        return listener, path

    def test_kill_session_failure_never_calls_runtime_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            listener, path = self._tmux_socket(temp)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            def runner(args, **_kwargs):
                command = args[3]
                if command == "has-session": return subprocess.CompletedProcess(args, 0, "", "")
                if command == "list-panes": return subprocess.CompletedProcess(args, 0, "123\n", "")
                if command == "kill-session": return subprocess.CompletedProcess(args, 1, "", "denied")
                raise AssertionError(command)
            try:
                with mock.patch.object(self.runtime, "_capture_process_guard", return_value={"identities": {}, "pgrps": {}}):
                    with self.assertRaisesRegex(RuntimeError, "kill-session failed"):
                        self.runtime.prepare_after_stop(
                            lambda: self.runtime.stop_dedicated_tui(
                                path, "xia-claude", runner=runner,
                                server_guard_capture=lambda *_args, **_kwargs: {"pid": 9, "starttime": 1, "socket_dev": 1, "socket_ino": 1},
                            ), prepare,
                        )
                prepare.assert_not_called()
            finally:
                listener.close()

    def test_has_session_connection_failure_never_calls_runtime_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            listener, path = self._tmux_socket(temp)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            def runner(args, **_kwargs):
                command = args[3]
                if command in {"has-session", "list-sessions"}:
                    return subprocess.CompletedProcess(args, 1, "", "connection refused")
                raise AssertionError(command)
            try:
                with self.assertRaisesRegex(RuntimeError, "session query failed"):
                    self.runtime.prepare_after_stop(
                        lambda: self.runtime.stop_dedicated_tui(path, "xia-claude", runner=runner), prepare,
                    )
                prepare.assert_not_called()
            finally:
                listener.close()

    def test_session_disappears_but_old_process_and_health_delay_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            listener, path = self._tmux_socket(temp)
            killed = False
            def runner(args, **_kwargs):
                nonlocal killed
                command = args[3]
                if command == "has-session":
                    return subprocess.CompletedProcess(args, 1 if killed else 0, "", "")
                if command == "list-panes": return subprocess.CompletedProcess(args, 0, "123\n", "")
                if command == "kill-session":
                    killed = True
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "list-sessions": return subprocess.CompletedProcess(args, 0, "", "")
                raise AssertionError(command)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            alive = mock.Mock(side_effect=[True, False])
            port = mock.Mock(side_effect=[True, False])
            sleeps = mock.Mock()
            try:
                with mock.patch.object(self.runtime, "_capture_process_guard", return_value={"identities": {}, "pgrps": {}}), \
                        mock.patch.object(self.runtime, "_process_guard_alive", side_effect=alive):
                    result = self.runtime.prepare_after_stop(
                        lambda: self.runtime.stop_dedicated_tui(
                            path, "xia-claude", runner=runner, port_probe=port,
                            monotonic=mock.Mock(side_effect=[0.0, 0.1]), sleeper=sleeps,
                            server_guard_capture=lambda *_args, **_kwargs: {"pid": 9, "starttime": 1, "socket_dev": 1, "socket_ino": 1},
                        ), prepare,
                    )
                self.assertEqual(result, Path(temp) / "runtime")
                self.assertEqual(alive.call_count, 2); self.assertEqual(port.call_count, 2)
                sleeps.assert_called_once_with(0.1); prepare.assert_called_once()
            finally:
                listener.close()

    def test_process_starttime_distinguishes_live_process_from_pid_reuse(self):
        guard = {"identities": {123: (123, 1000)}, "pgrps": {123: 1000}}
        live = lambda pid: (1, 123, 1000) if pid == 123 else None
        reused = lambda pid: (1, 123, 2000) if pid == 123 else None
        self.assertTrue(self.runtime._process_guard_alive(guard, proc_reader=live))
        self.assertFalse(self.runtime._process_guard_alive(guard, proc_reader=reused))

    def test_post_kill_query_connection_failure_waits_then_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            listener, path = self._tmux_socket(temp)
            killed = False; post_probes = 0
            def runner(args, **_kwargs):
                nonlocal killed, post_probes
                command = args[3]
                if command == "has-session":
                    return subprocess.CompletedProcess(
                        args, 1 if killed else 0, "", "connection refused" if killed else "",
                    )
                if command == "list-panes": return subprocess.CompletedProcess(args, 0, "123\n", "")
                if command == "kill-session": killed = True; return subprocess.CompletedProcess(args, 0, "", "")
                if command == "list-sessions":
                    post_probes += 1
                    return subprocess.CompletedProcess(
                        args, 1 if post_probes == 1 else 0, "",
                        "connection refused" if post_probes == 1 else "",
                    )
                raise AssertionError(command)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            try:
                with mock.patch.object(self.runtime, "_capture_process_guard", return_value={"identities": {}, "pgrps": {}}), \
                        mock.patch.object(self.runtime, "_process_guard_alive", return_value=False):
                    result = self.runtime.prepare_after_stop(
                        lambda: self.runtime.stop_dedicated_tui(
                            path, "xia-claude", runner=runner, port_probe=lambda *_: False,
                            monotonic=mock.Mock(side_effect=[0.0, 0.1]), sleeper=lambda _delay: None,
                            server_guard_capture=lambda *_args, **_kwargs: {"pid": 9, "starttime": 1, "socket_dev": 1, "socket_ino": 1},
                            stale_socket_retire=lambda *_args, **_kwargs: False,
                        ), prepare,
                    )
                self.assertEqual(result, Path(temp) / "runtime")
                self.assertEqual(post_probes, 2); prepare.assert_called_once()
            finally:
                listener.close()

    def test_post_kill_permission_failure_fails_closed_without_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            listener, path = self._tmux_socket(temp)
            killed = False
            def runner(args, **_kwargs):
                nonlocal killed
                command = args[3]
                if command == "has-session":
                    if killed:
                        return subprocess.CompletedProcess(args, 1, "", "permission denied")
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "list-panes": return subprocess.CompletedProcess(args, 0, "123\n", "")
                if command == "kill-session": killed = True; return subprocess.CompletedProcess(args, 0, "", "")
                if command == "list-sessions":
                    return subprocess.CompletedProcess(args, 1, "", "permission denied")
                raise AssertionError(command)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            try:
                with mock.patch.object(self.runtime, "_capture_process_guard", return_value={"identities": {}, "pgrps": {}}):
                    with self.assertRaisesRegex(RuntimeError, "session query failed"):
                        self.runtime.prepare_after_stop(
                            lambda: self.runtime.stop_dedicated_tui(
                                path, "xia-claude", runner=runner,
                                server_guard_capture=lambda *_args, **_kwargs: {
                                    "pid": 9, "starttime": 1, "socket_dev": 1, "socket_ino": 1,
                                },
                            ), prepare,
                        )
                prepare.assert_not_called()
            finally:
                listener.close()

    def _assert_mixed_post_kill_query_failure_does_not_prepare(self, listing_stderr):
        with tempfile.TemporaryDirectory() as temp:
            listener, path = self._tmux_socket(temp)
            killed = False
            def runner(args, **_kwargs):
                nonlocal killed
                command = args[3]
                if command == "has-session":
                    return subprocess.CompletedProcess(
                        args, 1 if killed else 0, "", "connection refused" if killed else "",
                    )
                if command == "list-panes": return subprocess.CompletedProcess(args, 0, "123\n", "")
                if command == "kill-session": killed = True; return subprocess.CompletedProcess(args, 0, "", "")
                if command == "list-sessions":
                    return subprocess.CompletedProcess(args, 1, "", listing_stderr)
                raise AssertionError(command)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            try:
                with mock.patch.object(self.runtime, "_capture_process_guard", return_value={"identities": {}, "pgrps": {}}):
                    with self.assertRaisesRegex(RuntimeError, "session query failed"):
                        self.runtime.prepare_after_stop(
                            lambda: self.runtime.stop_dedicated_tui(
                                path, "xia-claude", runner=runner,
                                server_guard_capture=lambda *_args, **_kwargs: {
                                    "pid": 9, "starttime": 1, "socket_dev": 1, "socket_ino": 1,
                                },
                            ), prepare,
                        )
                prepare.assert_not_called()
            finally:
                listener.close()

    def test_post_kill_transient_probe_plus_unknown_listing_fails_closed(self):
        self._assert_mixed_post_kill_query_failure_does_not_prepare("unexpected tmux failure")

    def test_post_kill_transient_probe_plus_malformed_listing_fails_closed(self):
        self._assert_mixed_post_kill_query_failure_does_not_prepare("protocol error: malformed response")

    def test_control_is_read_after_stop_and_new_generation_drives_publish_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); state = root / "state"; state.mkdir(mode=0o700)
            first = {"version": 1, "generation": 1, "session_id": "00000000-0000-4000-8000-000000000001",
                     "model": "opus", "bootstrap_token": "old"}
            second = {"version": 1, "generation": 2, "session_id": "00000000-0000-4000-8000-000000000002",
                      "model": "sonnet", "bootstrap_token": "new"}
            (state / "control.json").write_text(json.dumps(first))
            published = []
            def stop(): (state / "control.json").write_text(json.dumps(second))
            def prepare(_install, _state, _workspace, generation, session_id, model, bootstrap):
                published.append((generation, session_id, model, bootstrap)); return state / "runtime"
            result = self.runtime.prepare_controlled_runtime_after_stop(
                root / "socket", "xia-claude", root / "install", state, root / "workspace",
                stop_action=stop, prepare_action=prepare,
            )
            self.assertEqual(published, [(2, second["session_id"], "sonnet", "new")])
            self.assertEqual(result["generation"], 2); self.assertEqual(result["session_id"], second["session_id"])
            self.assertEqual(result["model"], "sonnet"); self.assertEqual(result["bootstrap_token"], "new")

    def test_real_tmux_sleep_pane_stops_and_allows_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "tmux.sock"
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            for attempt in range(10):
                session = f"xia-real-stop-{attempt}"
                subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "new-session", "-d", "-s", session,
                                "/bin/sleep", "30"], check=True)
                try:
                    result = self.runtime.prepare_after_stop(
                        lambda: self.runtime.stop_dedicated_tui(
                            socket_path, session, timeout=2, port_probe=lambda *_: False,
                        ), prepare,
                    )
                    self.assertEqual(result, Path(temp) / "runtime")
                finally:
                    subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "kill-server"], capture_output=True)
            self.assertEqual(prepare.call_count, 10)

    def test_real_tmux_ignore_hup_pane_times_out_without_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "tmux.sock"; session = "xia-real-stubborn"
            code = "import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
            subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "new-session", "-d", "-s", session,
                            "/usr/bin/python3", "-c", code], check=True)
            pane_pid = int(subprocess.run(
                ["/usr/bin/tmux", "-S", str(socket_path), "list-panes", "-t", session, "-F", "#{pane_pid}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip())
            pane_identity = self.runtime._proc_stat(pane_pid)
            self.assertIsNotNone(pane_identity)
            self.assertNotEqual(pane_identity[1], os.getpgrp())
            time.sleep(0.1)
            prepare = mock.Mock(return_value=Path(temp) / "runtime")
            try:
                with self.assertRaisesRegex(RuntimeError, "did not fully exit"):
                    self.runtime.prepare_after_stop(
                        lambda: self.runtime.stop_dedicated_tui(
                            socket_path, session, timeout=0.3, port_probe=lambda *_: False,
                        ), prepare,
                    )
                prepare.assert_not_called()
            finally:
                current = self.runtime._proc_stat(pane_pid)
                if current is not None and current[2] == pane_identity[2]:
                    try: os.killpg(pane_identity[1], signal.SIGKILL)
                    except ProcessLookupError: pass
                subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "kill-server"], capture_output=True)

    def test_real_tmux_rotate_during_process_and_port_drain_publishes_latest_control(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); socket_path = root / "tmux.sock"; session = "xia-real-rotate"
            state = root / "state"; state.mkdir(mode=0o700)
            first = {"version": 1, "generation": 1, "session_id": "00000000-0000-4000-8000-000000000001",
                     "model": "opus", "bootstrap_token": "old"}
            second = {"version": 1, "generation": 2, "session_id": "00000000-0000-4000-8000-000000000002",
                      "model": "sonnet", "bootstrap_token": "new"}
            (state / "control.json").write_text(json.dumps(first))
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
            code = (
                "import signal,socket,time; "
                "signal.signal(signal.SIGHUP,signal.SIG_IGN); "
                "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
                f"s.bind(('127.0.0.1',{port})); s.listen(); time.sleep(30)"
            )
            subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "new-session", "-d", "-s", session,
                            "/usr/bin/python3", "-c", code], check=True)
            pane_pid = int(subprocess.run(
                ["/usr/bin/tmux", "-S", str(socket_path), "list-panes", "-t", session, "-F", "#{pane_pid}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip())
            identity = self.runtime._proc_stat(pane_pid)
            self.assertIsNotNone(identity); pane_pgrp = identity[1]
            self.assertNotEqual(pane_pgrp, os.getpgrp())
            deadline = time.monotonic() + 2
            while not self.runtime._port_open("127.0.0.1", port):
                if time.monotonic() >= deadline: self.fail("test pane did not open its port")
                time.sleep(0.01)

            rotated_while_draining = []
            def rotate_after_tmux_disappears():
                while subprocess.run(
                    ["/usr/bin/tmux", "-S", str(socket_path), "has-session", "-t", session],
                    capture_output=True,
                ).returncode == 0:
                    time.sleep(0.01)
                rotated_while_draining.append(self.runtime._port_open("127.0.0.1", port))
                (state / "control.json").write_text(json.dumps(second))
                os.killpg(pane_pgrp, signal.SIGTERM)

            rotator = threading.Thread(target=rotate_after_tmux_disappears, daemon=True)
            rotator.start()
            published = []
            def prepare(_install, _state, _workspace, generation, session_id, model, bootstrap):
                published.append((generation, session_id, model, bootstrap,
                                  self.runtime._port_open("127.0.0.1", port)))
                return state / "runtime"
            try:
                result = self.runtime.prepare_controlled_runtime_after_stop(
                    socket_path, session, root / "install", state, root / "workspace",
                    stop_action=lambda: self.runtime.stop_dedicated_tui(
                        socket_path, session, port=port, timeout=2,
                    ),
                    prepare_action=prepare,
                )
                rotator.join(timeout=2); self.assertFalse(rotator.is_alive())
                self.assertEqual(rotated_while_draining, [True])
                self.assertEqual(published, [(2, second["session_id"], "sonnet", "new", False)])
                self.assertEqual(result["generation"], 2)
            finally:
                current = self.runtime._proc_stat(pane_pid)
                if current is not None and current[2] == identity[2]:
                    try: os.killpg(pane_pgrp, signal.SIGKILL)
                    except ProcessLookupError: pass
                subprocess.run(["/usr/bin/tmux", "-S", str(socket_path), "kill-server"], capture_output=True)


class XiaPrepareRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "xia_claude_channel" / "prepare_runtime.py"
        spec = importlib.util.spec_from_file_location("xia_prepare_runtime_for_test", path)
        cls.prepare = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.prepare)

    def make_runtime(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        template = root / "claude-home.json"
        template.write_text('{"hasCompletedOnboarding":true}\n')
        os.chmod(template, 0o644)
        kwargs = dict(root=root, relay_uid=os.getuid(), relay_gid=os.getgid(),
                      root_uid=os.getuid(), root_gid=os.getgid(), onboarding_template=template)
        self.prepare.prepare_runtime(**kwargs)
        return temporary, root, kwargs

    def test_correct_config_dir_private_files_and_idempotent_no_overwrite(self):
        temporary, root, kwargs = self.make_runtime()
        try:
            state = root / "var/lib/cc-xia-relay/channel-state"
            config = root / "var/lib/cc-xia-relay/claude-channel-home/.claude/.claude.json"
            wrong = root / "var/lib/cc-xia-relay/claude-channel-home/.claude.json"
            self.assertTrue(config.is_file()); self.assertFalse(wrong.exists())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual((state / "channel.token").stat().st_mode & 0o777, 0o600)
            self.assertEqual((state / "tmux" / f"tmux-{os.getuid()}").stat().st_mode & 0o777, 0o700)
            customized = '{"hasCompletedOnboarding":true,"theme":"dark"}\n'
            config.write_text(customized); os.chmod(config, 0o600)
            self.prepare.prepare_runtime(**kwargs)
            self.assertEqual(config.read_text(), customized)
        finally:
            temporary.cleanup()

    def test_symlink_and_dangling_symlink_files_fail_closed(self):
        for target_name, relative in [
            ("token", "var/lib/cc-xia-relay/channel-state/channel.token"),
            ("onboarding", "var/lib/cc-xia-relay/claude-channel-home/.claude/.claude.json"),
        ]:
            for dangling in (False, True):
                with self.subTest(target=target_name, dangling=dangling):
                    temporary, root, kwargs = self.make_runtime()
                    try:
                        target = root / relative
                        target.unlink()
                        destination = root / ("missing" if dangling else "victim")
                        if not dangling:
                            destination.write_text("do not touch")
                        target.symlink_to(destination)
                        with self.assertRaises(self.prepare.PrepareError):
                            self.prepare.prepare_runtime(**kwargs)
                        if not dangling:
                            self.assertEqual(destination.read_text(), "do not touch")
                    finally:
                        temporary.cleanup()

    def test_symlink_directory_wrong_permissions_and_wrong_owner_fail_closed(self):
        temporary, root, kwargs = self.make_runtime()
        try:
            tmux = root / "var/lib/cc-xia-relay/channel-state/tmux"
            (tmux / f"tmux-{os.getuid()}").rmdir()
            tmux.rmdir(); tmux.symlink_to(root / "outside", target_is_directory=True)
            with self.assertRaises(self.prepare.PrepareError):
                self.prepare.prepare_runtime(**kwargs)
        finally:
            temporary.cleanup()

        temporary, root, kwargs = self.make_runtime()
        try:
            token = root / "var/lib/cc-xia-relay/channel-state/channel.token"
            os.chmod(token, 0o644)
            with self.assertRaisesRegex(self.prepare.PrepareError, "permissions"):
                self.prepare.prepare_runtime(**kwargs)
            os.chmod(token, 0o600)
            fd = os.open(token, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                with self.assertRaisesRegex(self.prepare.PrepareError, "owner"):
                    self.prepare._validate_file_fd(
                        fd, uid=os.getuid() + 1, gid=os.getgid(), mode=0o600,
                        label="channel token", maximum=4096,
                    )
            finally:
                os.close(fd)
        finally:
            temporary.cleanup()

        temporary, root, kwargs = self.make_runtime()
        try:
            onboarding = root / "var/lib/cc-xia-relay/claude-channel-home/.claude/.claude.json"
            os.chmod(onboarding, 0o644)
            with self.assertRaisesRegex(self.prepare.PrepareError, "permissions"):
                self.prepare.prepare_runtime(**kwargs)
        finally:
            temporary.cleanup()

    def test_launcher_contract_checks_config_path_and_supervises_ready_channel(self):
        launcher = (Path(__file__).parent / "xia_claude_channel" / "launcher.sh").read_text()
        runtime_state = (Path(__file__).parent / "xia_claude_channel" / "runtime_state.py").read_text()
        self.assertIn('$XIA_CHANNEL_HOME/.claude/.claude.json', launcher)
        self.assertGreaterEqual(launcher.count("health_matches"), 3)
        self.assertIn("health_failures >= 3", launcher)
        self.assertIn('"kill-session", "-t"', runtime_state)
        self.assertIn('runtime"', launcher)
        self.assertNotIn('runtime-$generation', launcher)
        loop_start = launcher.index("while :; do")
        guarded_publish = launcher.index("runtime_state.py\" prepare-after-stop", loop_start)
        start_tui = launcher.index('new-session -d', guarded_publish)
        self.assertLess(guarded_publish, start_tui)
        monitor = launcher[launcher.index("health_failures=0"):]
        self.assertIn('runtime_state.py" stop-tui', monitor)
        self.assertIn("|| exit 75", monitor)
        self.assertIn("break", monitor)
        self.assertNotIn("systemctl", monitor)


if __name__ == "__main__": unittest.main()
