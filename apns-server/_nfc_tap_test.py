"""NFC 螃蟹挂件 /nfc/tap 测试 (2026-10-21, v2 多 tag + 回家模式 + 胶囊).

覆盖:
  1. 鉴权: 错/缺 k、密钥文件缺失或权限不对 → 404 且不通知; 旧单 key 文件回退为 bedside
  2. 多 key 路由: tags 文件里每把 key 对应各自档案, 床头=summon, 门口=home
  3. 20s 去重窗口(按 tag): 窗口内二次碰卡只回页面, 不通知/不动设备/不冒泡; f=1 回落页同样
  4. 回家模式: 天黑执行设备、白天不动设备并注明; test=1 只 dry-run; 设备失败文案
  5. 通知载荷: channel source=nfc-tap, 文本格式; channel 失败回落 tmux
  6. 私聊胶囊: role=assistant + mihome_event, 文案; test 不冒泡, 去重不冒泡
  7. 页面: 200 HTML, intent URL, 无外部资源, 门口版文案

设备调用全部 mock, 绝不真调米家; 不碰线上 8291 服务。
"""
import io
import json
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

BEDSIDE = "nfc-bedside-secret-0123456789abcdef"
DOOR = "nfc-door-secret-0123456789abcdefgh"

OK_RESULT = {
    "dry_run": False,
    "curtain": {"ok": True, "detail": "close sent"},
    "light": {"ok": True, "already_on": False, "detail": "on"},
}


class _Headers(dict):
    def get_all(self, name, default=None):
        value = self.get(name)
        return [value] if value is not None else default


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


class NfcTapBase(unittest.TestCase):
    def setUp(self) -> None:
        push._NFC_TAP_GATES.reset()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.legacy_file = self.dir / "nfc_tap_secret"
        self.tags_file = self.dir / "nfc_tags.json"
        _write_private(self.tags_file, json.dumps({"tags": {
            "bedside": {"key": BEDSIDE, "name": "床头螃蟹", "location": "床头", "action": "summon"},
            "door": {"key": DOOR, "name": "门口螃蟹", "location": "门口", "action": "home"},
        }}))
        for env, val in (
            (PushHandler._NFC_TAP_SECRET_FILE_ENV, str(self.legacy_file)),
            (PushHandler._NFC_TAGS_FILE_ENV, str(self.tags_file)),
        ):
            old = os.environ.get(env)
            os.environ[env] = val
            self.addCleanup(self._restore_env, env, old)
        self._patch_cls("_NFC_TAP_ASYNC", False)
        self.night = True
        self.home_calls = []
        self.home_result = dict(OK_RESULT)
        self._patch_cls("_nfc_is_night", staticmethod(lambda now=None: self.night))
        # 出门模式 (2026-10-23) 依赖真实时钟/节假日表；默认关掉，专门的测试见 _nfc_leave_test.py。
        self.leave = False
        self._patch_cls("_nfc_is_leave_time", classmethod(lambda cls, now=None: self.leave))

        def fake_home(*, dry_run):
            self.home_calls.append(dry_run)
            return {**self.home_result, "dry_run": dry_run}

        self._patch_cls("_nfc_run_home_devices", staticmethod(fake_home))
        self.channel_calls = []
        self.tmux_calls = []
        self.bubbles = []
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

    def _patch_cls(self, name, value) -> None:
        old = PushHandler.__dict__[name]
        setattr(PushHandler, name, value)
        self.addCleanup(setattr, PushHandler, name, old)

    @staticmethod
    def _restore_env(env, old) -> None:
        if old is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = old

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
        h._chat_for_contact = lambda contact: types.SimpleNamespace(
            append=lambda **rec: self.bubbles.append((contact, rec)) or rec
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
        self.assertEqual(self.bubbles, [])
        self.assertEqual(self.home_calls, [])

    def test_missing_key_404(self) -> None:
        self.assert_404_silent(self.tap(""))

    def test_wrong_key_404(self) -> None:
        self.assert_404_silent(self.tap("?k=nope-nope-nope-nope-nope"))

    def test_wrong_key_does_not_consume_dedupe_window(self) -> None:
        self.tap("?k=wrong-wrong-wrong-wrong")
        self.tap(f"?k={BEDSIDE}")
        self.assertEqual(len(self.channel_calls), 1)

    def test_tags_file_bad_mode_fails_closed(self) -> None:
        os.chmod(self.tags_file, 0o644)
        self.assert_404_silent(self.tap(f"?k={BEDSIDE}"))

    def test_legacy_secret_used_only_without_tags_file(self) -> None:
        legacy = "legacy-secret-0123456789abcdef"
        _write_private(self.legacy_file, legacy + "\n")
        self.assert_404_silent(self.tap(f"?k={legacy}"))  # tags file wins
        self.tags_file.unlink()
        h = self.tap(f"?k={legacy}")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.channel_calls[0]["metadata"]["nfc_tag"], "bedside")

    def test_no_files_fails_closed(self) -> None:
        self.tags_file.unlink()
        self.assert_404_silent(self.tap(f"?k={BEDSIDE}"))

    def test_route_bypasses_ip_allowlist(self) -> None:
        self.assertEqual(self.tap(f"?k={BEDSIDE}").status, 200)


class NfcTapRoutingTest(NfcTapBase):
    def test_bedside_is_summon_without_devices(self) -> None:
        h = self.tap(f"?k={BEDSIDE}")
        meta = self.channel_calls[0]["metadata"]
        self.assertEqual((meta["nfc_tag"], meta["nfc_action"]), ("bedside", "summon"))
        self.assertEqual(self.home_calls, [])
        self.assertIn("小克来了", self.body(h))
        self.assertIn("我收到啦，马上来找你", self.body(h))

    def test_door_is_home_and_runs_devices_at_night(self) -> None:
        h = self.tap(f"?k={DOOR}")
        meta = self.channel_calls[0]["metadata"]
        self.assertEqual((meta["nfc_tag"], meta["nfc_action"]), ("door", "home"))
        self.assertEqual(self.home_calls, [False])
        self.assertIn("欢迎回家", self.body(h))
        self.assertIn("窗帘关好了，灯开了，我也来了", self.body(h))
        self.assertRegex(
            self.channel_calls[0]["text"],
            r"^\[NFC·门口螃蟹·回家\] \d{2}:\d{2} 方小南到家碰了门口螃蟹：窗帘已关、灯已开（60% 暖光）",
        )

    def test_gates_are_per_tag(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        self.tap(f"?k={DOOR}")
        self.assertEqual(len(self.channel_calls), 2)


class NfcTapHomeTest(NfcTapBase):
    def test_daytime_does_not_touch_devices(self) -> None:
        self.night = False
        h = self.tap(f"?k={DOOR}")
        self.assertEqual(self.home_calls, [])
        self.assertIn("白天，没动设备", self.channel_calls[0]["text"])
        self.assertIn("欢迎回家", self.body(h))
        self.assertIn(">我来了<", self.body(h))
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏门口的🦀")

    def test_test_flag_only_dry_runs(self) -> None:
        self.tap(f"?k={DOOR}&test=1")
        self.assertEqual(self.home_calls, [True])
        text = self.channel_calls[0]["text"]
        self.assertIn("[NFC·门口螃蟹·回家·测试]", text)
        self.assertIn("dry-run", text)

    def test_dedupe_does_not_repeat_devices(self) -> None:
        self.tap(f"?k={DOOR}")
        h2 = self.tap(f"?k={DOOR}")
        self.assertEqual(self.home_calls, [False])
        self.assertEqual(len(self.channel_calls), 1)
        self.assertEqual(len(self.bubbles), 1)
        self.assertIn("我已经知道啦，在呢", self.body(h2))

    def test_fallback_page_does_not_touch_devices(self) -> None:
        self.tap(f"?k={DOOR}&f=1")
        self.assertEqual(self.home_calls, [])
        self.assertEqual(self.channel_calls, [])

    def test_device_failures_are_named(self) -> None:
        self.home_result = {
            "curtain": {"ok": False, "detail": "code=[-704042011]"},
            "light": {"ok": False, "already_on": False, "detail": "超时"},
        }
        self.tap(f"?k={DOOR}")
        text = self.channel_calls[0]["text"]
        self.assertIn("窗帘关闭失败（code=[-704042011]）", text)
        self.assertIn("开灯失败（超时）", text)
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏门口的🦀")

    def test_partial_failure_and_light_already_on(self) -> None:
        self.home_result = {
            "curtain": {"ok": False, "detail": "离线"},
            "light": {"ok": True, "already_on": True, "detail": "already on"},
        }
        self.tap(f"?k={DOOR}")
        text = self.channel_calls[0]["text"]
        self.assertIn("窗帘关闭失败（离线）、灯本来就开着，没动", text)
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏门口的🦀 · 灯亮了")

    def test_dry_run_summary(self) -> None:
        out = PushHandler._nfc_home_summary({**OK_RESULT, "dry_run": True})
        self.assertEqual(out, "窗帘已关、灯已开（60% 暖光）（dry-run，没真动设备）")


class NfcHomeRunnerTest(unittest.TestCase):
    """Exercise the real subprocess runner with harmless stand-in commands."""

    def run_with(self, cmd):
        old = PushHandler._NFC_HOME_CMD
        PushHandler._NFC_HOME_CMD = cmd
        self.addCleanup(setattr, PushHandler, "_NFC_HOME_CMD", old)
        return PushHandler._nfc_run_home_devices(dry_run=True)

    def test_garbage_output_is_failure(self) -> None:
        out = self.run_with(("/bin/echo", "not json"))
        self.assertFalse(out["curtain"]["ok"])
        self.assertFalse(out["light"]["ok"])
        self.assertIn("脚本无结果", out["curtain"]["detail"])

    def test_json_output_is_parsed(self) -> None:
        line = json.dumps({"dry_run": True, "curtain": {"ok": True, "detail": "x"},
                           "light": {"ok": False, "already_on": False, "detail": "y"}})
        out = self.run_with(("python3", "-c", f"print({line!r})"))
        self.assertTrue(out["curtain"]["ok"])
        self.assertFalse(out["light"]["ok"])
        self.assertEqual(out["light"]["detail"], "y")

    def test_missing_binary_is_failure(self) -> None:
        out = self.run_with(("/nonexistent/python-xyz",))
        self.assertFalse(out["curtain"]["ok"])


class NfcTapBubbleTest(NfcTapBase):
    def test_bedside_bubble_shape(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        contact, rec = self.bubbles[0]
        self.assertEqual(contact, "xiaoke")
        self.assertEqual(rec["role"], "assistant")
        self.assertEqual(rec["text"], "方小南捏了捏🤏床头的🦀")
        self.assertIs(rec["metadata"]["mihome_event"], True)
        self.assertEqual(rec["metadata"]["severity"], "info")
        self.assertEqual(rec["metadata"]["items"][0]["human"], rec["text"])

    def test_door_night_bubble_has_suffix(self) -> None:
        self.tap(f"?k={DOOR}")
        self.assertEqual(self.bubbles[0][1]["text"], "方小南捏了捏🤏门口的🦀 · 窗帘关了，灯亮了")

    def test_test_flag_no_bubble(self) -> None:
        self.tap(f"?k={BEDSIDE}&test=1")
        self.tap(f"?k={DOOR}&test=1")
        self.assertEqual(self.bubbles, [])
        self.assertEqual(len(self.channel_calls), 2)

    def test_dedupe_no_bubble(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        self.tap(f"?k={BEDSIDE}")
        self.assertEqual(len(self.bubbles), 1)


class NfcTapDedupeTest(NfcTapBase):
    def test_gate_window_math(self) -> None:
        gate = push._NfcTapGate(window_seconds=20)
        self.assertTrue(gate.try_acquire(now=100.0))
        self.assertFalse(gate.try_acquire(now=105.0))
        self.assertFalse(gate.try_acquire(now=119.9))
        self.assertTrue(gate.try_acquire(now=120.0))
        self.assertFalse(gate.try_acquire(now=121.0))

    def test_default_window_is_20s(self) -> None:
        self.assertEqual(push._NFC_TAP_GATES.get("x").window, 20.0)

    def test_after_window_notifies_again(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        push._NFC_TAP_GATES.get("bedside")._last -= 21
        self.tap(f"?k={BEDSIDE}")
        self.assertEqual(len(self.channel_calls), 2)

    def test_fallback_page_never_notifies_or_autolaunches(self) -> None:
        h = self.tap(f"?k={BEDSIDE}&f=1")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.channel_calls, [])
        self.assertNotIn("location.href", self.body(h))
        self.tap(f"?k={BEDSIDE}")
        self.assertEqual(len(self.channel_calls), 1)


class NfcTapPayloadTest(NfcTapBase):
    def test_channel_payload(self) -> None:
        self.tap(f"?k={BEDSIDE}")
        call = self.channel_calls[0]
        self.assertEqual(call["contact_id"], "xiaoke")
        self.assertTrue(call["message_id"].startswith("nfc:bedside:"))
        self.assertEqual(call["metadata"]["source"], "nfc-tap")
        self.assertIs(call["metadata"]["test"], False)
        self.assertRegex(call["text"], r"^\[NFC·螃蟹被碰了\] \d{2}:\d{2} 方小南碰了一下螃蟹挂件")
        self.assertEqual(self.tmux_calls, [])

    def test_text_uses_beijing_time(self) -> None:
        utc = datetime(2026, 10, 21, 13, 50, tzinfo=ZoneInfo("UTC"))
        text = PushHandler._nfc_tap_notification_text(utc)
        self.assertTrue(text.startswith("[NFC·螃蟹被碰了] 21:50 方小南碰了一下螃蟹挂件"))

    def test_test_flag_marks_text(self) -> None:
        self.tap(f"?k={BEDSIDE}&test=1")
        call = self.channel_calls[0]
        self.assertIn("测试", call["text"])
        self.assertIs(call["metadata"]["test"], True)

    def test_channel_failure_falls_back_to_tmux(self) -> None:
        self.channel_ok = False
        self.tap(f"?k={BEDSIDE}")
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertEqual(self.tmux_calls[0][0], "cctg")
        self.assertIn("[NFC·螃蟹被碰了]", self.tmux_calls[0][1])


class NfcTapNightTest(unittest.TestCase):
    def test_night_window_uses_sun_times(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        orig = push._nfc_sun_minutes
        push._nfc_sun_minutes = lambda day: {"sunrise": 391, "sunset": 1047}
        self.addCleanup(setattr, push, "_nfc_sun_minutes", orig)
        is_night = PushHandler._nfc_is_night
        self.assertTrue(is_night(datetime(2026, 10, 21, 22, 10, tzinfo=tz)))
        self.assertTrue(is_night(datetime(2026, 10, 21, 17, 27, tzinfo=tz)))
        self.assertFalse(is_night(datetime(2026, 10, 21, 17, 26, tzinfo=tz)))
        self.assertTrue(is_night(datetime(2026, 10, 22, 6, 30, tzinfo=tz)))
        self.assertFalse(is_night(datetime(2026, 10, 22, 6, 31, tzinfo=tz)))

    def test_night_falls_back_to_18_06(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        orig = push._nfc_sun_minutes

        def boom(day):
            raise RuntimeError("no sun-bubble")

        push._nfc_sun_minutes = boom
        self.addCleanup(setattr, push, "_nfc_sun_minutes", orig)
        self.assertTrue(PushHandler._nfc_is_night(datetime(2026, 10, 21, 18, 0, tzinfo=tz)))
        self.assertFalse(PushHandler._nfc_is_night(datetime(2026, 10, 21, 17, 59, tzinfo=tz)))
        self.assertFalse(PushHandler._nfc_is_night(datetime(2026, 10, 22, 6, 0, tzinfo=tz)))


class NfcTapPageTest(NfcTapBase):
    def test_page_has_intent_and_no_external_assets(self) -> None:
        h = self.tap(f"?k={BEDSIDE}")
        html = self.body(h)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.sent_headers["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'none'", h.sent_headers["Content-Security-Policy"])
        self.assertIn("<title>小克来了</title>", html)
        self.assertIn("打开 CC Companion", html)
        self.assertIn("location.href", html)
        self.assertIn("package=com.fangxiaonan.cccompanion", html)
        self.assertIn("component=com.fangxiaonan.cccompanion/.MainActivity", html)
        self.assertIn("S.com.fangxiaonan.cccompanion.extra.OPEN_CONTACT_ID=xiaoke", html)
        self.assertIn("S.browser_fallback_url=https%3A%2F%2Fcompanion-vps2", html)
        self.assertIsNone(re.search(r"(src|href)=\"https?:", html))
        self.assertNotIn("__", html.replace("__SUB", ""))

    def test_door_fallback_keeps_door_key(self) -> None:
        html = self.body(self.tap(f"?k={DOOR}"))
        self.assertIn(f"k%3D{DOOR}%26f%3D1", html)
        self.assertIn("<title>欢迎回家</title>", html)


if __name__ == "__main__":
    unittest.main()
