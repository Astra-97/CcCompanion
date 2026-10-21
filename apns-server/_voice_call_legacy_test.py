#!/usr/bin/env python3
"""Regression tests for the call sheet's legacy PTT echo + ambience envelope.

2026-10-21: the first sentence of a call reached Claude as plain chat (no
CCC voice protocol, no TTS).  Two causes, both guarded server-side:
  * ambience timing outside the known-good envelope hard-muted the WS mic
    before the server accepted the first turn;
  * the Android call sheet's leftover MediaRecorder -> /voice-call/asr ->
    plain /chat/send path duplicated the first utterance as text.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import push
import voice_call_legacy as legacy
from voice_protocol import VOICE_INTERNAL_HEADER, VOICE_REPLY_TOKEN_FIELD

import _voice_reply_protocol_test as vrp

TOKEN = vrp.TOKEN


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def fake_dedupe(clock: FakeClock, wait_sec: float = 12.0) -> legacy.LegacyCallDedupe:
    return legacy.LegacyCallDedupe(clock=clock, sleep=clock.sleep, wait_sec=wait_sec)


class AmbienceEnvelopeTest(unittest.TestCase):
    def test_known_good_config_is_served_unchanged(self) -> None:
        cfg = {"gain": 0.6, "delay_ms": 2000, "on_ms": 3000, "off_ms": 2000}
        clean, adjusted = legacy.sanitize_ambience_config(cfg)
        self.assertEqual(clean, cfg)
        self.assertEqual(adjusted, [])

    def test_piano_configs_that_ate_the_first_sentence_are_clamped(self) -> None:
        for cfg in (
            {"gain": 0.7, "delay_ms": 1500, "on_ms": 6000, "off_ms": 3000},
            {"gain": 0.7, "delay_ms": 600, "on_ms": 20000, "off_ms": 1500},
            {"gain": 0.7, "delay_ms": 600, "on_ms": 10000, "off_ms": 1000},
        ):
            with self.subTest(cfg=cfg):
                clean, adjusted = legacy.sanitize_ambience_config(cfg)
                self.assertGreaterEqual(clean["delay_ms"], 2000)
                self.assertLessEqual(clean["on_ms"], 3000)
                self.assertGreaterEqual(clean["off_ms"], 2000)
                self.assertEqual(clean["gain"], 0.7)
                self.assertIn("on_ms", adjusted)

    def test_missing_or_garbage_fields_fall_back_to_defaults(self) -> None:
        clean, adjusted = legacy.sanitize_ambience_config(
            {"gain": "loud", "delay_ms": True, "off_ms": float("nan")}
        )
        self.assertEqual(clean, legacy.AMBIENCE_DEFAULTS)
        self.assertEqual(sorted(adjusted), ["delay_ms", "gain", "off_ms"])
        self.assertEqual(legacy.sanitize_ambience_config(None)[0], legacy.AMBIENCE_DEFAULTS)
        clean, _ = legacy.sanitize_ambience_config({"gain": 3})
        self.assertEqual(clean["gain"], 1.0)

    def test_config_endpoint_serves_clamped_timing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.json").write_text(
                json.dumps({"gain": 0.7, "delay_ms": 600, "on_ms": 10000, "off_ms": 1000}),
                encoding="utf-8",
            )
            handler = object.__new__(push.PushHandler)
            responses = []
            handler._send_json = lambda status, payload: responses.append((status, payload))
            dedupe = fake_dedupe(FakeClock())
            with patch.object(push.PushHandler, "_VOICE_AMBIENCE_DIR", Path(tmp)), \
                    patch.object(push, "_LEGACY_CALL_DEDUPE", dedupe):
                handler._handle_voice_ambience_config()
        status, body = responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(
            {k: body[k] for k in ("gain", "delay_ms", "on_ms", "off_ms")},
            {"gain": 0.7, "delay_ms": 2000, "on_ms": 3000, "off_ms": 2000},
        )
        self.assertIn("xiaoke", dedupe._call_open)


class AmbienceNoMuteCapabilityTest(unittest.TestCase):
    """2026-10-21: clients that no longer mute the mic for ambience may get
    long waiting music; everyone else keeps the old clamp."""

    LONG = {"gain": 0.6, "delay_ms": 300, "on_ms": 60000, "off_ms": 2000}

    def test_caps_query_parsing(self) -> None:
        self.assertEqual(legacy.parse_client_caps(""), frozenset())
        self.assertEqual(
            legacy.parse_client_caps("caps=ambience_no_mute_v1,x&caps=y"),
            frozenset({"ambience_no_mute_v1", "x", "y"}),
        )
        self.assertEqual(legacy.parse_client_caps("%%%"), frozenset())

    def test_relaxed_envelope_only_with_capability(self) -> None:
        clean, adjusted = legacy.sanitize_ambience_config(self.LONG, no_mute=True)
        self.assertEqual(clean, self.LONG)
        self.assertEqual(adjusted, [])
        old, adjusted = legacy.sanitize_ambience_config(self.LONG)
        self.assertEqual((old["delay_ms"], old["on_ms"]), (2000, 3000))
        self.assertEqual(sorted(adjusted), ["delay_ms", "on_ms"])
        clean, _ = legacy.sanitize_ambience_config({"delay_ms": 10, "on_ms": 999999}, no_mute=True)
        self.assertEqual((clean["delay_ms"], clean["on_ms"]), (300, 60000))

    def test_override_block_applies_to_capable_clients_only(self) -> None:
        cfg = {**legacy.AMBIENCE_DEFAULTS, "no_mute_v1": {"delay_ms": 800, "on_ms": 45000, "bogus": 1}}
        clean, adjusted = legacy.sanitize_ambience_config(cfg, no_mute=True)
        self.assertEqual((clean["delay_ms"], clean["on_ms"]), (800, 45000))
        self.assertNotIn("bogus", clean)
        self.assertEqual(adjusted, [])
        old, adjusted = legacy.sanitize_ambience_config(cfg)
        self.assertEqual(old, legacy.AMBIENCE_DEFAULTS)
        self.assertEqual(adjusted, [])

    def _serve(self, path: str, cfg: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            handler = object.__new__(push.PushHandler)
            handler.path = path
            responses = []
            handler._send_json = lambda status, payload: responses.append((status, payload))
            with patch.object(push.PushHandler, "_VOICE_AMBIENCE_DIR", Path(tmp)), \
                    patch.object(push, "_LEGACY_CALL_DEDUPE", fake_dedupe(FakeClock())):
                handler._handle_voice_ambience_config()
        self.assertEqual(responses[-1][0], 200)
        return responses[-1][1]

    def test_endpoint_echoes_mic_policy_and_relaxes_only_for_capable_client(self) -> None:
        cfg = {**legacy.AMBIENCE_DEFAULTS, "no_mute_v1": {"delay_ms": 800, "on_ms": 60000}}
        new = self._serve("/voice-call/ambience-config?caps=ambience_no_mute_v1", cfg)
        self.assertEqual(new["mic_policy"], "ambience_no_mute_v1")
        self.assertEqual((new["delay_ms"], new["on_ms"]), (800, 60000))
        old = self._serve("/voice-call/ambience-config", cfg)
        self.assertNotIn("mic_policy", old)
        self.assertEqual((old["delay_ms"], old["on_ms"]), (2000, 3000))

    def test_shipped_config_keeps_old_clients_in_the_safe_envelope(self) -> None:
        cfg = json.loads(Path(push.PushHandler._VOICE_AMBIENCE_DIR, "config.json").read_text(encoding="utf-8"))
        _, adjusted = legacy.sanitize_ambience_config(cfg)
        self.assertEqual(adjusted, [])
        _, adjusted = legacy.sanitize_ambience_config(cfg, no_mute=True)
        self.assertEqual(adjusted, [])


class LegacyDedupeUnitTest(unittest.TestCase):
    def test_only_call_sheet_uploads_are_tracked(self) -> None:
        d = fake_dedupe(FakeClock())
        self.assertFalse(d.note_legacy_asr("xiaoke", "voice.m4a", "你好"))
        self.assertIsNone(d.claim_legacy_send("xiaoke", "你好"))
        self.assertFalse(d.note_legacy_asr("xiaoke", "voice_call_1.m4a", " 。"))
        self.assertTrue(d.note_legacy_asr("xiaoke", "voice_call_1.m4a", "你好"))

    def test_claim_is_exact_text_one_shot_and_expires(self) -> None:
        clock = FakeClock()
        d = fake_dedupe(clock)
        d.note_legacy_asr("xiaoke", "voice_call_1.m4a", "Hello，Hello，能听见吗？")
        self.assertIsNone(d.claim_legacy_send("xiaoke", "别的话"))
        self.assertIsNotNone(d.claim_legacy_send("xiaoke", "Hello，Hello，能听见吗？"))
        self.assertIsNone(d.claim_legacy_send("xiaoke", "Hello，Hello，能听见吗？"))
        d.note_legacy_asr("xiaoke", "voice_call_2.m4a", "你好呀")
        clock.now += legacy.LEGACY_ASR_TTL_SEC + 1
        self.assertIsNone(d.claim_legacy_send("xiaoke", "你好呀"))

    def test_similar_ws_transcript_of_same_call_matches(self) -> None:
        clock = FakeClock()
        d = fake_dedupe(clock)
        d.note_call_open("xiaoke")
        clock.now += 4
        turn = d.note_voice_turn("xiaoke", "好啦现在让我来弹一首钢琴曲")
        d.set_turn_record(turn, {"ts": "ts-voice", "text": "好啦现在让我来弹一首钢琴曲"})
        clock.now += 2
        d.note_legacy_asr("xiaoke", "voice_call_1.m4a", "好啦，现在让我来弹一个钢琴曲。")
        claim = d.claim_legacy_send("xiaoke", "好啦，现在让我来弹一个钢琴曲。")
        self.assertEqual(d.wait_for_voice_turn(claim)["ts"], "ts-voice")

    def test_previous_call_turn_or_different_sentence_never_matches(self) -> None:
        clock = FakeClock()
        d = fake_dedupe(clock, wait_sec=3)
        d.note_voice_turn("xiaoke", "能听见吗")          # previous call
        clock.now += 10
        d.note_call_open("xiaoke")
        clock.now += 3
        d.note_voice_turn("xiaoke", "今天天气怎么样呀")   # same call, other words
        clock.now += 2
        d.note_legacy_asr("xiaoke", "voice_call_1.m4a", "能听见吗")
        claim = d.claim_legacy_send("xiaoke", "能听见吗")
        start = clock.now
        self.assertIsNone(d.wait_for_voice_turn(claim))
        self.assertGreaterEqual(clock.now - start, 3)

    def test_turn_arriving_during_wait_is_found(self) -> None:
        clock = FakeClock()
        d = fake_dedupe(clock)
        d.note_call_open("xiaoke")
        clock.now += 5
        d.note_legacy_asr("xiaoke", "voice_call_1.m4a", "你好")
        claim = d.claim_legacy_send("xiaoke", "你好")
        sleeps = []

        def sleep(seconds: float) -> None:
            clock.now += seconds
            sleeps.append(seconds)
            if len(sleeps) == 5:
                d.note_voice_turn("xiaoke", "你好")   # refused as busy: no record

        d._sleep = sleep
        self.assertEqual(d.wait_for_voice_turn(claim), {})


class PushLegacyEchoTest(unittest.TestCase):
    """Drives the real /voice-call/asr and /chat/send handlers."""

    def setUp(self) -> None:
        push.PushHandler._chat_append_dedupe_cache = {}
        self.clock = FakeClock()
        self.dedupe = fake_dedupe(self.clock, wait_sec=3)
        patcher = patch.object(push, "_LEGACY_CALL_DEDUPE", self.dedupe)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.handler, self.chat = vrp.PushVoiceProtocolTest._base_handler(vrp.PushVoiceProtocolTest())
        self.handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handler.state.attachments_dir = Path(self.tmp.name)

    def _legacy_asr(self, transcript: str, filename: str = "voice_call_1792584568267.m4a") -> None:
        audio = b"\x00" * 64
        self.handler.path = f"/voice-call/asr?contact_id=xiaoke&filename={filename}"
        self.handler.headers["Content-Length"] = str(len(audio))
        self.handler.rfile = io.BytesIO(audio)
        self.handler._clean_contact_id = lambda value: value
        self.handler._run_stackchan_voice_helper = (
            lambda *_a, **_k: (True, {"transcript": transcript})
        )
        self.handler._handle_voice_call_asr()
        self.assertEqual(self.handler.responses[-1][1]["transcript"], transcript)

    def _voice_turn(self, text: str) -> None:
        self.handler._handle_chat_send({
            "contact_id": "xiaoke",
            "text": text,
            VOICE_REPLY_TOKEN_FIELD: TOKEN,
        })

    def test_plain_echo_of_ws_voice_turn_is_swallowed(self) -> None:
        with patch.object(push.PushHandler, "_VOICE_AMBIENCE_DIR", Path(self.tmp.name)):
            self.handler._handle_voice_ambience_config()
        self.clock.now += 4
        self._voice_turn("Hello Hello 能听见吗")
        self.clock.now += 1
        self._legacy_asr("Hello，Hello，能听见吗？")
        self.handler._handle_chat_send({"contact_id": "xiaoke", "text": "Hello，Hello，能听见吗？"})

        status, body = self.handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(body["deduped"], "voice_call_legacy_duplicate")
        self.assertEqual(body["record"]["ts"], "ts-1")
        self.assertEqual(len(self.chat.appended), 1)
        self.assertEqual(len(self.handler.injected), 1)
        self.assertIn("CCC_VOICE_REPLY", self.handler.injected[0][1])

    def test_plain_echo_waits_for_late_ws_turn(self) -> None:
        """11:56 ordering: the plain copy reached /chat/send before the WS turn."""
        real = legacy.LegacyCallDedupe(wait_sec=5, step_sec=0.02)
        with patch.object(push, "_LEGACY_CALL_DEDUPE", real):
            real.note_call_open("xiaoke")
            self._legacy_asr("你好你好")
            legacy_done = threading.Event()

            def legacy_send() -> None:
                self.handler._handle_chat_send({"contact_id": "xiaoke", "text": "你好你好"})
                legacy_done.set()

            worker = threading.Thread(target=legacy_send)
            worker.start()
            self.assertFalse(legacy_done.wait(0.3))
            self._voice_turn("你好，你好")
            worker.join(5)
        self.assertTrue(legacy_done.is_set())
        deduped = [r for r in self.handler.responses if r[1].get("deduped")]
        self.assertEqual(len(deduped), 1)
        self.assertEqual([r["text"] for r in self.chat.appended], ["你好，你好"])

    def test_ws_missed_utterance_still_reaches_chat_as_plain_text(self) -> None:
        self._legacy_asr("好啦，现在让我来弹一个钢琴曲。")
        self.handler._handle_chat_send({"contact_id": "xiaoke", "text": "好啦，现在让我来弹一个钢琴曲。"})
        status, body = self.handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertNotIn("deduped", body)
        self.assertEqual(self.chat.appended[-1]["text"], "好啦，现在让我来弹一个钢琴曲。")
        self.assertNotIn("CCC_VOICE_REPLY", self.handler.injected[-1][1])

    def test_ordinary_typed_message_is_untouched(self) -> None:
        self.handler._handle_chat_send({"contact_id": "xiaoke", "text": "晚上吃什么"})
        self.assertEqual(self.handler.responses[-1][0], 200)
        self.assertNotIn("deduped", self.handler.responses[-1][1])
        self.assertEqual(self.clock.now, 1000.0)  # never waited


if __name__ == "__main__":
    unittest.main()
