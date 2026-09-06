#!/usr/bin/env python3
"""Regression tests for /chat/voice voice-message upload + ASR injection."""

from __future__ import annotations

from email.message import Message
from pathlib import Path
import io
import json
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push
import voice_message
from chat_history import ChatHistory
from contacts.registry import default_contact_routes
from link_preview import LinkPreviewBundle
from push import PushHandler


RICH_TRANSCRIPT = "<|zh|><|HAPPY|><|Speech|><|Laughter|><|withitn|>今天真的太开心啦，哈哈！"


def _headers(pairs):
    msg = Message()
    for name, value in pairs:
        msg[name] = value
    return msg


class SenseVoiceParseTest(unittest.TestCase):
    def test_parses_emotion_and_events(self) -> None:
        result = voice_message.parse_sensevoice_output(RICH_TRANSCRIPT)
        self.assertEqual(result["text"], "今天真的太开心啦，哈哈！")
        self.assertEqual(result["language"], "zh")
        self.assertEqual(result["emotion"], "HAPPY")
        self.assertEqual(result["events"], ["Laughter"])
        self.assertIn("<|HAPPY|>", result["raw"])

    def test_strips_unknown_tags_without_breaking(self) -> None:
        result = voice_message.parse_sensevoice_output("<|en|><|NEUTRAL|><|woitn|>hello world")
        self.assertEqual(result["text"], "hello world")
        self.assertEqual(result["emotion"], "NEUTRAL")
        self.assertEqual(result["events"], [])

    def test_empty_and_garbage_input(self) -> None:
        for raw in (None, "", "   "):
            result = voice_message.parse_sensevoice_output(raw)
            self.assertEqual(result["text"], "")
            self.assertEqual(result["emotion"], "")
            self.assertEqual(result["events"], [])

    def test_plain_speech_event_is_not_surfaced(self) -> None:
        result = voice_message.parse_sensevoice_output("<|zh|><|SAD|><|Speech|>嗯。")
        self.assertEqual(result["text"], "嗯。")
        self.assertEqual(result["emotion"], "SAD")
        self.assertEqual(result["events"], [])


class TranscribeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "clip.m4a"
        self.audio.write_bytes(b"fake-audio")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_api_key_raises(self) -> None:
        with self.assertRaises(voice_message.VoiceAsrError):
            voice_message.transcribe_voice_audio(self.audio, api_key="")

    def test_successful_transcription_is_parsed(self) -> None:
        response = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"text": RICH_TRANSCRIPT},
        )
        with patch.object(voice_message.httpx, "post", return_value=response) as post:
            result = voice_message.transcribe_voice_audio(self.audio, api_key="sk-test")
        self.assertEqual(result["text"], "今天真的太开心啦，哈哈！")
        self.assertEqual(result["emotion"], "HAPPY")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(kwargs["data"]["model"], voice_message.SENSEVOICE_MODEL)

    def test_http_error_raises(self) -> None:
        response = types.SimpleNamespace(status_code=401, json=lambda: {})
        with patch.object(voice_message.httpx, "post", return_value=response):
            with self.assertRaises(voice_message.VoiceAsrError):
                voice_message.transcribe_voice_audio(self.audio, api_key="sk-test")

    def test_network_error_raises(self) -> None:
        with patch.object(voice_message.httpx, "post", side_effect=TimeoutError("boom")):
            with self.assertRaises(voice_message.VoiceAsrError):
                voice_message.transcribe_voice_audio(self.audio, api_key="sk-test")

    def test_empty_transcript_raises(self) -> None:
        response = types.SimpleNamespace(status_code=200, json=lambda: {"text": "  "})
        with patch.object(voice_message.httpx, "post", return_value=response):
            with self.assertRaises(voice_message.VoiceAsrError):
                voice_message.transcribe_voice_audio(self.audio, api_key="sk-test")


class VoiceMessageHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.attachments = root / "attachments"
        self.attachments.mkdir()
        self.chats = {
            "xiaoke": ChatHistory(root / "xiaoke.jsonl"),
            "kimi": ChatHistory(root / "kimi.jsonl"),
        }
        self.state = types.SimpleNamespace(
            contact_chats=self.chats,
            contact_routes=default_contact_routes(),
            contact_catalog=None,
            attachments_dir=self.attachments,
            active_session="",
            default_session="main",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def handler(self, body: bytes, query: str = "", content_type: str = "audio/mp4") -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.state = self.state
        handler.path = f"/chat/voice?{query}"
        handler.headers = _headers([
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("User-Agent", "CcCompanion-Android/1.0"),
        ])
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        self.responses: list[tuple[int, dict]] = []
        handler._send_json = lambda status, payload: self.responses.append((status, payload))
        return handler

    def _asr_ok(self):
        return {
            "text": "今天真的太开心啦，哈哈！",
            "language": "zh",
            "emotion": "HAPPY",
            "events": ["Laughter"],
            "raw": RICH_TRANSCRIPT,
        }

    # ── validation ──

    def test_rejects_non_audio_extension(self) -> None:
        h = self.handler(b"abc", "contact_id=xiaoke&filename=evil.exe")
        h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 415)
        self.assertFalse(payload["ok"])
        self.assertEqual(list(self.attachments.iterdir()), [])

    def test_rejects_non_audio_mime(self) -> None:
        h = self.handler(b"abc", "contact_id=xiaoke&filename=v.m4a", content_type="image/png")
        h._handle_chat_voice()
        self.assertEqual(self.responses[0][0], 415)

    def test_rejects_path_traversal_filename(self) -> None:
        h = self.handler(b"abc", "contact_id=xiaoke&filename=../evil.m4a")
        h._handle_chat_voice()
        self.assertEqual(self.responses[0][0], 400)
        self.assertEqual(self.responses[0][1]["error"], "invalid filename")

    def test_rejects_oversized_upload(self) -> None:
        h = self.handler(b"", "contact_id=xiaoke&filename=v.m4a")
        h.headers = _headers([
            ("Content-Type", "audio/mp4"),
            ("Content-Length", str(voice_message.VOICE_MESSAGE_MAX_BYTES + 1)),
        ])
        h._handle_chat_voice()
        self.assertEqual(self.responses[0][0], 400)
        self.assertIn("max 10MB", self.responses[0][1]["error"])

    def test_rejects_contact_without_voice_capability(self) -> None:
        # kairos 不在 voice_message 支持列表里（_clean_contact_id 需要它在册）。
        self.state.contact_chats["kairos"] = ChatHistory(Path(self.tmp.name) / "kairos.jsonl")
        h = self.handler(b"abc", "contact_id=kairos&filename=v.m4a")
        h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 501)
        self.assertIn("voice", payload["error"])

    # ── xiaoke pipeline ──

    def test_xiaoke_voice_message_stores_transcribes_and_injects(self) -> None:
        h = self.handler(
            b"audio-bytes",
            "contact_id=xiaoke&filename=voice.m4a&duration_ms=4200",
        )
        injected: list[str] = []
        h._channel_transport_enabled_for = lambda contact_id: False
        h._inject_to_session = lambda session, text, source=None, sender=None: (
            injected.append(text) or (True, "")
        )
        with patch.object(voice_message, "transcribe_voice_audio", return_value=self._asr_ok()), \
             patch.object(voice_message, "voice_message_api_key", return_value="sk-test"):
            h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["asr"], "ok")
        self.assertEqual(payload["emotion"], "HAPPY")

        record = payload["record"]
        self.assertEqual(record["role"], "user")
        self.assertEqual(record["attachment_type"], "audio")
        self.assertTrue(record["attachment_url"].startswith("/attachments/"))
        metadata = record["metadata"]
        self.assertEqual(metadata["type"], "voice")
        self.assertEqual(metadata["transcript"], "今天真的太开心啦，哈哈！")
        self.assertEqual(metadata["emotion"], "HAPPY")
        self.assertEqual(metadata["events"], ["Laughter"])
        self.assertEqual(metadata["duration_ms"], 4200)
        self.assertEqual(metadata["asr"], "ok")

        stored = list(self.attachments.iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].suffix, ".m4a")
        self.assertEqual(stored[0].read_bytes(), b"audio-bytes")

        self.assertEqual(len(injected), 1)
        hint = injected[0]
        self.assertIn("[用户发来一条语音消息 (时长 4.2s)]", hint)
        self.assertIn("转写: 今天真的太开心啦，哈哈！", hint)
        self.assertIn("情绪: HAPPY", hint)
        self.assertIn("声音事件: Laughter", hint)
        self.assertIn(f"本地路径: {stored[0]}", hint)

        history = [json.loads(line) for line in (Path(self.tmp.name) / "xiaoke.jsonl").read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["metadata"]["emotion"], "HAPPY")

    def test_xiaoke_asr_failure_still_delivers(self) -> None:
        h = self.handler(b"audio-bytes", "contact_id=xiaoke&filename=voice.m4a")
        injected: list[str] = []
        h._channel_transport_enabled_for = lambda contact_id: False
        h._inject_to_session = lambda session, text, source=None, sender=None: (
            injected.append(text) or (True, "")
        )
        with patch.object(
            voice_message, "transcribe_voice_audio",
            side_effect=voice_message.VoiceAsrError("siliconflow_http_500"),
        ), patch.object(voice_message, "voice_message_api_key", return_value="sk-test"):
            h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["asr"], "failed")
        self.assertEqual(payload["record"]["text"], "[语音消息] 语音识别失败")
        self.assertEqual(payload["record"]["metadata"]["asr"], "failed")
        self.assertIn("语音识别失败", injected[0])

    def test_xiaoke_inject_failure_surfaces_502_but_keeps_record(self) -> None:
        h = self.handler(b"audio-bytes", "contact_id=xiaoke&filename=voice.m4a")
        h._channel_transport_enabled_for = lambda contact_id: False
        h._inject_to_session = lambda session, text, source=None, sender=None: (False, "tmux down")
        with patch.object(voice_message, "transcribe_voice_audio", return_value=self._asr_ok()), \
             patch.object(voice_message, "voice_message_api_key", return_value="sk-test"):
            h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 502)
        self.assertFalse(payload["ok"])
        self.assertIn("record", payload)
        self.assertEqual(len(list(self.attachments.iterdir())), 1)

    def test_xiaoke_channel_transport_path(self) -> None:
        h = self.handler(b"audio-bytes", "contact_id=xiaoke&filename=voice.m4a&duration_ms=1500")
        sent: list[dict] = []
        h._channel_transport_enabled_for = lambda contact_id: True
        h._channel_message_id = lambda body, contact_id, text, quoted_ts: "mid-1"

        def fake_channel(*, message_id, contact_id, text, quoted_ts, user_record):
            sent.append({"text": text, "user_record": user_record})
            return True, "", {}

        h._send_to_channel_transport = fake_channel
        with patch.object(voice_message, "transcribe_voice_audio", return_value=self._asr_ok()), \
             patch.object(voice_message, "voice_message_api_key", return_value="sk-test"):
            h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["transport"], "channel")
        meta = sent[0]["user_record"]["metadata"]
        self.assertEqual(meta["transport"], "channel")
        self.assertEqual(meta["attachment_type"], "audio")
        self.assertTrue(meta["attachment_path"].endswith(".m4a"))

    # ── kimi pipeline ──

    def test_kimi_voice_message_delegates_with_precommitted_record(self) -> None:
        h = self.handler(b"audio-bytes", "contact_id=kimi&filename=voice.m4a&duration_ms=2000")
        captured: list[dict] = []

        def fake_kimi_send(body, contact_id):
            captured.append(body)
            h._send_json(200, {"ok": True, "record": body["_kimi_precommitted_record"]})

        h._handle_kimi_chat_send = fake_kimi_send
        with patch.object(voice_message, "transcribe_voice_audio", return_value=self._asr_ok()), \
             patch.object(voice_message, "voice_message_api_key", return_value="sk-test"):
            h._handle_chat_voice()
        status, payload = self.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(len(captured), 1)
        text = captured[0]["text"]
        self.assertIn("[用户发来一条语音消息 (时长 2.0s)]", text)
        self.assertIn("转写: 今天真的太开心啦，哈哈！", text)
        self.assertIn("情绪: HAPPY", text)
        # Kimi Web 读不到 VPS 本地文件，hint 不带本地路径。
        self.assertNotIn("本地路径", text)
        record = captured[0]["_kimi_precommitted_record"]
        self.assertEqual(record["metadata"]["type"], "voice")
        self.assertEqual(record["attachment_type"], "audio")

        history = [json.loads(line) for line in (Path(self.tmp.name) / "kimi.jsonl").read_text().splitlines()]
        self.assertEqual(len(history), 1)


class KimiBusyQueuePrecommittedTest(unittest.TestCase):
    """enqueue_busy must reuse a precommitted record instead of double-appending."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatHistory(Path(self.tmp.name) / "kimi.jsonl")
        self.precommitted = self.chat.append(
            role="user",
            text="[语音消息]",
            source="android-app:kimi",
            attachment_url="/attachments/abc.m4a",
            attachment_type="audio",
            metadata={"type": "voice", "emotion": "HAPPY"},
        )
        self.state = types.SimpleNamespace(
            contact_chats={"kimi": self.chat},
            kimi_turn_lock=threading.Lock(),
            kimi_active_turn={"user_ts": "busy"},
            kimi_prepare_token="",
            kimi_terminal_acquire_token="",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_busy_reuses_precommitted_record(self) -> None:
        handler = object.__new__(PushHandler)
        handler.state = self.state
        self.responses: list[tuple[int, dict]] = []
        handler._send_json = lambda status, payload: self.responses.append((status, payload))
        handler._kimi_link_bundle = lambda text: LinkPreviewBundle()
        handler._kimi_netease_login_card_allowed = lambda: False
        enqueued: list[dict] = []
        handler._enqueue_kimi_chat_turn = lambda item: enqueued.append(item) or 1
        queued_marks: list[dict] = []
        handler._set_chat_queued = lambda *args, **kwargs: queued_marks.append(kwargs)
        handler._kimi_chat_queue = lambda: []
        handler._chat_for_contact = lambda contact_id: self.chat

        handler._handle_kimi_web_chat_send(
            {
                "text": "[用户发来一条语音消息]\n转写: 哈哈\n情绪: HAPPY",
                "_kimi_precommitted_record": self.precommitted,
            },
            "kimi",
            web=types.SimpleNamespace(),
        )
        status, payload = self.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["record"]["ts"], self.precommitted["ts"])
        self.assertEqual(enqueued[0]["record"]["metadata"]["emotion"], "HAPPY")
        # 关键回归点：busy 入队不得二次 append 历史。
        lines = (Path(self.tmp.name) / "kimi.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
