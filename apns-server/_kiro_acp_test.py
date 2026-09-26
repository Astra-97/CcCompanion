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
    _metadata_context_percent,
    _metadata_effort,
    _text_from_update,
)
# kiro 切模型 (2026-09-10) + kiro 推理强度 (2026-09-10)
from kiro_preferences import (
    KIRO_APP_DEFAULT_EFFORT,
    KIRO_APP_DEFAULT_MODEL,
    KIRO_APP_EFFORTS,
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

    # kiro 状态栏 (2026-09-10)
    def test_metadata_notification_caches_latest_context_usage(self):
        def handle(_process, message):
            if message.get("method") != "session/prompt":
                return _basic_handler()(_process, message)
            request_id = message["id"]
            session_id = message["params"]["sessionId"]
            return [
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "sessionId": session_id, "contextUsagePercentage": 17.25}},
                # A foreign session's metadata must never overwrite ours.
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "sessionId": "someone-else", "contextUsagePercentage": 88.8}},
                # Tolerate a nested metadata envelope and the bare spelling.
                {"jsonrpc": "2.0", "method": "kiro.dev/metadata", "params": {
                    "metadata": {"contextUsagePercentage": 23.5}}},
                # Malformed values must never overwrite the last good one.
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "contextUsagePercentage": "junk"}},
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "contextUsagePercentage": 412}},
                {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}},
            ]

        process = FakeKiroACPProcess(handle)
        client = self._client(_scripted_factory([process]))
        session_id = client.prepare_session()
        self.assertIsNone(client.context_usage_percent())

        client.prompt_existing("hello", session_id=session_id, turn_id="turn-1")

        self.assertEqual(23.5, client.context_usage_percent())

    def test_metadata_context_percent_extraction_is_bounded(self):
        self.assertEqual(0.0, _metadata_context_percent({"contextUsagePercentage": 0}))
        self.assertEqual(100.0, _metadata_context_percent({"contextUsagePercentage": 100}))
        self.assertEqual(
            12.5,
            _metadata_context_percent({"data": {"contextUsagePercentage": "12.5"}}),
        )
        for bad in (
            None,
            "12",
            {"contextUsagePercentage": True},
            {"contextUsagePercentage": -1},
            {"contextUsagePercentage": 100.5},
            {"contextUsagePercentage": "nope"},
            {"metadata": {"contextUsagePercentage": None}},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(_metadata_context_percent(bad))

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
        # kiro 对齐 CC (2026-09-26): Stop + attachments are now advertised
        # with the same exact-turn stop contract as Kimi.
        self.assertIn("attachments", contacts["kiro"]["capabilities"])
        self.assertTrue(contacts["kiro"]["stop"]["supported"])
        self.assertEqual("/chat/stop", contacts["kiro"]["stop"]["endpoint"])
        self.assertEqual(["contact_id", "user_ts"], contacts["kiro"]["stop"]["required_fields"])
        handler2 = types.SimpleNamespace(calls=[])
        handler2._handle_kiro_chat_send = lambda body, contact_id: handler2.calls.append((body, contact_id))
        self.assertTrue(dispatch_contact_send(handler2, "kiro", {"text": "hi"}))

    def test_text_only_ingress_rejects_attachments_and_cards(self):
        self.assertFalse(rejects_inbound({"text": "plain"}))
        # kiro 对齐 CC (2026-09-26): opaque staged IDs are allowed (Kimi parity);
        # every legacy attachment/voice/card shape is still rejected.
        self.assertFalse(rejects_inbound({"text": "x", "attachment_ids": ["a"]}))
        self.assertTrue(rejects_inbound({"text": "x", "attachment_url": "/attachments/a"}))
        self.assertTrue(rejects_inbound({"text": "x", "attachment_path": "/tmp/a.png"}))
        self.assertTrue(rejects_inbound({"text": "x", "voice_mode": "conversation"}))
        self.assertTrue(rejects_inbound({"text": "x", "metadata": {"via": "card"}}))

        handler, kiro, _xiaoke = self._handler(self._acp())
        handler._handle_chat_send({"contact_id": "kiro", "text": "x", "attachment_url": "/attachments/a"})
        self.assertEqual(415, handler.responses[-1][0])
        self.assertEqual("kiro_text_only", handler.responses[-1][1]["error"])
        self.assertEqual([], kiro.records)

    def test_empty_text_is_rejected(self):
        handler, kiro, _xiaoke = self._handler(self._acp())
        handler._handle_kiro_chat_send({"text": "  "}, "kiro")
        self.assertEqual(400, handler.responses[-1][0])
        self.assertEqual([], kiro.records)

    def test_busy_turn_queues_without_prompting(self):
        # kiro r4 (2026-09-26): Kimi parity — a message sent while an App turn
        # runs is accepted into the queue (was 409 kiro_turn_active).
        acp = self._acp()
        handler, kiro, _xiaoke = self._handler(acp)
        handler.state.kiro_active_turn = {
            "user_ts": "active",
            "cancel_event": threading.Event(),
            "session_id": "kiro-session-1",
        }
        with patch("push.threading.Thread"):
            handler._handle_kiro_chat_send({"text": "第二条"}, "kiro")
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual((True, 1, "kiro_turn_active"),
                         (payload["queued"], payload["queue_position"], payload["reason"]))
        self.assertEqual(["第二条"], [row["text"] for row in kiro.records])
        self.assertEqual(1, len(handler.state.kiro_chat_queue))

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


# ---------------------------------------------------------------------------
# kiro 推理强度 (2026-09-10): TuiCommand bridge, pin replay, metadata read-back
# ---------------------------------------------------------------------------

def _effort_handler(session_id="kiro-session-1", *, supported=True):
    """Catalog handler answering the /effort TuiCommand bridge like 2.21.2."""
    base = _catalog_handler(session_id)

    def handle(process, message):
        if message.get("method") == "_kiro.dev/commands/execute":
            command = (message.get("params") or {}).get("command") or {}
            if command.get("command") == "effort":
                value = str((command.get("args") or {}).get("value") or "")
                if supported and value in ("low", "medium", "high", "xhigh", "max"):
                    return [{"jsonrpc": "2.0", "id": message["id"], "result": {"success": True}}]
                return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                    "success": False,
                    "message": "Effort configuration is currently not available — provider private detail",
                }}]
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"success": False}}]
        return base(process, message)

    return handle


def _effort_execute_calls(process):
    return [
        r for r in process.requests
        if r.get("method") == "_kiro.dev/commands/execute"
        and (r.get("params", {}).get("command") or {}).get("command") == "effort"
    ]


class KiroEffortTest(unittest.TestCase):
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

    def test_set_effort_sends_tui_command_bridge_shape(self):
        process = FakeKiroACPProcess(_effort_handler())
        client = self._client(_scripted_factory([process]))
        client.prepare_session()

        self.assertEqual("high", client.set_effort("high"))

        calls = _effort_execute_calls(process)
        self.assertEqual(1, len(calls))
        self.assertEqual(
            {"sessionId": "kiro-session-1", "command": {"command": "effort", "args": {"value": "high"}}},
            calls[0]["params"],
        )
        self.assertEqual("high", client.current_effort())
        # 已确认在线：再次 prepare 不重复发送。
        client.prepare_session()
        self.assertEqual(1, len(_effort_execute_calls(process)))

    def test_pin_effort_validates_closed_set(self):
        client = self._client(_scripted_factory([]))
        for bad in ("", "bogus", "HIGHx", "../high"):
            with self.assertRaisesRegex(KiroACPError, "invalid Kiro effort", msg=bad):
                client.pin_effort(bad)
        self.assertEqual("xhigh", client.pin_effort(" XHigh "))

    def test_set_effort_refusal_raises_without_provider_message(self):
        process = FakeKiroACPProcess(_effort_handler(supported=False))
        client = self._client(_scripted_factory([process]))
        client.prepare_session()

        with self.assertRaises(KiroACPError) as ctx:
            client.set_effort("high")
        self.assertNotIn("private", str(ctx.exception))
        self.assertEqual("", client.current_effort())
        # 钉选仍然记下，供后续 prepare 重放。
        self.assertEqual("high", client._pinned_effort)

    def test_prepare_replays_effort_after_model_pin(self):
        process = FakeKiroACPProcess(_effort_handler())
        client = self._client(_scripted_factory([process]))

        client.prepare_session(model="claude-sonnet-4.5", effort="max")

        methods = [r.get("method") for r in process.requests]
        self.assertLess(methods.index("session/set_model"), methods.index("_kiro.dev/commands/execute"))
        self.assertEqual("max", client.current_effort())

    def test_effort_refusal_defers_until_model_or_session_changes(self):
        process = FakeKiroACPProcess(_effort_handler(supported=False))
        client = self._client(_scripted_factory([process]))
        client.prepare_session(model="auto", effort="high")
        self.assertEqual(1, len(_effort_execute_calls(process)))

        # 同一 (session, model, effort) 组合下不再每轮重试、不再刷告警。
        client.prepare_session(model="auto", effort="high")
        self.assertEqual(1, len(_effort_execute_calls(process)))

        # 换了模型（可能支持 effort）立即重试。
        client.prepare_session(model="claude-sonnet-4.5", effort="high")
        self.assertEqual(2, len(_effort_execute_calls(process)))

    def test_effort_pin_survives_restart_and_replays_after_load(self):
        Path(self.state_path).write_text(
            json.dumps({"version": 2, "session_id": "durable", "cwd": str(Path(self.tmp.name).resolve())}),
            encoding="utf-8",
        )
        first = FakeKiroACPProcess(_effort_handler(session_id="durable"))
        client = self._client(_scripted_factory([first]))
        client.prepare_session(effort="low")
        self.assertEqual("low", client.current_effort())
        client.close()

        second = FakeKiroACPProcess(_effort_handler(session_id="durable"))
        client._popen_factory = _scripted_factory([second])
        self.assertEqual("durable", client.prepare_session())
        calls = _effort_execute_calls(second)
        self.assertEqual(1, len(calls))
        self.assertEqual("low", calls[0]["params"]["command"]["args"]["value"])

    def test_new_session_same_process_clears_readback_and_replays(self):
        """审核修复 (2026-09-10)：session/new 是否保留 effort 未经证实，客户端
        保守地在会话切换时清读回缓存，钉选必须重放而不是走"已是当前值"捷径。"""
        process = FakeKiroACPProcess(_effort_handler())
        client = self._client(_scripted_factory([process]))
        client.prepare_session(effort="high")
        self.assertEqual("high", client.current_effort())
        self.assertEqual(1, len(_effort_execute_calls(process)))

        # 同进程新会话（进程没有重启，_start 不会清缓存）。
        def new_handler(_p, message):
            if message.get("method") == "session/new":
                return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                    "sessionId": "fresh-2", "models": _KIRO_MODELS_BLOCK,
                }}]
            return _effort_handler()(_p, message)

        process._handler = new_handler
        self.assertEqual("fresh-2", client.new_session())
        # 重放已在新会话上发生：execute 带着新 sessionId 再发一次。
        calls = _effort_execute_calls(process)
        self.assertEqual(2, len(calls))
        self.assertEqual("fresh-2", calls[-1]["params"]["sessionId"])
        self.assertEqual("high", calls[-1]["params"]["command"]["args"]["value"])
        self.assertEqual("high", client.current_effort())

    def test_session_load_transition_clears_readback_and_replays(self):
        """session/load 同理：读回缓存按会话围栏，load 之后钉选重放。"""
        first = FakeKiroACPProcess(_effort_handler())
        client = self._client(_scripted_factory([first]))
        client.prepare_session(effort="max")
        self.assertEqual("max", client.current_effort())
        client._save_session_id("kiro-session-1")
        client._loaded_session_id = ""  # 强制下一轮 prepare 走 load 而非快车道
        # 进程仍存活（同进程 load），_start 不会清缓存。
        self.assertEqual("kiro-session-1", client.prepare_session())
        self.assertEqual("max", client.current_effort())
        calls = _effort_execute_calls(first)
        self.assertEqual(2, len(calls))
        self.assertEqual("max", calls[-1]["params"]["command"]["args"]["value"])

    def test_metadata_effort_read_back_is_validated_and_session_fenced(self):
        self.assertEqual("high", _metadata_effort({"effort": "high"}))
        self.assertEqual("max", _metadata_effort({"effort": " Max "}))
        for bad in (None, {}, {"effort": "bogus"}, {"effort": 3}, {"effort": True}):
            self.assertEqual("", _metadata_effort(bad))

        def handle(_process, message):
            if message.get("method") != "session/prompt":
                return _basic_handler()(_process, message)
            request_id = message["id"]
            session_id = message["params"]["sessionId"]
            return [
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "sessionId": session_id, "effort": "xhigh"}},
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "sessionId": "someone-else", "effort": "low"}},
                {"jsonrpc": "2.0", "method": "_kiro.dev/metadata", "params": {
                    "sessionId": session_id, "effort": "bogus"}},
                {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}},
            ]

        process = FakeKiroACPProcess(handle)
        client = self._client(_scripted_factory([process]))
        session_id = client.prepare_session()
        self.assertEqual("", client.current_effort())

        client.prompt_existing("hello", session_id=session_id, turn_id="turn-1")

        self.assertEqual("xhigh", client.current_effort())

    def test_new_session_applies_effort(self):
        process = FakeKiroACPProcess(_effort_handler(session_id="fresh"))
        client = self._client(_scripted_factory([process]))
        self.assertEqual("fresh", client.new_session(model="auto", effort="medium"))
        calls = _effort_execute_calls(process)
        self.assertEqual(1, len(calls))
        self.assertEqual("medium", calls[0]["params"]["command"]["args"]["value"])

class KiroPreferenceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "kiro_preferences.json"

    def _store(self, catalog=("auto", "claude-haiku-4.5")):
        return KiroPreferenceStore(self.path, catalog_loader=lambda: catalog)

    def test_default_is_auto_and_persists_across_instances(self):
        store = self._store()
        self.assertEqual(KIRO_APP_DEFAULT_MODEL, store.snapshot_model())
        self.assertEqual((KIRO_APP_DEFAULT_MODEL, KIRO_APP_DEFAULT_EFFORT), store.snapshot())
        self.assertEqual(("claude-haiku-4.5", KIRO_APP_DEFAULT_EFFORT),
                         store.save_validated("claude-haiku-4.5"))
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual("claude-haiku-4.5", self._store().snapshot_model())

    def test_empty_catalog_fails_closed(self):
        store = self._store(catalog=())
        self.assertEqual("auto", store.snapshot_model())
        with self.assertRaises(KiroPreferenceError):
            store.save_validated("auto")
        self.assertFalse(self.path.exists())

    def test_unknown_or_malformed_model_rejected(self):
        store = self._store()
        for bad in ("gpt-99", "", "../escape", "a" * 300):
            with self.assertRaises(KiroPreferenceError, msg=bad):
                store.save_validated(bad)
        self.assertEqual("auto", store.snapshot_model())

    def test_catalog_contradiction_drops_persisted_model(self):
        store = self._store()
        store.save_validated("claude-haiku-4.5")
        shrunk = KiroPreferenceStore(self.path, catalog_loader=lambda: ("auto",))
        self.assertEqual("auto", shrunk.snapshot_model())
        # An unavailable catalog cannot disprove a persisted selection.
        offline = KiroPreferenceStore(self.path, catalog_loader=lambda: ())
        self.assertEqual("claude-haiku-4.5", offline.snapshot_model())

    # kiro 推理强度 (2026-09-10)
    def test_effort_defaults_persists_and_validates_closed_set(self):
        store = self._store()
        self.assertEqual(KIRO_APP_DEFAULT_EFFORT, store.snapshot_effort())
        self.assertEqual(("auto", "xhigh"), store.save_validated(effort="XHigh"))
        self.assertEqual(("auto", "xhigh"), self._store().snapshot())
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({"version": 1, "model": "auto", "effort": "xhigh"}, persisted)
        for bad in ("", "bogus", " ultra ", "high;"):
            with self.assertRaises(KiroPreferenceError, msg=bad):
                self._store().save_validated(effort=bad)
        self.assertEqual(("auto", "xhigh"), self._store().snapshot())

    def test_effort_only_save_keeps_model_and_needs_no_catalog(self):
        store = self._store()
        store.save_validated("claude-haiku-4.5")
        # effort 校验不依赖动态目录：目录为空时也能单独保存 effort。
        offline = KiroPreferenceStore(self.path, catalog_loader=lambda: ())
        self.assertEqual(("claude-haiku-4.5", "low"), offline.save_validated(effort="low"))

    def test_legacy_file_without_effort_gets_default(self):
        self.path.write_text(
            json.dumps({"version": 1, "model": "claude-haiku-4.5"}), encoding="utf-8"
        )
        store = self._store()
        self.assertEqual(("claude-haiku-4.5", KIRO_APP_DEFAULT_EFFORT), store.snapshot())

    def test_unknown_persisted_effort_falls_back_to_default(self):
        self.path.write_text(
            json.dumps({"version": 1, "model": "auto", "effort": "ludicrous"}), encoding="utf-8"
        )
        self.assertEqual(("auto", KIRO_APP_DEFAULT_EFFORT), self._store().snapshot())


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
            # kiro 推理强度 (2026-09-10)
            set_effort=lambda e: calls.append(("set_effort", e)) or e,
            pin_effort=lambda e: calls.append(("pin_effort", e)) or e,
            current_effort=lambda: "",
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
        # kiro 推理强度 (2026-09-10)：当前值 + 封闭五档表 + wire 读回。
        self.assertEqual(KIRO_APP_DEFAULT_EFFORT, payload["effort"])
        self.assertEqual(KIRO_APP_DEFAULT_EFFORT, payload["selection"]["effort"])
        self.assertEqual(list(KIRO_APP_EFFORTS), payload["available_efforts"])
        self.assertEqual("", payload["current_effort"])

    # kiro 状态栏 (2026-09-10)
    def test_get_payload_includes_header_status_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)
            acp.current_model_id = lambda: "claude-haiku-4.5"
            acp.context_usage_percent = lambda: 42.4
            handler, _kiro = self._handler(acp, self._store(tmp))
            handler._handle_kiro_preferences_get()
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual("claude-haiku-4.5", payload["current_model"])
        self.assertEqual(42.4, payload["context_usage_percent"])
        display = payload["header_display"]
        self.assertEqual(1, display["version"])
        self.assertEqual("claude-haiku-4.5", display["model"])
        self.assertEqual(42.4, display["context_percent"])
        self.assertEqual("claude-haiku-4.5 · 42%", display["text"])
        self.assertEqual("Kiro 状态加载中", display["loading_text"])
        self.assertEqual("Kiro 状态暂不可用", display["unavailable_text"])

    def test_get_payload_header_degrades_gracefully_without_wire_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)
            acp.current_model_id = lambda: ""
            acp.context_usage_percent = lambda: None
            handler, _kiro = self._handler(acp, self._store(tmp))
            handler._handle_kiro_preferences_get()
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        # 钉选（auto）兜底为当前模型；百分比未知时是 null 而不是假数字，
        # 顶栏只显示模型名（优雅降级，不伪造“加载中”）。
        self.assertEqual("auto", payload["current_model"])
        self.assertIsNone(payload["context_usage_percent"])
        display = payload["header_display"]
        self.assertEqual("auto", display["model"])
        self.assertIsNone(display["context_percent"])
        self.assertEqual("auto", display["text"])

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
            self.assertEqual("claude-haiku-4.5", self._store(tmp).snapshot_model())

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
            acp.prepare_session = lambda **kw: calls.append(("prepare", kw.get("model"), kw.get("effort"))) or "s1"
            acp.prompt_existing = lambda text, *, on_update=None, **_kw: on_update and on_update("好")
            acp.cancel = lambda *_a: True
            acp.close = lambda: None
            store = self._store(tmp)
            store.save_validated("claude-haiku-4.5", "xhigh")
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
            self.assertEqual(("prepare", "claude-haiku-4.5", "xhigh"), calls[0])

    # kiro 推理强度 (2026-09-10) — POST 的 effort 维度
    def test_post_effort_only_persists_applies_and_needs_no_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            handler._handle_kiro_preferences_post({"effort": "xhigh"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["applied_immediately"])
            self.assertEqual("xhigh", payload["effort"])
            self.assertEqual("auto", payload["model"])
            self.assertEqual([("set_effort", "xhigh")], calls)
            persisted = json.loads((Path(tmp) / "kiro_preferences.json").read_text(encoding="utf-8"))
            self.assertEqual({"version": 1, "model": "auto", "effort": "xhigh"}, persisted)
            self.assertIn("推理强度设为 xhigh", kiro.records[-1]["text"])
            self.assertEqual("system:kiro-effort-switch", kiro.records[-1]["source"])
            # 跨重启保留。
            self.assertEqual(("auto", "xhigh"), self._store(tmp).snapshot())

    def test_post_effort_invalid_and_missing_fields_are_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            for body in ({"effort": "bogus"}, {"effort": ""}, {}):
                handler._handle_kiro_preferences_post(body)
                self.assertEqual(400, handler.responses[-1][0], body)
                self.assertEqual("invalid_kiro_selection", handler.responses[-1][1]["error"])
            self.assertEqual([], calls)
            self.assertEqual([], kiro.records)
            self.assertFalse((Path(tmp) / "kiro_preferences.json").exists())

    def test_post_effort_while_busy_pins_without_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)
            acp.busy = True
            handler, kiro = self._handler(acp, self._store(tmp))
            handler.state.kiro_active_turn = {"user_ts": "t", "cancel_event": threading.Event(), "session_id": "s"}
            handler._handle_kiro_preferences_post({"effort": "low"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertFalse(payload["applied_immediately"])
            self.assertEqual([("pin_effort", "low")], calls)
            self.assertIn("暂不生效", kiro.records[-1]["text"])

    def test_post_effort_refusal_degrades_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)

            def refused_set(effort):
                calls.append(("set_effort", effort))
                raise KiroACPError("Kiro effort is not available on the current model")

            acp.set_effort = refused_set
            handler, kiro = self._handler(acp, self._store(tmp))
            handler._handle_kiro_preferences_post({"effort": "max"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertFalse(payload["applied_immediately"])
            self.assertEqual("max", payload["effort"])
            self.assertIn("暂不生效", kiro.records[-1]["text"])
            self.assertNotIn("not available", kiro.records[-1]["text"])

    def test_post_model_and_effort_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            handler._handle_kiro_preferences_post({"model": "claude-haiku-4.5", "effort": "medium"})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["applied_immediately"])
            self.assertEqual(
                [("set_model", "claude-haiku-4.5"), ("set_effort", "medium")], calls
            )
            self.assertEqual(("claude-haiku-4.5", "medium"), self._store(tmp).snapshot())
            notices = [r["text"] for r in kiro.records]
            self.assertTrue(any("已切到 claude-haiku-4.5 模型" in text for text in notices))
            self.assertTrue(any("推理强度设为 medium" in text for text in notices))

    def test_post_same_effort_does_not_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            store = self._store(tmp)
            store.save_validated(effort="high")
            handler, kiro = self._handler(self._acp(calls), store)
            handler._handle_kiro_preferences_post({"effort": "high"})
            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual([], kiro.records)

    def test_post_explicit_null_is_400_like_the_old_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            handler, kiro = self._handler(self._acp(calls), self._store(tmp))
            for body in ({"model": None}, {"effort": None}, {"model": None, "effort": "high"}):
                handler._handle_kiro_preferences_post(body)
                self.assertEqual(400, handler.responses[-1][0], body)
                self.assertEqual("invalid_kiro_selection", handler.responses[-1][1]["error"])
            self.assertEqual([], calls)
            self.assertEqual([], kiro.records)
            self.assertFalse((Path(tmp) / "kiro_preferences.json").exists())

    def test_chat_prepare_omits_effort_when_never_chosen(self):
        # kiro 推理强度 (2026-09-10)：默认空档不 pin——prepare 收到 effort=None，
        # Kiro 自己的每模型默认档位不被覆写。
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            acp = self._acp(calls)
            acp.prepare_session = lambda **kw: calls.append(("prepare", kw.get("model"), kw.get("effort"))) or "s1"
            acp.prompt_existing = lambda text, *, on_update=None, **_kw: on_update and on_update("好")
            acp.cancel = lambda *_a: True
            acp.close = lambda: None
            handler, kiro = self._handler(acp, self._store(tmp))
            state = handler.state
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
            self.assertEqual(("prepare", "auto", None), calls[0])


# ---------------------------------------------------------------------------
# kiro 对齐 CC (2026-09-26): image blocks, bounded cancel, Stop, attachments,
# memory recall
# ---------------------------------------------------------------------------

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
    b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _image_capable_handler(image=True):
    base = _basic_handler()

    def handle(process, message):
        if message.get("method") == "initialize":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": image, "audio": False, "embeddedContext": False},
                },
            }}]
        return base(process, message)

    return handle


class KiroParityProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = str(Path(self.tmp.name) / "kiro_acp_session.json")

    def _client(self, factory, **kwargs):
        return KiroACPClient(
            command="/fake/kiro-cli",
            cwd=self.tmp.name,
            state_path=self.state_path,
            request_timeout=5,
            prompt_timeout=10,
            popen_factory=factory,
            **kwargs,
        )

    def _prompt_blocks(self, process):
        prompts = [req for req in process.requests if req.get("method") == "session/prompt"]
        self.assertEqual(1, len(prompts))
        return prompts[0]["params"]["prompt"]

    def test_image_blocks_precede_text_when_agent_advertises_images(self):
        process = FakeKiroACPProcess(_image_capable_handler(image=True))
        client = self._client(_scripted_factory([process]))
        session_id = client.prepare_session()
        self.assertTrue(client.image_prompt_supported())
        client.prompt_existing(
            "看图",
            session_id=session_id,
            turn_id="turn-1",
            images=[
                {"mime_type": "image/png", "data": "QUJD"},
                {"mime_type": "image/heic", "data": "REVG"},  # not an ACP-safe type
                {"mime_type": "image/jpeg", "data": ""},      # empty payload dropped
                "junk",
            ],
        )
        self.assertEqual(
            [{"type": "image", "mimeType": "image/png", "data": "QUJD"}, {"type": "text", "text": "看图"}],
            self._prompt_blocks(process),
        )

    def test_images_are_dropped_when_agent_does_not_advertise_them(self):
        for handler in (_image_capable_handler(image=False), _basic_handler()):
            process = FakeKiroACPProcess(handler)
            client = self._client(_scripted_factory([process]))
            session_id = client.prepare_session()
            self.assertFalse(client.image_prompt_supported())
            client.prompt_existing(
                "看图", session_id=session_id, turn_id="turn-1",
                images=[{"mime_type": "image/png", "data": "QUJD"}],
            )
            self.assertEqual([{"type": "text", "text": "看图"}], self._prompt_blocks(process))

    def test_unacknowledged_cancel_recycles_process_after_grace(self):
        client = self._client(_scripted_factory([]), cancel_grace_seconds=0.3)
        client._process_alive = lambda: True
        client._loaded_session_id = "s1"
        release = threading.Event()
        self.addCleanup(release.set)
        cancelled, closed = [], []

        def request(method, params, timeout, ensure_started=True):
            release.wait(5)  # Kiro never answers the cancelled prompt
            return {"stopReason": "cancelled"}

        client._request = request
        client.cancel = lambda turn_id, session_id: cancelled.append(turn_id) or True
        client.close = lambda: closed.append(True)
        gate = threading.Event()
        threading.Timer(0.05, gate.set).start()
        import time as _time
        begin = _time.monotonic()
        with self.assertRaises(KiroACPCancelled):
            client.prompt_existing("hello", session_id="s1", turn_id="turn-1", cancel_event=gate)
        self.assertLess(_time.monotonic() - begin, 3.0)
        self.assertEqual(["turn-1"], cancelled)
        self.assertEqual([True], closed)
        self.assertFalse(client.busy)


class KiroParityHandlerTest(KiroChatHandlerTest):
    """Reuses the Kiro handler harness; only the new tests live here."""

    # Do not re-run every inherited test a second time.
    def run(self, result=None):
        if not self._testMethodName.startswith("test_parity_"):
            return result
        return super().run(result)

    def _handler(self, kiro_acp):
        handler, kiro, xiaoke = super()._handler(kiro_acp)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        handler.state.attachments_dir = Path(self._tmp.name) / "attachments"
        handler.state.attachments_dir.mkdir()
        return handler, kiro, xiaoke

    def _recording_acp(self, reply="Kiro 回复。", failure=None):
        acp = self._acp(reply=reply, failure=failure)
        acp.prompts = []

        def prompt_existing(text, *, on_update=None, images=(), **kwargs):
            acp.prompts.append({"text": text, "images": list(images), **kwargs})
            if reply and on_update is not None:
                on_update(reply)
            if failure is not None:
                raise failure

        acp.prompt_existing = prompt_existing
        return acp

    def _staged(self, handler, name, data, *, kind, media_type):
        path = handler.state.attachments_dir / f"{len(list(handler.state.attachments_dir.iterdir()))}{Path(name).suffix}"
        path.write_bytes(data)
        return {
            "attachment_id": f"id-{name}",
            "attachment_url": f"/attachments/{path.name}",
            "filename": name,
            "type": kind,
            "media_type": media_type,
            "size": len(data),
            "stored_path": str(path),
        }

    # ----- Stop -----

    def test_parity_stop_contract_matches_kimi(self):
        handler, _kiro, _xiaoke = self._handler(self._acp())
        handler._handle_kiro_chat_stop("")
        self.assertEqual((400, "missing_turn_identity"), (handler.responses[-1][0], handler.responses[-1][1]["error"]))

        handler._handle_kiro_chat_stop("ts-1")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["already_finished"])

        cancel_event = threading.Event()
        handler.state.kiro_active_turn = {"user_ts": "ts-7", "cancel_event": cancel_event, "session_id": "s"}
        handler._handle_kiro_chat_stop("ts-6")
        self.assertEqual((409, "stale_turn"), (handler.responses[-1][0], handler.responses[-1][1]["error"]))
        self.assertFalse(cancel_event.is_set())

        handler._handle_kiro_chat_stop("ts-7")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["stopped"])
        self.assertEqual("ts-7", handler.responses[-1][1]["user_ts"])
        self.assertTrue(cancel_event.is_set())

    def test_parity_chat_stop_routes_kiro_through_registry(self):
        from contacts import dispatch_contact_stop

        calls = []
        stub = types.SimpleNamespace(_handle_kiro_chat_stop=lambda user_ts, body=None: calls.append(user_ts))
        self.assertTrue(dispatch_contact_stop(stub, {"contact_id": "kiro", "user_ts": " ts-3 "}))
        self.assertEqual(["ts-3"], calls)

        handler, _kiro, _xiaoke = self._handler(self._acp())
        handler._handle_chat_stop({"contact_id": "kiro", "user_ts": "missing"})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["already_finished"])

    def test_parity_stop_mid_turn_persists_partial_as_interrupted(self):
        holder = {}

        def prompt_existing(text, *, on_update=None, cancel_event=None, **_kwargs):
            on_update("写到一半")
            handler_ref = holder["handler"]
            handler_ref._handle_kiro_chat_stop(handler_ref.state.kiro_active_turn["user_ts"])
            self.assertTrue(cancel_event.is_set())
            raise KiroACPCancelled("Kiro generation cancelled")

        acp = self._acp()
        acp.prompt_existing = prompt_existing
        handler, kiro, _xiaoke = self._handler(acp)
        holder["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual("写到一半\n\n**[已停止生成]**", kiro.records[-1]["text"])
        self.assertEqual("kiro-acp:interrupted", kiro.records[-1]["source"])
        self.assertEqual("interrupted", handler.state.chat_reply_states["kiro"]["reply_state"])
        self.assertEqual({}, handler.state.kiro_active_turn)

    # ----- Attachments -----

    def test_parity_staged_image_and_file_reach_kiro(self):
        acp = self._recording_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        image = self._staged(handler, "猫.png", _PNG_BYTES, kind="image", media_type="image/png")
        heic = self._staged(handler, "live.heic", b"heic", kind="image", media_type="image/heic")
        doc = self._staged(handler, "报告.pdf", b"%PDF-1.4", kind="file", media_type="application/pdf")
        handler._consume_staged_attachments = lambda body, contact_id: (
            body.pop("attachment_ids", None), [image, heic, doc]
        )[1]
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_chat_send({"contact_id": "kiro", "text": "看看", "attachment_ids": ["a", "b", "c"]})

        self.assertEqual(200, handler.responses[-1][0])
        user = kiro.records[0]
        self.assertEqual(image["attachment_url"], user["attachment_url"])
        self.assertEqual("image", user["attachment_type"])
        self.assertEqual(3, len(user["metadata"]["attachments"]))
        prompt = acp.prompts[0]
        import base64 as _b64
        self.assertEqual(
            [{"mime_type": "image/png", "data": _b64.b64encode(_PNG_BYTES).decode("ascii")}],
            prompt["images"],
        )
        self.assertTrue(prompt["text"].startswith("看看\n\n"))
        for item, kind in ((image, "图片"), (heic, "图片"), (doc, "文件")):
            self.assertIn(f"[用户发了{kind}: {item['filename']}]\n本地路径: {item['stored_path']}", prompt["text"])
        self.assertEqual("Kiro 回复。", kiro.records[-1]["text"])

    def test_parity_attachment_only_message_is_accepted(self):
        acp = self._recording_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        doc = self._staged(handler, "a.txt", b"hello", kind="file", media_type="text/plain")
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "", "_pwa_staged_attachments": [doc]}, "kiro")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("", kiro.records[0]["text"])
        self.assertTrue(acp.prompts[0]["text"].startswith("[用户发了文件: a.txt]"))
        self.assertEqual([], acp.prompts[0]["images"])

    def test_parity_oversized_or_foreign_image_is_path_only(self):
        acp = self._recording_acp()
        handler, _kiro, _xiaoke = self._handler(acp)
        big = self._staged(handler, "big.png", b"x" * 16, kind="image", media_type="image/png")
        outside = Path(self._tmp.name) / "outside.png"
        outside.write_bytes(_PNG_BYTES)
        foreign = {**big, "filename": "o.png", "stored_path": str(outside)}
        handler._KIRO_INLINE_IMAGE_MAX_BYTES = 8
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "图", "_pwa_staged_attachments": [big, foreign]}, "kiro")
        self.assertEqual([], acp.prompts[0]["images"])
        self.assertIn("big.png", acp.prompts[0]["text"])
        self.assertIn("o.png", acp.prompts[0]["text"])

    def test_parity_busy_or_failed_prepare_discards_uncommitted_attachments(self):
        acp = self._recording_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        doc = self._staged(handler, "a.txt", b"hello", kind="file", media_type="text/plain")
        handler.state.kiro_active_turn = {"user_ts": "t", "cancel_event": threading.Event(), "session_id": "s"}
        # kiro r4: busy now queues (Kimi parity) — the attachment is committed
        # with the queued history row and must stay valid until delivery.
        with patch("push.threading.Thread"):
            handler._handle_kiro_chat_send({"text": "x", "_pwa_staged_attachments": [doc]}, "kiro")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["queued"])
        self.assertTrue(Path(doc["stored_path"]).exists())
        self.assertEqual(1, len(kiro.records))
        handler.state.kiro_chat_queue.clear()

        handler.state.kiro_active_turn = {}
        doc2 = self._staged(handler, "b.txt", b"hello", kind="file", media_type="text/plain")

        def prepare(**_kw):
            raise KiroACPError("down")

        acp.prepare_session = prepare
        handler._handle_kiro_chat_send({"text": "x", "_pwa_staged_attachments": [doc2]}, "kiro")
        self.assertEqual(503, handler.responses[-1][0])
        self.assertFalse(Path(doc2["stored_path"]).exists())
        self.assertEqual(1, len(kiro.records))  # only the queued row above

    # ----- Memory recall -----

    def _enable_recall(self, handler, result):
        from push import KairosRecallIndex

        class Recall:
            def __init__(self):
                self.calls = []

            def recall_result(self, query, *, exclude_memory_keys=()):
                self.calls.append((query, tuple(exclude_memory_keys)))
                return result

        recall = Recall()
        handler.state.kiro_semantic_memory_recall_enabled = True
        handler.state.kiro_semantic_memory_recall_lock = threading.Lock()
        handler.state.kiro_semantic_memory_recall = recall
        handler.state.kiro_semantic_memory_recall_init_attempted = True
        handler.state.kiro_recall_card_lock = threading.Lock()
        handler.state.kiro_recall_index = KairosRecallIndex(Path(self._tmp.name) / "kiro_recall_index.json")
        return recall

    def _recall_result(self):
        return types.SimpleNamespace(
            context="【记忆浮现·自动检索】\n<retrieved_memory_data>安全上下文</retrieved_memory_data>",
            items=(
                {"date": "2026-09-01", "title": "记忆一", "snippet": "第一条", "memory_id": "cb1d1274-a604-4dab-928c-99e907a1eeec"},
                {"date": "2026-09-02", "title": "记忆二", "snippet": "第二条"},
            ),
            memory_keys=("v1:" + "c" * 64,),
        )

    def test_parity_recall_injects_context_and_card_then_commits(self):
        acp = self._recording_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        recall = self._enable_recall(handler, self._recall_result())
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你还记得我喜欢吃什么吗"}, "kiro")

        self.assertEqual([("你还记得我喜欢吃什么吗", ())], recall.calls)
        roles = [(r["role"], r["source"]) for r in kiro.records]
        self.assertEqual(
            [("user", "android-app:kiro"), ("assistant", "memory-recall:kiro"), ("assistant", "kiro-acp")],
            roles,
        )
        card = kiro.records[1]
        self.assertEqual("💭 浮现了 2 条记忆（摘要见卡片）", card["text"])
        self.assertTrue(card["metadata"]["recall_card"])
        self.assertEqual(kiro.records[0]["ts"], card["metadata"]["kiro_user_ts"])
        self.assertFalse(card["metadata"]["turn_terminal"])
        self.assertEqual("cb1d1274-a604-4dab-928c-99e907a1eeec", card["metadata"]["items"][0]["memory_id"])
        self.assertEqual(
            "你还记得我喜欢吃什么吗\n\n" + self._recall_result().context,
            acp.prompts[0]["text"],
        )
        # Committed after the prompt succeeded: the next turn excludes it.
        self.assertEqual(("v1:" + "c" * 64,), handler.state.kiro_recall_index.keys("kiro-session-1"))
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "那我最讨厌什么呢"}, "kiro")
        self.assertEqual(("v1:" + "c" * 64,), recall.calls[-1][1])

    def test_parity_recall_gate_matches_cc_hook(self):
        acp = self._recording_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        recall = self._enable_recall(handler, self._recall_result())
        for text in ("好", "👋👋👋👋👋👋👋", "   嗯嗯  ", "【日程·自动触发】明天九点开会记得带电脑"):
            with patch("push.threading.Thread", _immediate_thread):
                handler._handle_kiro_chat_send({"text": text}, "kiro")
        self.assertEqual([], recall.calls)
        self.assertFalse(any(r["source"] == "memory-recall:kiro" for r in kiro.records))
        self.assertEqual(text, acp.prompts[-1]["text"])

    def test_parity_recall_not_committed_when_turn_fails(self):
        acp = self._recording_acp(reply="", failure=KiroACPError("x"))
        handler, _kiro, _xiaoke = self._handler(acp)
        self._enable_recall(handler, self._recall_result())
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你还记得我喜欢吃什么吗"}, "kiro")
        self.assertEqual((), handler.state.kiro_recall_index.keys("kiro-session-1"))

    def test_parity_recall_failure_is_fail_open(self):
        acp = self._recording_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        recall = self._enable_recall(handler, None)
        recall.recall_result = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("memory down"))
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你还记得我喜欢吃什么吗"}, "kiro")
        self.assertEqual("你还记得我喜欢吃什么吗", acp.prompts[0]["text"])
        self.assertEqual("Kiro 回复。", kiro.records[-1]["text"])

    def test_parity_kiro_recall_never_touches_kimi_state(self):
        acp = self._recording_acp()
        handler, _kiro, _xiaoke = self._handler(acp)
        self._enable_recall(handler, self._recall_result())
        kimi_index = types.SimpleNamespace(
            keys=lambda _s: self.fail("kimi index read"),
            add=lambda *_a: self.fail("kimi index write"),
        )
        handler.state.kimi_recall_index = kimi_index
        handler.state.kimi_semantic_memory_recall_enabled = True
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你还记得我喜欢吃什么吗"}, "kiro")
        self.assertEqual(1, len(handler.state.kiro_recall_index.keys("kiro-session-1")))


# ---------------------------------------------------------------------------
# kiro 对齐 CC r3 (2026-09-26): 「忙活了 N 下」counts distinct toolCallIds.
# ---------------------------------------------------------------------------

def _wire(update_name, **fields):
    return {"sessionId": "s", "update": {"sessionUpdate": update_name, **fields}}


# Shapes recorded from kiro-cli 2.21.2 in an isolated /tmp ACP session.
_PROBE_TOOL_STREAM = (
    _wire("tool_call", toolCallId="toolu_1", title="Running: echo probe-ok", kind="execute",
          rawInput={"command": "echo probe-ok"}, _meta={"kiro": {"toolName": "shell"}}),
    _wire("tool_call", toolCallId="toolu_2", title="Reading hostname:1", kind="read",
          locations=[{"path": "/etc/hostname"}], rawInput={"operations": []}),
    _wire("tool_call_update", toolCallId="toolu_2", kind="read", status="completed",
          title="Reading hostname:1", rawOutput={"items": [{"Text": "SECRET-HOST"}]}),
    _wire("tool_call_update", toolCallId="toolu_1",
          content=[{"type": "content", "content": {"type": "text", "text": "probe-ok\n"}}]),
    _wire("tool_call_update", toolCallId="toolu_1", kind="execute", status="completed",
          title="Running: echo probe-ok", rawOutput={"items": []}),
    _wire("tool_call", toolCallId="toolu_3", title="Running: date -u", kind="execute"),
    _wire("tool_call_update", toolCallId="toolu_3",
          content=[{"type": "content", "content": {"type": "text", "text": "Sat\n"}}]),
)


class KiroToolCountProjectionTest(unittest.TestCase):
    def test_tool_events_carry_only_bounded_identity_title_kind_status(self):
        event = _activity_from_update(_PROBE_TOOL_STREAM[2])
        self.assertEqual({
            "kind": "activity", "label": "正在使用工具", "tool_call_id": "toolu_2",
            "tool_update": True, "title": "Reading hostname:1", "tool_kind": "read",
            "status": "completed",
        }, event)
        self.assertNotIn("SECRET-HOST", json.dumps(event, ensure_ascii=False))
        self.assertNotIn("/etc/hostname", json.dumps(_activity_from_update(_PROBE_TOOL_STREAM[1])))
        # Unknown kind/status values and malformed ids are dropped, not echoed.
        odd = _activity_from_update(_wire("tool_call", toolCallId="bad id!", kind="rm -rf", status="x"))
        self.assertEqual({"kind": "activity", "label": "正在使用工具"}, odd)

    def test_tool_title_is_redacted_and_bounded(self):
        event = _activity_from_update(_wire(
            "tool_call", toolCallId="t1", kind="fetch",
            title="Fetching https://x.test/a?token=url-secret#frag with Bearer abc.def token=raw-secret "
                  + "y" * 200,
        ))
        title = event["title"]
        self.assertLessEqual(len(title), 80)
        for secret in ("url-secret", "abc.def", "raw-secret", "#frag"):
            self.assertNotIn(secret, title)
        self.assertTrue(title.startswith("Fetching https://x.test/a"))

    def test_note_tool_event_counts_each_call_id_once(self):
        tools = {}
        for params in _PROBE_TOOL_STREAM:
            event = _activity_from_update(params)
            PushHandler._kiro_note_tool_event(tools, event)
        self.assertEqual(["toolu_1", "toolu_2", "toolu_3"], list(tools))
        self.assertEqual(
            ["completed", "completed", "in_progress"], [t["status"] for t in tools.values()],
        )
        # A late update never reopens a finished call.
        PushHandler._kiro_note_tool_event(tools, {"tool_call_id": "toolu_2", "status": "in_progress"})
        self.assertEqual("completed", tools["toolu_2"]["status"])
        self.assertEqual(
            [
                "Running: echo probe-ok · 执行命令 · 已完成",
                "Reading hostname:1 · 读取 · 已完成",
                "Running: date -u · 执行命令 · 进行中",
            ],
            PushHandler._kiro_tool_lines(tools),
        )


class KiroToolCountHandlerTest(KiroChatHandlerTest):
    def _tool_acp(self, *, thoughts=270, failure=None, stream=_PROBE_TOOL_STREAM):
        seen = {}

        def prompt_existing(text, *, on_update=None, on_activity=None, **_kwargs):
            # opus/max streams hundreds of thought chunks around 3 tool calls.
            for _ in range(thoughts // 2):
                on_activity(_activity_from_update(_wire("agent_thought_chunk", content={"type": "text", "text": "x"})))
            for params in stream:
                on_activity(_activity_from_update(params))
                on_activity(_activity_from_update(_wire("agent_thought_chunk", content={"type": "text", "text": "y"})))
            for _ in range(thoughts // 2):
                on_activity(_activity_from_update(_wire("agent_thought_chunk", content={"type": "text", "text": "z"})))
            live = dict(seen["handler"].state.chat_reply_states["kiro"])
            seen["live"] = live
            if on_update is not None:
                on_update("完成了。")
            if failure is not None:
                raise failure

        acp = types.SimpleNamespace(
            prepare_session=lambda **_kw: "kiro-session-1",
            prompt_existing=prompt_existing,
            cancel=lambda _turn, _session: True,
            close=lambda: None,
            new_session=lambda **_kw: "kiro-session-2",
        )
        return acp, seen

    def test_live_count_is_distinct_tool_calls_not_events(self):
        acp, seen = self._tool_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        seen["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "查三样东西"}, "kiro")
        live = seen["live"]
        self.assertEqual(3, live["activity_count"])
        self.assertEqual(3, len(live["activity_items"]))
        self.assertIn("Reading hostname:1 · 读取 · 已完成", live["activity_items"])
        self.assertIn(live["activity_text"], {"正在思考", "正在使用工具"})

    def test_history_keeps_one_kimi_shaped_summary_before_the_answer(self):
        acp, seen = self._tool_acp()
        handler, kiro, _xiaoke = self._handler(acp)
        seen["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "查三样东西"}, "kiro")
        roles = [(r["role"], r["source"]) for r in kiro.records]
        self.assertEqual(
            [("user", "android-app:kiro"), ("task", "kiro-acp:activity"), ("assistant", "kiro-acp")],
            roles,
        )
        summary = kiro.records[1]["metadata"]
        user_ts = kiro.records[0]["ts"]
        self.assertEqual({
            "activity_summary": True,
            "activity_count": 3,
            "activity_items": [
                "Running: echo probe-ok · 执行命令 · 已完成",
                "Reading hostname:1 · 读取 · 已完成",
                # Never reported finished; the completed turn closes it.
                "Running: date -u · 执行命令 · 已完成",
            ],
            "status": "completed",
            "kiro_user_ts": user_ts,
            "turn_terminal": False,
            "turn_message_kind": "auxiliary_activity",
        }, summary)
        self.assertEqual("completed", handler.state.chat_reply_states["kiro"]["reply_state"])

    def test_interrupted_turn_marks_open_calls_interrupted(self):
        acp, seen = self._tool_acp(failure=KiroACPCancelled("stop"))
        handler, kiro, _xiaoke = self._handler(acp)
        seen["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "查三样东西"}, "kiro")
        summary = next(r for r in kiro.records if r["source"] == "kiro-acp:activity")["metadata"]
        self.assertEqual("interrupted", summary["status"])
        self.assertEqual("Running: date -u · 执行命令 · 已中断", summary["activity_items"][-1])
        self.assertEqual("kiro-acp:interrupted", kiro.records[-1]["source"])

    def test_failed_turn_summary_is_failed(self):
        acp, seen = self._tool_acp(failure=KiroACPError("boom"))
        handler, kiro, _xiaoke = self._handler(acp)
        seen["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "查三样东西"}, "kiro")
        summary = next(r for r in kiro.records if r["source"] == "kiro-acp:activity")["metadata"]
        self.assertEqual("failed", summary["status"])
        self.assertEqual("Running: date -u · 执行命令 · 失败", summary["activity_items"][-1])

    def test_thinking_only_turn_has_no_tool_card(self):
        acp, seen = self._tool_acp(stream=())
        handler, kiro, _xiaoke = self._handler(acp)
        seen["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "想一想"}, "kiro")
        self.assertEqual(0, seen["live"]["activity_count"])
        self.assertEqual([], seen["live"]["activity_items"])
        self.assertEqual("正在思考", seen["live"]["activity_text"])
        self.assertEqual(["user", "assistant"], [r["role"] for r in kiro.records])

    def test_thought_chunks_do_not_republish_the_same_state(self):
        acp, seen = self._tool_acp(stream=())
        handler, _kiro, _xiaoke = self._handler(acp)
        seen["handler"] = handler
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "想一想"}, "kiro")
        # queued/generating/one thinking transition/draft/completed — not 270+.
        self.assertLess(handler.state.chat_stream_revisions.get("kiro", 0), 10)


if __name__ == "__main__":
    unittest.main()
