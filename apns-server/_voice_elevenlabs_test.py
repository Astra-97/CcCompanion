#!/usr/bin/env python3
"""Offline tests for ElevenLabs call voice (2026-10-21): parsing, switch, fallback.

No network: every HTTP call is patched at ``voice_elevenlabs._request``.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import stackchan_voice_call as H
import voice_elevenlabs as E
from voice_protocol import VOICE_ENGLISH_HINT, build_voice_reply_instruction

TOKEN = "a" * 32


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _frames(raw: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(raw):
        assert raw[i : i + 2] == b"MM"
        ftype = raw[i + 2]
        (length,) = struct.unpack(">I", raw[i + 3 : i + 7])
        out.append((ftype, raw[i + 7 : i + 7 + length]))
        i += 7 + length
    return out


class _Stdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


class ParsingTest(unittest.TestCase):
    def test_parse_single_and_multichannel(self) -> None:
        self.assertEqual(E.parse_stt_response({"text": "  你好 hello  "}), "你好 hello")
        self.assertEqual(
            E.parse_stt_response({"transcripts": [{"text": "a"}, {"text": ""}, {"text": "b"}]}), "a b"
        )
        self.assertEqual(E.parse_stt_response({"text": ""}), "")
        self.assertEqual(E.parse_stt_response(None), "")

    def test_align_pcm_chunks_keeps_whole_samples(self) -> None:
        out = list(E.align_pcm_chunks(iter([b"\x01", b"\x02\x03", b"", b"\x04\x05\x06"])))
        self.assertTrue(all(len(c) % 2 == 0 for c in out))
        self.assertEqual(b"".join(out), b"\x01\x02\x03\x04\x05\x06")

    def test_strip_audio_tags(self) -> None:
        self.assertEqual(E.strip_audio_tags("[softly] Hey you. [laughs] ok"), "Hey you. ok")
        self.assertEqual(E.strip_audio_tags("数组 a[0] 不动"), "数组 a[0] 不动")

    def test_tts_body_has_no_voice_settings_or_language(self) -> None:
        body = json.loads(E._tts_body("hi"))
        self.assertEqual(body, {"text": "hi", "model_id": "eleven_v4"})


class SwitchTest(unittest.TestCase):
    def test_env_off_and_flag_file_and_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key"
            key.write_text("k\n")
            flag = Path(tmp) / "off"
            env = {"CC_VOICE_ELEVENLABS_KEY_FILE": str(key), "ELEVENLABS_API_KEY": ""}
            with patch.dict(os.environ, env), patch.object(E, "DISABLED_FLAG_FILE", flag):
                self.assertTrue(E.enabled())
                with patch.dict(os.environ, {"CC_VOICE_ELEVENLABS": "off"}):
                    self.assertFalse(E.enabled())
                flag.write_text("")
                self.assertFalse(E.enabled())
                flag.unlink()
                key.unlink()
                self.assertFalse(E.enabled())


class SpeechToTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wav = Path(self.tmp.name) / "u.wav"
        self.wav.write_bytes(b"RIFFfake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_falls_back_to_scribe_v1_when_v2_rejected(self) -> None:
        seen = []

        def fake_request(path, *, data, **_kw):
            model = "scribe_v2" if b"scribe_v2" in data else "scribe_v1"
            seen.append(model)
            if model == "scribe_v2":
                raise E.ElevenLabsError("bad model", status=422)
            return _Resp(json.dumps({"text": "hello 你好"}).encode())

        with patch.object(E, "_request", fake_request):
            self.assertEqual(E.speech_to_text(self.wav), ("hello 你好", "scribe_v1"))
        self.assertEqual(seen, ["scribe_v2", "scribe_v1"])

    def test_auth_error_does_not_retry_models(self) -> None:
        calls = []

        def fake_request(*_a, **_kw):
            calls.append(1)
            raise E.ElevenLabsError("unauthorized", status=401)

        with patch.object(E, "_request", fake_request):
            with self.assertRaises(E.ElevenLabsError):
                E.speech_to_text(self.wav)
        self.assertEqual(len(calls), 1)

    def test_request_omits_language_code(self) -> None:
        bodies = []

        def fake_request(path, *, data, **_kw):
            bodies.append(data)
            return _Resp(b'{"text": "ok"}')

        with patch.object(E, "_request", fake_request):
            E.speech_to_text(self.wav)
        self.assertNotIn(b"language_code", bodies[0])
        self.assertIn(b'name="tag_audio_events"', bodies[0])


class HelperStreamFallbackTest(unittest.TestCase):
    def _run(self, stream_fn):
        out = _Stdout()
        with patch.object(E, "tts_pcm_stream", stream_fn), patch.object(sys, "stdout", out), patch.object(
            sys, "stderr", io.StringIO()
        ):
            rc = H._elevenlabs_tts_stream("hello")
        return rc, _frames(out.buffer.getvalue())

    def test_failure_before_audio_returns_none_and_writes_nothing(self) -> None:
        def boom(*_a, **_kw):
            raise E.ElevenLabsError("HTTP 500")
            yield b""  # pragma: no cover

        rc, frames = self._run(boom)
        self.assertIsNone(rc)
        self.assertEqual(frames, [])

    def test_success_emits_meta_pcm_end(self) -> None:
        rc, frames = self._run(lambda *_a, **_kw: iter([b"\x00\x01", b"\x02\x03"]))
        self.assertEqual(rc, 0)
        self.assertEqual([f[0] for f in frames], [0, 1, 1, 2])
        meta = json.loads(frames[0][1])
        self.assertEqual((meta["sample_rate"], meta["provider"]), (16000, "elevenlabs"))

    def test_mid_stream_failure_emits_error_frame(self) -> None:
        def partial(*_a, **_kw):
            yield b"\x00\x01"
            raise E.ElevenLabsError("reset")

        rc, frames = self._run(partial)
        self.assertEqual(rc, 1)
        self.assertEqual([f[0] for f in frames], [0, 1, 3])


class EnglishHintTest(unittest.TestCase):
    def test_hint_only_when_requested(self) -> None:
        self.assertNotIn(VOICE_ENGLISH_HINT, build_voice_reply_instruction(TOKEN))
        self.assertIn(VOICE_ENGLISH_HINT, build_voice_reply_instruction(TOKEN, english=True))
        self.assertIn(VOICE_ENGLISH_HINT, build_voice_reply_instruction(TOKEN, mode="sleep", english=True))


if __name__ == "__main__":
    unittest.main()
