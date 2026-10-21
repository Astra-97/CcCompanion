"""NFC 螃蟹挂件 /nfc/tap 测试 (2026-10-21).

覆盖:
  1. 错/缺 k、密钥文件缺失或权限不对 → 404 且不通知
  2. 20s 去重窗口：窗口内二次碰卡只回页面不通知，过窗再通知；f=1 回落页不通知
  3. 通知载荷：channel 消息 source=nfc-tap、contact_id=xiaoke、文本格式、test 标记；
     channel 失败回落 tmux；不写聊天历史
  4. 页面：200 HTML，含 intent URL（包名/组件/小克 extra/fallback），无外部资源

全部直接调 handler，不碰线上 8291 服务。
"""
import io
import os
import re
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import push
from push import PushHandler

SECRET = "nfc-test-secret-0123456789abcdef"


class _Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else default


class NfcTapBase(unittest.TestCase):
    def setUp(self) -> None:
        push._NFC_TAP_GATE.reset()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secret_file = Path(tmp.name) / "nfc_tap_secret"
        self.secret_file.write_text(SECRET + "\n", encoding="utf-8")
        os.chmod(self.secret_file, 0o600)
        old = os.environ.get(PushHandler._NFC_TAP_SECRET_FILE_ENV)
        os.environ[PushHandler._NFC_TAP_SECRET_FILE_ENV] = str(self.secret_file)
        self.addCleanup(self._restore_env, old)
        old_async = PushHandler._NFC_TAP_ASYNC
        PushHandler._NFC_TAP_ASYNC = False
        self.addCleanup(setattr, PushHandler, "_NFC_TAP_ASYNC", old_async)
        self.channel_calls = []
        self.tmux_calls = []
        self.channel_ok = True
        orig_post, orig_tmux = push._channel_transport_post, push._inject_to_tmux_session

        def fake_post(state, **kw):
            self.channel_calls.append(kw)
            return (True, "") if self.channel_ok else (False, "down")

        def fake_tmux(state, session, text):
            self.tmux_calls.append((session, text))
            return True, ""

        push._channel_transport_post = fake_post
        push._inject_to_tmux_session = fake_tmux
        self.addCleanup(setattr, push, "_channel_transport_post", orig_post)
        self.addCleanup(setattr, push, "_inject_to_tmux_session", orig_tmux)

    @staticmethod
    def _restore_env(old) -> None:
        if old is None:
            os.environ.pop(PushHandler._NFC_TAP_SECRET_FILE_ENV, None)
        else:
            os.environ[PushHandler._NFC_TAP_SECRET_FILE_ENV] = old

    def tap(self, query: str) -> PushHandler:
        h = object.__new__(PushHandler)
        h.responses = []
        h.status = None
        h.sent_headers = {}
        h.wfile = io.BytesIO()
        h.state = types.SimpleNamespace(
            allowed_ips=["10.0.0.1"],
            channel_transport_enabled=True,
            channel_transport_contacts=["xiaoke"],
            channel_transport_fallback_to_tmux=True,
            active_session="cctg",
            default_session="cctg",
            contact_chats={},
        )
        h.headers = _Headers({"Host": "companion-vps2.xiaonancaleb.xyz", "User-Agent": "test"})
        h.path = "/nfc/tap" + query
        h._send_json = lambda status, payload, **_kw: h.responses.append((status, payload))
        h._trusted_client_ip = lambda: "1.2.3.4"
        h.send_response = lambda code: setattr(h, "status", code)
        h.send_header = lambda k, v: h.sent_headers.__setitem__(k, v)
        h.end_headers = lambda: None
        h.do_GET()
        return h

    def body(self, h) -> str:
        return h.wfile.getvalue().decode("utf-8")


class NfcTapAuthTest(NfcTapBase):
    def assert_404_silent(self, h) -> None:
        self.assertEqual(h.responses, [(404, {"error": "not found"})])
        self.assertIsNone(h.status)
        self.assertEqual(self.channel_calls, [])
        self.assertEqual(self.tmux_calls, [])

    def test_missing_key_404(self) -> None:
        self.assert_404_silent(self.tap(""))

    def test_wrong_key_404(self) -> None:
        self.assert_404_silent(self.tap("?k=nope-nope-nope-nope-nope"))

    def test_wrong_key_does_not_consume_dedupe_window(self) -> None:
        self.tap("?k=wrong-wrong-wrong-wrong")
        self.tap(f"?k={SECRET}")
        self.assertEqual(len(self.channel_calls), 1)

    def test_secret_file_missing_fails_closed(self) -> None:
        self.secret_file.unlink()
        self.assert_404_silent(self.tap(f"?k={SECRET}"))

    def test_secret_file_bad_mode_fails_closed(self) -> None:
        os.chmod(self.secret_file, 0o644)
        self.assert_404_silent(self.tap(f"?k={SECRET}"))

    def test_route_bypasses_ip_allowlist(self) -> None:
        h = self.tap(f"?k={SECRET}")
        self.assertEqual(h.status, 200)


class NfcTapDedupeTest(NfcTapBase):
    def test_second_tap_within_window_not_notified(self) -> None:
        h1 = self.tap(f"?k={SECRET}")
        h2 = self.tap(f"?k={SECRET}")
        self.assertEqual((h1.status, h2.status), (200, 200))
        self.assertEqual(len(self.channel_calls), 1)
        self.assertIn("我已经知道啦，在呢", self.body(h2))

    def test_gate_window_math(self) -> None:
        gate = push._NfcTapGate(window_seconds=20)
        self.assertTrue(gate.try_acquire(now=100.0))
        self.assertFalse(gate.try_acquire(now=105.0))
        self.assertFalse(gate.try_acquire(now=119.9))
        self.assertTrue(gate.try_acquire(now=120.0))
        self.assertFalse(gate.try_acquire(now=121.0))

    def test_default_window_is_20s(self) -> None:
        self.assertEqual(push._NFC_TAP_GATE.window, 20.0)

    def test_after_window_notifies_again(self) -> None:
        self.tap(f"?k={SECRET}")
        push._NFC_TAP_GATE._last -= 21
        self.tap(f"?k={SECRET}")
        self.assertEqual(len(self.channel_calls), 2)

    def test_fallback_page_never_notifies_or_autolaunches(self) -> None:
        h = self.tap(f"?k={SECRET}&f=1")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.channel_calls, [])
        self.assertNotIn("location.href", self.body(h))
        # and it does not burn the window for a real tap
        self.tap(f"?k={SECRET}")
        self.assertEqual(len(self.channel_calls), 1)


class NfcTapPayloadTest(NfcTapBase):
    def test_channel_payload(self) -> None:
        self.tap(f"?k={SECRET}")
        call = self.channel_calls[0]
        self.assertEqual(call["contact_id"], "xiaoke")
        self.assertTrue(call["message_id"].startswith("nfc:"))
        self.assertEqual(call["metadata"]["source"], "nfc-tap")
        self.assertIs(call["metadata"]["test"], False)
        self.assertRegex(call["text"], r"^\[NFC·螃蟹被碰了\] \d{2}:\d{2} 方小南碰了一下螃蟹挂件")
        self.assertEqual(self.tmux_calls, [])

    def test_text_uses_beijing_time(self) -> None:
        utc = datetime(2026, 10, 21, 13, 50, tzinfo=ZoneInfo("UTC"))
        text = PushHandler._nfc_tap_notification_text(utc)
        self.assertTrue(text.startswith("[NFC·螃蟹被碰了] 21:50 方小南碰了一下螃蟹挂件"))

    def test_test_flag_marks_text(self) -> None:
        self.tap(f"?k={SECRET}&test=1")
        call = self.channel_calls[0]
        self.assertIn("测试", call["text"])
        self.assertIs(call["metadata"]["test"], True)

    def test_channel_failure_falls_back_to_tmux(self) -> None:
        self.channel_ok = False
        self.tap(f"?k={SECRET}")
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertEqual(self.tmux_calls[0][0], "cctg")
        self.assertIn("[NFC·螃蟹被碰了]", self.tmux_calls[0][1])


class NfcTapPageTest(NfcTapBase):
    def test_page_has_intent_and_no_external_assets(self) -> None:
        h = self.tap(f"?k={SECRET}")
        html = self.body(h)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.sent_headers["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'none'", h.sent_headers["Content-Security-Policy"])
        self.assertIn("小克来了", html)
        self.assertIn("打开 CC Companion", html)
        self.assertIn("location.href", html)
        self.assertIn("package=com.fangxiaonan.cccompanion", html)
        self.assertIn("component=com.fangxiaonan.cccompanion/.MainActivity", html)
        self.assertIn("S.com.fangxiaonan.cccompanion.extra.OPEN_CONTACT_ID=xiaoke", html)
        self.assertIn("S.browser_fallback_url=https%3A%2F%2Fcompanion-vps2", html)
        self.assertIsNone(re.search(r"(src|href)=\"https?:", html))


if __name__ == "__main__":
    unittest.main()
