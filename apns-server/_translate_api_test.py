#!/usr/bin/env python3
"""Regression tests for translate_api + POST /chat/translate handler."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push
import translate_api
from push import PushHandler


SAMPLE_THINKING = "Let me check the config file first.\n\n```bash\ncat config.toml\n```\n\nThe timeout is 30s."


def _ok_response(translated: str = "让我先看一下配置文件。", prompt_tokens: int = 120, completion_tokens: int = 60):
    return types.SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [{"message": {"content": translated}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


class TranslateTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "cache"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _call(self, text: str = SAMPLE_THINKING, **kwargs):
        kwargs.setdefault("cache_dir", self.cache_dir)
        kwargs.setdefault("api_key", "sk-test")
        return translate_api.translate_text(text, **kwargs)

    def test_missing_api_key_raises(self) -> None:
        with patch.object(translate_api, "openrouter_api_key", return_value=""):
            with self.assertRaises(translate_api.TranslateError) as ctx:
                self._call(api_key=None)
        self.assertEqual(str(ctx.exception), "openrouter_api_key_missing")

    def test_empty_text_raises(self) -> None:
        for raw in ("", "   "):
            with self.assertRaises(translate_api.TranslateError) as ctx:
                self._call(raw)
            self.assertEqual(str(ctx.exception), "empty_text")

    def test_successful_translation(self) -> None:
        with patch.object(translate_api.httpx, "post", return_value=_ok_response()) as post:
            result = self._call()
        self.assertEqual(result["translated"], "让我先看一下配置文件。")
        self.assertFalse(result["cached"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["usage"]["prompt_tokens"], 120)
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(kwargs["json"]["model"], translate_api.TRANSLATE_MODEL)
        self.assertEqual(kwargs["json"]["messages"][0]["role"], "system")
        self.assertEqual(kwargs["json"]["messages"][1]["content"], SAMPLE_THINKING)

    def test_system_prompt_guards_chinese_passthrough(self) -> None:
        # 2026-09-11 修复：prompt 必须明确——输入已是中文则原样返回、不得翻成
        # 任何语言（旧 prompt 写死"把英文直译为中文"，曾把中文思维链反向翻成英文）
        prompt = translate_api.TRANSLATE_SYSTEM_PROMPT
        self.assertIn("原样返回", prompt)
        self.assertIn("不得", prompt)
        self.assertNotIn("把用户给出的英文内容", prompt)
        captured: dict = {}
        response = _ok_response()

        def _post(url, **kwargs):
            captured.update(kwargs)
            return response

        with patch.object(translate_api.httpx, "post", side_effect=_post):
            self._call()
        sent_prompt = captured["json"]["messages"][0]["content"]
        self.assertEqual(sent_prompt, prompt)

    def test_cache_key_includes_prompt_version(self) -> None:
        # 缓存键 = sha256(PROMPT_VERSION + text)：prompt 改动后旧译文不得再命中，
        # 否则会复用旧 prompt 产出的"中文被翻成英文"错误译文（2026-09-11 事故）。
        key = translate_api._cache_key(SAMPLE_THINKING)
        legacy_key = hashlib.sha256(SAMPLE_THINKING.encode("utf-8")).hexdigest()
        self.assertNotEqual(key, legacy_key)
        # 旧格式缓存条目（sha256(text)）即使存在也不得命中
        self.cache_dir.mkdir(parents=True)
        (self.cache_dir / f"{legacy_key}.json").write_text(
            json.dumps({"model": translate_api.TRANSLATE_MODEL, "translated": "旧污染译文"}),
            encoding="utf-8",
        )
        with patch.object(translate_api.httpx, "post", return_value=_ok_response()) as post:
            result = self._call()
        self.assertEqual(post.call_count, 1)
        self.assertFalse(result["cached"])
        self.assertEqual(result["translated"], "让我先看一下配置文件。")

    def test_prompt_version_derived_from_prompt_content(self) -> None:
        # 独立审核建议：版本从 prompt 内容派生，改 prompt 自动换缓存版本，
        # 不存在"改了 prompt 忘 bump 版本"导致陈旧缓存命中。
        expected = hashlib.sha256(
            translate_api.TRANSLATE_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest()[:12]
        self.assertEqual(translate_api.PROMPT_VERSION, expected)
        # prompt 内容一旦变化，派生版本必然不同
        other = hashlib.sha256(b"some other prompt").hexdigest()[:12]
        self.assertNotEqual(translate_api.PROMPT_VERSION, other)

    def test_chinese_input_short_circuits_no_api(self) -> None:
        # 独立审核建议：短路下沉到 translate_text，/chat/translate 手动入口
        # （push.py 直调本函数）同样覆盖——中文为主不发模型、不需要 API key。
        zh = "她又在玩梗试探我了,问屁股臭不臭这种整活问题。"
        with patch.object(translate_api.httpx, "post") as post, \
             patch.object(translate_api, "openrouter_api_key", side_effect=AssertionError("不应解析 key")):
            result = self._call(zh, api_key=None)
        post.assert_not_called()
        self.assertEqual(result["translated"], zh)
        self.assertFalse(result["cached"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["usage"], {})

    def test_chinese_mixed_with_code_short_circuits_no_api(self) -> None:
        zh_mixed = "先看配置：\n\n```bash\ncat config.toml\n```\n\n超时改成三十秒就行。"
        with patch.object(translate_api.httpx, "post") as post:
            result = self._call(zh_mixed)
        post.assert_not_called()
        self.assertEqual(result["translated"], zh_mixed)

    def test_chinese_short_circuit_does_not_touch_cache(self) -> None:
        # 恒等结果不进缓存（无意义），也不读缓存
        zh = "这段思维链本来就是中文。"
        with patch.object(translate_api, "_cache_read", side_effect=AssertionError("不应读缓存")), \
             patch.object(translate_api, "_cache_write", side_effect=AssertionError("不应写缓存")):
            result = self._call(zh)
        self.assertEqual(result["translated"], zh)

    def test_cache_hit_skips_api(self) -> None:
        with patch.object(translate_api.httpx, "post", return_value=_ok_response()) as post:
            first = self._call()
            second = self._call()
        self.assertEqual(post.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["translated"], first["translated"])

    def test_cache_ignored_for_other_model(self) -> None:
        with patch.object(translate_api.httpx, "post", return_value=_ok_response()) as post:
            self._call()
            result = self._call(model="qwen/qwen3-14b")
        self.assertEqual(post.call_count, 2)
        self.assertFalse(result["cached"])

    def test_cache_survives_corrupt_file(self) -> None:
        key = translate_api._cache_key(SAMPLE_THINKING)
        self.cache_dir.mkdir(parents=True)
        (self.cache_dir / f"{key}.json").write_text("{not json", encoding="utf-8")
        with patch.object(translate_api.httpx, "post", return_value=_ok_response()) as post:
            result = self._call()
        self.assertEqual(post.call_count, 1)
        self.assertFalse(result["cached"])
        self.assertEqual(result["translated"], "让我先看一下配置文件。")

    def test_http_error_raises_stable_code(self) -> None:
        response = types.SimpleNamespace(status_code=402, json=lambda: {})
        with patch.object(translate_api.httpx, "post", return_value=response):
            with self.assertRaises(translate_api.TranslateError) as ctx:
                self._call()
        self.assertEqual(str(ctx.exception), "openrouter_http_402")

    def test_network_error_degrades(self) -> None:
        with patch.object(translate_api.httpx, "post", side_effect=TimeoutError("boom")):
            with self.assertRaises(translate_api.TranslateError) as ctx:
                self._call()
        self.assertTrue(str(ctx.exception).startswith("openrouter_request_failed"))

    def test_empty_reply_raises_and_is_not_cached(self) -> None:
        with patch.object(translate_api.httpx, "post", return_value=_ok_response("  ")) as post:
            with self.assertRaises(translate_api.TranslateError):
                self._call()
            with self.assertRaises(translate_api.TranslateError):
                self._call()
        self.assertEqual(post.call_count, 2)

    def test_overlong_text_is_truncated(self) -> None:
        long_text = "word " * 6000  # 30000 chars > MAX_TEXT_CHARS
        captured: dict = {}
        response = _ok_response()

        def _post(url, **kwargs):
            captured.update(kwargs)
            return response

        with patch.object(translate_api.httpx, "post", side_effect=_post):
            result = self._call(long_text)
        self.assertTrue(result["truncated"])
        sent = captured["json"]["messages"][1]["content"]
        self.assertEqual(len(sent), translate_api.MAX_TEXT_CHARS)

    def test_env_file_fallback_reads_systemd_override(self) -> None:
        fake = Path(self.tmp.name) / "openrouter.conf"
        fake.write_text('[Service]\nEnvironment="OPENROUTER_API_KEY=sk-from-file"\n', encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True), \
             patch.object(translate_api, "SERVICE_ENV_FILE", fake):
            self.assertEqual(translate_api.openrouter_api_key(), "sk-from-file")

    def test_env_var_wins_over_file(self) -> None:
        fake = Path(self.tmp.name) / "openrouter.conf"
        fake.write_text('Environment="OPENROUTER_API_KEY=sk-from-file"\n', encoding="utf-8")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-from-env"}), \
             patch.object(translate_api, "SERVICE_ENV_FILE", fake):
            self.assertEqual(translate_api.openrouter_api_key(), "sk-from-env")


class ChatTranslateHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.responses: list[tuple[int, dict]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def handler(self) -> PushHandler:
        h = object.__new__(PushHandler)
        h._send_json = lambda status, payload: self.responses.append((status, payload))
        return h

    def test_success_payload_shape(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            return_value={"translated": "译文", "cached": False, "truncated": False},
        ):
            self.handler()._handle_chat_translate({"text": SAMPLE_THINKING})
        status, payload = self.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["translated"], "译文")
        self.assertFalse(payload["cached"])
        self.assertFalse(payload["truncated"])

    def test_empty_text_is_400(self) -> None:
        self.handler()._handle_chat_translate({"text": "  "})
        status, payload = self.responses[0]
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "empty_text")

    def test_upstream_failure_is_502_with_stable_code(self) -> None:
        with patch.object(
            translate_api, "translate_text",
            side_effect=translate_api.TranslateError("openrouter_http_402"),
        ):
            self.handler()._handle_chat_translate({"text": SAMPLE_THINKING})
        status, payload = self.responses[0]
        self.assertEqual(status, 502)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "openrouter_http_402")

    def test_unexpected_failure_is_500_and_does_not_raise(self) -> None:
        with patch.object(translate_api, "translate_text", side_effect=RuntimeError("boom")):
            self.handler()._handle_chat_translate({"text": SAMPLE_THINKING})
        status, payload = self.responses[0]
        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
