#!/usr/bin/env python3
"""Protocol and race tests for negotiated sleep/commute voice modes."""

from __future__ import annotations

import asyncio
import json
import struct
import unittest
from unittest.mock import AsyncMock, patch

import voice_call_ws as voice


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload) -> None:
        if isinstance(payload, str):
            self.sent.append(json.loads(payload))


def pcm_frame(amplitude: int) -> bytes:
    return struct.pack("<h", amplitude) * (voice.FRAME_BYTES // 2)


class VoiceModeSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_silence_only_refreshes_bounded_preroll(self) -> None:
        session = voice.Session(FakeWs())  # type: ignore[arg-type]
        session.continuation_turn_active = True
        quiet = pcm_frame(0)
        for _ in range(voice.VAD_PREROLL_FRAMES + 20):
            session._ingest_audio_bytes(quiet)

        self.assertFalse(session.interrupt_event.is_set())
        self.assertFalse(session.continuation_speech_detected)
        self.assertEqual(len(session.continuation_preroll), voice.VAD_PREROLL_FRAMES)
        self.assertTrue(session.audio_q.empty())

    async def test_real_speech_interrupts_and_replays_preroll_in_order(self) -> None:
        session = voice.Session(FakeWs())  # type: ignore[arg-type]
        session.continuation_turn_active = True
        quiet_a = pcm_frame(1)
        quiet_b = pcm_frame(2)
        speech_a = pcm_frame(voice.VAD_RMS_SPEAK + 50)
        speech_b = pcm_frame(voice.VAD_RMS_SPEAK + 100)

        session._ingest_audio_bytes(quiet_a + quiet_b + speech_a + speech_b)
        self.assertTrue(session.interrupt_event.is_set())
        self.assertTrue(session.continuation_speech_detected)
        self.assertEqual(
            session.continuation_takeover_frames,
            [quiet_a, quiet_b, speech_a, speech_b],
        )

        session._finish_continuation_audio()
        replayed = [session.audio_q.get_nowait() for _ in range(4)]
        self.assertEqual(replayed, [quiet_a, quiet_b, speech_a, speech_b])
        self.assertFalse(session.continuation_turn_active)

    async def test_audio_during_turn_accepted_await_is_not_lost(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.started = True
        session.capabilities = frozenset({"voice_modes_v1", "turn_accepted_v1"})
        session.voice_mode = "sleep"
        accepted_entered = asyncio.Event()
        release_accepted = asyncio.Event()
        quiet = pcm_frame(3)
        speech = pcm_frame(voice.VAD_RMS_SPEAK + 75)

        async def delayed_accepted(*_args, **_kwargs) -> None:
            accepted_entered.set()
            await release_accepted.wait()

        with (
            patch.object(voice, "send_turn_accepted", side_effect=delayed_accepted),
            patch.object(voice, "handle_utterance", new=AsyncMock()),
        ):
            start = asyncio.create_task(session._start_continuation({
                "type": "continue",
                "mode": "sleep",
                "generation": 2,
            }))
            await accepted_entered.wait()
            self.assertTrue(session.continuation_turn_active)
            self.assertIsNone(session.continuation_task)
            session._ingest_audio_bytes(quiet + speech)
            self.assertTrue(session.interrupt_event.is_set())
            release_accepted.set()
            await start
            while session.continuation_turn_active:
                await asyncio.sleep(0)

        self.assertEqual(
            [session.audio_q.get_nowait(), session.audio_q.get_nowait()],
            [quiet, speech],
        )
        self.assertFalse(session.continuation_turn_active)

    async def test_old_client_cannot_enable_modes(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.started = True
        session.capabilities = frozenset()
        session.voice_mode = "conversation"
        await session._start_continuation({
            "type": "continue",
            "mode": "sleep",
            "generation": 1,
        })
        self.assertEqual(ws.sent, [])
        self.assertFalse(session.turn_active)

    async def test_mode_mismatch_and_stale_generation_fail_closed(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.started = True
        session.capabilities = frozenset({"voice_modes_v1", "turn_accepted_v1"})
        session.voice_mode = "sleep"
        session.last_continue_generation = 4

        await session._start_continuation({
            "type": "continue",
            "mode": "commute",
            "generation": 5,
        })
        await session._start_continuation({
            "type": "continue",
            "mode": "sleep",
            "generation": 4,
        })

        self.assertEqual(
            [(item["type"], item["reason"]) for item in ws.sent],
            [
                ("continue_rejected", "mode_inactive"),
                ("continue_rejected", "stale_generation"),
            ],
        )
        self.assertNotIn("turn_accepted", [item["type"] for item in ws.sent])
        self.assertFalse(session.turn_active)

    async def test_mode_change_generation_floor_rejects_old_same_mode_ticket(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.started = True
        session.capabilities = frozenset({"voice_modes_v1", "turn_accepted_v1"})
        session.voice_mode = "sleep"
        # Client bumps on sleep -> conversation and again on conversation ->
        # sleep.  A delayed ticket from the first sleep window is now below the
        # floor even though its mode string once again matches.
        session._raise_generation_floor(20)
        await session._start_continuation({
            "type": "continue",
            "mode": "sleep",
            "generation": 19,
        })
        self.assertEqual(ws.sent[-1]["reason"], "stale_generation")
        self.assertEqual(session.last_continue_generation, 20)

        # Invalid / overflow floors never poison a healthy session.
        session._raise_generation_floor(True)
        session._raise_generation_floor(2**63)
        self.assertEqual(session.last_continue_generation, 20)

    async def test_pending_speech_beats_continue(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.started = True
        session.capabilities = frozenset({"voice_modes_v1", "turn_accepted_v1"})
        session.voice_mode = "sleep"
        session.utterance.has_spoken = True

        await session._start_continuation({
            "type": "continue",
            "mode": "sleep",
            "generation": 1,
        })

        self.assertEqual(ws.sent[-1]["reason"], "speech_active")
        self.assertFalse(session.turn_active)

    async def test_valid_continue_accepts_once_and_is_mode_bound(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.started = True
        session.capabilities = frozenset({"voice_modes_v1", "turn_accepted_v1"})
        session.voice_mode = "commute"
        session.mode_epoch = 7
        release = asyncio.Event()

        async def hold_continuation(**kwargs) -> None:
            self.assertEqual(kwargs["mode"], "commute")
            self.assertEqual(kwargs["epoch"], 7)
            self.assertEqual(kwargs["generation"], 9)
            await release.wait()

        with patch.object(session, "_run_continuation", side_effect=hold_continuation):
            await session._start_continuation({
                "type": "continue",
                "mode": "commute",
                "generation": 9,
            })
            self.assertTrue(session.turn_active)
            accepted = ws.sent[-1]
            self.assertEqual(accepted["type"], "turn_accepted")
            self.assertIs(accepted["continuation"], True)
            self.assertEqual(accepted["generation"], 9)
            self.assertEqual(session.last_continue_generation, 9)

            await session._start_continuation({
                "type": "continue",
                "mode": "commute",
                "generation": 10,
            })
            self.assertEqual(ws.sent[-1]["reason"], "turn_active")
            release.set()
            assert session.continuation_task is not None
            await session.continuation_task

    async def test_mode_change_race_still_runs_terminal_producing_handler(self) -> None:
        ws = FakeWs()
        session = voice.Session(ws)  # type: ignore[arg-type]
        session.capabilities = frozenset({"voice_modes_v1", "turn_accepted_v1"})
        session.voice_mode = "conversation"
        handler = AsyncMock()
        with patch.object(voice, "handle_utterance", handler):
            await session._run_continuation(
                mode="sleep",
                epoch=1,
                generation=2,
                turn_id="accepted-turn",
            )
        handler.assert_awaited_once()

    async def test_continuation_payload_carries_private_mode_fields(self) -> None:
        with patch.object(
            voice,
            "_request_json",
            return_value={"ok": True, "record": {"ts": "private-control"}},
        ) as request:
            result = voice._send_live_contact_message(
                "xiaoke",
                "",
                "1a" * 16,
                "sleep",
                True,
            )
        self.assertEqual(result, "private-control")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["voice_mode"], "sleep")
        self.assertIs(payload["voice_continuation"], True)
        self.assertTrue(request.call_args.kwargs["internal_voice"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
