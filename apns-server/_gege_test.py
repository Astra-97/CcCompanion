"""哥哥写手 gege (2026-10-29) tests.

  1. gege_writer.parse_command: commands vs. writing requests, ［💬微信］ prefix,
     bare numbers only right after /list
  2. pi_rpc.PiRPCClient against a fake pi (local python child speaking pi's
     JSONL RPC): streaming prompt, /gege notify answers, auth errors, abort
  3. push handler: real _handle_gege_chat_send + fake client — app turns stay in
     the app, WeChat turns also go to the wecom outbox; /list then a number opens;
     /roll re-prompts the returned text; auth / not-configured replies are visible
  4. registry + wecom_bridge accept gege

Never touches the live 8291 service or Tokyo.
"""
import json
import os
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import gege_writer
import push
import wecom_bridge
from chat_history import ChatStreamBus
from contacts import chat_contact_directory, default_contact_routes
from pi_rpc import PiRPCAuthRequired, PiRPCCancelled, PiRPCClient
from push import PushHandler

from _kiro_acp_test import FakeChat, _immediate_thread

FAKE_PI = textwrap.dedent(r'''
    import json, sys, time
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    out = sys.stdout.buffer
    def emit(obj):
        out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")); out.flush()
    for raw in sys.stdin.buffer:
        cmd = json.loads(raw)
        rid, kind = cmd.get("id"), cmd.get("type")
        if kind == "get_state":
            emit({"id": rid, "type": "response", "command": "get_state", "success": True,
                  "data": {"model": None if mode == "nomodel" else {"id": "m"}, "isStreaming": False, "isCompacting": False}})
        elif kind == "abort":
            emit({"id": rid, "type": "response", "command": "abort", "success": True})
        elif kind == "prompt":
            msg = cmd.get("message", "")
            if msg.startswith("/gege "):
                emit({"type": "extension_ui_request", "id": "u1", "method": "notify",
                      "message": "GEGE " + json.dumps({"ok": True, "cmd": msg.split()[1], "echo": msg}, ensure_ascii=False)})
                emit({"id": rid, "type": "response", "command": "prompt", "success": True, "data": {"disposition": "handled"}})
                continue
            if mode == "nokey":
                emit({"id": rid, "type": "response", "command": "prompt", "success": False, "error": "No API key found for anthropic."})
                continue
            emit({"id": rid, "type": "response", "command": "prompt", "success": True, "data": {"disposition": "started"}})
            emit({"type": "agent_start"})
            emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
            if mode == "slow":
                emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "半句"}})
                line = sys.stdin.buffer.readline()
                emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "aborted", "content": [{"type": "text", "text": "半句"}]}})
                emit({"type": "agent_settled", "aborted": True})
                continue
            if mode == "error":
                emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "401 invalid x-api-key", "content": []}})
                emit({"type": "agent_settled", "aborted": False})
                continue
            for piece in ("夜里 ", "的舰桥。\n\n", "哥哥。"):
                emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": piece}})
            emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                  "content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "夜里 的舰桥。\n\n哥哥。" + msg[-2:]}]}})
            emit({"type": "agent_settled", "aborted": False})
''')


class ParseCommandTest(unittest.TestCase):
    def test_commands(self) -> None:
        p = gege_writer.parse_command
        self.assertEqual(p("/new 舰桥夜话"), ("new", "舰桥夜话"))
        self.assertEqual(p("［💬微信］/new"), ("new", ""))
        self.assertEqual(p("新开一篇 雨夜"), ("new", "雨夜"))
        self.assertEqual(p("/list"), ("list", ""))
        self.assertEqual(p("存档"), ("list", ""))
        self.assertEqual(p("/open 3"), ("open", "3"))
        self.assertEqual(p("切换到第２篇"), ("open", "2"))
        self.assertEqual(p("/ROLL"), ("roll", ""))
        self.assertEqual(p("［💬微信］/undo"), ("undo", ""))
        self.assertEqual(p("撤回"), ("undo", ""))
        self.assertEqual(p("/set 窗口 300k"), ("set", "窗口 300k"))
        self.assertEqual(p("/set 长篇"), ("set", "长篇"))
        self.assertEqual(p("/rename 新名字"), ("rename", "新名字"))
        self.assertEqual(p("/help"), ("help", ""))

    def test_writing_requests_are_not_commands(self) -> None:
        p = gege_writer.parse_command
        self.assertIsNone(p("写一段哥哥撤回消息的片段"))
        self.assertIsNone(p("继续"))
        self.assertIsNone(p("3"))
        self.assertEqual(p("3", list_pending=True), ("open", "3"))
        self.assertIsNone(p("x" * 300))

    def test_format_list_and_results(self) -> None:
        text = gege_writer.format_list([
            {"n": 1, "name": "", "first": "第一段要求", "messages": 4, "modified": "2026-10-29T02:00:00Z", "current": False},
            {"n": 2, "name": "雨夜", "messages": 2, "modified": "2026-10-29T03:00:00Z", "current": True},
        ])
        self.assertIn("1. 《第一段要求…》", text)
        self.assertIn("2. 《雨夜》 10-29 11:00，2 条 ← 当前", text)
        self.assertNotIn("**", text)
        self.assertIn("没有这个编号", gege_writer.format_result({"ok": False, "cmd": "open", "error": "no_such_archive", "count": 2}))
        self.assertIn("窗口 300k", gege_writer.format_result({"ok": True, "cmd": "set", "settings": {"window": 300000, "keep": 100000, "summary": 20000}}))


class PiRPCClientTest(unittest.TestCase):
    def client(self, mode="ok") -> PiRPCClient:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "fake_pi.py"
        script.write_text(FAKE_PI, encoding="utf-8")
        client = PiRPCClient([sys.executable, str(script), mode], start_timeout=10, command_timeout=10)
        self.addCleanup(client.close)
        return client

    def test_prompt_streams_and_returns_final_text(self) -> None:
        client = self.client()
        seen = []
        answer = client.prompt("写一段", on_delta=seen.append)
        self.assertEqual(answer, "夜里 的舰桥。\n\n哥哥。一段")  # U+2028 is not a record boundary
        self.assertEqual(seen[-1], "夜里 的舰桥。\n\n哥哥。")
        # the process stays up for the next turn
        self.assertTrue(client.alive())
        self.assertTrue(client.prompt("继续").endswith("继续"))

    def test_gege_command_returns_notify_json(self) -> None:
        client = self.client()
        result = client.gege("open 2")
        self.assertEqual(result, {"ok": True, "cmd": "open", "echo": "/gege open 2"})

    def test_auth_errors(self) -> None:
        with self.assertRaises(PiRPCAuthRequired):
            self.client("nokey").prompt("写")
        with self.assertRaises(PiRPCAuthRequired):
            self.client("nomodel").prompt("写")
        with self.assertRaises(PiRPCAuthRequired):
            self.client("error").prompt("写")

    def test_cancel_returns_partial(self) -> None:
        client = self.client("slow")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(PiRPCCancelled) as caught:
            client.prompt("写", cancel_event=cancel)
        self.assertEqual(caught.exception.partial, "半句")


class FakeClient:
    def __init__(self, *, answers=None, gege_results=None, failure=None):
        self.prompts = []
        self.commands = []
        self.answers = list(answers or ["哥哥的正文。"])
        self.gege_results = dict(gege_results or {})
        self.failure = failure
        self.closed = 0

    def prompt(self, text, *, on_delta=None, cancel_event=None, **_kw):
        self.prompts.append(text)
        if self.failure is not None:
            raise self.failure
        answer = self.answers.pop(0) if self.answers else "又一段。"
        if on_delta is not None:
            on_delta(answer[:2])
        return answer

    def gege(self, command, **_kw):
        self.commands.append(command)
        name = command.split()[0]
        return self.gege_results.get(name, {"ok": True, "cmd": name})

    def close(self):
        self.closed += 1


class GegeHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.outbox_dir = Path(tmp.name) / "outbox"
        self.outbox_dir.mkdir()
        old = os.environ.get(wecom_bridge.OUTBOX_DIR_ENV)
        os.environ[wecom_bridge.OUTBOX_DIR_ENV] = str(self.outbox_dir)
        self.addCleanup(lambda: os.environ.pop(wecom_bridge.OUTBOX_DIR_ENV, None) if old is None else os.environ.__setitem__(wecom_bridge.OUTBOX_DIR_ENV, old))

    def handler(self, client):
        chat = FakeChat()
        state = types.SimpleNamespace(
            contact_chats={"gege": chat},
            contact_routes=default_contact_routes(),
            contact_catalog=[],
            contact_typing_states={},
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
            chat_stream_revisions={},
            chat_stream_bus=ChatStreamBus(),
            gege_client=client,
            gege_lock=threading.Lock(),
            gege_queue=__import__("collections").deque(),
            gege_worker_running=False,
            gege_active={},
            gege_list_until=0.0,
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler.headers = {}
        handler.responses = []
        handler._send_json = lambda status, payload, **_kw: handler.responses.append((status, payload))
        handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
        handler._chat_for_contact = lambda contact_id: state.contact_chats[contact_id]
        return handler, chat

    def send(self, handler, body):
        with patch("push.threading.Thread", _immediate_thread):
            handler._handle_gege_chat_send(body, "gege")

    def wecom(self, text, msg_id="m-1"):
        return wecom_bridge.inbound_body({"contact_id": "gege", "agent_id": "1000007", "msg_id": msg_id, "msg_type": "text", "text": text})

    def outbox(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.outbox_dir.glob("*.json"))]

    def test_app_turn_stays_in_app(self) -> None:
        client = FakeClient(answers=["舰桥上的灯。"])
        handler, chat = self.handler(client)
        self.send(handler, {"text": "写一段"})
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(client.prompts, ["写一段"])
        self.assertEqual([(r["role"], r["text"]) for r in chat.records], [("user", "写一段"), ("assistant", "舰桥上的灯。")])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(handler.state.chat_reply_states["gege"]["reply_state"], "completed")

    def test_wecom_turn_strips_prefix_and_mirrors(self) -> None:
        client = FakeClient(answers=["第一段。\n\n第二段。"])
        handler, chat = self.handler(client)
        self.send(handler, self.wecom("哥哥在舰桥等我"))
        self.assertEqual(client.prompts, ["哥哥在舰桥等我"])  # model never sees ［💬微信］
        self.assertEqual(chat.records[0]["text"], "［💬微信］哥哥在舰桥等我")
        items = self.outbox()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["contact_id"], items[0]["agent_id"], items[0]["text"]), ("gege", "1000007", "第一段。\n\n第二段。"))

    def test_list_then_number_opens(self) -> None:
        client = FakeClient(gege_results={
            "list": {"ok": True, "cmd": "list", "archives": [{"n": 1, "name": "雨夜", "messages": 2, "modified": "2026-10-29T03:00:00Z", "current": True}]},
            "open": {"ok": True, "cmd": "open", "archive": {"name": "雨夜", "turns": 1, "last_tail": "他说：晚安。"}},
        })
        handler, chat = self.handler(client)
        self.send(handler, self.wecom("/list", "m-1"))
        self.send(handler, self.wecom("1", "m-2"))
        self.assertEqual(client.commands, ["list", "open 1"])
        self.assertEqual(client.prompts, [])
        self.assertIn("1. 《雨夜》", chat.records[1]["text"])
        self.assertIn("已切换到《雨夜》", chat.records[3]["text"])
        self.assertEqual(len(self.outbox()), 2)
        # after the open a bare number is a writing request again
        self.send(handler, self.wecom("2", "m-3"))
        self.assertEqual(client.prompts, ["2"])

    def test_roll_reprompts_returned_text(self) -> None:
        client = FakeClient(answers=["新版本。"], gege_results={"roll": {"ok": True, "cmd": "roll", "text": "写雨夜"}})
        handler, chat = self.handler(client)
        self.send(handler, self.wecom("/roll"))
        self.assertEqual(client.commands, ["roll"])
        self.assertEqual(client.prompts, ["写雨夜"])
        self.assertEqual(chat.records[-1]["text"], "新版本。")
        self.assertEqual(self.outbox()[0]["text"], "新版本。")

    def test_undo_and_help_answer_without_model(self) -> None:
        client = FakeClient(gege_results={"undo": {"ok": True, "cmd": "undo", "text": "写雨夜", "archive": {"name": "雨夜"}}})
        handler, chat = self.handler(client)
        self.send(handler, {"text": "/undo"})
        self.send(handler, {"text": "/help"})
        self.assertEqual(client.prompts, [])
        self.assertIn("撤回了上一轮", chat.records[1]["text"])
        self.assertIn("/roll", chat.records[3]["text"])

    def test_auth_required_is_visible_and_mirrored(self) -> None:
        client = FakeClient(failure=PiRPCAuthRequired("No API key found"))
        handler, chat = self.handler(client)
        self.send(handler, self.wecom("写"))
        self.assertEqual(chat.records[-1]["text"], gege_writer.AUTH_REQUIRED_TEXT)
        self.assertEqual(self.outbox()[0]["text"], gege_writer.AUTH_REQUIRED_TEXT)

    def test_not_configured(self) -> None:
        handler, chat = self.handler(None)
        self.send(handler, self.wecom("写"))
        self.assertEqual(chat.records[-1]["text"], gege_writer.NOT_CONFIGURED_TEXT)

    def test_attachments_rejected(self) -> None:
        handler, chat = self.handler(FakeClient())
        handler._discard_uncommitted_staged_attachments = lambda _items: None
        self.send(handler, {"text": "看图", "_pwa_staged_attachments": [{"attachment_url": "/attachments/x.png"}]})
        self.assertEqual(handler.responses[-1][0], 415)
        self.assertEqual(chat.records, [])


class RegistryTest(unittest.TestCase):
    def test_gege_is_a_chat_contact(self) -> None:
        state = types.SimpleNamespace(contact_routes=default_contact_routes(), contact_catalog=[], contact_chats={"gege": object()})
        contacts = {c["id"]: c for c in chat_contact_directory(state)}
        self.assertIn("chat", contacts["gege"]["capabilities"])
        self.assertEqual(contacts["gege"]["display_name"], "哥哥写手")

    def test_wecom_bridge_accepts_gege(self) -> None:
        clean, error = wecom_bridge.parse_inbound({"contact_id": "gege", "agent_id": "1000007", "id": "1", "text": "/roll"})
        self.assertEqual(error, "")
        self.assertEqual(clean["contact_id"], "gege")
        self.assertIn("gege", wecom_bridge.INBOUND_CONTACTS)
        self.assertNotIn("gege", wecom_bridge.IMAGE_CONTACTS)


if __name__ == "__main__":
    unittest.main()
