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


class MostlyEnglishTest(unittest.TestCase):
    def test_english_dominant(self) -> None:
        self.assertTrue(translate_api._mostly_english(EN_THINKING))

    def test_chinese_skipped(self) -> None:
        self.assertFalse(translate_api._mostly_english(ZH_THINKING))

    def test_short_or_symbolic_skipped(self) -> None:
        self.assertFalse(translate_api._mostly_english(""))
        self.assertFalse(translate_api._mostly_english("```\n{}\n```"))
        self.assertFalse(translate_api._mostly_english("ok"))


class TranslateThinkingAutoTest(unittest.TestCase):
    def test_english_triggers_translation(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            return_value={"translated": ZH_TRANSLATION, "cached": False},
        ) as mock_translate:
            display, original = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, ZH_TRANSLATION)
        self.assertEqual(original, EN_THINKING)
        mock_translate.assert_called_once()
        # 自动预翻译必须带 30s 上限，不拖累聊天入库
        self.assertEqual(mock_translate.call_args.kwargs["timeout"], translate_api.AUTO_TIMEOUT_SEC)

    def test_chinese_skips_api(self) -> None:
        with patch.object(translate_api, "translate_text") as mock_translate:
            display, original = translate_api.translate_thinking_auto(ZH_THINKING)
        mock_translate.assert_not_called()
        self.assertEqual(display, ZH_THINKING)
        self.assertIsNone(original)

    def test_failure_falls_back_to_original(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            side_effect=translate_api.TranslateError("openrouter_http_402"),
        ):
            display, original = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, EN_THINKING)
        self.assertIsNone(original)

    def test_unexpected_exception_falls_back_to_original(self) -> None:
        with patch.object(translate_api, "translate_text", side_effect=RuntimeError("boom")):
            display, original = translate_api.translate_thinking_auto(EN_THINKING)
        self.assertEqual(display, EN_THINKING)
        self.assertIsNone(original)

    def test_empty_input_passthrough(self) -> None:
        with patch.object(translate_api, "translate_text") as mock_translate:
            display, original = translate_api.translate_thinking_auto("   ")
        mock_translate.assert_not_called()
        self.assertEqual(display, "   ")
        self.assertIsNone(original)

    def test_echo_reply_treated_as_noop(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            return_value={"translated": EN_THINKING.strip(), "cached": False},
        ):
            display, original = translate_api.translate_thinking_auto(EN_THINKING)
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

    def test_chinese_thinking_stored_verbatim_without_api_call(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
