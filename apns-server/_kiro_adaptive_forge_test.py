"""Window-adaptive Kiro forge thresholds + soft notice + [[KIRO_FORGE]] (2026-10-30).

hard = min(threshold% × window, kiro_auto_forge_max_tokens); soft = hard × 75/80.
No test spawns kiro-cli or reaches a model.
"""
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _kiro_acp_test as acp_tests  # module import: keep its TestCases out of this file
from _kiro_forge_test import FakeACP, _make_handler
from push import (
    KIRO_FORGE_MARKER,
    _parse_kiro_auto_forge_soft_ratio,
    _kiro_context_window_for,
    _kiro_forge_thresholds,
    _strip_kiro_forge_marker,
)

NO_TASKS = {"finished": [], "pending": []}


class ThresholdMathTest(unittest.TestCase):
    def test_200k_window_keeps_80_percent(self):
        hard, soft = _kiro_forge_thresholds(200_000)
        self.assertAlmostEqual(80.0, hard)
        self.assertAlmostEqual(75.0, soft)  # 150k tokens

    def test_1m_window_caps_at_300k(self):
        hard, soft = _kiro_forge_thresholds(1_000_000)
        self.assertAlmostEqual(30.0, hard)  # 300k tokens
        self.assertAlmostEqual(28.125, soft)  # 281.25k tokens
        self.assertAlmostEqual(281_250, soft / 100 * 1_000_000)

    def test_unknown_window_falls_back_to_configured_percent(self):
        self.assertEqual((80.0, 75.0), _kiro_forge_thresholds(None))
        self.assertEqual((80.0, 75.0), _kiro_forge_thresholds(0))

    def test_config_overrides(self):
        hard, soft = _kiro_forge_thresholds(200_000, threshold_percent=70.0, max_tokens=100_000)
        self.assertAlmostEqual(50.0, hard)
        self.assertAlmostEqual(50.0 * 75 / 80, soft)
        hard, soft = _kiro_forge_thresholds(
            1_000_000, threshold_percent=70.0, max_tokens=0, soft_ratio=0.5)
        self.assertEqual((70.0, 35.0), (hard, soft))
        hard, soft = _kiro_forge_thresholds(1_000_000, max_tokens=500_000)
        self.assertAlmostEqual(50.0, hard)
        # ratio 0 or 1 disables the soft notice.
        self.assertEqual((80.0, None), _kiro_forge_thresholds(200_000, soft_ratio=0))
        self.assertEqual((80.0, None), _kiro_forge_thresholds(200_000, soft_ratio=1))

    def test_soft_ratio_parse(self):
        self.assertAlmostEqual(0.9375, _parse_kiro_auto_forge_soft_ratio("75/80"))
        self.assertAlmostEqual(0.9, _parse_kiro_auto_forge_soft_ratio(0.9))
        self.assertEqual(0.0, _parse_kiro_auto_forge_soft_ratio(0))
        self.assertAlmostEqual(0.9375, _parse_kiro_auto_forge_soft_ratio("x/0"))
        self.assertAlmostEqual(0.9375, _parse_kiro_auto_forge_soft_ratio(2.5))

    def test_window_lookup(self):
        self.assertEqual(200_000, _kiro_context_window_for("claude-sonnet-4.5"))
        self.assertEqual(1_000_000, _kiro_context_window_for("auto"))
        self.assertEqual(1_000_000, _kiro_context_window_for("claude-opus-4.7-1m"))
        self.assertEqual(1_000_000, _kiro_context_window_for("claude-sonnet-5[1m]"))
        self.assertIsNone(_kiro_context_window_for("claude-new-model"))
        self.assertIsNone(_kiro_context_window_for(""))
        self.assertEqual(500_000, _kiro_context_window_for("claude-new-model", {"claude-new-model": 500_000}))

    def test_marker_strip(self):
        self.assertEqual(("好，先到这。", True), _strip_kiro_forge_marker(f"好，先到这。\n{KIRO_FORGE_MARKER}\n"))
        self.assertEqual(("", True), _strip_kiro_forge_marker(f"  {KIRO_FORGE_MARKER}  "))
        inline = f"写 `{KIRO_FORGE_MARKER}` 就能 forge"
        self.assertEqual((inline, False), _strip_kiro_forge_marker(inline))


class AdaptiveAutoForgeTest(unittest.TestCase):
    def _handler(self, tmp, usage, window):
        acp = FakeACP(usage=usage)
        handler = _make_handler(Path(tmp), acp)
        handler._kiro_context_window_tokens = lambda: window
        return handler, acp

    def test_1m_window_forges_at_30_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, acp = self._handler(tmp, 29.0, 1_000_000)
            self.assertEqual(("session_old", False), handler._maybe_forge_kiro_session("session_old"))
            acp.usage = 30.5
            with patch("push._scan_kiro_session_tasks", return_value=dict(NO_TASKS)):
                self.assertEqual(("session_new", True), handler._maybe_forge_kiro_session("session_old"))
            self.assertIn("自动", handler.notifications[-1][0])

    def test_200k_window_still_80_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, acp = self._handler(tmp, 79.0, 200_000)
            self.assertEqual(("session_old", False), handler._maybe_forge_kiro_session("session_old"))
            acp.usage = 80.0
            with patch("push._scan_kiro_session_tasks", return_value=dict(NO_TASKS)):
                self.assertEqual(("session_new", True), handler._maybe_forge_kiro_session("session_old"))

    def test_unknown_window_uses_configured_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, _acp = self._handler(tmp, 50.0, None)
            self.assertEqual(("session_old", False), handler._maybe_forge_kiro_session("session_old"))

    def test_soft_notice_only_once_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, acp = self._handler(tmp, 74.0, 200_000)
            self.assertEqual("", handler._kiro_soft_forge_notice("session_old"))
            acp.usage = 76.0
            notice = handler._kiro_soft_forge_notice("session_old")
            self.assertIn(KIRO_FORGE_MARKER, notice)
            self.assertIn("76%", notice)
            self.assertIn("80%", notice)
            self.assertEqual("", handler._kiro_soft_forge_notice("session_old"))
            # After a forge the new session gets its own single notice.
            self.assertIn(KIRO_FORGE_MARKER, handler._kiro_soft_forge_notice("session_new"))
            acp.usage = 81.0  # hard threshold handles this, no soft notice
            self.assertEqual("", handler._kiro_soft_forge_notice("session_third"))

    def test_soft_notice_on_1m_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, _acp = self._handler(tmp, 28.2, 1_000_000)  # > 281.25k
            notice = handler._kiro_soft_forge_notice("session_old")
            self.assertIn("约 30%", notice)


class SelfForgeTest(unittest.TestCase):
    def test_self_forge_after_turn_runs_pipeline_with_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=76.0)
            handler = _make_handler(Path(tmp), acp)
            with patch("push._scan_kiro_session_tasks", return_value=dict(NO_TASKS)):
                handler._kiro_self_forge_after_turn("session_old")
            self.assertEqual("session_new", acp.active)
            chat = handler.state.contact_chats["kiro"]
            self.assertEqual("system:kiro-forge", chat.rows[-1]["source"])
            self.assertIn("自己决定 forge", chat.rows[-1]["text"])
            self.assertIn("自己", handler.notifications[-1][0])
            self.assertEqual("", getattr(handler.state, "kiro_self_forge_pending_session", ""))

    def test_busy_self_forge_is_deferred_to_next_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=40.0)
            handler = _make_handler(Path(tmp), acp)
            handler.state.kiro_active_turn = {"user_ts": "ts-next"}
            handler._kiro_self_forge_after_turn("session_old")
            self.assertEqual("session_old", acp.active)
            self.assertEqual("session_old", handler.state.kiro_self_forge_pending_session)
            handler.state.kiro_active_turn = {}
            with patch("push._scan_kiro_session_tasks", return_value=dict(NO_TASKS)):
                # usage 40% is under every threshold: only the request forges.
                self.assertEqual(("session_new", True), handler._maybe_forge_kiro_session("session_old"))
            self.assertEqual("", handler.state.kiro_self_forge_pending_session)
            self.assertIn("自己决定 forge", handler.state.contact_chats["kiro"].rows[-1]["text"])

    def test_stale_pending_request_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=40.0, active="session_other")
            handler = _make_handler(Path(tmp), acp)
            handler.state.kiro_self_forge_pending_session = "session_old"
            self.assertEqual(("session_other", False), handler._maybe_forge_kiro_session("session_other"))
            self.assertEqual("", handler.state.kiro_self_forge_pending_session)
            self.assertEqual([], acp.forged)


class MarkerSendPathTest(unittest.TestCase):
    """The worker strips [[KIRO_FORGE]] before history/WeChat and then forges."""

    def _acp(self, reply, prompts, usage=None, model="claude-sonnet-4.5"):
        def prompt_existing(text, *, on_update=None, **_kwargs):
            prompts.append(text)
            if on_update is not None:
                on_update(reply)

        return types.SimpleNamespace(
            prepare_session=lambda **_kw: "kiro-session-1",
            prompt_existing=prompt_existing,
            cancel=lambda _turn, _session: True,
            close=lambda: None,
            new_session=lambda **_kw: "kiro-session-2",
            context_usage_percent=lambda: usage,
            current_model_id=lambda: model,
        )

    def _handler(self, acp):
        handler, kiro, _xiaoke = acp_tests.KiroChatHandlerTest._handler(acp_tests.KiroChatHandlerTest(), acp)
        handler.self_forges = []
        handler.mirrored = []
        handler._kiro_self_forge_after_turn = lambda sid: handler.self_forges.append(sid)
        handler._mirror_reply_to_wecom = lambda cid, rec, visible, **_kw: handler.mirrored.append(visible)
        return handler, kiro

    def test_marker_is_stripped_and_triggers_forge(self):
        prompts = []
        handler, kiro = self._handler(self._acp(f"那今天先聊到这。\n{KIRO_FORGE_MARKER}", prompts))
        with patch("push.threading.Thread", acp_tests._immediate_thread):
            handler._handle_kiro_chat_send({"text": "晚安"}, "kiro")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("那今天先聊到这。", kiro.records[-1]["text"])
        self.assertEqual(["那今天先聊到这。"], handler.mirrored)
        self.assertEqual(["kiro-session-1"], handler.self_forges)
        self.assertNotIn(KIRO_FORGE_MARKER, str(handler.state.chat_drafts))

    def test_no_marker_no_forge(self):
        prompts = []
        handler, kiro = self._handler(self._acp("普通回复", prompts))
        with patch("push.threading.Thread", acp_tests._immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好"}, "kiro")
        self.assertEqual("普通回复", kiro.records[-1]["text"])
        self.assertEqual([], handler.self_forges)

    def test_soft_notice_rides_on_next_prompt_once(self):
        prompts = []
        handler, _kiro = self._handler(self._acp("嗯嗯", prompts, usage=76.0))
        with patch("push.threading.Thread", acp_tests._immediate_thread):
            handler._handle_kiro_chat_send({"text": "第一句"}, "kiro")
            handler._handle_kiro_chat_send({"text": "第二句"}, "kiro")
        self.assertEqual(2, len(prompts))
        self.assertIn(KIRO_FORGE_MARKER, prompts[0])
        self.assertTrue(prompts[0].startswith("第一句"))
        self.assertNotIn(KIRO_FORGE_MARKER, prompts[1])
        self.assertEqual([], handler.self_forges)


if __name__ == "__main__":
    unittest.main()
