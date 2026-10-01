"""语音消息真实时长（2026-10-01）：voice_duration.audio_duration_ms 纯函数
+ _handle_voice_push 把 duration_ms 写进 metadata/响应 的回归测试。

App 气泡此前硬编码兜底显示 "0:12"；病根是 /voice/push 的 TTS 附件 metadata
没有 duration_ms（App 解析层 ApiClient 本就支持读 metadata.duration_ms）。
"""

from __future__ import annotations

import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from push import PushHandler
import voice_duration


def _write_wav(path: Path, *, frames: int, rate: int) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"\x00\x00" * frames)


class _FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item


class AudioDurationMsTest(unittest.TestCase):
    def test_wav_duration_from_stdlib_wave(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "voice.wav"
            _write_wav(target, frames=16000, rate=16000)
            self.assertEqual(1000, voice_duration.audio_duration_ms(target))
            # mime 与后缀不符时后缀优先判定 wav，结果一致。
            self.assertEqual(1000, voice_duration.audio_duration_ms(target, "audio/wav"))

    def test_wav_duration_rounds_subsecond(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "short.wav"
            _write_wav(target, frames=12500, rate=16000)  # 781.25ms
            self.assertEqual(781, voice_duration.audio_duration_ms(target))

    def test_missing_file_returns_zero(self):
        self.assertEqual(0, voice_duration.audio_duration_ms("/nonexistent/x.wav"))

    def test_corrupt_wav_without_ffprobe_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "broken.wav"
            target.write_bytes(b"not a wav")
            with patch("shutil.which", return_value=None):
                self.assertEqual(0, voice_duration.audio_duration_ms(target))

    def test_mp3_without_ffprobe_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "voice.mp3"
            target.write_bytes(b"\xff\xfb" + b"\x00" * 100)
            with patch("shutil.which", return_value=None):
                self.assertEqual(0, voice_duration.audio_duration_ms(target, "audio/mpeg"))

    def test_mp3_duration_via_ffprobe_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "voice.mp3"
            target.write_bytes(b"\xff\xfb" + b"\x00" * 100)
            completed = types.SimpleNamespace(stdout="2.500\n")
            with patch("shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "subprocess.run", return_value=completed
            ) as run_mock:
                self.assertEqual(2500, voice_duration.audio_duration_ms(target, "audio/mpeg"))
            args = run_mock.call_args[0][0]
            self.assertEqual("ffprobe", args[0])
            self.assertIn(str(target), args)

    def test_ffprobe_garbage_output_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "voice.mp3"
            target.write_bytes(b"\xff\xfb" + b"\x00" * 100)
            completed = types.SimpleNamespace(stdout="N/A\n")
            with patch("shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "subprocess.run", return_value=completed
            ):
                self.assertEqual(0, voice_duration.audio_duration_ms(target))


class VoicePushDurationMetadataTest(unittest.TestCase):
    def _make_handler(self, attachments_dir: str):
        chat = _FakeChat()
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(attachments_dir=Path(attachments_dir))
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
        handler._chat_for_contact = lambda _contact_id: chat
        handler._send_chat_notification = lambda *_args, **_kwargs: None
        return handler, chat

    def test_voice_push_records_real_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored_name = "voice_call_test.wav"
            _write_wav(Path(tmp) / stored_name, frames=32000, rate=16000)  # 2.0s
            handler, chat = self._make_handler(tmp)
            handler._run_stackchan_voice_helper = lambda *_args, **_kwargs: (
                True,
                {"stored_name": stored_name, "mime_type": "audio/wav", "bytes": 64044},
            )

            handler._handle_voice_push({"text": "你好"})

            self.assertEqual(200, handler.responses[-1][0])
            payload = handler.responses[-1][1]
            self.assertEqual(2000, payload["duration_ms"])
            self.assertEqual(1, len(chat.records))
            metadata = chat.records[0]["metadata"]
            self.assertEqual("voice", metadata["type"])
            self.assertEqual(2000, metadata["duration_ms"])

    def test_voice_push_unmeasurable_audio_still_succeeds_with_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored_name = "voice_call_test.mp3"
            (Path(tmp) / stored_name).write_bytes(b"\xff\xfb" + b"\x00" * 100)
            handler, chat = self._make_handler(tmp)
            handler._run_stackchan_voice_helper = lambda *_args, **_kwargs: (
                True,
                {"stored_name": stored_name, "mime_type": "audio/mpeg", "bytes": 102},
            )
            # 探测失败（无 ffprobe）不许拖垮主流程：0 = 时长未知，App 端实测兜底。
            with patch("shutil.which", return_value=None):
                handler._handle_voice_push({"text": "你好"})

            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual(0, handler.responses[-1][1]["duration_ms"])
            self.assertEqual(0, chat.records[0]["metadata"]["duration_ms"])


if __name__ == "__main__":
    unittest.main()
