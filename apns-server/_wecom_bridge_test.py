"""企业微信 ↔ 联系人桥测试 (2026-10-26)。

覆盖:
  1. /wecom/inbound 鉴权: 缺密钥 401 / 错密钥 403 / 密钥文件不是 0600 fail-closed
  2. 入站: kimi / kairos 走各自 app 发送管线, 文本带［💬微信］前缀, metadata
     source=wecom + wecom={msg_id, agent_id, msg_type}; 不支持的联系人 400
  3. 按 agent:msg_id 去重, 失败且未入历史时释放 id
  4. 出站: 只有回答的用户消息带 wecom 标记才写 outbox; app 消息不写
  5. outbox 文件格式 / 0600 / 原子落盘
  6. Kimi / Kairos prompt 在微信来源时换成微信入口说明

全部直接调 handler, 不碰线上 8291 服务。
"""
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import push
import wecom_bridge
from push import PushHandler

SECRET = "wecom-test-secret-0123456789abcdef"


class _Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else default


class WecomBase(unittest.TestCase):
    def setUp(self) -> None:
        push._WECOM_INBOUND_SEEN.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.secret_file = self.tmp / "secret"
        self.secret_file.write_text(SECRET + "\n", encoding="utf-8")
        os.chmod(self.secret_file, 0o600)
        self.outbox = self.tmp / "outbox"
        self.outbox.mkdir(mode=0o700)
        for key, value in (
            (wecom_bridge.SECRET_FILE_ENV, str(self.secret_file)),
            (wecom_bridge.OUTBOX_DIR_ENV, str(self.outbox)),
        ):
            old = os.environ.get(key)
            os.environ[key] = value
            self.addCleanup(self._restore_env, key, old)

    @staticmethod
    def _restore_env(key, old) -> None:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    def handler(self, body, *, secret=SECRET) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.responses = []
        handler._send_json = lambda status, payload, **_kw: handler.responses.append((status, payload))
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = _Headers({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        if secret is not None:
            headers["X-Wecom-Webhook-Secret"] = secret
        handler.headers = headers
        handler.rfile = io.BytesIO(raw)
        handler.path = "/wecom/inbound"
        return handler

    def outbox_items(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.outbox.glob("*.json"))]


class WecomInboundTest(WecomBase):
    BODY = {"contact_id": "kimi", "agent_id": "1000003", "id": "7000000000000000001", "msg_type": "text", "text": "卡拉米在吗"}

    def run_inbound(self, body=None, *, secret=SECRET, dispatch_status=200, record=True):
        captured = []

        def fake_dispatch(handler, contact_id, forward):
            captured.append((contact_id, forward))
            payload = {"ok": True, "queued": True}
            if record:
                payload["record"] = {"ts": "t1", "text": forward["text"], "metadata": forward["metadata"]}
            handler._send_json(dispatch_status, payload)
            return True

        h = self.handler(self.BODY if body is None else body, secret=secret)
        with patch("push.dispatch_contact_send", side_effect=fake_dispatch):
            h._handle_wecom_inbound()
        return h, captured

    def test_missing_secret_is_401(self) -> None:
        h, captured = self.run_inbound(secret=None)
        self.assertEqual(h.responses[-1][0], 401)
        self.assertEqual(captured, [])

    def test_wrong_secret_is_403(self) -> None:
        h, captured = self.run_inbound(secret="nope-nope-nope-nope-nope")
        self.assertEqual(h.responses[-1][0], 403)
        self.assertEqual(captured, [])

    def test_secret_file_not_0600_fails_closed(self) -> None:
        os.chmod(self.secret_file, 0o644)
        h, captured = self.run_inbound()
        self.assertEqual(h.responses[-1][0], 401)
        self.assertEqual(captured, [])

    def test_kimi_delivery_prefix_and_metadata(self) -> None:
        h, captured = self.run_inbound()
        self.assertEqual(h.responses[-1][0], 200)
        self.assertEqual(len(captured), 1)
        contact_id, forward = captured[0]
        self.assertEqual(contact_id, "kimi")
        self.assertEqual(forward["text"], "［💬微信］卡拉米在吗")
        self.assertEqual(forward["metadata"], {
            "source": "wecom",
            "wecom": {"msg_id": "7000000000000000001", "agent_id": "1000003", "msg_type": "text"},
        })

    def test_kairos_goes_to_kairos(self) -> None:
        body = {**self.BODY, "contact_id": "kairos", "agent_id": "1000004", "id": "k1"}
        h, captured = self.run_inbound(body)
        self.assertEqual(h.responses[-1][0], 200)
        self.assertEqual(captured[0][0], "kairos")
        self.assertEqual(captured[0][1]["metadata"]["wecom"]["agent_id"], "1000004")

    def test_unsupported_contact_is_400(self) -> None:
        for contact in ("xiaoke", "apples", "kiro", ""):
            push._WECOM_INBOUND_SEEN.clear()
            h, captured = self.run_inbound({**self.BODY, "contact_id": contact})
            self.assertEqual(h.responses[-1][0], 400, contact)
            self.assertEqual(captured, [], contact)

    def test_bad_fields_are_400(self) -> None:
        for patch_ in ({"text": ""}, {"text": None}, {"id": ""}, {"id": True}, {"agent_id": "abc"}):
            h, captured = self.run_inbound({**self.BODY, **patch_})
            self.assertEqual(h.responses[-1][0], 400, patch_)
            self.assertEqual(captured, [])

    def test_duplicate_is_acknowledged_once(self) -> None:
        self.run_inbound()
        h, captured = self.run_inbound()
        self.assertEqual(h.responses[-1], (200, {"ok": True, "duplicate": True, "id": "7000000000000000001"}))
        self.assertEqual(captured, [])

    def test_failure_without_record_releases_id(self) -> None:
        h, _ = self.run_inbound(dispatch_status=503, record=False)
        self.assertEqual(h.responses[-1][0], 503)
        h, captured = self.run_inbound()
        self.assertEqual(h.responses[-1][0], 200)
        self.assertEqual(len(captured), 1)

    def test_text_truncated(self) -> None:
        h, captured = self.run_inbound({**self.BODY, "text": "a" * 9000})
        self.assertEqual(len(captured[0][1]["text"]), len(wecom_bridge.USER_PREFIX) + wecom_bridge.TEXT_LIMIT)


class WecomOutboundTest(WecomBase):
    def handler_only(self) -> PushHandler:
        return object.__new__(PushHandler)

    def test_app_message_never_mirrors(self) -> None:
        h = self.handler_only()
        for rec in (
            {"ts": "t", "text": "hi"},
            {"ts": "t", "metadata": {"source": "garden"}},
            {"ts": "t", "metadata": {"wecom": {"agent_id": "1000003"}}},  # no source=wecom
            {"ts": "t", "metadata": {"source": "wecom"}},  # no marker
            None,
        ):
            self.assertFalse(h._mirror_reply_to_wecom("kimi", rec, "回复"), rec)
        self.assertEqual(self.outbox_items(), [])

    def test_wecom_message_mirrors_to_its_agent(self) -> None:
        h = self.handler_only()
        rec = {"ts": "u1", "metadata": wecom_bridge.inbound_body({
            "contact_id": "kimi", "agent_id": "1000003", "msg_id": "m1", "msg_type": "text", "text": "x",
        })["metadata"]}
        self.assertTrue(h._mirror_reply_to_wecom("kimi", rec, "第一段\n\n第二段", reply_ts="r1"))
        items = self.outbox_items()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual({k: item[k] for k in ("kind", "contact_id", "agent_id", "in_reply_to", "user_ts", "reply_ts", "text")}, {
            "kind": "reply", "contact_id": "kimi", "agent_id": "1000003", "in_reply_to": "m1",
            "user_ts": "u1", "reply_ts": "r1", "text": "第一段\n\n第二段",
        })
        path = next(self.outbox.glob("*.json"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p for p in self.outbox.iterdir() if ".tmp-" in p.name], [])

    def test_missing_outbox_dir_is_logged_not_raised(self) -> None:
        os.environ[wecom_bridge.OUTBOX_DIR_ENV] = str(self.tmp / "nope")
        h = self.handler_only()
        rec = {"ts": "u1", "metadata": {"source": "wecom", "wecom": {"agent_id": "1000003", "msg_id": "m"}}}
        self.assertFalse(h._mirror_reply_to_wecom("kimi", rec, "x"))

    def test_empty_reply_not_written(self) -> None:
        h = self.handler_only()
        rec = {"ts": "u1", "metadata": {"source": "wecom", "wecom": {"agent_id": "1000003", "msg_id": "m"}}}
        self.assertFalse(h._mirror_reply_to_wecom("kimi", rec, "   "))
        self.assertEqual(self.outbox_items(), [])


class WecomKimiPipelineTest(WecomBase):
    """Real _handle_kimi_chat_send + fake Kimi Web: outbox only for WeChat turns."""

    def make(self, reply_text):
        from _kimi_chat_test import FakeWebChat, KimiWebChatRoutingTest

        handler, chat, web = KimiWebChatRoutingTest.make_handler(None, web=FakeWebChat(text=reply_text))
        return handler, chat, web, KimiWebChatRoutingTest.wait_idle

    def test_app_turn_replies_only_in_app(self) -> None:
        handler, chat, web, wait_idle = self.make("app 里的回复")
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        wait_idle(handler)
        self.assertEqual(chat.records[-1]["text"], "app 里的回复")
        self.assertEqual(self.outbox_items(), [])
        prompt = next(row[2] for row in web.calls if row[0] == "submit")
        self.assertIn("cc_companion_kimi_private", prompt)

    def test_wecom_turn_also_goes_to_wecom_outbox(self) -> None:
        handler, chat, web, wait_idle = self.make("微信那边的回复")
        body = wecom_bridge.inbound_body({
            "contact_id": "kimi", "agent_id": "1000003", "msg_id": "m-1", "msg_type": "text", "text": "在吗",
        })
        handler._handle_kimi_chat_send(body, "kimi")
        wait_idle(handler)
        user = chat.records[0]
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["text"], "［💬微信］在吗")
        self.assertEqual(user["metadata"]["source"], "wecom")
        self.assertEqual(chat.records[-1]["text"], "微信那边的回复")
        items = self.outbox_items()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["contact_id"], items[0]["agent_id"], items[0]["text"], items[0]["in_reply_to"]),
                         ("kimi", "1000003", "微信那边的回复", "m-1"))
        prompt = next(row[2] for row in web.calls if row[0] == "submit")
        self.assertIn("wecom_karami_app", prompt)
        # the next ordinary app turn is not mirrored
        handler._handle_kimi_chat_send({"text": "app again"}, "kimi")
        wait_idle(handler)
        self.assertEqual(len(self.outbox_items()), 1)


class WecomKairosTaskTest(WecomBase):
    def make(self):
        from _kimi_chat_test import FakeChat
        from link_preview import LinkPreviewBundle

        chat = FakeChat()
        h = object.__new__(PushHandler)
        h.tasks, h.responses = [], []
        h._send_json = lambda status, payload, **_kw: h.responses.append((status, payload))
        h._chat_for_contact = lambda _c: chat
        h._enrich_user_links = lambda _t: LinkPreviewBundle(previews=(), prompt_context="")
        h._reading_ai_grant_after_user_append = lambda *_a: None
        h._set_typing_for_contact = lambda *_a, **_k: None
        h._clear_chat_draft = lambda *_a: None
        h._enqueue_kairos_task = lambda task: h.tasks.append(task)
        return h, chat

    def test_wecom_marker_rides_on_the_task(self) -> None:
        h, chat = self.make()
        body = wecom_bridge.inbound_body({
            "contact_id": "kairos", "agent_id": "1000004", "msg_id": "k-1", "msg_type": "text", "text": "在吗",
        })
        h._handle_kairos_chat_send(body, "kairos")
        self.assertEqual(chat.records[0]["text"], "［💬微信］在吗")
        self.assertEqual(h.tasks[0]["wecom"], {"msg_id": "k-1", "agent_id": "1000004", "msg_type": "text"})
        self.assertIn("wecom_kairos_app", h._kairos_prompt_for_task(h.tasks[0]))

    def test_app_task_has_no_marker(self) -> None:
        h, _chat = self.make()
        h._handle_kairos_chat_send({"text": "hi"}, "kairos")
        self.assertNotIn("wecom", h.tasks[0])


class WecomPromptTest(unittest.TestCase):
    def test_kimi_prompt_entry(self) -> None:
        h = object.__new__(PushHandler)
        h._kimi_bqb_protocol = lambda: ""
        app = h._kimi_prompt("hi")
        wx = h._kimi_prompt("［💬微信］hi", wecom_entry=True)
        self.assertIn("cc_companion_kimi_private", app)
        self.assertNotIn("微信", app)
        self.assertIn("wecom_karami_app", wx)
        self.assertIn("不要用 Markdown", wx)
        self.assertIn("对方说：［💬微信］hi", wx)

    def test_kairos_prompt_entry(self) -> None:
        h = object.__new__(PushHandler)
        app = h._kairos_prompt_for_task({"text": "hi"})
        wx = h._kairos_prompt_for_task({"text": "［💬微信］hi", "wecom": {"agent_id": "1000004", "msg_id": "m"}})
        self.assertIn("cc_companion_kairos_private", app)
        self.assertIn("wecom_kairos_app", wx)
        self.assertIn("contact_id: kairos", wx)
        self.assertNotIn("cc_companion_kairos_private", wx)

    def test_client_cannot_forge_marker(self) -> None:
        # /chat/send strips metadata.wecom before any pipeline sees it.
        src = Path(push.__file__).read_text(encoding="utf-8")
        start = src.index("def _handle_chat_send(")
        block = src[start:start + 4000]
        self.assertIn("wecom_bridge.META_KEY", block)


if __name__ == "__main__":
    unittest.main()
