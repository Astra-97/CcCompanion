#!/usr/bin/env python3
"""Regression tests for paced, observable voice-call TTS streaming."""

from __future__ import annotations

import asyncio
import json
import struct
import unittest
from unittest.mock import patch

import voice_call_ws as V


def frame(frame_type: int, payload: bytes = b"") -> bytes:
    return b"MM" + bytes([frame_type]) + struct.pack(">I", len(payload))


class ScriptedReader:
    def __init__(self, reads: list[tuple[float, bytes]]) -> None:
        self.reads = list(reads)

    async def readexactly(self, size: int) -> bytes:
        delay, value = self.reads.pop(0)
        if delay:
            await asyncio.sleep(delay)
        if len(value) != size:
            raise AssertionError(f"expected read({size}), scripted {len(value)} bytes")
        return value


class EofReader:
    async def readline(self) -> bytes:
        return b""


class FakeProcess:
    def __init__(self, reads: list[tuple[float, bytes]]) -> None:
        self.stdout = ScriptedReader(reads)
        self.stderr = EofReader()
        self.returncode = None

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, payload: object) -> None:
        if isinstance(payload, str):
            self.sent.append(json.loads(payload))
        else:
            self.sent.append(payload)


class TtsTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def collect(self, proc: FakeProcess, **kwargs):
        async def create_process(*_args, **_kwargs):
            return proc

        with patch.object(V.asyncio, "create_subprocess_exec", create_process):
            return [
                event
                async for event in V.stream_stackchan_tts("not logged", **kwargs)
            ]

    async def test_idle_timeout_refreshes_per_frame_not_from_helper_start(self) -> None:
        # Three individually healthy 12ms PCM/frame waits exceed the 20ms
        # first-audio budget cumulatively.  Once the first valid PCM arrives,
        # each subsequent complete frame gets one idle window.
        proc = FakeProcess(
            [
                (0, frame(V._TTS_FRAME_META)),
                (0.012, frame(V._TTS_FRAME_PCM, b"xx")),
                (0, b"xx"),
                (0.012, frame(V._TTS_FRAME_PCM, b"yy")),
                (0, b"yy"),
                (0.012, frame(V._TTS_FRAME_END)),
            ]
        )
        events = await self.collect(
            proc, timeout=0.02, idle_timeout=0.02, total_timeout=0.2
        )
        self.assertEqual(
            [kind for kind, _ in events], ["meta", "pcm", "pcm", "end"]
        )

    async def test_first_frame_and_frame_idle_timeouts_are_distinct(self) -> None:
        first = await self.collect(
            FakeProcess([(0.03, frame(V._TTS_FRAME_META))]),
            timeout=0.005,
            idle_timeout=0.02,
            total_timeout=0.2,
        )
        self.assertIn("first frame timeout", first[-1][1])

        idle = await self.collect(
            FakeProcess(
                [
                    (0, frame(V._TTS_FRAME_META)),
                    (0, frame(V._TTS_FRAME_PCM, b"xx")),
                    (0, b"xx"),
                    (0.03, frame(V._TTS_FRAME_END)),
                ]
            ),
            timeout=0.02,
            idle_timeout=0.005,
            total_timeout=0.2,
        )
        self.assertIn("frame idle timeout", idle[-1][1])

    async def test_meta_unknown_and_split_payload_do_not_refresh_deadline(self) -> None:
        meta_then_slow_pcm = await self.collect(
            FakeProcess(
                [
                    (0, frame(V._TTS_FRAME_META)),
                    (0.012, frame(V._TTS_FRAME_META)),
                    (0.012, frame(V._TTS_FRAME_PCM, b"xx")),
                ]
            ),
            timeout=0.02,
            idle_timeout=0.02,
            total_timeout=0.2,
        )
        self.assertIn("first frame timeout", meta_then_slow_pcm[-1][1])

        split_payload = await self.collect(
            FakeProcess(
                [
                    (0, frame(V._TTS_FRAME_PCM, b"aa")),
                    (0, b"aa"),
                    (0.003, frame(V._TTS_FRAME_PCM, b"bb")),
                    (0.004, b"bb"),
                ]
            ),
            timeout=0.02,
            idle_timeout=0.005,
            total_timeout=0.2,
        )
        self.assertIn("frame idle timeout", split_payload[-1][1])

    async def test_interrupt_cancels_blocked_helper_read_promptly(self) -> None:
        interrupt = asyncio.Event()
        proc = FakeProcess([(1.0, frame(V._TTS_FRAME_META))])

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            interrupt.set()

        trigger_task = asyncio.create_task(trigger())
        started = asyncio.get_running_loop().time()
        events = await self.collect(
            proc,
            timeout=1,
            idle_timeout=1,
            total_timeout=2,
            interrupt_event=interrupt,
        )
        elapsed = asyncio.get_running_loop().time() - started
        await trigger_task
        self.assertEqual(events, [])
        self.assertLess(elapsed, 0.15)
        self.assertIsNotNone(proc.returncode)


class TtsPacingAndProtocolTest(unittest.IsolatedAsyncioTestCase):
    def test_pacing_preserves_first_frame_and_bounds_future_audio(self) -> None:
        self.assertEqual(
            V._tts_pacing_delay(
                audio_started_at=10,
                now=10,
                bytes_sent=0,
                next_bytes=V.FRAME_BYTES,
                lead_sec=0.2,
            ),
            0,
        )
        delay = V._tts_pacing_delay(
            audio_started_at=10,
            now=10,
            bytes_sent=V.TTS_BYTES_PER_SEC // 5,
            next_bytes=V.TTS_BYTES_PER_SEC // 10,
            lead_sec=0.2,
        )
        self.assertAlmostEqual(delay, 0.1)
        self.assertEqual(
            V._tts_pacing_delay(
                audio_started_at=10,
                now=11,
                bytes_sent=V.TTS_BYTES_PER_SEC // 5,
                next_bytes=V.TTS_BYTES_PER_SEC // 10,
                lead_sec=0.2,
            ),
            0,
        )

    async def test_stream_adds_audio_metadata_and_logs_no_reply_text(self) -> None:
        secret_reply = "private spoken reply"

        async def provider(*_args, **_kwargs):
            yield ("meta", {"sample_rate": V.SAMPLE_RATE})
            yield ("pcm", b"\x00\x00" * 320)
            yield ("end", None)

        ws = FakeWs()
        with patch.object(V, "stream_stackchan_tts", provider), self.assertLogs(
            V.logger, level="INFO"
        ) as captured:
            completed, info = await V.stream_tts_from_helper(
                ws,
                secret_reply,
                asyncio.Event(),
                pacing_lead_sec=1,
                session_id="session123",
                turn_id="turn456",
            )
            await V.send_tts_terminal_frame(
                ws,
                "tts_end",
                session_id="session123",
                turn_id="turn456",
                info=info,
            )

        self.assertTrue(completed)
        self.assertEqual(info["frames"], 1)
        start = next(item for item in ws.sent if isinstance(item, dict))
        self.assertEqual(start["type"], "tts_start")
        self.assertEqual(start["turn_id"], "turn456")
        self.assertEqual(start["encoding"], "pcm_s16le")
        terminal = ws.sent[-1]
        self.assertEqual(terminal["type"], "tts_end")
        self.assertEqual(terminal["frames"], 1)
        joined = "\n".join(captured.output)
        self.assertIn('"event":"provider_end"', joined)
        self.assertIn('"event":"last_ws_send"', joined)
        self.assertIn('"event":"tts_end_sent"', joined)
        self.assertNotIn(secret_reply, joined)

    async def test_interrupt_cancels_blocked_websocket_send_promptly(self) -> None:
        class BlockingWs(FakeWs):
            async def send(self, payload: object) -> None:
                if isinstance(payload, (bytes, bytearray)):
                    await asyncio.sleep(1)
                else:
                    await super().send(payload)

        async def provider(*_args, **_kwargs):
            yield ("meta", {"sample_rate": V.SAMPLE_RATE})
            yield ("pcm", b"\x00" * V.FRAME_BYTES)
            yield ("end", None)

        interrupt = asyncio.Event()

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            interrupt.set()

        trigger_task = asyncio.create_task(trigger())
        started = asyncio.get_running_loop().time()
        with patch.object(V, "stream_stackchan_tts", provider):
            completed, info = await V.stream_tts_from_helper(
                BlockingWs(),
                "not logged",
                interrupt,
                pacing_lead_sec=1,
                session_id="session123",
                turn_id="turn456",
            )
        elapsed = asyncio.get_running_loop().time() - started
        await trigger_task
        self.assertFalse(completed)
        self.assertTrue(info["started"])
        self.assertLess(elapsed, 0.15)

    async def test_turn_release_timeout_forces_reconnect(self) -> None:
        class BlockingWs(FakeWs):
            def __init__(self) -> None:
                super().__init__()
                self.close_code = None
                self.closed_with: tuple[int, str] | None = None

            async def send(self, _payload: object) -> None:
                await asyncio.sleep(1)

            async def close(self, *, code: int, reason: str) -> None:
                self.closed_with = (code, reason)
                self.close_code = code

        ws = BlockingWs()
        started = asyncio.get_running_loop().time()
        with patch.object(V, "TURN_TERMINAL_SEND_TIMEOUT_SEC", 0.01):
            with self.assertRaises(V.TurnTerminalDeliveryError):
                await V.send_turn_released(
                    ws,
                    frozenset({"turn_accepted_v1"}),
                    session_id="session123",
                    turn_id="turn456",
                    reason_code="empty_reply",
                )
        self.assertEqual(ws.closed_with, (1011, "turn terminal delivery failed"))
        self.assertLess(asyncio.get_running_loop().time() - started, 0.15)

    async def test_tts_and_error_delivery_failures_also_force_reconnect(self) -> None:
        class BlockingWs(FakeWs):
            def __init__(self) -> None:
                super().__init__()
                self.close_code = None
                self.closed = False

            async def send(self, _payload: object) -> None:
                await asyncio.sleep(1)

            async def close(self, *, code: int, reason: str) -> None:
                self.closed = code == 1011 and reason == "turn terminal delivery failed"
                self.close_code = code

        info = {"bytes": 640, "frames": 1, "total_ms": 20}
        for terminal_type in ("tts_end", "tts_interrupted", "error"):
            with self.subTest(terminal_type=terminal_type):
                ws = BlockingWs()
                with patch.object(V, "TURN_TERMINAL_SEND_TIMEOUT_SEC", 0.01):
                    with self.assertRaises(V.TurnTerminalDeliveryError):
                        if terminal_type == "error":
                            await V.send_error(
                                ws,
                                "per-turn failure",
                                fatal=False,
                                turn_id="turn456",
                            )
                        else:
                            await V.send_tts_terminal_frame(
                                ws,
                                terminal_type,
                                session_id="session123",
                                turn_id="turn456",
                                info=info,
                            )
                self.assertTrue(ws.closed)

    async def test_turn_accepted_is_capability_gated_and_anonymous(self) -> None:
        old_ws = FakeWs()
        sent = await V.send_turn_accepted(
            old_ws,
            frozenset(),
            session_id="session123",
            turn_id="turn456",
            speech_ms=900,
        )
        self.assertFalse(sent)
        self.assertEqual(old_ws.sent, [])

        new_ws = FakeWs()
        with self.assertLogs(V.logger, level="INFO") as captured:
            sent = await V.send_turn_accepted(
                new_ws,
                frozenset({"turn_accepted_v1"}),
                session_id="session123",
                turn_id="turn456",
                speech_ms=900,
            )
        self.assertTrue(sent)
        self.assertEqual(new_ws.sent[0]["type"], "turn_accepted")
        self.assertEqual(new_ws.sent[0]["turn_id"], "turn456")
        self.assertNotIn("text", new_ws.sent[0])
        self.assertIn('"event":"turn_accepted_sent"', captured.output[0])

        released = await V.send_turn_released(
            old_ws,
            frozenset(),
            session_id="session123",
            turn_id="turn456",
            reason_code="empty_reply",
        )
        self.assertFalse(released)
        self.assertEqual(old_ws.sent, [])

    async def test_pipeline_sends_turn_accepted_only_after_vad_endpoint(self) -> None:
        class EndedUtterance:
            buf = bytearray(V.FRAME_BYTES * 5)
            speech_ms = V.VAD_MIN_SPEECH_MS

            def feed(self, _frame: bytes) -> bool:
                return True

            def reset(self) -> None:
                pass

        ws = FakeWs()
        session = V.Session(ws)
        session.capabilities = frozenset({"turn_accepted_v1"})
        session.utterance = EndedUtterance()
        session.interrupt_event.set()  # stale signal from the previous turn
        await session.audio_q.put(b"\x00" * V.FRAME_BYTES)
        call_order: list[str] = []

        original_send = ws.send

        async def ordered_send(payload: object) -> None:
            if isinstance(payload, str) and json.loads(payload).get("type") == "turn_accepted":
                call_order.append("turn_accepted")
                # Fresh interrupt races with the accepted-frame send.
                session.interrupt_event.set()
            await original_send(payload)

        ws.send = ordered_send

        async def handled(*_args, **_kwargs) -> None:
            call_order.append(
                "handle_interrupted" if session.interrupt_event.is_set()
                else "handle_not_interrupted"
            )
            session.end_event.set()

        with patch.object(V, "handle_utterance", handled):
            await session.pipeline_loop()

        self.assertEqual(call_order, ["turn_accepted", "handle_interrupted"])
        self.assertFalse(session.turn_active)


class TurnLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def invoke(
        self,
        *,
        transcript: str = "正常问题",
        reply: str = "正常回答",
        speech_ms: int = 1000,
        interrupt_after_reply: bool = False,
        tts_mode: str = "normal",
        capabilities: frozenset = frozenset({"turn_accepted_v1"}),
    ) -> list[dict]:
        ws = FakeWs()
        interrupt = asyncio.Event()

        async def asr(*_args, **_kwargs):
            return True, {"transcript": transcript}

        async def live(*_args, **_kwargs):
            if interrupt_after_reply:
                interrupt.set()
            return reply

        async def tts(fake_ws, *_args, **_kwargs):
            info = {
                "started": tts_mode != "interrupt_before_start",
                "bytes": 640,
                "frames": 1,
                "total_ms": 20,
            }
            if tts_mode == "interrupt_before_start":
                interrupt.set()
                return False, info
            await V.send_json(
                fake_ws,
                {
                    "type": "tts_start",
                    "sampleRate": V.SAMPLE_RATE,
                    "turn_id": "turn456",
                },
            )
            if tts_mode == "error":
                info["error"] = "provider failure"
                return False, info
            return True, info

        with (
            patch.object(V, "write_wav", lambda *_args, **_kwargs: None),
            patch.object(V, "run_stackchan", asr),
            patch.object(V, "send_live_contact_and_wait_reply", live),
            patch.object(V, "stream_tts_from_helper", tts),
        ):
            await V.handle_utterance(
                ws,
                b"\x00" * (V.FRAME_BYTES * 5),
                interrupt,
                "xiaoke",
                speech_ms,
                capabilities,
                "session123",
                "turn456",
            )
        return [item for item in ws.sent if isinstance(item, dict)]

    async def test_all_silent_early_returns_release_the_accepted_turn(self) -> None:
        cases = (
            ({"transcript": "好", "speech_ms": 400}, "asr_ghost_rejected"),
            ({"transcript": "谢谢观看"}, "asr_hallucination_rejected"),
            ({"transcript": ""}, "empty_transcript"),
            ({"reply": ""}, "empty_reply"),
            ({"interrupt_after_reply": True}, "interrupted_pre_tts"),
            ({"tts_mode": "interrupt_before_start"}, "interrupted_pre_tts"),
        )
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                frames = await self.invoke(**kwargs)
                released = [f for f in frames if f.get("type") == "turn_released"]
                self.assertEqual(len(released), 1, frames)
                self.assertEqual(released[0]["turn_id"], "turn456")
                self.assertEqual(released[0]["reason_code"], reason)
                self.assertNotIn("text", released[0])
                self.assertNotIn(
                    "tts_interrupted", [f.get("type") for f in frames]
                )

    async def test_tts_and_error_terminals_do_not_double_release(self) -> None:
        normal = await self.invoke()
        self.assertIn("tts_end", [f.get("type") for f in normal])
        self.assertNotIn("turn_released", [f.get("type") for f in normal])

        tts_error = await self.invoke(tts_mode="error")
        self.assertIn("error", [f.get("type") for f in tts_error])
        self.assertNotIn("turn_released", [f.get("type") for f in tts_error])

    async def test_legacy_client_gets_no_turn_lifecycle_frames(self) -> None:
        frames = await self.invoke(reply="", capabilities=frozenset())
        types = [f.get("type") for f in frames]
        self.assertNotIn("turn_accepted", types)
        self.assertNotIn("turn_released", types)

    async def test_blocked_terminal_send_ends_pipeline_and_closes_socket(self) -> None:
        class EndedUtterance:
            buf = bytearray(V.FRAME_BYTES * 5)
            speech_ms = 1000

            def feed(self, _frame: bytes) -> bool:
                return True

            def reset(self) -> None:
                pass

        class BlockingTerminalWs(FakeWs):
            def __init__(self) -> None:
                super().__init__()
                self.close_code = None
                self.closed_with: tuple[int, str] | None = None

            async def send(self, payload: object) -> None:
                if isinstance(payload, str):
                    decoded = json.loads(payload)
                    if decoded.get("type") == "turn_released":
                        await asyncio.sleep(1)
                        return
                await super().send(payload)

            async def close(self, *, code: int, reason: str) -> None:
                self.closed_with = (code, reason)
                self.close_code = code

        async def empty_asr(*_args, **_kwargs):
            return True, {"transcript": ""}

        ws = BlockingTerminalWs()
        session = V.Session(ws)
        session.capabilities = frozenset({"turn_accepted_v1"})
        session.utterance = EndedUtterance()
        await session.audio_q.put(b"\x00" * V.FRAME_BYTES)
        with (
            patch.object(V, "write_wav", lambda *_args, **_kwargs: None),
            patch.object(V, "run_stackchan", empty_asr),
            patch.object(V, "TURN_TERMINAL_SEND_TIMEOUT_SEC", 0.01),
        ):
            await asyncio.wait_for(session.pipeline_loop(), timeout=0.2)

        self.assertTrue(session.end_event.is_set())
        self.assertIn("turn_released delivery failed", session.close_reason)
        self.assertEqual(ws.closed_with, (1011, "turn terminal delivery failed"))


if __name__ == "__main__":
    unittest.main()
