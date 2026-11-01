"""哈基米 hajiki (2026-11-01) tests.

  1. registry: hajiki is a chat contact (chat/history/draft/busy/stop/
     attachments/realtime), apples/toolbot unchanged
  2. push handler: real _handle_hajiki_chat_send + fake pi client — the turn
     lands in hajiki's history, errors are visible, pictures reach pi as
     ImageContent (2026-11-01), other files are refused, not-configured
     answers without touching Tokyo
  5. pi_images + PiRPCClient: pictures are shrunk/encoded and sent in the
     prompt command's ``images`` field (fake pi child)
  3. config: _hajiki_client_from_config builds the Tokyo launch only when
     hajiki_ssh_host is set
  4. WeChat (企业微信「哈基米」1000009): WeChat turns strip the source prefix,
     get the plain-text note and are mirrored to the outbox; app turns never are

Never touches the live 8291 service or Tokyo.
"""
import base64
import collections
import io
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

from PIL import Image

import push
from chat_history import ChatStreamBus
import wecom_bridge
from contacts import chat_contact_directory, default_contact_routes
import pi_images
from pi_rpc import PiRPCAuthRequired, PiRPCCancelled, PiRPCClient, PiRPCError
from push import PushHandler

from _kiro_acp_test import FakeChat, _immediate_thread


class FakeClient:
    def __init__(self, *, answers=None, failure=None):
        self.prompts = []
        self.kwargs = []
        self.answers = list(answers or ["小南！哈基米来啦！"])
        self.failure = failure
        self.closed = 0
        self.images = []

    def prompt(self, text, *, on_delta=None, cancel_event=None, images=None, **kw):
        self.prompts.append(text)
        self.kwargs.append(kw)
        self.images.append(images)
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

    def staged_png(self, size=(40, 30), color=(255, 0, 0)):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "a.png"
        Image.new("RGB", size, color).save(path, format="PNG")
        return {"attachment_id": "a1", "attachment_url": "/attachments/a.png", "filename": "红.png",
                "type": "image", "media_type": "image/png", "size": path.stat().st_size, "stored_path": str(path)}

    def test_picture_reaches_pi(self) -> None:
        client = FakeClient(answers=["是一块红色！"])
        handler, chat = self.handler(client)
        self.send(handler, {"text": "图里是什么", "_pwa_staged_attachments": [self.staged_png()]})
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(client.prompts, ["图里是什么"])
        images = client.images[0]
        self.assertEqual(len(images), 1)
        self.assertEqual((images[0]["type"], images[0]["mimeType"]), ("image", "image/png"))
        with Image.open(io.BytesIO(base64.b64decode(images[0]["data"]))) as img:
            self.assertEqual(img.size, (40, 30))
        user = chat.records[0]
        self.assertEqual((user["attachment_url"], user["attachment_type"], user["attachment_filename"]),
                         ("/attachments/a.png", "image", "红.png"))
        self.assertEqual(chat.records[-1]["text"], "是一块红色！")

    def test_picture_without_caption(self) -> None:
        client = FakeClient()
        handler, chat = self.handler(client)
        self.send(handler, {"text": "", "_pwa_staged_attachments": [self.staged_png()]})
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(client.prompts, [PushHandler._HAJIKI_IMAGE_ONLY_TEXT])
        self.assertEqual(len(client.images[0]), 1)

    def test_text_turn_sends_no_images(self) -> None:
        client = FakeClient()
        handler, chat = self.handler(client)
        self.send(handler, {"text": "在吗"})
        self.assertIsNone(client.images[0])

    def test_unreadable_picture_is_visible(self) -> None:
        client = FakeClient()
        handler, chat = self.handler(client)
        bad = self.staged_png()
        Path(bad["stored_path"]).write_bytes(b"not an image")
        self.send(handler, {"text": "看", "_pwa_staged_attachments": [bad]})
        self.assertEqual(client.prompts, [])
        self.assertEqual(chat.records[-1]["text"], PushHandler._HAJIKI_IMAGE_TEXT)
        self.assertEqual(chat.records[-1]["source"], "hajiki:image-error")

    def test_other_files_rejected(self) -> None:
        handler, chat = self.handler(FakeClient())
        discarded = []
        handler._discard_uncommitted_staged_attachments = discarded.append
        staged = [{"attachment_url": "/attachments/x.pdf", "type": "file"}]
        self.send(handler, {"text": "看文件", "_pwa_staged_attachments": staged})
        self.assertEqual(handler.responses[-1][0], 415)
        self.assertEqual(handler.responses[-1][1]["reason"], PushHandler._HAJIKI_FILES_TEXT)
        self.assertEqual(discarded, [staged])
        self.assertEqual(chat.records, [])

    def test_too_many_pictures_rejected(self) -> None:
        handler, chat = self.handler(FakeClient())
        handler._discard_uncommitted_staged_attachments = lambda _items: None
        self.send(handler, {"text": "", "_pwa_staged_attachments": [self.staged_png()] * (PushHandler._HAJIKI_MAX_IMAGES + 1)})
        self.assertEqual(handler.responses[-1][0], 415)
        self.assertEqual(chat.records, [])

    def test_empty_text_rejected(self) -> None:
        handler, chat = self.handler(FakeClient())
        self.send(handler, {"text": "  "})
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(chat.records, [])


class HajikiWecomTest(unittest.TestCase):
    handler = HajikiHandlerTest.handler
    send = HajikiHandlerTest.send

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.outbox_dir = __import__("pathlib").Path(tmp.name)
        old = os.environ.get(wecom_bridge.OUTBOX_DIR_ENV)
        os.environ[wecom_bridge.OUTBOX_DIR_ENV] = str(self.outbox_dir)
        self.addCleanup(lambda: os.environ.pop(wecom_bridge.OUTBOX_DIR_ENV, None) if old is None else os.environ.__setitem__(wecom_bridge.OUTBOX_DIR_ENV, old))

    def outbox(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.outbox_dir.glob("*.json"))]

    def test_wecom_turn_mirrors_to_1000009(self) -> None:
        client = FakeClient(answers=["喵！哈基米在！\n\n小南找我呀？"])
        handler, chat = self.handler(client)
        self.send(handler, wecom_bridge.inbound_body({"contact_id": "hajiki", "agent_id": "1000009", "msg_id": "h-1", "msg_type": "text", "text": "哈基米在吗"}))
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertTrue(client.prompts[0].startswith(PushHandler._HAJIKI_WECOM_NOTE))
        self.assertTrue(client.prompts[0].endswith("\n\n哈基米在吗"))
        self.assertNotIn(wecom_bridge.USER_PREFIX, client.prompts[0])
        items = self.outbox()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["contact_id"], items[0]["agent_id"], items[0]["text"]), ("hajiki", "1000009", "喵！哈基米在！\n\n小南找我呀？"))

    def test_wecom_error_is_mirrored_too(self) -> None:
        handler, chat = self.handler(FakeClient(failure=PiRPCError("boom")))
        self.send(handler, wecom_bridge.inbound_body({"contact_id": "hajiki", "agent_id": "1000009", "msg_id": "h-2", "msg_type": "text", "text": "在吗"}))
        self.assertEqual(self.outbox()[0]["text"], PushHandler._HAJIKI_ERROR_TEXT)

    def test_app_turn_is_not_mirrored(self) -> None:
        client = FakeClient()
        handler, chat = self.handler(client)
        self.send(handler, {"text": "在吗"})
        self.assertEqual(client.prompts, ["在吗"])
        self.assertEqual(self.outbox(), [])

    def test_bridge_accepts_hajiki_pictures(self) -> None:
        self.assertIn("hajiki", wecom_bridge.INBOUND_CONTACTS)
        self.assertIn("hajiki", wecom_bridge.IMAGE_CONTACTS)
        self.assertNotIn("hajiki", wecom_bridge.IMAGE_REPLY_CONTACTS)
        clean, error = wecom_bridge.parse_inbound({"contact_id": "hajiki", "agent_id": "1000009", "id": "1", "msg_type": "image",
                                                   "text": "（发来一张图片）", "media": {"status": "ok", "path": "/x.jpg"}})
        self.assertEqual(error, "")
        self.assertEqual(clean["media"], {"path": "/x.jpg"})
        self.assertIn("wecom_hajiki_app", "\n".join(wecom_bridge.prompt_entry_lines("hajiki")))

    def test_wecom_picture_reaches_pi_and_mirrors(self) -> None:
        client = FakeClient(answers=["一块红色～"])
        handler, chat = self.handler(client)
        staged = HajikiHandlerTest.staged_png(self)
        clean = {"contact_id": "hajiki", "agent_id": "1000009", "msg_id": "h-3", "msg_type": "image",
                 "text": "（发来一张图片，请看看并描述内容）"}
        self.send(handler, wecom_bridge.inbound_body(clean, [staged]))
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(client.prompts[0], PushHandler._HAJIKI_WECOM_NOTE + "（发来一张图片，请看看并描述内容）")
        self.assertEqual(len(client.images[0]), 1)
        self.assertEqual(chat.records[0]["attachment_type"], "image")
        self.assertEqual(self.outbox()[0]["text"], "一块红色～")


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
        for cap in ("chat", "history", "draft", "busy", "stop", "attachments", "realtime"):
            self.assertIn(cap, hajiki["capabilities"])
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


FAKE_PI_IMAGES = textwrap.dedent(r'''
    import json, sys
    out = sys.stdout.buffer
    def emit(obj):
        out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")); out.flush()
    for raw in sys.stdin.buffer:
        cmd = json.loads(raw)
        rid, kind = cmd.get("id"), cmd.get("type")
        if kind == "get_state":
            emit({"id": rid, "type": "response", "command": "get_state", "success": True,
                  "data": {"model": {"id": "m"}, "isStreaming": False, "isCompacting": False}})
        elif kind == "prompt":
            images = cmd.get("images")
            note = "none" if images is None else ",".join(f"{i['type']}:{i['mimeType']}:{len(i['data'])>0}" for i in images)
            emit({"id": rid, "type": "response", "command": "prompt", "success": True, "data": {"disposition": "started"}})
            emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                  "content": [{"type": "text", "text": cmd.get("message", "") + "|" + note}]}})
            emit({"type": "agent_settled", "aborted": False})
''')


class PiImagesTest(unittest.TestCase):
    def tmp(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def decode(self, content):
        return Image.open(io.BytesIO(base64.b64decode(content["data"])))

    def test_small_png_kept(self) -> None:
        path = self.tmp() / "a.png"
        Image.new("RGB", (20, 10), (0, 0, 255)).save(path)
        content = pi_images.image_content(path, "image/png")
        self.assertEqual(content["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(content["data"]), path.read_bytes())

    def test_large_picture_shrunk_to_jpeg(self) -> None:
        path = self.tmp() / "big.jpg"
        Image.new("RGB", (4000, 1000), (10, 200, 10)).save(path, quality=95)
        content = pi_images.image_content(path, "image/jpeg")
        self.assertEqual(content["mimeType"], "image/jpeg")
        with self.decode(content) as img:
            self.assertEqual(img.size, (2000, 500))

    def test_exif_rotation_applied(self) -> None:
        path = self.tmp() / "rot.jpg"
        img = Image.new("RGB", (60, 20), (200, 0, 0))
        exif = img.getexif()
        exif[0x0112] = 6  # rotate 90 CW on display
        img.save(path, exif=exif)
        with self.decode(pi_images.image_content(path, "image/jpeg")) as out:
            self.assertEqual(out.size, (20, 60))

    def test_gif_becomes_jpeg_frame(self) -> None:
        path = self.tmp() / "a.gif"
        Image.new("P", (16, 16)).save(path)
        self.assertIn(pi_images.image_content(path, "image/gif")["mimeType"], ("image/jpeg", "image/png"))

    def test_garbage_rejected(self) -> None:
        path = self.tmp() / "x.png"
        path.write_bytes(b"nope")
        with self.assertRaises(ValueError):
            pi_images.image_content(path, "image/png")


class PiRPCImagesTest(unittest.TestCase):
    def test_images_go_in_the_prompt_command(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = Path(tmp.name) / "fake_pi.py"
        script.write_text(FAKE_PI_IMAGES, encoding="utf-8")
        client = PiRPCClient([sys.executable, str(script)], start_timeout=10, command_timeout=10)
        self.addCleanup(client.close)
        image = {"type": "image", "data": base64.b64encode(b"x" * 300000).decode(), "mimeType": "image/jpeg"}
        self.assertEqual(client.prompt("看", images=[image]), "看|image:image/jpeg:True")
        self.assertEqual(client.prompt("在吗"), "在吗|none")


if __name__ == "__main__":
    unittest.main()
