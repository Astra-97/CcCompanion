import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from push import PushHandler, KIMI_FORGE_DEFER_MARKER

import _kimi_chat_test as kimi_chat_test
from _kimi_forge_test import _make_handler, FakeWeb
from _kimi_chat_test import FakeChat, FakeWebChat

_make_routing_handler = kimi_chat_test.KimiWebChatRoutingTest.make_handler
_wait_idle = kimi_chat_test.KimiWebChatRoutingTest.wait_idle


def _warn_handler(tmp: str, web: "FakeWeb", *, forge_threshold=0.8, warn=0.75, usage=0.5) -> PushHandler:
    handler = _make_handler(Path(tmp), web)
    handler.state.kimi_auto_forge_context_threshold = forge_threshold
    handler.state.kimi_auto_forge_warn_threshold = warn
    handler.state.kimi_forge_warn_state = {}
    handler.state.kimi_forge_deferred = False
    handler._kimi_context_usage = lambda _session: usage
    return handler


class ForgeWarnContextTest(unittest.TestCase):
    """需求一：75% 预警块的一次性注入语义。"""

    def test_below_warn_injects_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = _warn_handler(tmp, FakeWeb(), usage=0.5)
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))

    def test_in_band_injects_once_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = _warn_handler(tmp, FakeWeb(), usage=0.77)
            first = handler._kimi_forge_warn_context("session_old")
            self.assertIn("上下文预警", first)
            self.assertIn(KIMI_FORGE_DEFER_MARKER, first)
            self.assertIn("80%", first)
            # 同一会话同一档位只注入一次。
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))

    def test_usage_fall_below_warn_resets_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _warn_handler(tmp, web, usage=0.77)
            self.assertTrue(handler._kimi_forge_warn_context("session_old"))
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))
            # 回落（如自动 compaction）后再次越线：重新预警。
            handler._kimi_context_usage = lambda _session: 0.5
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))
            handler._kimi_context_usage = lambda _session: 0.78
            self.assertIn("上下文预警", handler._kimi_forge_warn_context("session_old"))

    def test_at_forge_threshold_warn_stays_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = _warn_handler(tmp, FakeWeb(), usage=0.85)
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))

    def test_forge_disabled_disables_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = _warn_handler(tmp, FakeWeb(), forge_threshold=0.0, usage=0.9)
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))

    def test_warn_zero_disabled_and_warn_above_forge_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = _warn_handler(tmp, FakeWeb(), warn=0.0, usage=0.77)
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))
            handler = _warn_handler(tmp, FakeWeb(), warn=0.9, usage=0.85)
            self.assertEqual("", handler._kimi_forge_warn_context("session_old"))

    def test_new_session_gets_fresh_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = _warn_handler(tmp, FakeWeb(), usage=0.77)
            self.assertTrue(handler._kimi_forge_warn_context("session_old"))
            # forge 换会话后（session 指针不同）预警重新可用。
            self.assertIn("上下文预警", handler._kimi_forge_warn_context("session_new"))


class ForgeDeferMarkerTest(unittest.TestCase):
    """需求一：[[CCC_KIMI_FORGE_DEFER]] 标记的剥离语法。"""

    def test_standalone_trailing_marker_is_stripped(self):
        visible, found = PushHandler._kimi_extract_forge_defer_marker(
            f"活还没干完，再拖一拖\n{KIMI_FORGE_DEFER_MARKER}\n"
        )
        self.assertTrue(found)
        self.assertEqual("活还没干完，再拖一拖", visible)

    def test_marker_not_on_last_line_is_still_stripped(self):
        visible, found = PushHandler._kimi_extract_forge_defer_marker(
            f"{KIMI_FORGE_DEFER_MARKER}\n补充一句说明"
        )
        self.assertTrue(found)
        self.assertEqual("补充一句说明", visible)

    def test_inline_or_repeated_marker_stays_plain_text(self):
        inline = f"详见 {KIMI_FORGE_DEFER_MARKER} 的说明"
        self.assertEqual((inline, False), PushHandler._kimi_extract_forge_defer_marker(inline))
        doubled = f"{KIMI_FORGE_DEFER_MARKER}\n{KIMI_FORGE_DEFER_MARKER}"
        self.assertEqual((doubled, False), PushHandler._kimi_extract_forge_defer_marker(doubled))

    def test_empty_and_markerless_text_untouched(self):
        self.assertEqual(("", False), PushHandler._kimi_extract_forge_defer_marker(""))
        self.assertEqual(("你好", False), PushHandler._kimi_extract_forge_defer_marker("你好"))


class ForgeDeferAutoForgeTest(unittest.TestCase):
    """需求一：延期标志对自动 forge 的暂缓 / 硬上限 / 清除语义。"""

    TASKS = {"finished": [], "pending": []}

    def test_deferred_skips_forge_below_hard_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _warn_handler(tmp, web, usage=0.85)
            handler.state.kimi_forge_deferred = True
            with patch("push._scan_kimi_session_tasks") as scan:
                session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            scan.assert_not_called()
            self.assertEqual([], web.created)
            self.assertTrue(handler.state.kimi_forge_deferred)

    def test_hard_cap_forges_despite_deferral_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _warn_handler(tmp, web, usage=0.93)
            handler.state.kimi_forge_deferred = True
            handler.state.kimi_forge_warn_state = {"session_id": "session_old", "injected": True}
            with patch("push._scan_kimi_session_tasks", return_value=dict(self.TASKS)):
                session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_new", True), (session_id, forged))
            chat = handler.state.contact_chats["kimi"]
            notice = chat.rows[0]["text"]
            self.assertIn("硬上限", notice)
            self.assertIn("延期", notice)
            # forge 成功后延期与预警状态都清除。
            self.assertFalse(handler.state.kimi_forge_deferred)
            self.assertEqual({}, handler.state.kimi_forge_warn_state)

    def test_usage_fall_below_threshold_clears_deferral(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _warn_handler(tmp, web, usage=0.5)
            handler.state.kimi_forge_deferred = True
            session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertFalse(handler.state.kimi_forge_deferred)

    def test_normal_forge_notice_has_no_hardcap_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _warn_handler(tmp, web, usage=0.85)
            with patch("push._scan_kimi_session_tasks", return_value=dict(self.TASKS)):
                session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_new", True), (session_id, forged))
            chat = handler.state.contact_chats["kimi"]
            self.assertNotIn("硬上限", chat.rows[0]["text"])

    def test_manual_forge_clears_deferral(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _warn_handler(tmp, web, usage=0.5)
            handler.state.kimi_forge_deferred = True
            with patch("push._scan_kimi_session_tasks", return_value=dict(self.TASKS)):
                handler._handle_kimi_forge({})
            self.assertEqual(200, handler.responses[-1][0])
            self.assertFalse(handler.state.kimi_forge_deferred)


class ForgeWarnDeferEndToEndTest(unittest.TestCase):
    """需求一：私聊投递链路里的预警注入与延期标记剥离。"""

    def _warn_ready_handler(self, web, *, usage):
        handler, chat, web = _make_routing_handler(None, web=web)
        handler.state.kimi_auto_forge_context_threshold = 0.8
        handler.state.kimi_auto_forge_warn_threshold = 0.75
        handler.state.kimi_forge_warn_state = {}
        handler.state.kimi_forge_deferred = False
        handler.state.kimi_forge_seed_retain_messages = 0
        handler.state.kimi_sessions_root = None
        handler.state.token_store_path = str(Path(tempfile.mkdtemp()) / "tokens" / "device_tokens.json")
        handler._kimi_context_usage = lambda _session: usage
        handler._send_chat_notification = lambda *_args: None
        return handler, chat, web

    def test_prompt_carries_warn_block_once_in_band(self):
        handler, _chat, web = self._warn_ready_handler(FakeWebChat(), usage=0.77)
        handler._handle_kimi_chat_send({"text": "第一条"}, "kimi")
        _wait_idle(handler)
        handler._handle_kimi_chat_send({"text": "第二条"}, "kimi")
        _wait_idle(handler)
        prompts = [row[2] for row in web.calls if row[0] == "submit"]
        self.assertEqual(2, len(prompts))
        self.assertIn("上下文预警", prompts[0])
        self.assertIn(KIMI_FORGE_DEFER_MARKER, prompts[0])
        self.assertNotIn("上下文预警", prompts[1])

    def test_defer_marker_stripped_and_defers_autoforge(self):
        web = FakeWebChat(text=f"还在干活，别换会话\n{KIMI_FORGE_DEFER_MARKER}")
        handler, chat, _web = self._warn_ready_handler(web, usage=0.77)
        handler._handle_kimi_chat_send({"text": "进度如何"}, "kimi")
        _wait_idle(handler)
        final = chat.records[-1]
        self.assertEqual("还在干活，别换会话", final["text"])
        self.assertNotIn("CCC_KIMI_FORGE_DEFER", final["text"])
        self.assertTrue(handler.state.kimi_forge_deferred)
        # 下一轮 usage 0.85（已超 0.8 阈值、低于 0.92 硬上限）：自动 forge 被延期跳过。
        handler._kimi_context_usage = lambda _session: 0.85
        session_id, forged = handler._maybe_forge_kimi_session("web-session-1")
        self.assertEqual(("web-session-1", False), (session_id, forged))


if __name__ == "__main__":
    unittest.main()
