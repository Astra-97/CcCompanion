"""哈基米 hajiki (2026-11-01) tests.

  1. registry: hajiki is a chat contact (chat/history/draft/busy/stop/realtime),
     no attachments, apples/toolbot unchanged
  2. push handler: real _handle_hajiki_chat_send + fake pi client — the turn
     lands in hajiki's history, errors are visible, attachments are refused,
     not-configured answers without touching Tokyo
  3. config: _hajiki_client_from_config builds the Tokyo launch only when
     hajiki_ssh_host is set

Never touches the live 8291 service or Tokyo.
"""
import collections
import threading
import types
import unittest
from unittest.mock import patch

import push
from chat_history import ChatStreamBus
from contacts import chat_contact_directory, default_contact_routes
from pi_rpc import PiRPCAuthRequired, PiRPCCancelled, PiRPCError
from push import PushHandler

from _kiro_acp_test import FakeChat, _immediate_thread


class FakeClient:
    def __init__(self, *, answers=None, failure=None):
        self.prompts = []
        self.kwargs = []
        self.answers = list(answers or ["小南！哈基米来啦！"])
        self.failure = failure
        self.closed = 0

    def prompt(self, text, *, on_delta=None, cancel_event=None, **kw):
        self.prompts.append(text)
        self.kwargs.append(kw)
        if self.failure is not None:
            raise self.failure
        answer = self.answers.pop(0) if self.answers else "嗯嗯！"
        if on_delta is not None:
            on_delta(answer[:2])
        return answer

    def close(self):
        self.closed += 1


class HajikiHandlerTest(unittest.TestCase):
    def handler(self, client):
        chat = FakeChat()
        state = types.SimpleNamespace(
            contact_chats={"hajiki": chat},
            contact_routes=default_contact_routes(),
            contact_catalog=[],
            contact_typing_states={},
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
            chat_stream_revisions={},
            chat_stream_bus=ChatStreamBus(),
            hajiki_client=client,
            hajiki_lock=threading.Lock(),
            hajiki_queue=collections.deque(),
            hajiki_worker_running=False,
            hajiki_active={},
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
            handler._handle_hajiki_chat_send(body)

    def test_turn_lands_in_history(self) -> None:
        client = FakeClient(answers=["小南！哈基米来啦！"])
        handler, chat = self.handler(client)
        self.send(handler, {"text": "你好，你是谁"})
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(client.prompts, ["你好，你是谁"])
        self.assertGreaterEqual(client.kwargs[0].get("timeout", 0), 600)
        self.assertEqual([r["role"] for r in chat.records], ["user", "assistant"])
        self.assertEqual(chat.records[0]["source"], "android-app:hajiki")
        self.assertEqual(chat.records[-1]["text"], "小南！哈基米来啦！")
        self.assertEqual(chat.records[-1]["source"], "hajiki:pi")
        self.assertFalse(handler.state.hajiki_worker_running)
        self.assertEqual(handler.state.hajiki_active, {})

    def test_errors_are_visible(self) -> None:
        for failure, source in (
            (PiRPCAuthRequired("401"), "hajiki:auth-required"),
            (PiRPCError("boom"), "hajiki:error"),
            (PiRPCCancelled("说到一半"), "hajiki:interrupted"),
        ):
            client = FakeClient(failure=failure)
            handler, chat = self.handler(client)
            self.send(handler, {"text": "在吗"})
            self.assertEqual(chat.records[-1]["role"], "assistant")
            self.assertEqual(chat.records[-1]["source"], source)
        self.assertIn("说到一半", chat.records[-1]["text"])

    def test_not_configured(self) -> None:
        handler, chat = self.handler(None)
        self.send(handler, {"text": "在吗"})
        self.assertEqual(chat.records[-1]["text"], PushHandler._HAJIKI_NOT_CONFIGURED_TEXT)

    def test_attachments_rejected(self) -> None:
        handler, chat = self.handler(FakeClient())
        handler._discard_uncommitted_staged_attachments = lambda _items: None
        self.send(handler, {"text": "看图", "_pwa_staged_attachments": [{"attachment_url": "/attachments/x.png"}]})
        self.assertEqual(handler.responses[-1][0], 415)
        self.assertEqual(chat.records, [])

    def test_empty_text_rejected(self) -> None:
        handler, chat = self.handler(FakeClient())
        self.send(handler, {"text": "  "})
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(chat.records, [])


class RegistryTest(unittest.TestCase):
    def test_hajiki_is_a_chat_contact(self) -> None:
        state = types.SimpleNamespace(
            contact_routes=default_contact_routes(), contact_catalog=[],
            contact_chats={"hajiki": object(), "apples": object(), "toolbot": object()},
        )
        contacts = {c["id"]: c for c in chat_contact_directory(state)}
        hajiki = contacts["hajiki"]
        self.assertEqual(hajiki["display_name"], "哈基米")
        self.assertFalse(hajiki["read_only"])
        for cap in ("chat", "history", "draft", "busy", "stop", "realtime"):
            self.assertIn(cap, hajiki["capabilities"])
        self.assertNotIn("attachments", hajiki["capabilities"])
        self.assertTrue(hajiki["stop"]["supported"])
        self.assertIn("group_chat", contacts["apples"]["capabilities"])
        self.assertEqual(contacts["toolbot"]["capabilities"], ["history", "realtime"])


class ConfigTest(unittest.TestCase):
    def test_unset_host_disables(self) -> None:
        self.assertIsNone(push._hajiki_client_from_config({}))

    def test_tokyo_launch(self) -> None:
        client = push._hajiki_client_from_config({"hajiki_ssh_host": "tokyo", "hajiki_remote_user": "pi"})
        self.assertIsNotNone(client)
        self.assertEqual(client.argv, ["/home/pi/hajiki/bin/hajiki-rpc"])
        self.assertEqual(client.remote.target.cwd, "/home/pi/hajiki/workdir")
        self.assertEqual(client.remote.target.pidfile, "/home/pi/hajiki/run/rpc.pid")


if __name__ == "__main__":
    unittest.main()
