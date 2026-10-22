"""螃蟹桌宠 POST /pet/tap 测试 (2026-10-22).

覆盖:
  1. 鉴权: 缺/错 X-Auth-Token → 401 且不通知; strict_auth=false 也不放行 (fail-closed)
  2. 路由: 第三个 tag "pet" (summon), 不动设备
  3. 计数合并 (v2): body count/duration_s; count==1 / >1 两种文案; count 截到 999, duration 最少 1
  4. 不做 20s 去重, 只防刷: 60s 内第 7 次结算丢弃 (deduped=true); 与 bedside/door 互不影响
  5. 通知载荷: [桌宠·螃蟹被捏了]; test=1 (query 或 body) 带测试标记
  6. 私聊胶囊: 「方小南捏了捏🤏屏幕上的🦀」, count>1 加「 ×N」; test 不冒, 被丢不冒
  7. body 校验: 空 body 可; 非 JSON/非对象/非数字 400; 过大 413
  8. nfc_tags.json 里叫 "pet" 的 tag 被忽略
  9. 爆锤 (hammer=true): channel「…后面抡起爆锤把螃蟹砸晕了」, 胶囊「方小南抡起爆锤🔨砸晕了屏幕上的🦀 ×N」
 10. POST /pet/screenshot: 鉴权; 存图 + 方小南图片消息落私聊 + [桌宠·截屏] 通知带路径; test 不落私聊; 非图 415; 防刷 429

全部 mock, 不碰线上 8291 服务。
"""
import io
import json
import tempfile
import types
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import push
from push import PushHandler
from _nfc_tap_test import BEDSIDE, DOOR, NfcTapBase, _Headers, _write_private

SECRET = "pet-shared-secret-0123456789"


class PetTapBase(NfcTapBase):
    def setUp(self) -> None:
        super().setUp()
        push._PET_TAP_LIMIT.reset()
        self.addCleanup(push._PET_TAP_LIMIT.reset)

    def pet(self, *, token=SECRET, body=b"", query="", strict=True, path="/pet/tap") -> PushHandler:
        h = object.__new__(PushHandler)
        h.responses = []
        h.wfile = io.BytesIO()
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        h.rfile = io.BytesIO(body)
        h.state = types.SimpleNamespace(
            shared_secret=SECRET,
            strict_auth=strict,
            allowed_ips=["10.0.0.1"],
            channel_transport_enabled=True,
            channel_transport_contacts=["xiaoke"],
            channel_transport_fallback_to_tmux=True,
            active_session="cctg",
            default_session="cctg",
            contact_chats={},
            attachments_dir=getattr(self, "attach_dir", None),
        )
        h._chat_for_contact = lambda contact: types.SimpleNamespace(
            append=lambda **rec: self.bubbles.append((contact, rec)) or rec
        )
        headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        if token is not None:
            headers["X-Auth-Token"] = token
        h.headers = _Headers(headers)
        h.path = path + query
        h._check_ip_allowed = lambda: True
        h._send_json = lambda status, payload, **_kw: h.responses.append((status, payload))
        h._trusted_client_ip = lambda: "127.0.0.1"
        h.do_POST()
        return h


class PetTapAuthTest(PetTapBase):
    def assert_401_silent(self, h) -> None:
        self.assertEqual(h.responses, [(401, {"ok": False, "error": "unauthorized"})])
        self.assertEqual(self.channel_calls, [])
        self.assertEqual(self.tmux_calls, [])
        self.assertEqual(self.bubbles, [])

    def test_missing_token_401(self) -> None:
        self.assert_401_silent(self.pet(token=None))

    def test_wrong_token_401(self) -> None:
        self.assert_401_silent(self.pet(token="nope"))

    def test_strict_auth_off_still_requires_token(self) -> None:
        self.assert_401_silent(self.pet(token=None, strict=False))

    def test_nfc_key_is_not_a_pet_credential(self) -> None:
        self.assert_401_silent(self.pet(token=BEDSIDE))

    def test_unauthorized_does_not_consume_limit(self) -> None:
        for _ in range(10):
            self.pet(token="bad")
        h = self.pet()
        self.assertEqual(h.responses, [(200, {"ok": True, "deduped": False})])


class PetTapRoutingTest(PetTapBase):
    def test_ok_response_and_payload(self) -> None:
        h = self.pet(body={})
        self.assertEqual(h.responses, [(200, {"ok": True, "deduped": False})])
        call = self.channel_calls[0]
        self.assertEqual(call["contact_id"], "xiaoke")
        self.assertTrue(call["message_id"].startswith("pet:pet:"))
        meta = call["metadata"]
        self.assertEqual((meta["nfc_tag"], meta["nfc_action"]), ("pet", "summon"))
        self.assertEqual(meta["source"], "pet-tap")
        self.assertIs(meta["pet_tap"], True)
        self.assertIs(meta["test"], False)
        self.assertEqual((meta["count"], meta["duration_s"]), (1, 1))
        self.assertRegex(
            call["text"],
            r"^\[桌宠·螃蟹被捏了\] \d{2}:\d{2} 方小南捏了一下手机屏幕上的螃蟹"
            r"（她正在用手机，用 companion reply contact_id=xiaoke 跟她打招呼）$",
        )
        self.assertEqual(self.home_calls, [])
        self.assertEqual(self.tmux_calls, [])

    def test_empty_body_ok(self) -> None:
        h = self.pet(body=b"")
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(len(self.channel_calls), 1)

    def test_text_uses_beijing_time(self) -> None:
        utc = datetime(2026, 10, 22, 0, 5, tzinfo=ZoneInfo("UTC"))
        text = PushHandler._nfc_tap_notification_text(utc, profile=dict(PushHandler._PET_PROFILE))
        self.assertTrue(text.startswith("[桌宠·螃蟹被捏了] 08:05 方小南捏了一下手机屏幕上的螃蟹"))

    def test_channel_failure_falls_back_to_tmux(self) -> None:
        self.channel_ok = False
        self.pet()
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertIn("[桌宠·螃蟹被捏了]", self.tmux_calls[0][1])


class PetTapCountTest(PetTapBase):
    def test_count_one_text(self) -> None:
        self.pet(body={"count": 1, "duration_s": 1})
        self.assertRegex(self.channel_calls[0]["text"], r"^\[桌宠·螃蟹被捏了\] \d{2}:\d{2} 方小南捏了一下手机屏幕上的螃蟹（")
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏屏幕上的🦀")

    def test_count_many_text_and_bubble(self) -> None:
        self.pet(body={"count": 7, "duration_s": 12})
        call = self.channel_calls[0]
        self.assertRegex(
            call["text"],
            r"^\[桌宠·螃蟹被捏了\] \d{2}:\d{2} 方小南 12 秒内捏了 7 下手机屏幕上的螃蟹"
            r"（她正在用手机，用 companion reply contact_id=xiaoke 跟她打招呼）$",
        )
        self.assertEqual((call["metadata"]["count"], call["metadata"]["duration_s"]), (7, 12))
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏屏幕上的🦀 ×7")
        self.assertEqual(self.bubbles[0][1]["metadata"]["items"][0]["human"], "方小南捏了捏🤏屏幕上的🦀 ×7")

    def test_count_clamped_to_999_and_duration_min_1(self) -> None:
        self.pet(body={"count": 5000, "duration_s": 0})
        meta = self.channel_calls[0]["metadata"]
        self.assertEqual((meta["count"], meta["duration_s"]), (999, 1))
        self.assertIn("1 秒内捏了 999 下", self.channel_calls[0]["text"])

    def test_nonpositive_count_is_one(self) -> None:
        self.pet(body={"count": -3})
        self.assertEqual(self.channel_calls[0]["metadata"]["count"], 1)
        self.assertIn("捏了一下", self.channel_calls[0]["text"])

    def test_float_duration_rounds(self) -> None:
        self.pet(body={"count": 3, "duration_s": 4.6})
        self.assertIn("5 秒内捏了 3 下", self.channel_calls[0]["text"])

    def test_non_numeric_count_400(self) -> None:
        for bad in ({"count": "7"}, {"count": True}, {"duration_s": [1]}):
            h = self.pet(body=bad)
            self.assertEqual(h.responses[0][0], 400, bad)
        self.assertEqual(self.channel_calls, [])

    def test_test_text_mentions_count(self) -> None:
        self.pet(body={"count": 4, "duration_s": 3, "test": True})
        text = self.channel_calls[0]["text"]
        self.assertTrue(text.startswith("[桌宠·螃蟹被捏了·测试] "))
        self.assertIn("3 秒内捏了 4 下", text)
        self.assertEqual(self.bubbles, [])


class PetTapRateLimitTest(PetTapBase):
    def test_no_20s_dedupe(self) -> None:
        h1 = self.pet()
        h2 = self.pet()
        self.assertEqual(h1.responses, [(200, {"ok": True, "deduped": False})])
        self.assertEqual(h2.responses, [(200, {"ok": True, "deduped": False})])
        self.assertEqual(len(self.channel_calls), 2)
        self.assertEqual(len(self.bubbles), 2)

    def test_seventh_settlement_in_a_minute_is_dropped(self) -> None:
        for _ in range(6):
            self.assertFalse(self.pet().responses[0][1]["deduped"])
        h7 = self.pet(body={"count": 3})
        self.assertEqual(h7.responses, [(200, {"ok": True, "deduped": True})])
        self.assertEqual(len(self.channel_calls), 6)
        self.assertEqual(len(self.bubbles), 6)

    def test_limiter_window_math(self) -> None:
        lim = push._PetTapRateLimit(limit=6, window_seconds=60)
        for i in range(6):
            self.assertTrue(lim.try_acquire(now=100.0 + i))
        self.assertFalse(lim.try_acquire(now=130.0))
        self.assertFalse(lim.try_acquire(now=159.9))
        self.assertTrue(lim.try_acquire(now=160.0))  # first hit (t=100) aged out
        self.assertFalse(lim.try_acquire(now=160.5))

    def test_default_limit_is_6_per_minute(self) -> None:
        self.assertEqual((push._PET_TAP_LIMIT.limit, push._PET_TAP_LIMIT.window), (6, 60.0))

    def test_independent_of_bedside_and_door(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        self.tap(f"?k={DOOR}")
        self.assertFalse(self.pet().responses[0][1]["deduped"])
        self.tap(f"?k={BEDSIDE}")  # bedside still deduped by its own 20s bucket
        self.assertEqual(len(self.channel_calls), 3)
        for _ in range(5):
            self.pet()
        self.assertTrue(self.pet().responses[0][1]["deduped"])
        push._NFC_TAP_GATES.get("bedside")._last -= 21
        self.tap(f"?k={BEDSIDE}")  # pet limiter full does not block NFC
        self.assertEqual(len(self.channel_calls), 3 + 5 + 1)

    def test_pet_tap_does_not_consume_bedside_window(self) -> None:
        self.pet()
        self.tap(f"?k={BEDSIDE}")
        self.assertEqual(len(self.channel_calls), 2)

    def test_nfc_tags_file_cannot_claim_pet_tag(self) -> None:
        _write_private(self.tags_file, json.dumps({"tags": {
            "bedside": {"key": BEDSIDE, "action": "summon"},
            "pet": {"key": "pet-nfc-key-0123456789abcdef", "action": "summon"},
        }}))
        self.assertNotIn("pet", PushHandler._nfc_tags())
        self.assertIn("bedside", PushHandler._nfc_tags())


class PetTapBubbleAndTestFlagTest(PetTapBase):
    def test_bubble_shape(self) -> None:
        self.pet()
        contact, rec = self.bubbles[0]
        self.assertEqual(contact, "xiaoke")
        self.assertEqual(rec["role"], "assistant")
        self.assertEqual(rec["text"], "方小南捏了捏🤏屏幕上的🦀")
        self.assertEqual(rec["source"], "pet-tap")
        self.assertIs(rec["metadata"]["mihome_event"], True)
        self.assertEqual(rec["metadata"]["items"][0]["human"], rec["text"])
        self.assertEqual(rec["metadata"]["device_event"]["kind"], "pet_tap")

    def test_query_test_flag(self) -> None:
        h = self.pet(query="?test=1")
        self.assertEqual(h.responses[0], (200, {"ok": True, "deduped": False}))
        self.assertEqual(self.bubbles, [])
        call = self.channel_calls[0]
        self.assertIs(call["metadata"]["test"], True)
        self.assertTrue(call["text"].startswith("[桌宠·螃蟹被捏了·测试] "))
        self.assertIn("不用回她", call["text"])

    def test_body_test_flag(self) -> None:
        self.pet(body={"test": True})
        self.assertEqual(self.bubbles, [])
        self.assertIs(self.channel_calls[0]["metadata"]["test"], True)


class PetTapBodyTest(PetTapBase):
    def test_non_object_json_400(self) -> None:
        h = self.pet(body=[1, 2])
        self.assertEqual(h.responses[0][0], 400)
        self.assertEqual(self.channel_calls, [])

    def test_garbage_400(self) -> None:
        h = self.pet(body=b"{nope")
        self.assertEqual(h.responses[0][0], 400)
        self.assertEqual(self.channel_calls, [])

    def test_too_large_413(self) -> None:
        h = self.pet(body=b"{" + b" " * 5000 + b"}")
        self.assertEqual(h.responses[0][0], 413)
        self.assertEqual(self.channel_calls, [])


class PetTapHammerTest(PetTapBase):
    def test_hammer_text_and_bubble(self) -> None:
        self.pet(body={"count": 15, "duration_s": 12, "hammer": True})
        call = self.channel_calls[0]
        self.assertRegex(
            call["text"],
            r"^\[桌宠·螃蟹被捏了\] \d{2}:\d{2} 方小南 12 秒内捏了 15 下，后面抡起爆锤把螃蟹砸晕了"
            r"（她正在用手机，用 companion reply contact_id=xiaoke 跟她打招呼）$",
        )
        self.assertIs(call["metadata"]["hammer"], True)
        self.assertEqual(self.bubbles[0][1]["text"], "方小南抡起爆锤🔨砸晕了屏幕上的🦀 ×15")

    def test_hammer_must_be_true_literal(self) -> None:
        self.pet(body={"count": 9, "duration_s": 4, "hammer": "yes"})
        self.assertNotIn("爆锤", self.channel_calls[0]["text"])
        self.assertIs(self.channel_calls[0]["metadata"]["hammer"], False)
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏屏幕上的🦀 ×9")

    def test_hammer_single_count(self) -> None:
        self.pet(body={"count": 1, "hammer": True})
        self.assertIn("方小南抡起爆锤把手机屏幕上的螃蟹砸晕了", self.channel_calls[0]["text"])
        self.assertEqual(self.bubbles[0][1]["text"], "方小南抡起爆锤🔨砸晕了屏幕上的🦀")


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class PetScreenshotTest(PetTapBase):
    def setUp(self) -> None:
        super().setUp()
        push._PET_SHOT_LIMIT.reset()
        self.addCleanup(push._PET_SHOT_LIMIT.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.attach_dir = push.Path(tmp.name)

    def shot(self, body=JPEG, **kw) -> PushHandler:
        return self.pet(body=body, path="/pet/screenshot", **kw)

    def test_requires_native_auth(self) -> None:
        for token in (None, "bad", BEDSIDE):
            h = self.shot(token=token)
            self.assertEqual(h.responses, [(401, {"ok": False, "error": "unauthorized"})])
        self.assertEqual(self.channel_calls, [])
        self.assertEqual(self.bubbles, [])
        self.assertEqual(list(self.attach_dir.iterdir()), [])

    def test_saves_appends_and_notifies(self) -> None:
        h = self.shot()
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertRegex(payload["url"], r"^/attachments/[0-9a-f]{32}\.jpg$")
        saved = self.attach_dir / payload["url"].rsplit("/", 1)[1]
        self.assertEqual(saved.read_bytes(), JPEG)
        contact, rec = self.bubbles[0]
        self.assertEqual(contact, "xiaoke")
        self.assertEqual(rec["role"], "user")
        self.assertEqual(rec["text"], "双击螃蟹截屏")
        self.assertEqual(rec["source"], "pet-screenshot")
        self.assertEqual((rec["attachment_url"], rec["attachment_type"]), (payload["url"], "image"))
        call = self.channel_calls[0]
        self.assertRegex(
            call["text"],
            r"^\[桌宠·截屏\] \d{2}:\d{2} 方小南双击螃蟹，发来一张当前屏幕截图（图片在 /.+\.jpg，",
        )
        self.assertIn(str(saved.resolve()), call["text"])
        self.assertIn("companion reply contact_id=xiaoke", call["text"])
        self.assertEqual(call["metadata"]["source"], "pet-screenshot")
        self.assertEqual(call["metadata"]["image_path"], str(saved.resolve()))

    def test_test_flag_no_chat_record(self) -> None:
        h = self.shot(query="?test=1")
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(self.bubbles, [])
        self.assertTrue(self.channel_calls[0]["text"].startswith("[桌宠·截屏·测试] "))
        self.assertIs(self.channel_calls[0]["metadata"]["test"], True)

    def test_png_ok_garbage_415_empty_400(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        self.assertTrue(self.shot(body=png).responses[0][1]["url"].endswith(".png"))
        self.assertEqual(self.shot(body=b"not an image").responses[0][0], 415)
        self.assertEqual(self.shot(body=b"").responses[0][0], 400)
        self.assertEqual(len(self.channel_calls), 1)

    def test_rate_limited_after_6_per_minute(self) -> None:
        for _ in range(6):
            self.assertEqual(self.shot().responses[0][0], 200)
        self.assertEqual(self.shot().responses[0], (429, {"ok": False, "error": "rate_limited"}))
        self.assertEqual(len(self.channel_calls), 6)

    def test_screenshot_limit_independent_of_tap_limit(self) -> None:
        for _ in range(6):
            self.pet()
        self.assertTrue(self.pet().responses[0][1]["deduped"])
        self.assertEqual(self.shot().responses[0][0], 200)


if __name__ == "__main__":
    unittest.main()
