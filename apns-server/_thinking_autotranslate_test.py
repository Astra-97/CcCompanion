#!/usr/bin/env python3
"""Regression tests for 思考链服务端自动预翻译 (2026-09-08).

覆盖 translate_api.translate_thinking_auto 的启发/降级策略，以及
/chat/append 两条入库路径（append / merge_thinking_to_last_assistant）
译文入库 + metadata.thinking_original 留原文。
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push  # noqa: F401  (import 验证 push.py 语法/接线)
import translate_api
from chat_history import ChatHistory
from push import PushHandler


EN_THINKING = (
    "Let me check the config file first, then adjust the timeout value.\n\n"
    "```bash\ncat config.toml\n```\n\n"
    "The current timeout is thirty seconds, which should be enough."
)
ZH_THINKING = "让我先看一下配置文件，然后把超时时间调成三十秒，应该就够用了。"
ZH_TRANSLATION = "让我先检查配置文件，然后调整超时时间。\n\n当前超时是三十秒，应该够用。"


class TranslateThinkingAutoTest(unittest.TestCase):
    def test_english_triggers_translation(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            return_value={"translated": ZH_TRANSLATION, "cached": False},
        ) as mock_translate:
            display, original, _suspect = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, ZH_TRANSLATION)
        self.assertEqual(original, EN_THINKING)
        mock_translate.assert_called_once()
        # 自动预翻译必须带 30s 上限，不拖累聊天入库
        self.assertEqual(mock_translate.call_args.kwargs["timeout"], translate_api.AUTO_TIMEOUT_SEC)

    def test_chinese_short_circuits_identity(self) -> None:
        # 2026-09-11 防线：中文为主的输入恒等短路，不发模型——qwen 曾把中文
        # 思维链反向翻成英文，新 prompt 下仍会改动标点，中译中零收益只有风险。
        with patch.object(translate_api, "translate_text") as mock_translate:
            display, original, _suspect = translate_api.translate_thinking_auto(ZH_THINKING)
        mock_translate.assert_not_called()
        self.assertEqual(display, ZH_THINKING)
        self.assertIsNone(original)

    def test_chinese_mixed_with_code_short_circuits(self) -> None:
        mixed = "先看配置：\n\n```bash\ncat config.toml\n```\n\n超时改成三十秒就行。"
        self.assertTrue(translate_api._is_chinese_dominant(mixed))
        with patch.object(translate_api, "translate_text") as mock_translate:
            display, original, _suspect = translate_api.translate_thinking_auto(mixed)
        mock_translate.assert_not_called()
        self.assertEqual(display, mixed)
        self.assertIsNone(original)

    def test_english_with_one_chinese_word_still_translates(self) -> None:
        # 非中文为主的输入不设门控、全部走模型（Astra 2026-09-08 拍板不变）
        text = EN_THINKING + "\n\n顺便记一下这个词：配置。"
        self.assertFalse(translate_api._is_chinese_dominant(text))
        with patch.object(
            translate_api, "translate_text",
            return_value={"translated": ZH_TRANSLATION, "cached": False},
        ) as mock_translate:
            display, original, _suspect = translate_api.translate_thinking_auto(text)
        mock_translate.assert_called_once()
        self.assertEqual(display, ZH_TRANSLATION)
        self.assertEqual(original, text)

    def test_failure_falls_back_to_original(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            side_effect=translate_api.TranslateError("openrouter_http_402"),
        ):
            display, original, _suspect = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, EN_THINKING)
        self.assertIsNone(original)

    def test_unexpected_exception_falls_back_to_original(self) -> None:
        with patch.object(translate_api, "translate_text", side_effect=RuntimeError("boom")):
            display, original, _suspect = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, EN_THINKING)
        self.assertIsNone(original)

    def test_empty_input_passthrough(self) -> None:
        with patch.object(translate_api, "translate_text") as mock_translate:
            display, original, _suspect = translate_api.translate_thinking_auto("   ")
        mock_translate.assert_not_called()
        self.assertEqual(display, "   ")
        self.assertIsNone(original)

    def test_echo_reply_treated_as_noop(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            return_value={"translated": EN_THINKING.strip(), "cached": False},
        ):
            display, original, _suspect = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, EN_THINKING)
        self.assertIsNone(original)


class ChatAppendAutoTranslateTest(unittest.TestCase):
    """Handler 级：/chat/append 入库的思考链已是译文，原文在 metadata。"""

    def append_handler(self) -> PushHandler:
        handler = object.__new__(PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = ChatHistory(Path(tmp.name) / "history.jsonl")
        handler.state = types.SimpleNamespace(
            shared_secret="",
            strict_auth=True,
            contact_chats={"xiaoke": chat},
            attachments_dir=Path(tmp.name),
            settings={},
            tokens=types.SimpleNamespace(all_active=lambda: []),
            apns_enabled=False,
        )
        handler.headers = {}
        handler._chat_for_contact = lambda _contact: chat
        handler._source_for_request = lambda *a: "claude-code"
        handler._has_pending_group_reply = lambda: False
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.chat = chat
        return handler

    def _translated(self, text: str, **kwargs):
        return {"translated": ZH_TRANSLATION, "cached": False, "truncated": False, "usage": {}}

    def test_append_stores_translation_and_original_in_metadata(self) -> None:
        handler = self.append_handler()
        with patch.object(translate_api, "translate_text", side_effect=self._translated):
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "配置已更新。",
                "thinking": EN_THINKING,
            })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        rec = payload["record"]
        self.assertEqual(rec["thinking"], ZH_TRANSLATION)
        self.assertEqual(rec["metadata"]["thinking_original"], EN_THINKING)
        stored = handler.chat.read_since()
        self.assertEqual(stored[0]["thinking"], ZH_TRANSLATION)
        self.assertEqual(stored[0]["metadata"]["thinking_original"], EN_THINKING)

    def test_chinese_thinking_short_circuits_and_stored_as_is(self) -> None:
        # 中文思考链恒等短路：不发模型、原文直接入库、metadata 不留 thinking_original
        handler = self.append_handler()
        with patch.object(translate_api, "translate_text") as mock_translate:
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "中文思考链原样入库。",
                "thinking": ZH_THINKING,
            })
        mock_translate.assert_not_called()
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        rec = payload["record"]
        self.assertEqual(rec["thinking"], ZH_THINKING)
        self.assertNotIn("thinking_original", rec.get("metadata") or {})

    def test_translation_failure_keeps_english_and_still_appends(self) -> None:
        handler = self.append_handler()
        with patch.object(
            translate_api, "translate_text",
            side_effect=translate_api.TranslateError("openrouter_request_failed: TimeoutError"),
        ):
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "配置已更新（失败降级）。",
                "thinking": EN_THINKING,
            })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        rec = payload["record"]
        self.assertEqual(rec["thinking"], EN_THINKING)
        self.assertNotIn("thinking_original", rec.get("metadata") or {})

    def test_existing_metadata_is_preserved(self) -> None:
        handler = self.append_handler()
        with patch.object(translate_api, "translate_text", side_effect=self._translated):
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "配置已更新（带 metadata）。",
                "thinking": EN_THINKING,
                "metadata": {"custom_tag": "sess-1"},
            })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        meta = payload["record"]["metadata"]
        self.assertEqual(meta["thinking_original"], EN_THINKING)
        self.assertEqual(meta["custom_tag"], "sess-1")

    def test_merge_path_stores_translation_and_original(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": "claude-code",
            "text": "先落一条正文。",
        })
        with patch.object(translate_api, "translate_text", side_effect=self._translated):
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "",
                "thinking": EN_THINKING,
            })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["merged"], payload)
        stored = handler.chat.read_since()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["thinking"], ZH_TRANSLATION)
        self.assertEqual(stored[0]["metadata"]["thinking_original"], EN_THINKING)


class AbridgedTranslateQualityTest(unittest.TestCase):
    """缩水质检 (2026-10-16)：长原文译文 <45% 判疑似缩水 → 同 prompt 重试一次，
    两版取较长；都缩水则取较长 + suspect 标记。"""

    # 长英文原文 (~378 字符 > 200 阈值)
    LONG_SOURCE = (
        "The user asked me to review the translation pipeline tonight. " * 6
    ).strip()
    SHORT_ZH = "[laughs] 说得对，谁懂啊～"  # 12 字符 ≈ 3%，明显缩水
    # 正常完整译文 ~180 字符 ≥ 45%，不触发重试
    FULL_ZH = "用户今晚让我审查翻译管线。" * 15

    def test_abridged_triggers_retry_and_takes_longer(self) -> None:
        with patch.object(
            translate_api,
            "translate_text",
            side_effect=[
                {"translated": self.SHORT_ZH, "cached": False},
                {"translated": self.FULL_ZH, "cached": False},
            ],
        ) as mock_translate:
            display, original, suspect = translate_api.translate_thinking_auto(self.LONG_SOURCE)
        self.assertEqual(mock_translate.call_count, 2)
        # 重试必须走独立缓存键（salt），否则直接命中首次的缩水缓存
        self.assertEqual(
            mock_translate.call_args_list[1].kwargs["cache_salt"],
            translate_api.ABRIDGED_RETRY_CACHE_SALT,
        )
        self.assertEqual(display, self.FULL_ZH)
        self.assertEqual(original, self.LONG_SOURCE)
        self.assertFalse(suspect)  # 较长版已不缩水，不落标记

    def test_both_abridged_takes_longer_and_flags_suspect(self) -> None:
        longer_but_short = "用户让我审查翻译管线，今晚就把它看完再说。"  # 22 字符，仍 < 45%
        self.assertGreater(len(longer_but_short), len(self.SHORT_ZH))
        self.assertLess(len(longer_but_short), len(self.LONG_SOURCE) * 0.45)
        with patch.object(
            translate_api,
            "translate_text",
            side_effect=[
                {"translated": self.SHORT_ZH, "cached": False},
                {"translated": longer_but_short, "cached": False},
            ],
        ) as mock_translate:
            display, original, suspect = translate_api.translate_thinking_auto(self.LONG_SOURCE)
        self.assertEqual(mock_translate.call_count, 2)
        self.assertEqual(display, longer_but_short)  # 两版取较长
        self.assertEqual(original, self.LONG_SOURCE)
        self.assertTrue(suspect)

    def test_retry_shorter_keeps_first_and_flags(self) -> None:
        first_short = "用户让我审查翻译管线，今晚就把它看完再说。"  # 22 字符 > SHORT_ZH
        self.assertGreater(len(first_short), len(self.SHORT_ZH))
        with patch.object(
            translate_api,
            "translate_text",
            side_effect=[
                {"translated": first_short, "cached": False},
                {"translated": self.SHORT_ZH, "cached": False},
            ],
        ):
            display, _original, suspect = translate_api.translate_thinking_auto(self.LONG_SOURCE)
        self.assertEqual(display, first_short)
        self.assertTrue(suspect)

    def test_retry_failure_keeps_first_and_flags(self) -> None:
        with patch.object(
            translate_api,
            "translate_text",
            side_effect=[
                {"translated": self.SHORT_ZH, "cached": False},
                translate_api.TranslateError("openrouter_http_402"),
            ],
        ) as mock_translate:
            display, original, suspect = translate_api.translate_thinking_auto(self.LONG_SOURCE)
        self.assertEqual(mock_translate.call_count, 2)
        self.assertEqual(display, self.SHORT_ZH)
        self.assertEqual(original, self.LONG_SOURCE)
        self.assertTrue(suspect)

    def test_normal_length_translation_no_retry(self) -> None:
        with patch.object(
            translate_api,
            "translate_text",
            return_value={"translated": self.FULL_ZH, "cached": False},
        ) as mock_translate:
            display, original, suspect = translate_api.translate_thinking_auto(self.LONG_SOURCE)
        mock_translate.assert_called_once()  # 长度正常，API 只调一次
        self.assertEqual(display, self.FULL_ZH)
        self.assertEqual(original, self.LONG_SOURCE)
        self.assertFalse(suspect)

    def test_short_source_below_threshold_no_retry(self) -> None:
        # 原文不足 200 字符：即使译文极短也不触发阈值、不重试
        short_source = "Let me check the config file and adjust the timeout value."
        self.assertLess(len(short_source), translate_api.ABRIDGED_MIN_SOURCE_CHARS)
        with patch.object(
            translate_api,
            "translate_text",
            return_value={"translated": "好。", "cached": False},
        ) as mock_translate:
            display, original, suspect = translate_api.translate_thinking_auto(short_source)
        mock_translate.assert_called_once()
        self.assertEqual(display, "好。")
        self.assertEqual(original, short_source)
        self.assertFalse(suspect)


class ChatAppendAbridgedSuspectTest(unittest.TestCase):
    """Handler 级：两版都缩水时 metadata 落 thinking_translation_suspect 标记。"""

    LONG_SOURCE = AbridgedTranslateQualityTest.LONG_SOURCE
    SHORT_ZH = AbridgedTranslateQualityTest.SHORT_ZH

    def append_handler(self) -> PushHandler:
        handler = object.__new__(PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = ChatHistory(Path(tmp.name) / "history.jsonl")
        handler.state = types.SimpleNamespace(
            shared_secret="",
            strict_auth=True,
            contact_chats={"xiaoke": chat},
            attachments_dir=Path(tmp.name),
            settings={},
            tokens=types.SimpleNamespace(all_active=lambda: []),
            apns_enabled=False,
        )
        handler.headers = {}
        handler._chat_for_contact = lambda _contact: chat
        handler._source_for_request = lambda *a: "claude-code"
        handler._has_pending_group_reply = lambda: False
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.chat = chat
        return handler

    def _abridged(self, text: str, **kwargs):
        return {"translated": self.SHORT_ZH, "cached": False, "truncated": False, "usage": {}}

    def test_append_path_stores_suspect_flag_in_metadata(self) -> None:
        handler = self.append_handler()
        with patch.object(translate_api, "translate_text", side_effect=self._abridged) as mock_translate:
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "翻译管线审查完了。",
                "thinking": self.LONG_SOURCE,
            })
        self.assertEqual(mock_translate.call_count, 2)  # 缩水触发重试
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        meta = payload["record"]["metadata"]
        self.assertEqual(meta["thinking_original"], self.LONG_SOURCE)
        self.assertIs(meta["thinking_translation_suspect"], True)
        stored = handler.chat.read_since()
        self.assertIs(stored[0]["metadata"]["thinking_translation_suspect"], True)

    def test_merge_path_stores_suspect_flag_in_metadata(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": "claude-code",
            "text": "先落一条正文（缩水标记）。",
        })
        with patch.object(translate_api, "translate_text", side_effect=self._abridged):
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": "claude-code",
                "text": "",
                "thinking": self.LONG_SOURCE,
            })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["merged"], payload)
        stored = handler.chat.read_since()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["thinking"], self.SHORT_ZH)
        self.assertEqual(stored[0]["metadata"]["thinking_original"], self.LONG_SOURCE)
        self.assertIs(stored[0]["metadata"]["thinking_translation_suspect"], True)


if __name__ == "__main__":
    unittest.main()
