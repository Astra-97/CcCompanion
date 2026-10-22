"""螃蟹桌宠 POST /pet/tap 测试 (2026-10-22).

覆盖:
  1. 鉴权: 缺/错 X-Auth-Token → 401 且不通知; strict_auth=false 也不放行 (fail-closed)
  2. 路由: 第三个 tag "pet" (summon), 不动设备
  3. 去重: pet 自己的 20s 桶, 与 bedside/door 互不吞; 返回 {ok, deduped}
  4. 通知文本 / 载荷: [桌宠·螃蟹被捏了]; test=1 (query 或 body) 带测试标记
  5. 私聊胶囊: 「方小南捏了捏🤏屏幕上的🦀」; test 不冒, 去重不冒
  6. body 校验: 空 body 可; 非 JSON/非对象 400; 过大 413
  7. nfc_tags.json 里叫 "pet" 的 tag 被忽略 (桶不混用)

全部 mock, 不碰线上 8291 服务。
"""
import io
import json
import types
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import push
from push import PushHandler
from _nfc_tap_test import BEDSIDE, DOOR, NfcTapBase, _Headers, _write_private

SECRET = "pet-shared-secret-0123456789"


class PetTapBase(NfcTapBase):
    def pet(self, *, token=SECRET, body=b"", query="", strict=True) -> PushHandler:
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
        )
        h._chat_for_contact = lambda contact: types.SimpleNamespace(
            append=lambda **rec: self.bubbles.append((contact, rec)) or rec
        )
        headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        if token is not None:
            headers["X-Auth-Token"] = token
        h.headers = _Headers(headers)
        h.path = "/pet/tap" + query
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

    def test_unauthorized_does_not_consume_window(self) -> None:
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


class PetTapDedupeTest(PetTapBase):
    def test_second_tap_within_window_is_deduped(self) -> None:
        self.pet()
        h2 = self.pet()
        self.assertEqual(h2.responses, [(200, {"ok": True, "deduped": True})])
        self.assertEqual(len(self.channel_calls), 1)
        self.assertEqual(len(self.bubbles), 1)

    def test_after_window_notifies_again(self) -> None:
        self.pet()
        push._NFC_TAP_GATES.get("pet")._last -= 21
        h2 = self.pet()
        self.assertEqual(h2.responses[0][1]["deduped"], False)
        self.assertEqual(len(self.channel_calls), 2)

    def test_independent_of_bedside_and_door(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        self.tap(f"?k={DOOR}")
        h = self.pet()
        self.assertEqual(h.responses[0][1]["deduped"], False)
        self.tap(f"?k={BEDSIDE}")  # bedside still deduped by its own bucket
        self.assertEqual(len(self.channel_calls), 3)
        push._NFC_TAP_GATES.get("pet")._last -= 21
        self.tap(f"?k={DOOR}")  # door still deduped; pet's reset does not leak
        self.assertEqual(len(self.channel_calls), 3)

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


if __name__ == "__main__":
    unittest.main()
