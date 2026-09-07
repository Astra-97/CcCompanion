#!/usr/bin/env python3
"""Tests for voice_gemini: reply parsing + mocked OpenRouter roundtrip."""

from __future__ import annotations

import base64
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import voice_gemini


LISTEN_REPLY = (
    "转写核对：今天真的太开心啦，哈哈！\n"
    "细粒度情绪：能量高、语速偏快，尾音上扬带笑意\n"
    "声学特征：嗓音清亮，略有气息声\n"
    "一句话总结：心情很好地分享日常\n"
)


class ParseListenOutputTest(unittest.TestCase):
    def test_parses_all_four_fields(self) -> None:
        result = voice_gemini.parse_listen_output(LISTEN_REPLY)
        self.assertEqual(result["transcript_check"], "今天真的太开心啦，哈哈！")
        self.assertEqual(result["emotion_detail"], "能量高、语速偏快，尾音上扬带笑意")
        self.assertEqual(result["acoustic_desc"], "嗓音清亮，略有气息声")
        self.assertEqual(result["summary"], "心情很好地分享日常")

    def test_halfwidth_colon_and_bullets(self) -> None:
        result = voice_gemini.parse_listen_output(
            "- 转写核对: 你好\n* 一句话总结: 在打招呼\n"
        )
        self.assertEqual(result["transcript_check"], "你好")
        self.assertEqual(result["summary"], "在打招呼")

    def test_missing_fields_stay_empty(self) -> None:
        result = voice_gemini.parse_listen_output("随便一句没有前缀的话")
        self.assertEqual(result, {field: "" for field in (
            "transcript_check", "emotion_detail", "acoustic_desc", "summary",
        )})

    def test_empty_input(self) -> None:
        for raw in (None, "", "   "):
            result = voice_gemini.parse_listen_output(raw)
            self.assertFalse(any(result.values()))


class ListenVoiceAudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "clip.mp3"
        self.audio.write_bytes(b"fake-mp3-bytes")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ok_response(self, content: str = LISTEN_REPLY):
        return types.SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": content}}]},
        )

    def test_missing_api_key_raises(self) -> None:
        with self.assertRaises(voice_gemini.VoiceListenError):
            voice_gemini.listen_voice_audio(self.audio, api_key="")

    def test_success_payload_shape_and_parse(self) -> None:
        with patch.object(voice_gemini.httpx, "post", return_value=self._ok_response()) as post:
            result = voice_gemini.listen_voice_audio(self.audio, api_key="sk-or-test")
        self.assertEqual(result["summary"], "心情很好地分享日常")
        self.assertEqual(result["emotion_detail"], "能量高、语速偏快，尾音上扬带笑意")

        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-or-test")
        self.assertEqual(kwargs["headers"]["User-Agent"], "curl/7.81.0")
        self.assertEqual(kwargs["timeout"], voice_gemini.DEFAULT_LISTEN_TIMEOUT_SEC)
        payload = kwargs["json"]
        self.assertEqual(payload["model"], voice_gemini.GEMINI_LISTEN_MODEL)
        parts = payload["messages"][0]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "input_audio")
        self.assertEqual(parts[1]["input_audio"]["format"], "mp3")
        self.assertEqual(
            base64.b64decode(parts[1]["input_audio"]["data"]),
            b"fake-mp3-bytes",
        )

    def test_mp3_input_skips_ffmpeg(self) -> None:
        with patch.object(voice_gemini.subprocess, "run") as run, \
             patch.object(voice_gemini.httpx, "post", return_value=self._ok_response()):
            voice_gemini.listen_voice_audio(self.audio, api_key="sk-or-test")
        run.assert_not_called()

    def test_non_mp3_converts_then_cleans_up_temp(self) -> None:
        m4a = Path(self.tmp.name) / "clip.m4a"
        m4a.write_bytes(b"fake-m4a")

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"converted-mp3")
            return types.SimpleNamespace(returncode=0)

        with patch.object(voice_gemini.subprocess, "run", side_effect=fake_run) as run, \
             patch.object(voice_gemini.httpx, "post", return_value=self._ok_response()) as post:
            result = voice_gemini.listen_voice_audio(m4a, api_key="sk-or-test")
        self.assertTrue(result["summary"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], voice_gemini.FFMPEG_BIN)
        temp_mp3 = Path(cmd[-1])
        # 临时 mp3 用完即删。
        self.assertFalse(temp_mp3.exists())
        parts = post.call_args[1]["json"]["messages"][0]["content"]
        self.assertEqual(base64.b64decode(parts[1]["input_audio"]["data"]), b"converted-mp3")

    def test_ffmpeg_failure_raises_and_cleans_up(self) -> None:
        m4a = Path(self.tmp.name) / "clip.m4a"
        m4a.write_bytes(b"fake-m4a")
        created: list[str] = []

        def fake_run(cmd, **kwargs):
            created.append(cmd[-1])
            return types.SimpleNamespace(returncode=1)

        with patch.object(voice_gemini.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(voice_gemini.VoiceListenError):
                voice_gemini.listen_voice_audio(m4a, api_key="sk-or-test")
        self.assertFalse(Path(created[0]).exists())

    def test_http_error_raises(self) -> None:
        response = types.SimpleNamespace(status_code=402, json=lambda: {})
        with patch.object(voice_gemini.httpx, "post", return_value=response):
            with self.assertRaises(voice_gemini.VoiceListenError):
                voice_gemini.listen_voice_audio(self.audio, api_key="sk-or-test")

    def test_network_error_raises(self) -> None:
        with patch.object(voice_gemini.httpx, "post", side_effect=TimeoutError("boom")):
            with self.assertRaises(voice_gemini.VoiceListenError):
                voice_gemini.listen_voice_audio(self.audio, api_key="sk-or-test")

    def test_unparseable_reply_raises(self) -> None:
        with patch.object(voice_gemini.httpx, "post", return_value=self._ok_response("???")):
            with self.assertRaises(voice_gemini.VoiceListenError):
                voice_gemini.listen_voice_audio(self.audio, api_key="sk-or-test")

    def test_list_content_parts_are_joined(self) -> None:
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": [
                {"type": "text", "text": "转写核对：你好\n"},
                {"type": "text", "text": "一句话总结：在打招呼"},
            ]}}]},
        )
        with patch.object(voice_gemini.httpx, "post", return_value=response):
            result = voice_gemini.listen_voice_audio(self.audio, api_key="sk-or-test")
        self.assertEqual(result["transcript_check"], "你好")
        self.assertEqual(result["summary"], "在打招呼")


if __name__ == "__main__":
    unittest.main()
