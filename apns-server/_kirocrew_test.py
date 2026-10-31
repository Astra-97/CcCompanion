"""Kiro Crew contact kirocrew (2026-10-31) tests.

  1. kirocrew_remote_bridge against a local fake gateway (SSE /api/chat, 403
     on a stale secret, path allowlist) -- the bridge's JSONL contract
  2. KiroCrewClient against a fake remote whose "ssh" runs a local fake bridge:
     segments joined, tools, context %, error classes, Stop -> partial
  3. push handler: real _handle_kirocrew_chat_send + fake client -- turns,
     chat commands (/status /model /effort /new /stop /help), disabled switch,
     no "kiro" substring in sources (the Android client maps those to Kiro)
  4. registry: listed only when enabled; kiro untouched

Never touches the live 8291 service or Tokyo.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import kirocrew_client
from chat_history import ChatStreamBus
from contacts import chat_contact_directory, default_contact_routes
from kirocrew_client import (
    KiroCrewAuthRequired,
    KiroCrewBusy,
    KiroCrewCancelled,
    KiroCrewClient,
    KiroCrewError,
    KiroCrewPreferences,
)
from push import PushHandler

from _kiro_acp_test import FakeChat, _immediate_thread

BRIDGE = Path(__file__).with_name("kirocrew_remote_bridge.py")


# ── 1. bridge against a fake gateway ──────────────────────────────────────
class _FakeGateway(http.server.BaseHTTPRequestHandler):
    secret = "s1"
    rows: list = []
    seen: list = []

    def log_message(self, *_a):
        pass

    def _auth(self) -> bool:
        if self.headers.get("X-Internal-Secret") != type(self).secret:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"Token required"}')
            return False
        return True

    def do_GET(self):
        type(self).seen.append(("GET", self.path))
        if not self._auth():
            return
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(("POST", self.path, payload))
        if not self._auth():
            return
        if self.path != "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for row in type(self).rows:
            self.wfile.write(f"data: {json.dumps(row, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b": keepalive\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


class BridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        run_dir = self.home / ".kiro" / "crew" / "run"
        run_dir.mkdir(parents=True)
        (run_dir / f"gateway-{self.port}-127.0.0.1.secret").write_text("s1")
        _FakeGateway.secret = "s1"
        _FakeGateway.seen = []

    def bridge(self, op, request):
        env = {**os.environ, "HOME": str(self.home)}
        out = subprocess.run(
            [sys.executable, "-c", BRIDGE.read_text(encoding="utf-8"), str(self.port), op],
            input=json.dumps(request), capture_output=True, text=True, env=env, timeout=30,
        )
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    def test_chat_relays_segments_tools_and_context(self):
        _FakeGateway.rows = [
            {"type": "user", "content": "你好"},
            {"type": "chunk", "content": "跑一下"},
            {"type": "assistant", "content": "跑一下。"},
            {"type": "tool", "content": "🔧 echo hi", "cls": "msg msg-tool"},
            {"type": "chunk", "content": "hi"},
            {"type": "assistant", "content": "结果 hi"},
            {"type": "context_usage", "content": {"pct": 9.8, "used_tokens": 98000, "window_tokens": 1000000}, "cls": "context_usage"},
        ]
        rows = self.bridge("chat", {"agent": "xiaoke-persona", "slot": "ccc-kirocrew", "text": "你好"})
        self.assertEqual([r["t"] for r in rows], ["d", "final", "ev", "d", "final", "ev", "done"])
        self.assertEqual(rows[1]["c"], "跑一下。")
        self.assertEqual(rows[2]["type"], "tool")
        self.assertEqual(json.loads(rows[5]["c"])["pct"], 9.8)
        post = [s for s in _FakeGateway.seen if s[0] == "POST"][0]
        self.assertEqual(post[2], {"message": "你好", "agent": "xiaoke-persona", "slot": "ccc-kirocrew"})

    def test_stale_secret_is_reread_once(self):
        _FakeGateway.secret = "s2"  # gateway restarted after the first read...
        secret_file = self.home / ".kiro" / "crew" / "run" / f"gateway-{self.port}-127.0.0.1.secret"

        def rotate():  # ...and wrote its new secret
            secret_file.write_text("s2")

        original = _FakeGateway._auth

        def auth_then_rotate(handler):
            ok = original(handler)
            if not ok:
                rotate()
            return ok

        with patch.object(_FakeGateway, "_auth", auth_then_rotate):
            rows = self.bridge("get", {"path": "/api/health"})
        self.assertEqual(rows[-1]["status"], 200)
        self.assertEqual(len([s for s in _FakeGateway.seen if s[1] == "/api/health"]), 2)

    def test_path_allowlist(self):
        rows = self.bridge("get", {"path": "/etc/passwd"})
        self.assertEqual(rows[-1]["status"], 400)
        self.assertEqual(_FakeGateway.seen, [])

    def test_http_error_becomes_err_row(self):
        _FakeGateway.secret = "never"
        rows = self.bridge("chat", {"agent": "a", "slot": "s", "text": "x"})
        self.assertEqual(rows[-1]["t"], "err")
        self.assertEqual(rows[-1]["status"], 403)


# ── 2. client against a fake ssh ──────────────────────────────────────────
FAKE_BRIDGE = textwrap.dedent(r'''
    import json, sys, time
    mode = sys.argv[1]
    req = json.loads(sys.stdin.read() or "{}")
    def emit(o):
        sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n"); sys.stdout.flush()
    if mode == "ok":
        emit({"t": "d", "c": "第一"}); emit({"t": "final", "c": "第一段。"})
        emit({"t": "ev", "type": "tool", "c": "🔧 ls -la", "cls": "msg msg-tool"})
        emit({"t": "ev", "type": "permission", "c": "rm /tmp/x", "cls": "msg msg-permission"})
        emit({"t": "d", "c": "第二"}); emit({"t": "final", "c": "第二段。"})
        emit({"t": "ev", "type": "context_usage", "c": json.dumps({"pct": 12.5, "used_tokens": 125000, "window_tokens": 1000000})})
        emit({"t": "done"})
    elif mode == "busy":
        emit({"t": "err", "status": 409, "body": {"error": "slot busy", "code": "slot_busy"}})
    elif mode == "auth":
        emit({"t": "err", "status": 503, "body": {"error": {"code": "kiro_prerequisite_required"}}})
    elif mode == "slow":
        emit({"t": "d", "c": "半句"})
        time.sleep(1.5)
        emit({"t": "final", "c": "半句"}); emit({"t": "done"})
    elif mode == "empty_error":
        emit({"t": "ev", "type": "error", "c": "Kiro CLI sign-in is required"}); emit({"t": "done"})
''')


class FakeRemote:
    def __init__(self, mode: str, script: Path):
        self.mode = mode
        self.script = script
        self.runs = []

    def remote_argv(self, argv, *, multiplex=True):
        return [sys.executable, str(self.script), self.mode]

    def run(self, argv, *, input=None, timeout=10.0, **_kw):
        self.runs.append(([argv[-1]], json.loads(input) if input else None))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"status": 200, "body": {"ok": True}}) + "\n")


class ClientTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.script = Path(tmp.name) / "fake_bridge.py"
        self.script.write_text(FAKE_BRIDGE, encoding="utf-8")

    def client(self, mode):
        remote = FakeRemote(mode, self.script)
        return KiroCrewClient(remote, bridge_source="# bridge"), remote

    def test_segments_tools_context(self):
        client, _remote = self.client("ok")
        updates, tool_calls = [], []
        turn = client.prompt("你好", on_update=updates.append, on_tool=tool_calls.append)
        self.assertEqual(turn.text, "第一段。\n\n第二段。")
        self.assertEqual(turn.tools, ["ls -la", "等待 Crew 授权：rm /tmp/x"])
        self.assertEqual(turn.context_pct, 12.5)
        self.assertEqual(turn.context_window_tokens, 1000000)
        self.assertIn("第一段。\n\n第二", updates)
        self.assertEqual(tool_calls[0], ["ls -la"])

    def test_error_classes(self):
        with self.assertRaises(KiroCrewBusy):
            self.client("busy")[0].prompt("x")
        with self.assertRaises(KiroCrewAuthRequired):
            self.client("auth")[0].prompt("x")
        with self.assertRaises(KiroCrewAuthRequired):
            self.client("empty_error")[0].prompt("x")

    def test_cancel_stops_slot_and_returns_partial(self):
        client, remote = self.client("slow")
        cancel = threading.Event()
        timer = threading.Timer(0.4, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(KiroCrewCancelled) as caught:
            client.prompt("x", cancel_event=cancel)
        self.assertIn("半句", caught.exception.partial)
        self.assertIn((["post"], {"path": "/api/chat/slots/ccc-kirocrew/stop", "body": {}, "timeout": 30.0}), remote.runs)

    def test_control_calls(self):
        client, remote = self.client("ok")
        client.set_model("claude-opus-5.5")
        client.set_effort("high")
        client.reset()
        paths = [req["path"] for _argv, req in remote.runs]
        self.assertEqual(paths, [
            "/api/chat/slots/ccc-kirocrew/model",
            "/api/chat/slots/ccc-kirocrew/reasoning-effort",
            "/api/chat/slots/ccc-kirocrew/reset-conversation",
        ])
        with self.assertRaises(ValueError):
            client.set_effort("turbo")
        with self.assertRaises(ValueError):
            KiroCrewClient(remote, slot="../x")

    def test_ensure_slot_creates_and_titles_once(self):
        client, remote = self.client("ok")
        replies = iter([
            {"status": 404, "body": {"error": "not found"}},  # GET slot
            {"status": 200, "body": {"ok": True, "slot": "ccc-kirocrew"}},  # POST create
            {"status": 200, "body": {"ok": True}},  # PATCH title
        ])
        remote.run = lambda argv, *, input=None, timeout=10.0, **_kw: (
            remote.runs.append(([argv[-1]], json.loads(input))) or types.SimpleNamespace(returncode=0, stdout=json.dumps(next(replies)) + "\n")
        )
        client.ensure_slot()
        client.ensure_slot()  # cached: no further calls
        self.assertEqual([(op[0], req["path"]) for op, req in remote.runs], [
            ("get", "/api/chat/slots/ccc-kirocrew"),
            ("post", "/api/chat/slots"),
            ("patch", "/api/chat/slots/ccc-kirocrew/title"),
        ])
        self.assertEqual(remote.runs[1][1]["body"], {"name": "ccc-kirocrew", "agent": "xiaoke-persona", "agent_kind": "template"})

    def test_markdown_images(self):
        self.assertEqual(
            kirocrew_client.markdown_images_to_markers("看 ![图](/home/kiro/a.png) 和 ![x](https://e.com/b.png)"),
            "看 [[CCC_IMAGE:/home/kiro/a.png]] 和 ![x](https://e.com/b.png)",
        )

    def test_preferences_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            prefs = KiroCrewPreferences(path)
            self.assertEqual(prefs.snapshot(), ("", ""))
            prefs.save(model="claude-opus-5.5", effort="high")
            self.assertEqual(prefs.bump_epoch(), 1)
            again = KiroCrewPreferences(path)
            self.assertEqual((again.snapshot(), again.epoch), (("claude-opus-5.5", "high"), 1))
            with self.assertRaises(ValueError):
                again.save(effort="turbo")


# ── 3. push handler ───────────────────────────────────────────────────────
class FakeCrew:
    slot = "ccc-kirocrew"

    def __init__(self, *, text="小克在。", failure=None, tools=()):
        self.text = text
        self.failure = failure
        self.tools = list(tools)
        self.prompts = []
        self.calls = []

    def prompt(self, text, *, on_update=None, on_tool=None, cancel_event=None):
        self.prompts.append(text)
        if on_update is not None:
            on_update(self.text[:2])
        if self.tools and on_tool is not None:
            on_tool(self.tools)
        if self.failure is not None:
            raise self.failure
        return kirocrew_client.KiroCrewTurn(text=self.text, tools=self.tools, context_pct=11.0)

    def set_model(self, model):
        self.calls.append(("model", model))

    def set_effort(self, effort):
        self.calls.append(("effort", effort))

    def reset(self):
        self.calls.append(("reset",))

    def ensure_slot(self):
        self.calls.append(("ensure",))

    def slot_info(self):
        return {"context_pct": 11.0, "context_used_tokens": 110000, "context_window_tokens": 1000000, "running": False}


class KiroCrewHandlerTest(unittest.TestCase):
    def handler(self, client, *, enabled=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat, kiro_chat = FakeChat(), FakeChat()
        chats = {"kiro": kiro_chat}
        if enabled:
            chats["kirocrew"] = chat
        state = types.SimpleNamespace(
            contact_chats=chats,
            contact_routes=default_contact_routes(),
            contact_catalog=[],
            contact_typing_states={},
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
            chat_stream_revisions={},
            chat_stream_bus=ChatStreamBus(),
            kirocrew_client=client,
            kirocrew_remote=None,
            kirocrew_remote_home="/home/kiro",
            kirocrew_lock=threading.Lock(),
            kirocrew_queue=__import__("collections").deque(),
            kirocrew_worker_running=False,
            kirocrew_active={},
            kirocrew_applied=None,
            kirocrew_last_context_pct=None,
            kirocrew_preferences=KiroCrewPreferences(Path(tmp.name) / "prefs.json"),
            kirocrew_semantic_memory_recall_enabled=False,
            kiro_acp=types.SimpleNamespace(available_model_ids=lambda: ("auto", "claude-opus-5.5")),
            attachments_dir=tmp.name,
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler.headers = {}
        handler.responses = []
        handler._send_json = lambda status, payload, **_kw: handler.responses.append((status, payload))
        handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
        handler._chat_for_contact = lambda contact_id: state.contact_chats[contact_id]
        handler._kirocrew_credits = lambda: "Kiro credits（和 Kiro 联系人共用）：已用 1 / 1000"
        return handler, chat, kiro_chat

    def send(self, handler, body):
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kirocrew_chat_send(body, "kirocrew")

    def test_turn_happy_path(self):
        client = FakeCrew(tools=["ls -la"])
        handler, chat, kiro_chat = self.handler(client)
        self.send(handler, {"text": "你好呀"})
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(handler.responses[-1][1]["turn"]["transport"], "kiro-crew")
        self.assertEqual(client.prompts, ["你好呀"])
        roles = [(r["role"], r.get("text")) for r in chat.records]
        self.assertEqual(roles[0], ("user", "你好呀"))
        self.assertEqual(roles[-1], ("assistant", "小克在。"))
        self.assertEqual(chat.records[1]["metadata"]["activity_items"], ["ls -la · 工具 · 完成"])
        self.assertEqual(kiro_chat.records, [])
        self.assertEqual(handler.state.chat_reply_states["kirocrew"]["reply_state"], "completed")
        self.assertEqual(handler.state.kirocrew_last_context_pct, 11.0)
        # model/effort pinned on the slot before the first turn
        self.assertEqual(client.calls, [("ensure",), ("model", "auto"), ("effort", "")])
        for record in chat.records:
            self.assertNotIn("kiro", str(record.get("source") or "").lower())

    def test_failure_is_visible(self):
        handler, chat, _ = self.handler(FakeCrew(failure=KiroCrewError("gateway error 500")))
        self.send(handler, {"text": "在吗"})
        self.assertIn("没有成功回复", chat.records[-1]["text"])
        self.assertEqual(handler.state.chat_reply_states["kirocrew"]["reply_state"], "failed")

    def test_cancelled_keeps_partial(self):
        handler, chat, _ = self.handler(FakeCrew(failure=KiroCrewCancelled("半句话")))
        self.send(handler, {"text": "写长一点"})
        self.assertEqual(chat.records[-1]["text"], "半句话\n\n**[已停止生成]**")

    def test_commands_answer_without_model(self):
        client = FakeCrew()
        handler, chat, _ = self.handler(client)
        self.send(handler, {"text": "/status"})
        self.assertIn("上下文：11.0%", chat.records[-1]["text"])
        self.send(handler, {"text": "/model claude-opus-5.5"})
        self.assertEqual(handler.state.kirocrew_preferences.snapshot(), ("claude-opus-5.5", ""))
        self.send(handler, {"text": "/model nope-9"})
        self.assertIn("没有这个模型", chat.records[-1]["text"])
        self.send(handler, {"text": "/effort high"})
        self.assertEqual(handler.state.kirocrew_preferences.snapshot(), ("claude-opus-5.5", "high"))
        self.send(handler, {"text": "/effort turbo"})
        self.assertIn("只能是", chat.records[-1]["text"])
        self.send(handler, {"text": "/help"})
        self.assertIn("/compact", chat.records[-1]["text"])
        self.send(handler, {"text": "/new"})
        self.assertIn(("reset",), client.calls)
        self.assertEqual(handler.state.kirocrew_preferences.epoch, 1)
        self.assertEqual(client.prompts, [])

    def test_compact_is_a_crew_turn(self):
        client = FakeCrew(text="✅ Conversation compacted")
        handler, chat, _ = self.handler(client)
        self.send(handler, {"text": "/compact"})
        self.assertEqual(client.prompts, ["/compact"])

    def test_new_refused_while_busy_and_stop_cancels(self):
        handler, chat, _ = self.handler(FakeCrew())
        cancel = threading.Event()
        handler.state.kirocrew_active = {"user_ts": "ts-0", "cancel_event": cancel}
        self.send(handler, {"text": "/new"})
        self.assertIn("等这条回完", chat.records[-1]["text"])
        self.send(handler, {"text": "/stop"})
        self.assertTrue(cancel.is_set())
        self.assertEqual(chat.records[-1]["text"], "好，停下了。")

    def test_chat_stop_endpoint_is_exact_turn(self):
        handler, _chat, _ = self.handler(FakeCrew())
        cancel = threading.Event()
        handler.state.kirocrew_active = {"user_ts": "ts-9", "cancel_event": cancel}
        handler._handle_kirocrew_chat_stop({"contact_id": "kirocrew", "user_ts": "ts-1"})
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertFalse(cancel.is_set())
        handler._handle_kirocrew_chat_stop({"contact_id": "kirocrew", "user_ts": "ts-9"})
        self.assertTrue(cancel.is_set())

    def test_preferences_endpoints(self):
        handler, _chat, _ = self.handler(FakeCrew())
        handler._handle_kirocrew_preferences_post({"model": "claude-opus-5.5", "effort": "max"})
        self.assertEqual(handler.responses[-1][1]["model"], "claude-opus-5.5")
        handler._handle_kirocrew_preferences_post({"model": "not-in-catalog"})
        self.assertEqual(handler.responses[-1][0], 400)
        handler._handle_kirocrew_preferences_get()
        self.assertEqual(handler.responses[-1][1]["effort"], "max")

    def test_preferences_payload_matches_kiro_shape(self):
        # The Android client parses /kirocrew/preferences with the /kiro one's parser.
        handler, _chat, _ = self.handler(FakeCrew())
        handler.state.kirocrew_last_context_pct = 42.4
        handler._handle_kirocrew_preferences_get()
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "auto")
        self.assertEqual(payload["selection"], {"model": "auto", "effort": ""})
        self.assertEqual(payload["available_models"], ["auto", "claude-opus-5.5"])
        self.assertEqual([m["id"] for m in payload["models"]], ["auto", "claude-opus-5.5"])
        self.assertEqual(payload["available_efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertFalse(payload["effort_applies"])
        self.assertEqual(payload["current_model"], "auto")
        self.assertEqual(payload["context_usage_percent"], 42.4)
        display = payload["header_display"]
        self.assertEqual(display["version"], 1)
        self.assertEqual(display["text"], "auto · 42%")
        self.assertEqual(display["context_percent"], 42.4)
        handler._handle_kirocrew_preferences_post({"model": "claude-opus-5.5", "effort": "high"})
        payload = handler.responses[-1][1]
        self.assertTrue(payload["effort_applies"])
        self.assertEqual(payload["header_display"]["text"], "claude-opus-5.5 · 42%")

    def test_preferences_offer_auto_without_kiro_catalog(self):
        handler, _chat, _ = self.handler(FakeCrew())
        handler.state.kiro_acp = types.SimpleNamespace(available_models=lambda: ([], "none"))
        handler._handle_kirocrew_preferences_get()
        payload = handler.responses[-1][1]
        self.assertEqual(payload["available_models"], ["auto"])
        self.assertEqual(payload["header_display"]["text"], "auto")
        self.assertIsNone(payload["context_usage_percent"])

    def _terminal_handler(self, detail):
        crew = FakeCrew()
        crew.gets = []

        def get(path):
            crew.gets.append(path)
            return 200, detail

        crew.get = get
        handler, _chat, _ = self.handler(crew)
        handler.state.kirocrew_terminal_lock = threading.Lock()
        handler.state.kirocrew_terminal_cache = None
        handler.state.default_session = "cctg"
        handler.state.active_session = "cctg"
        return handler, crew

    def test_terminal_renders_slot_transcript_read_only_and_redacted(self):
        detail = {
            "key": "ccc-kirocrew", "running": True, "turn": 4,
            "messages": [
                {"role": "user", "content": "帮我看下日志", "cls": "msg msg-u", "ts": "2026-10-31T02:31:16+00:00"},
                {"role": "tool", "content": "🔧 Shell", "cls": "msg msg-tool", "ts": "2026-10-31T02:31:17+00:00",
                 "meta": {"purpose": "read the gateway journal",
                          "input": "journalctl -u kirocrew -n 5",
                          "output": "Dashboard: http://127.0.0.1:5476/?token=abcDEF123_secretvalue\n"
                                    "X-Internal-Secret: 0123456789abcdef0123456789abcdef\n"
                                    "api_key=sk-THISISAVERYSECRETKEY1234"}},
                {"role": "assistant", "content": "看完了", "cls": "msg msg-a", "ts": "2026-10-31T02:31:20+00:00"},
                {"role": "system", "content": "ctx warn", "cls": "msg msg-warn", "ts": "bad"},
            ],
        }
        handler, crew = self._terminal_handler(detail)
        with handler.state.chat_draft_lock:
            handler.state.chat_drafts["kirocrew"] = {"is_active": True, "text": "正在写第二段 token=hunter2hunter2"}
        handler.path = "/tmux/capture?session=kirocrew"
        handler._handle_tmux_capture()
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"], "kirocrew")
        self.assertEqual(payload["state"], "waiting")
        self.assertEqual(payload["mode"], "read_only")
        self.assertTrue(payload["busy"])
        self.assertNotIn("lease", payload)
        content = payload["content"]
        self.assertIn("Kiro Crew · ccc-kirocrew · 运行中 · turn 4", content)
        self.assertIn("[10:31:16] › 帮我看下日志", content)
        self.assertIn("🔧 Shell", content)
        self.assertIn("$ journalctl -u kirocrew -n 5", content)
        self.assertIn("◆ 看完了", content)
        self.assertIn("! ctx warn", content)
        self.assertIn("正在写第二段", content)
        for leaked in ("abcDEF123_secretvalue", "0123456789abcdef0123456789abcdef",
                       "sk-THISISAVERYSECRETKEY1234", "hunter2hunter2"):
            self.assertNotIn(leaked, content)
        self.assertIn("?token=[已隐藏]", content)
        self.assertEqual(crew.gets, ["/api/chat/slots/ccc-kirocrew?limit=40"])
        # Polls inside the cache window reuse the last read (one ssh call).
        handler._handle_tmux_capture()
        self.assertEqual(len(crew.gets), 1)

    def test_terminal_without_slot_and_gateway_down(self):
        handler, crew = self._terminal_handler({})
        crew.get = lambda path: (404, {"error": "slot not found"})
        handler.path = "/tmux/capture?session=kirocrew"
        handler._handle_tmux_capture()
        self.assertIn("会话还没有记录", handler.responses[-1][1]["content"])

        handler, crew = self._terminal_handler({})

        def down(path):
            raise kirocrew_client.KiroCrewUnavailable("ssh failed")

        crew.get = down
        handler.path = "/tmux/capture?session=kirocrew"
        handler._handle_tmux_capture()
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertIn("! Crew 网关暂时连不上", payload["content"])
        self.assertFalse(payload["busy"])

    def test_terminal_input_routes_are_read_only(self):
        handler, _crew = self._terminal_handler({})
        handler._handle_tmux_send({"session": "kirocrew", "text": "hi"})
        self.assertEqual(handler.responses[-1][0], 409)
        handler._handle_terminal_key({"session": "kirocrew", "key": "Enter"})
        self.assertEqual(handler.responses[-1][0], 409)
        handler._handle_terminal_resize({"session": "kirocrew", "columns": 80, "rows": 24})
        self.assertEqual(handler.responses[-1][0], 409)
        handler._handle_terminal_release({"target": "kirocrew"})
        self.assertEqual(handler.responses[-1][1]["error"], "kirocrew_terminal_read_only")

    def test_terminal_redaction_keeps_ordinary_text(self):
        from push import _kirocrew_terminal_redact
        text = "ls /home/kiro/Kiro-Crew-Workspace && echo hello_world 42"
        self.assertEqual(_kirocrew_terminal_redact(text), text)
        self.assertNotIn("ghp_", _kirocrew_terminal_redact("ghp_abcdefghijklmnopqrstuvwx"))
        self.assertEqual(_kirocrew_terminal_redact("Authorization: Bearer abc.def.ghi12345"), "Authorization: [已隐藏]")
        self.assertEqual(_kirocrew_terminal_redact("curl -H 'Bearer abc.def.ghi12345'"), "curl -H 'Bearer [已隐藏]'")

    def test_disabled(self):
        handler, _chat, _ = self.handler(None, enabled=False)
        handler._discard_uncommitted_staged_attachments = lambda _items: None
        self.send(handler, {"text": "在吗"})
        self.assertEqual(handler.responses[-1][0], 503)

    def test_attachments_reach_prompt_as_tokyo_paths(self):
        client = FakeCrew()
        handler, chat, _ = self.handler(client)
        stored = Path(handler.state.attachments_dir) / "abc.png"
        stored.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        puts = []
        handler.state.kirocrew_remote = types.SimpleNamespace(
            put_file=lambda local, remote: puts.append((local, remote)) or True,
            run=lambda *a, **k: None,
        )
        self.send(handler, {"text": "看图", "_pwa_staged_attachments": [{
            "attachment_url": "/attachments/abc.png", "type": "image", "filename": "a.png", "stored_path": str(stored),
        }]})
        self.assertEqual(puts[0][1], "/home/kiro/.cache/ccc/crew-attachments/abc.png")
        self.assertIn("本地路径: /home/kiro/.cache/ccc/crew-attachments/abc.png", client.prompts[0])
        self.assertEqual(chat.records[0]["attachment_url"], "/attachments/abc.png")


class KeepWarmTest(unittest.TestCase):
    def state(self, info):
        client = FakeCrew()
        client.slot_info = lambda: info
        return types.SimpleNamespace(
            kirocrew_client=client, kirocrew_lock=threading.Lock(),
            kirocrew_queue=__import__("collections").deque(), kirocrew_active={}, kirocrew_warm_gen="",
        ), client

    def test_outcomes(self):
        from push import _kirocrew_keep_warm_attempt
        state, client = self.state({})
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "warmed")
        self.assertEqual(client.calls, [("ensure",)])
        self.assertEqual(client.prompts, [])
        state, client = self.state({"turn_gen": "g1", "mcp_report": {"configured": []}})
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "warm")
        state, client = self.state({"turn_gen": "g2", "mcp_report": None})
        state.kirocrew_warm_gen = ""
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "warmed")
        self.assertEqual(client.prompts, ["/context"])
        self.assertEqual(state.kirocrew_active, {})
        # same gateway generation: /context does not set mcp_report, still warm
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "warm")
        self.assertEqual(client.prompts, ["/context"])
        # gateway restarted -> new generation -> warm once more
        client.slot_info = lambda: {"turn_gen": "g3", "mcp_report": None}
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "warmed")
        self.assertEqual(client.prompts, ["/context", "/context"])
        state, client = self.state({"mcp_report": None, "running": True})
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "busy")
        state, client = self.state({"mcp_report": None})
        state.kirocrew_queue.append({"text": "x"})
        self.assertEqual(_kirocrew_keep_warm_attempt(state), "busy")
        self.assertEqual(client.prompts, [])


# ── 4. registry ───────────────────────────────────────────────────────────
class RegistryTest(unittest.TestCase):
    def test_listed_only_when_enabled(self):
        routes = default_contact_routes()
        on = types.SimpleNamespace(contact_routes=routes, contact_catalog=[], contact_chats={"kiro": object(), "kirocrew": object()})
        off = types.SimpleNamespace(contact_routes=routes, contact_catalog=[], contact_chats={"kiro": object()})
        listed = {c["id"]: c for c in chat_contact_directory(on)}
        self.assertEqual(listed["kirocrew"]["display_name"], "Kiro Crew")
        self.assertIn("stop", listed["kirocrew"]["capabilities"])
        self.assertIn("attachments", listed["kirocrew"]["capabilities"])
        self.assertEqual(listed["kiro"]["display_name"], "小克")
        self.assertNotIn("kirocrew", {c["id"] for c in chat_contact_directory(off)})


if __name__ == "__main__":
    unittest.main()
