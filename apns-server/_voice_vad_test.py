#!/usr/bin/env python3
"""Regression tests for the voice-call VAD state machine.

Run: .venv/bin/python _voice_vad_test.py

Covers the two bugs that made calls feel like they "kept dropping":

1. After ~30s of room silence the utterance timer had already blown past
   MAX_UTTERANCE_MS, so the first frame of the next thing you said instantly
   terminated the utterance and it was discarded as too short. The call went
   deaf until you repeated yourself.
2. Frames below VAD_RMS_SPEAK before the VAD fired were dropped entirely, so
   the quiet attack of the first syllable never reached the ASR.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import voice_call_ws as V  # noqa: E402


def frame(rms_level: int) -> bytes:
    """Build a 20ms int16 frame whose RMS is approximately rms_level."""
    n = V.FRAME_BYTES // 2
    return struct.pack("<%dh" % n, *([rms_level] * n))


SILENCE = frame(100)   # room tone, below VAD_RMS_SILENCE
QUIET = frame(400)     # onset of a word: audible but below VAD_RMS_SPEAK
LOUD = frame(900)      # clearly speech

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


print("test_1: long idle must not deafen the mic")
u = V.Utterance()
for _ in range((V.MAX_UTTERANCE_MS + 5000) // V.FRAME_MS):
    assert not u.feed(SILENCE), "utterance ended during pure silence"
check("idle does not accumulate total_ms", u.total_ms == 0, f"(total_ms={u.total_ms})")
# Now speak for 1s, then go quiet long enough to end the utterance.
ended = False
for _ in range(1000 // V.FRAME_MS):
    ended = u.feed(LOUD)
    assert not ended, "utterance ended mid-speech"
for _ in range((V.VAD_SILENCE_END_MS + 100) // V.FRAME_MS):
    ended = u.feed(SILENCE)
    if ended:
        break
check("speech after long idle is captured", ended, "(never ended)")
check(
    "speech survives the min-speech gate",
    u.speech_ms >= V.VAD_MIN_SPEECH_MS,
    f"(speech_ms={u.speech_ms} need>={V.VAD_MIN_SPEECH_MS})",
)

print("test_2: pre-roll keeps the onset of the first syllable")
u = V.Utterance()
for _ in range(50):
    u.feed(SILENCE)
for _ in range(5):  # 100ms of quiet onset, below the speak threshold
    u.feed(QUIET)
u.feed(LOUD)
# buf should contain pre-roll frames, not just the single LOUD frame.
check(
    "onset audio is prepended",
    len(u.buf) > V.FRAME_BYTES,
    f"(buf={len(u.buf)}B == just {len(u.buf)//V.FRAME_BYTES} frame(s))",
)
check(
    "pre-roll is bounded",
    len(u.buf) <= (V.VAD_PREROLL_FRAMES + 1) * V.FRAME_BYTES,
    f"(buf={len(u.buf)}B)",
)

print("test_3: a genuinely over-long utterance is still capped")
u = V.Utterance()
ended = False
for _ in range((V.MAX_UTTERANCE_MS + 2000) // V.FRAME_MS):
    ended = u.feed(LOUD)
    if ended:
        break
check("max utterance cap still fires", ended, "(ran forever)")

print("test_4: normal end-of-sentence detection")
u = V.Utterance()
for _ in range(800 // V.FRAME_MS):
    assert not u.feed(LOUD)
ended = False
n = 0
for _ in range((V.VAD_SILENCE_END_MS + 200) // V.FRAME_MS):
    n += 1
    ended = u.feed(SILENCE)
    if ended:
        break
check("ends after the configured silence", ended, "(never ended)")
check(
    "does not end early (mid-sentence pause tolerated)",
    n * V.FRAME_MS >= V.VAD_SILENCE_END_MS,
    f"(ended after {n * V.FRAME_MS}ms, want >={V.VAD_SILENCE_END_MS}ms)",
)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all voice VAD tests passed")
