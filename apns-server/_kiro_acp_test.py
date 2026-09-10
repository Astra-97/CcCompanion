"""Tests for the Kiro ACP bridge (kiro 桥接 2026-09-09).

Everything is mocked: Kiro CLI is installed but not logged in on this host,
so no test may spawn the real binary or reach the real model.
"""
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from kiro_acp import (
    DEFAULT_KIRO_CWD,
    KiroACPAuthRequired,
    KiroACPBusy,
    KiroACPCancelled,
    KiroACPClient,
    KiroACPError,
    KiroACPQuotaExceeded,
    _activity_from_update,
    _classified_rpc_error,
    _text_from_update,
)
# kiro 切模型 (2026-09-10)
from kiro_preferences import (
    KIRO_APP_DEFAULT_MODEL,
    KiroPreferenceError,
    KiroPreferenceStore,
)
from chat_history import ChatStreamBus
from contacts import (
    chat_contact_directory,
    default_contact_routes,
    dispatch_contact_get,
    dispatch_contact_post,
    dispatch_contact_send,
)
from contacts.kiro import rejects_inbound
from push import PushHandler


# ---------------------------------------------------------------------------
# Fake stdio ACP process
# ---------------------------------------------------------------------------

class _FakeStdout:
    def __init__(self):
        self._queue = queue.Queue()

    def push(self, obj):
        self._queue.put(json.dumps(obj) + "\n")

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStderr:
    def __init__(self):
        self._queue = queue.Queue()

    def push_line(self, line):
        self._queue.put(line + "\n")

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    def __init__(self, on_message):
        self._on_message = on_message
        self._buffer = ""

    def write(self, data):
        self._buffer += data
        return len(data)

    def flush(self):
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            if line.strip():
                self._on_message(json.loads(line))


class FakeKiroACPProcess:
    """In-memory stand-in for ``kiro-cli acp`` over stdio.

    ``handler(process, request)`` returns response/notification objects to
    push back to the client's stdout, in order.
    """

    def __init__(self, handler):
        self._handler = handler
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr()
        self.stdin = _FakeStdin(self._on_message)
        self.pid = 4_000_000_000  # nonexistent: killpg fails harmlessly
        self._returncode = None
        self.requests = []

    def _on_message(self, message):
        self.requests.append(message)
        for response in self._handler(self, message) or []:
            self.stdout.push(response)

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode if self._returncode is not None else 0

    def crash(self):
        self._returncode = -15
        self.stdout.close()
        self.stderr.close()


def _scripted_factory(processes):
    remaining = list(processes)

    def factory(*_args, **_kwargs):
        if not remaining:
            raise AssertionError("unexpected extra kiro-cli acp spawn")
        return remaining.pop(0)

    return factory


def _basic_handler(session_id="kiro-session-1"):
    def handle(_process, message):
        method = message.get("method")
        if method == "initialize":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": 1}}]
        if method == "session/new":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": session_id}}]
        if method == "session/load":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": message["params"]["sessionId"]}}]
        if method == "session/prompt":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}}]
        raise AssertionError(f"unexpected method {method}")
    return handle


class KiroACPProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = str(Path(self.tmp.name) / "kiro_acp_session.json")

    def _client(self, factory):
        return KiroACPClient(
            command="/fake/kiro-cli",
            cwd=self.tmp.name,
            state_path=self.state_path,
            request_timeout=5,
            prompt_timeout=10,
            popen_factory=factory,
        )

    def test_new_session_round_trip_and_pointer_persisted(self):
        process = FakeKiroACPProcess(_basic_handler())
        client = self._client(_scripted_factory([process]))

        session_id = client.prepare_session()

        self.assertEqual("kiro-session-1", session_id)
        methods = [req.get("method") for req in process.requests]
        self.assertEqual(["initialize", "session/new"], methods)
        new_params = process.requests[1]["params"]
        self.assertEqual(str(Path(self.tmp.name).resolve()), new_params["cwd"])
        self.assertEqual([], new_params["mcpServers"])
        # Pointer round-trips through the state file, cwd-bound.
        self.assertEqual("kiro-session-1", client.load_session_id())
        payload = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        self.assertEqual(2, payload["version"])
        self.assertEqual("kiro-session-1", payload["session_id"])
        # Fast path: a second prepare reuses the live session silently.
        self.assertEqual("kiro-session-1", client.prepare_session())
        self.assertEqual(2, len(process.requests))

    def test_resume_existing_session_uses_load_not_new(self):
        Path(self.state_path).write_text(
            json.dumps({
                "version": 2,
                "session_id": "previous-session",
                "cwd": str(Path(self.tmp.name).resolve()),
            }),
            encoding="utf-8",
        )
        process = FakeKiroACPProcess(_basic_handler())
        client = self._client(_scripted_factory([process]))

        self.assertEqual("previous-session", client.prepare_session())

        methods = [req.get("method") for req in process.requests]
        self.assertEqual(["initialize", "session/load"], methods)
        self.assertEqual("previous-session", process.requests[1]["params"]["sessionId"])

    def test_legacy_or_foreign_cwd_pointer_starts_new_session(self):
        for bad_payload in (
            {"version": 1, "session_id": "s", "cwd": str(Path(self.tmp.name).resolve())},
            {"version": 2, "session_id": "s", "cwd": "/somewhere/else"},
            {"version": 2, "session_id": "../escape", "cwd": str(Path(self.tmp.name).resolve())},
        ):
            Path(self.state_path).write_text(json.dumps(bad_payload), encoding="utf-8")
            process = FakeKiroACPProcess(_basic_handler())
            client = self._client(_scripted_factory([process]))
            self.assertEqual("kiro-session-1", client.prepare_session())
            methods = [req.get("method") for req in process.requests]
            self.assertEqual(["initialize", "session/new"], methods, bad_payload)

    def test_streaming_chunks_concatenate_and_foreign_session_is_ignored(self):
        def handle(_process, message):
            if message.get("method") != "session/prompt":
                return _basic_handler()(_process, message)
            request_id = message["id"]
            session_id = message["params"]["sessionId"]
            return [
                {"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "secret reasoning"}},
                }},
                {"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "someone-else",
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "FORBIDDEN"}},
                }},
                {"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "你好，"}},
                }},
                # Kiro documents its stream as session/notification; accept it too.
                {"jsonrpc": "2.0", "method": "session/notification", "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "我是 Kiro。"}},
                }},
                {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}},
            ]

        process = FakeKiroACPProcess(handle)
        client = self._client(_scripted_factory([process]))
        session_id = client.prepare_session()
        chunks = []
        activities = []

        result = client.prompt_existing(
            "hello",
            session_id=session_id,
            turn_id="turn-1",
            on_update=chunks.append,
            on_activity=activities.append,
        )

        self.assertEqual("你好，我是 Kiro。", "".join(chunks))
        self.assertEqual("end_turn", result.stop_reason)
        self.assertEqual(session_id, result.session_id)
        self.assertEqual([{"kind": "activity", "label": "正在思考"}], activities)
        prompt = process.requests[-1]
        self.assertEqual("session/prompt", prompt["method"])
        self.assertEqual([{"type": "text", "text": "hello"}], prompt["params"]["prompt"])

    def test_process_crash_fails_turn_then_restart_resumes_persisted_session(self):
        def crashing_handler(process, message):
            if message.get("method") == "session/prompt":
                process.crash()  # no response; stdout closes under the reader
                return []
            return _basic_handler(session_id="durable-session")(process, message)

        first = FakeKiroACPProcess(crashing_handler)
        second = FakeKiroACPProcess(_basic_handler())
        client = self._client(_scripted_factory([first, second]))

        session_id = client.prepare_session()
        self.assertEqual("durable-session", session_id)
        with self.assertRaisesRegex(KiroACPError, "exited"):
            client.prompt_existing("boom", session_id=session_id, turn_id="turn-1")

        # The pointer survived the crash, so the restarted process resumes
        # the same conversation via session/load instead of starting fresh.
        self.assertEqual("durable-session", client.prepare_session())
        methods = [req.get("method") for req in second.requests]
        self.assertEqual(["initialize", "session/load"], methods)
        self.assertEqual("durable-session", second.requests[1]["params"]["sessionId"])

        chunks = []
        client.prompt_existing("again", session_id="durable-session", turn_id="turn-2", on_update=chunks.append)

    def test_auth_and_quota_errors_are_classified_without_echoing_payload(self):
        auth = _classified_rpc_error("session/prompt", {"code": -32000, "message": "provider private detail"})
        self.assertIsInstance(auth, KiroACPAuthRequired)
        self.assertNotIn("private", str(auth))
        quota = _classified_rpc_error("session/prompt", {"code": -32001, "message": "402 Payment Required: xyzzy-private-detail"})
        self.assertIsInstance(quota, KiroACPQuotaExceeded)
        self.assertNotIn("xyzzy-private-detail", str(quota))
        generic = _classified_rpc_error("session/prompt", {"code": -32603, "message": "internal detail"})
        self.assertIsInstance(generic, KiroACPError)
        self.assertNotIsInstance(generic, KiroACPAuthRequired)
        self.assertNotIn("internal detail", str(generic))

    def test_logged_out_cli_exit_is_classified_as_auth_required(self):
        """Unauthenticated kiro-cli answers nothing and exits with a stderr line."""
        def handle(process, _message):
            process.stderr.push_line("error: You are not logged in, please log in with kiro-cli login")
            process.crash()
            return []

        process = FakeKiroACPProcess(handle)
        client = self._client(_scripted_factory([process]))
        with self.assertRaises(KiroACPAuthRequired):
            client.prepare_session()
        # The stderr line itself is never surfaced.
        try:
            client.prepare_session()
        except KiroACPError as exc:
            self.assertNotIn("kiro-cli login", str(exc))

    def test_auth_error_over_the_wire_raises_typed_error(self):
        def handle(_process, message):
            if message.get("method") == "session/prompt":
                return [{"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000, "message": "authentication required"}}]
            return _basic_handler()(_process, message)

        process = FakeKiroACPProcess(handle)
        client = self._client(_scripted_factory([process]))
        session_id = client.prepare_session()
        with self.assertRaises(KiroACPAuthRequired):
            client.prompt_existing("hi", session_id=session_id, turn_id="turn-1")

    def test_prompt_requires_prepared_exact_session(self):
        client = self._client(_scripted_factory([]))
        with self.assertRaisesRegex(KiroACPError, "identities"):
            client.prompt_existing("hello", session_id="", turn_id="turn-1")
        client._process_alive = lambda: True
        with self.assertRaisesRegex(KiroACPError, "not prepared"):
            client.prompt_existing("hello", session_id="s1", turn_id="turn-1")

    def test_concurrent_turn_raises_busy(self):
        client = self._client(_scripted_factory([]))
        self.assertFalse(client.busy)
        client._turn_lock.acquire()
        self.addCleanup(client._turn_lock.release)
        self.assertTrue(client.busy)
        client._process_alive = lambda: True
        client._loaded_session_id = "s1"
        with self.assertRaises(KiroACPBusy):
            client.prompt_existing("hello", session_id="s1", turn_id="turn-1")

    def test_cancel_is_a_notification_fenced_to_the_exact_turn(self):
        client = self._client(_scripted_factory([]))
        sent = []
        client._process_alive = lambda: True
        client._write = sent.append
        client._active_turn_id = "turn-1"
        client._active_session_id = "s1"
        self.assertTrue(client.cancel("turn-1", "s1"))
        self.assertEqual("session/cancel", sent[0]["method"])
        self.assertNotIn("id", sent[0])
        self.assertFalse(client.cancel("turn-2", "s1"))
        self.assertFalse(client.cancel("turn-1", "other"))
        self.assertFalse(client.cancel("", "s1"))
        self.assertEqual(1, len(sent))

    def test_prompt_cancel_raises_after_protocol_cancel(self):
        client = self._client(_scripted_factory([]))
        client._process_alive = lambda: True
        client._loaded_session_id = "s1"
        cancelled = []
        gate = threading.Event()
        release = threading.Event()

        def request(method, params, timeout, ensure_started=True):
            if method == "session/prompt":
                release.wait(2)
                return {"stopReason": "cancelled"}
            return {}

        client._request = request
        client.cancel = lambda turn_id, session_id: cancelled.append((turn_id, session_id)) or True
        threading.Timer(0.02, gate.set).start()
        threading.Timer(0.05, release.set).start()
        with self.assertRaises(KiroACPCancelled):
            client.prompt_existing("hello", session_id="s1", turn_id="turn-1", cancel_event=gate)
        self.assertEqual([("turn-1", "s1")], cancelled)

    def test_permission_auto_allows_only_unambiguous_one_turn_tool_action(self):
        client = self._client(_scripted_factory([]))
        sent = []
        client._write = sent.append
        client._answer_permission({
            "id": 7,
            "params": {
                "toolCall": {"toolCallId": "tool-1"},
                "options": [
                    {"optionId": "once", "kind": "allow_once"},
                    {"optionId": "always", "kind": "allow_always"},
                    {"optionId": "no", "kind": "reject_once"},
                ],
            },
        })
        self.assertEqual("once", sent[-1]["result"]["outcome"]["optionId"])

        client._answer_permission({
            "id": 8,
            "params": {
                "toolCall": {"toolCallId": "tool-question"},
                "options": [
                    {"optionId": "a", "kind": "allow_once"},
                    {"optionId": "b", "kind": "allow_once"},
                    {"optionId": "skip", "kind": "reject_always"},
                ],
            },
        })
        self.assertEqual("cancelled", sent[-1]["result"]["outcome"]["outcome"])

    def test_text_and_activity_projection_stay_bounded(self):
        chunk = lambda kind, text: {"update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}
        self.assertEqual("hi", _text_from_update(chunk("agent_message_chunk", "hi")))
        self.assertEqual("hi", _text_from_update(chunk("AgentMessageChunk", "hi")))
        self.assertEqual("", _text_from_update(chunk("agent_thought_chunk", "reasoning")))
        self.assertEqual("", _text_from_update({"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": "x"}}}))
        self.assertEqual("", _text_from_update(None))
        activity = _activity_from_update(chunk("tool_call", "raw tool payload"))
        self.assertEqual({"kind": "activity", "label": "正在使用工具"}, activity)
        self.assertIsNone(_activity_from_update(chunk("agent_message_chunk", "hi")))


# ---------------------------------------------------------------------------
# Handler-level tests
# ---------------------------------------------------------------------------

class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item

    def tail(self, limit):
        return list(self.records[-limit:])


def _immediate_thread(target, **_kwargs):
    class ImmediateThread:
        def start(self):
            target()

    return ImmediateThread()


class KiroChatHandlerTest(unittest.TestCase):
    def _handler(self, kiro_acp):
        kiro = FakeChat()
        xiaoke = FakeChat()
        state = types.SimpleNamespace(
            contact_chats={"kiro": kiro, "xiaoke": xiaoke},
            contact_routes=default_contact_routes(),
            contact_catalog=[],
            contact_typing_states={"kiro": {"is_typing": False, "since": None}},
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
            chat_stream_revisions={},
            chat_stream_bus=ChatStreamBus(),
            kiro_turn_lock=threading.RLock(),
            kiro_active_turn={},
            kiro_prepare_token="",
            kiro_acp=kiro_acp,
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler.headers = {}
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
        handler._chat_for_contact = lambda contact_id: state.contact_chats[contact_id]
        handler._consume_staged_attachments = lambda body, contact_id: []
        return handler, kiro, xiaoke

    def _acp(self, reply="Kiro 回复。", failure=None):
        def prompt_existing(text, *, on_update=None, **_kwargs):
            if reply and on_update is not None:
                on_update(reply)
            if failure is not None:
                raise failure

        return types.SimpleNamespace(
            prepare_session=lambda **_kw: "kiro-session-1",
            prompt_existing=prompt_existing,
            cancel=lambda _turn, _session: True,
            close=lambda: None,
            new_session=lambda **_kw: "kiro-session-2",
        )

    def test_send_happy_path_appends_user_and_assistant(self):
        handler, kiro, xiaoke = self._handler(self._acp())
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好"}, "kiro")

        self.assertEqual(200, handler.responses[-1][0])
        payload = handler.responses[-1][1]
        self.assertEqual("kiro-acp", payload["turn"]["transport"])
        self.assertEqual("kiro-session-1", payload["turn"]["session_id"])
        self.assertEqual(
            [("user", "你好"), ("assistant", "Kiro 回复。")],
            [(r["role"], r["text"]) for r in kiro.records],
        )
        self.assertEqual([], xiaoke.records)
        self.assertEqual("kiro-acp", kiro.records[-1]["source"])
        self.assertEqual({}, handler.state.kiro_active_turn)
        self.assertFalse(handler.state.contact_typing_states["kiro"]["is_typing"])
        self.assertEqual("completed", handler.state.chat_reply_states["kiro"]["reply_state"])

    def test_chat_send_routes_kiro_through_registry(self):
        handler, kiro, _xiaoke = self._handler(self._acp())
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_chat_send({"contact_id": "kiro", "text": "端到端"})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("端到端", kiro.records[0]["text"])

    def test_contact_directory_advertises_kiro(self):
        handler, _kiro, _xiaoke = self._handler(self._acp())
        contacts = {c["id"]: c for c in chat_contact_directory(handler.state)}
        self.assertIn("kiro", contacts)
        self.assertIn("chat", contacts["kiro"]["capabilities"])
        self.assertFalse(contacts["kiro"]["stop"]["supported"])
        handler2 = types.SimpleNamespace(calls=[])
        handler2._handle_kiro_chat_send = lambda body, contact_id: handler2.calls.append((body, contact_id))
        self.assertTrue(dispatch_contact_send(handler2, "kiro", {"text": "hi"}))

    def test_text_only_ingress_rejects_attachments_and_cards(self):
        self.assertFalse(rejects_inbound({"text": "plain"}))
        self.assertTrue(rejects_inbound({"text": "x", "attachment_ids": ["a"]}))
        self.assertTrue(rejects_inbound({"text": "x", "attachment_url": "/attachments/a"}))
        self.assertTrue(rejects_inbound({"text": "x", "voice_mode": "conversation"}))
        self.assertTrue(rejects_inbound({"text": "x", "metadata": {"via": "card"}}))

        handler, kiro, _xiaoke = self._handler(self._acp())
        handler._handle_chat_send({"contact_id": "kiro", "text": "x", "attachment_ids": ["a"]})
        self.assertEqual(415, handler.responses[-1][0])
        self.assertEqual("kiro_text_only", handler.responses[-1][1]["error"])
        self.assertEqual([], kiro.records)

    def test_empty_text_is_rejected(self):
        handler, kiro, _xiaoke = self._handler(self._acp())
        handler._handle_kiro_chat_send({"text": "  "}, "kiro")
        self.assertEqual(400, handler.responses[-1][0])
        self.assertEqual([], kiro.records)

    def test_busy_turn_rejects_before_history_or_prompt(self):
        acp = self._acp()
        handler, kiro, _xiaoke = self._handler(acp)
        handler.state.kiro_active_turn = {
            "user_ts": "active",
            "cancel_event": threading.Event(),
            "session_id": "kiro-session-1",
        }
        handler._handle_kiro_chat_send({"text": "第二条"}, "kiro")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kiro_turn_active", handler.responses[-1][1]["error"])
        self.assertEqual([], kiro.records)

    def test_auth_required_at_prepare_is_clean_503_without_history(self):
        def prepare(**_kwargs):
            raise KiroACPAuthRequired("Kiro login is required")

        acp = self._acp()
        acp.prepare_session = prepare
        closed = []
        acp.close = lambda: closed.append(True)
        handler, kiro, _xiaoke = self._handler(acp)
        handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual(503, handler.responses[-1][0])
        self.assertEqual("kiro_auth_required", handler.responses[-1][1]["error"])
        self.assertIn("登录", handler.responses[-1][1]["reason"])
        self.assertEqual([], kiro.records)
        self.assertEqual([True], closed)
        self.assertEqual("", handler.state.kiro_prepare_token)

    def test_quota_exceeded_at_prepare_is_clean_503(self):
        def prepare(**_kwargs):
            raise KiroACPQuotaExceeded("Kiro credits or quota are exhausted")

        acp = self._acp()
        acp.prepare_session = prepare
        handler, kiro, _xiaoke = self._handler(acp)
        handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual(503, handler.responses[-1][0])
        self.assertEqual("kiro_quota_exceeded", handler.responses[-1][1]["error"])
        self.assertEqual([], kiro.records)

    def test_generic_prepare_failure_never_leaks_internal_detail(self):
        def prepare(**_kwargs):
            raise KiroACPError("Kiro ACP /private/path failed")

        acp = self._acp()
        acp.prepare_session = prepare
        handler, kiro, _xiaoke = self._handler(acp)
        handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual(503, handler.responses[-1][0])
        self.assertEqual("kiro_unavailable", handler.responses[-1][1]["error"])
        self.assertNotIn("/private/path", str(handler.responses))
        self.assertEqual([], kiro.records)

    def test_mid_turn_auth_failure_replies_with_login_guidance(self):
        handler, kiro, _xiaoke = self._handler(self._acp(reply="", failure=KiroACPAuthRequired("x")))
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("user", kiro.records[0]["role"])
        self.assertIn("登录", kiro.records[-1]["text"])
        self.assertEqual("kiro-acp:auth-required", kiro.records[-1]["source"])

    def test_mid_turn_quota_failure_replies_with_quota_guidance(self):
        handler, kiro, _xiaoke = self._handler(self._acp(reply="", failure=KiroACPQuotaExceeded("x")))
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertIn("额度", kiro.records[-1]["text"])

    def test_mid_turn_error_preserves_user_message_and_degrades(self):
        handler, kiro, _xiaoke = self._handler(
            self._acp(reply="", failure=KiroACPError("internal transport detail"))
        )
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "保留我"}, "kiro")
        self.assertEqual(
            [("user", "保留我"), ("assistant", "Kiro 这次没有成功回复。请稍后重试；原消息已经保留。")],
            [(r["role"], r["text"]) for r in kiro.records],
        )
        self.assertNotIn("internal transport detail", str(kiro.records))
        self.assertEqual({}, handler.state.kiro_active_turn)

    def test_mid_turn_crash_degrades_without_leaking(self):
        handler, kiro, _xiaoke = self._handler(
            self._acp(reply="", failure=RuntimeError("/private/path boom"))
        )
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertIn("异常退出", kiro.records[-1]["text"])
        self.assertNotIn("/private/path", str(kiro.records))

    def test_history_append_failure_is_500_without_prompt(self):
        acp = self._acp()
        prompted = []
        original_prompt = acp.prompt_existing
        acp.prompt_existing = lambda *a, **k: prompted.append(True) or original_prompt(*a, **k)
        handler, kiro, _xiaoke = self._handler(acp)

        def fail_append(**_record):
            raise RuntimeError("disk gone")

        kiro.append = fail_append
        handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual(500, handler.responses[-1][0])
        self.assertEqual("kiro_history_unavailable", handler.responses[-1][1]["error"])
        self.assertEqual([], prompted)

    def test_new_session_endpoint_recovers_stuck_pointer(self):
        acp = self._acp()
        handler, kiro, _xiaoke = self._handler(acp)
        handler._handle_kiro_new_session({})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("kiro-session-2", handler.responses[-1][1]["session_id"])
        self.assertIn("新的 Kiro 会话", kiro.records[-1]["text"])

    def test_new_session_rejected_while_turn_active(self):
        acp = self._acp()
        called = []
        acp.new_session = lambda **_kw: called.append(True) or "x"
        handler, kiro, _xiaoke = self._handler(acp)
        handler.state.kiro_active_turn = {"user_ts": "t", "cancel_event": threading.Event(), "session_id": "s"}
        handler._handle_kiro_new_session({})
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual([], called)

    def test_new_session_dispatches_through_contact_post_routes(self):
        handler = types.SimpleNamespace(calls=[])
        handler._handle_kiro_new_session = lambda body: handler.calls.append(body)
        self.assertTrue(dispatch_contact_post(handler, "/kiro/new_session", {}))
        self.assertEqual([{}], handler.calls)


# ---------------------------------------------------------------------------
# kiro 切模型 (2026-09-10): catalog capture, pinning, preference store, routes
# ---------------------------------------------------------------------------

_KIRO_MODELS_BLOCK = {
    "currentModelId": "auto",
    "availableModels": [
        {"modelId": "auto", "name": "auto", "description": "Models chosen by task"},
        {"modelId": "claude-sonnet-4.5", "name": "claude-sonnet-4.5", "description": "Sonnet"},
        {"modelId": "claude-haiku-4.5", "name": "claude-haiku-4.5", "description": "Haiku"},
        # Wire junk is dropped: bad ids, duplicates, control characters.
        {"modelId": "bad id with spaces", "name": "x", "description": "y"},
        {"modelId": "auto", "name": "dupe", "description": "dupe"},
        {"modelId": "glm-5", "name": "gl\x00m\n5", "description": "GLM\x1f"},
    ],
}


def _catalog_handler(session_id="kiro-session-1"):
    base = _basic_handler(session_id)

    def handle(process, message):
        method = message.get("method")
        if method == "session/new":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                "sessionId": session_id, "models": _KIRO_MODELS_BLOCK,
            }}]
        if method == "session/load":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                "sessionId": message["params"]["sessionId"], "models": _KIRO_MODELS_BLOCK,
            }}]
        if method == "session/set_model":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {}}]
        return base(process, message)

    return handle


class KiroModelCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = str(Path(self.tmp.name) / "kiro_acp_session.json")

    def _client(self, factory):
        return KiroACPClient(
            command="/fake/kiro-cli",
            cwd=self.tmp.name,
            state_path=self.state_path,
            request_timeout=5,
            prompt_timeout=10,
            popen_factory=factory,
        )

    def test_catalog_captured_sanitized_and_cached(self):
        process = FakeKiroACPProcess(_catalog_handler())
        client = self._client(_scripted_factory([process]))

        client.prepare_session()

        entries, source = client.available_models()
        self.assertEqual("live", source)
        self.assertEqual(
            ["auto", "claude-sonnet-4.5", "claude-haiku-4.5", "glm-5"],
            [entry["id"] for entry in entries],
        )
        self.assertEqual("gl m 5", entries[-1]["name"])
        self.assertEqual("auto", client.current_model_id())
        # Cache file landed next to the session pointer with 0600.
        cache_path = Path(self.tmp.name) / "kiro_models_cache.json"
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["version"])
        self.assertEqual(4, len(payload["models"]))
        self.assertEqual(0o600, cache_path.stat().st_mode & 0o777)

    def test_fresh_client_reads_catalog_from_cache(self):
        process = FakeKiroACPProcess(_catalog_handler())
        first = self._client(_scripted_factory([process]))
        first.prepare_session()

        second = self._client(_scripted_factory([]))
        entries, source = second.available_models()
        self.assertEqual("cache", source)
        self.assertEqual(("auto", "claude-sonnet-4.5", "claude-haiku-4.5", "glm-5"),
                         second.available_model_ids())

        empty = KiroACPClient(
            command="/fake/kiro-cli",
            cwd=self.tmp.name,
            state_path=str(Path(self.tmp.name) / "other_session.json"),
            request_timeout=5,
            prompt_timeout=10,
            popen_factory=_scripted_factory([]),
            catalog_path=str(Path(self.tmp.name) / "absent_cache.json"),
        )
        self.assertEqual(([], "none"), empty.available_models())

    def test_prepare_with_model_pins_and_sets_model_once(self):
        process = FakeKiroACPProcess(_catalog_handler())
        client = self._client(_scripted_factory([process]))

        session_id = client.prepare_session(model="claude-haiku-4.5")

        self.assertEqual("kiro-session-1", session_id)
        set_calls = [r for r in process.requests if r.get("method") == "session/set_model"]
        self.assertEqual(1, len(set_calls))
        self.assertEqual({"sessionId": "kiro-session-1", "modelId": "claude-haiku-4.5"},
                         set_calls[0]["params"])
        self.assertEqual("claude-haiku-4.5", client.current_model_id())
        # A second prepare sees the pin already current and stays quiet.
        client.prepare_session(model="claude-haiku-4.5")
        set_calls = [r for r in process.requests if r.get("method") == "session/set_model"]
        self.assertEqual(1, len(set_calls))

    def test_pinned_model_reapply_failure_does_not_break_prepare(self):
        # set_model RPC 失败只告警不抛出——钉选已持久化，下次 prepare 自愈。
        base = _catalog_handler()

        def handle(process, message):
            if message.get("method") == "session/set_model":
                return [{"jsonrpc": "2.0", "id": message["id"],
                         "error": {"code": -32000, "message": "boom"}}]
            return base(process, message)

        process = FakeKiroACPProcess(handle)
        client = self._client(_scripted_factory([process]))
        client.pin_model("claude-haiku-4.5")

        session_id = client.prepare_session()

        self.assertEqual("kiro-session-1", session_id)

    def test_pin_survives_restart_and_reapplies_after_load(self):
        Path(self.state_path).write_text(
            json.dumps({"version": 2, "session_id": "durable", "cwd": str(Path(self.tmp.name).resolve())}),
            encoding="utf-8",
        )
        first = FakeKiroACPProcess(_catalog_handler(session_id="durable"))
        client = self._client(_scripted_factory([first]))
        client.prepare_session(model="claude-sonnet-4.5")
        client.close()

        # After a process restart the session reloads with currentModelId
        # reset to auto, so the pin must be re-applied on the fresh process.
        second = FakeKiroACPProcess(_catalog_handler(session_id="durable"))
        client._popen_factory = _scripted_factory([second])
        self.assertEqual("durable", client.prepare_session())
        set_calls = [r for r in second.requests if r.get("method") == "session/set_model"]
        self.assertEqual(1, len(set_calls))
        self.assertEqual("claude-sonnet-4.5", set_calls[0]["params"]["modelId"])

    def test_pin_not_in_catalog_is_skipped_on_prepare(self):
        def handler_no_haiku(process, message):
            if message.get("method") == "session/new":
                return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                    "sessionId": "s1",
                    "models": {"currentModelId": "auto", "availableModels": [
                        {"modelId": "auto", "name": "auto", "description": ""},
                    ]},
                }}]
            return _basic_handler(session_id="s1")(process, message)

        # The pin predates the catalog (e.g. saved while the plan offered it).
        process = FakeKiroACPProcess(_catalog_handler(session_id="s1"))
        client = self._client(_scripted_factory([process]))
        client.prepare_session(model="claude-haiku-4.5")
        client.close()
        shrunk = FakeKiroACPProcess(handler_no_haiku)
        client._popen_factory = _scripted_factory([shrunk])
        client._loaded_session_id = ""
        client._save_session_id("s1")
        # Fresh pointer state: force a re-new by removing the pointer.
        Path(self.state_path).unlink()

        self.assertEqual("s1", client.prepare_session())
        set_calls = [r for r in shrunk.requests if r.get("method") == "session/set_model"]
        self.assertEqual([], set_calls)

    def test_set_model_validates_against_catalog_and_charset(self):
        process = FakeKiroACPProcess(_catalog_handler())
        client = self._client(_scripted_factory([process]))
        client.prepare_session()
        with self.assertRaisesRegex(KiroACPError, "not in the available catalog"):
            client.set_model("gpt-99")
        with self.assertRaisesRegex(KiroACPError, "invalid Kiro model id"):
            client.set_model("bad id")
        with self.assertRaisesRegex(KiroACPError, "not prepared"):
            self._client(_scripted_factory([])).set_model("auto")

    def test_new_session_applies_model(self):
        process = FakeKiroACPProcess(_catalog_handler(session_id="fresh"))
        client = self._client(_scripted_factory([process]))
        self.assertEqual("fresh", client.new_session(model="glm-5"))
        set_calls = [r for r in process.requests if r.get("method") == "session/set_model"]
        self.assertEqual("glm-5", set_calls[0]["params"]["modelId"])
        self.assertEqual("fresh", client.load_session_id())


class KiroPreferenceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "kiro_preferences.json"

    def _store(self, catalog=("auto", "claude-haiku-4.5")):
        return KiroPreferenceStore(self.path, catalog_loader=lambda: catalog)

    def test_default_is_auto_and_persists_across_instances(self):
        store = self._store()
        self.assertEqual(KIRO_APP_DEFAULT_MODEL, store.snapshot())
        self.assertEqual("claude-haiku-4.5", store.save_validated("claude-haiku-4.5"))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual("claude-haiku-4.5", self._store().snapshot())

    def test_empty_catalog_fails_closed(self):
        store = self._store(catalog=())
        self.assertEqual("auto", store.snapshot())
        with self.assertRaises(KiroPreferenceError):
            store.save_validated("auto")
        self.assertFalse(self.path.exists())

    def test_unknown_or_malformed_model_rejected(self):
        store = self._store()
        for bad in ("gpt-99", "", "../escape", "a" * 300):
            with self.assertRaises(KiroPreferenceError, msg=bad):
                store.save_validated(bad)
        self.assertEqual("auto", store.snapshot())

    def test_catalog_contradiction_drops_persisted_model(self):
        store = self._store()
        store.save_validated("claude-haiku-4.5")
        shrunk = KiroPreferenceStore(self.path, catalog_loader=lambda: ("auto",))
        self.assertEqual("auto", shrunk.snapshot())
        # An unavailable catalog cannot disprove a persisted selection.
        offline = KiroPreferenceStore(self.path, catalog_loader=lambda: ())
        self.assertEqual("claude-haiku-4.5", offline.snapshot())


class KiroPreferencesHandlerTest(unittest.TestCase):
    def _handler(self, acp, store):
        kiro = FakeChat()
        state = types.SimpleNamespace(
            contact_chats={"kiro": kiro},
            kiro_turn_lock=threading.RLock(),
            kiro_active_turn={},
            kiro_prepare_token="",
            kiro_acp=acp,
            kiro_preferences=store,
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._chat_for_contact = lambda contact_id: state.contact_chats[contact_id]
        handler._send_chat_notification = lambda *a, **k: None
        return handler, kiro

    def _acp(self, calls, models=("auto", "claude-haiku-4.5", "claude-sonnet-4.5")):
        entries = [{"id": m, "name": m, "description": f"{m} desc"} for m in models]
        return types.SimpleNamespace(
            available_models=lambda: (entries, "live"),
            available_model_ids=lambda: models,
            set_model=lambda m: calls.append(("set_model", m)) or m,
            pin_model=lambda m: calls.append(("pin_model", m)) or m,
            busy=False,
        )

    def _store(self, tmp, catalog=("auto", "claude-haiku-4.5", "claude-sonnet-4.5")):
        return KiroPreferenceStore(Path(tmp) / "kiro_preferences.json", catalog_loader=lambda: catalog)

    def test_get_payload_mirrors_kimi_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, _kiro = self._handler(self._acp(calls), self._store(tmp))
            handler._handle_kiro_preferences_get()
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual("Kiro", payload["provider"])
        self.assertEqual("auto", payload["model"])
        self.assertEqual("auto", payload["selection"]["model"])
        self.assertEqual(["auto", "claude-haiku-4.5", "claude-sonnet-4.5"], payload["available_models"])
        self.assertEqual("claude-haiku-4.5 desc", payload["models"][1]["description"])
        self.assertEqual("live", payload["catalog_source"])
        self.assertFalse(payload["busy"])
        self.assertEqual("next_turn", payload["applies_from"])

    def test_post_valid_model_persists_applies_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            handler._handle_kiro_preferences_post({"model": "claude-haiku-4.5"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["applied_immediately"])
            self.assertEqual("claude-haiku-4.5", payload["model"])
            self.assertEqual([("set_model", "claude-haiku-4.5")], calls)
            persisted = json.loads((Path(tmp) / "kiro_preferences.json").read_text(encoding="utf-8"))
            self.assertEqual("claude-haiku-4.5", persisted["model"])
            self.assertIn("已切到 claude-haiku-4.5 模型", kiro.records[-1]["text"])
            self.assertEqual("system:kiro-model-switch", kiro.records[-1]["source"])
            # 选择跨重启保留。
            self.assertEqual("claude-haiku-4.5", self._store(tmp).snapshot())

    def test_post_same_model_does_not_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            handler._handle_kiro_preferences_post({"model": "auto"})
            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual([], kiro.records)

    def test_post_invalid_model_is_400_and_unknown_catalog_is_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            handler._handle_kiro_preferences_post({"model": "gpt-99"})
            self.assertEqual(400, handler.responses[-1][0])
            self.assertEqual("invalid_kiro_selection", handler.responses[-1][1]["error"])
            self.assertEqual([], calls)
            self.assertEqual([], kiro.records)

            empty_store = KiroPreferenceStore(Path(tmp) / "other.json", catalog_loader=lambda: ())
            handler2, _ = self._handler(self._acp([]), empty_store)
            handler2._handle_kiro_preferences_post({"model": "auto"})
            self.assertEqual(503, handler2.responses[-1][0])
            self.assertEqual("kiro_model_catalog_unavailable", handler2.responses[-1][1]["error"])

    def test_post_while_busy_pins_without_set_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)
            acp.busy = True
            handler, kiro = self._handler(acp, self._store(tmp))
            handler.state.kiro_active_turn = {"user_ts": "t", "cancel_event": threading.Event(), "session_id": "s"}
            handler._handle_kiro_preferences_post({"model": "claude-sonnet-4.5"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertFalse(payload["applied_immediately"])
            self.assertEqual([("pin_model", "claude-sonnet-4.5")], calls)
            self.assertIn("下一条回复", kiro.records[-1]["text"])
            self.assertTrue(payload["busy"])

    def test_post_set_model_failure_still_saves_for_next_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)

            def failing_set(model):
                calls.append(("set_model", model))
                raise KiroACPError("Kiro ACP session was not prepared")

            acp.set_model = failing_set
            handler, kiro = self._handler(acp, self._store(tmp))
            handler._handle_kiro_preferences_post({"model": "claude-haiku-4.5"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertFalse(payload["applied_immediately"])
            self.assertIn("下一条回复", kiro.records[-1]["text"])
            self.assertNotIn("not prepared", kiro.records[-1]["text"])

    def test_preferences_routes_dispatch_through_registry(self):
        handler = types.SimpleNamespace(calls=[])
        handler._handle_kiro_preferences_get = lambda: handler.calls.append(("get",))
        handler._handle_kiro_preferences_post = lambda body: handler.calls.append(("post", body))
        self.assertTrue(dispatch_contact_get(handler, "/kiro/preferences"))
        self.assertTrue(dispatch_contact_post(handler, "/kiro/preferences", {"model": "auto"}))
        self.assertEqual([("get",), ("post", {"model": "auto"})], handler.calls)

    def test_chat_prepare_forwards_persisted_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)
            acp.prepare_session = lambda **kw: calls.append(("prepare", kw.get("model"))) or "s1"
            acp.prompt_existing = lambda text, *, on_update=None, **_kw: on_update and on_update("好")
            acp.cancel = lambda *_a: True
            acp.close = lambda: None
            store = self._store(tmp)
            store.save_validated("claude-haiku-4.5")
            handler, kiro = self._handler(acp, store)
            state = handler.state
            # chat send 需要的状态件补齐。
            state.contact_typing_states = {"kiro": {"is_typing": False, "since": None}}
            state.chat_draft_lock = threading.Lock()
            state.chat_drafts = {}
            state.chat_reply_states = {}
            state.chat_stream_revisions = {}
            state.chat_stream_bus = ChatStreamBus()
            handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
            with patch("push.threading.Thread", _immediate_thread):
                handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual(("prepare", "claude-haiku-4.5"), calls[0])


if __name__ == "__main__":
    unittest.main()
