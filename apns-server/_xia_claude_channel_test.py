import json
import os
import stat
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


if __name__ == "__main__": unittest.main()
