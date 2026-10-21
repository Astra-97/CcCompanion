"""Server-side guards for the voice call's leftover legacy paths.

Two independent problems are handled here, both without an APK change:

1. ``sanitize_ambience_config`` — the Android call screen hard-mutes the
   WebSocket microphone for every ambience pulse (``PcmRecorder.muted``:
   frames are dropped *and* local VAD is skipped) and only reopens it in the
   ``off_ms`` gap.  The first pulse starts ``delay_ms`` after the client's
   local "speech end" (600 ms of RMS < 500).  The server VAD needs 1200 ms of
   RMS < 300 to close an utterance, so a short ``delay_ms`` mutes the mic
   before the server ever accepts the turn, and a long ``on_ms`` turns a
   false trigger (a comma pause, a click) into a long deaf window.  On
   2026-10-21 a piano ambience with delay 600-1500 / on 6000-20000 made every
   first utterance vanish.  The config endpoint therefore clamps the timing
   to the known-good envelope no matter what lands in config.json.

2. ``LegacyCallDedupe`` — Android's call sheet still contains the old
   record-and-upload path (``MediaRecorder`` -> ``POST /voice-call/asr`` ->
   plain ``/chat/send``).  Its "开始说话" button stays enabled until the
   WebSocket is ready, so the first sentence of a call can be captured twice:
   once as a real WS voice turn (call protocol + TTS) and once as plain chat
   text, which can also race the voice turn into ``xiaoke_turn_active``.  The
   plain copy is recognisable because its text is exactly the transcript the
   server just returned from ``/voice-call/asr`` for a ``voice_call_*`` upload.
   If a WS voice turn with similar text exists for the same call, the plain
   copy is swallowed; otherwise it passes through unchanged (nothing lost).
"""

from __future__ import annotations

import collections
import difflib
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Ambience timing envelope
# ---------------------------------------------------------------------------

AMBIENCE_DEFAULTS: dict[str, float | int] = {
    "gain": 0.6,
    "delay_ms": 2000,
    "on_ms": 3000,
    "off_ms": 2000,
}

# (min, max) per field.  delay_ms >= 2000: client speech-end (600 ms) plus the
# server's 1200 ms end-of-utterance window plus a comma-pause margin.  on_ms
# <= 3000: the mic is deaf for the whole pulse.  off_ms >= 2000: the only
# window in which a falsely-armed call can still hear her.
AMBIENCE_BOUNDS: dict[str, tuple[float, float]] = {
    "gain": (0.0, 1.0),
    "delay_ms": (2000, 10000),
    "on_ms": (500, 3000),
    "off_ms": (2000, 10000),
}


def sanitize_ambience_config(raw: Any) -> tuple[dict[str, float | int], list[str]]:
    """Return ``(clean_config, adjusted_keys)`` for the four timing fields."""
    src = raw if isinstance(raw, dict) else {}
    clean: dict[str, float | int] = {}
    adjusted: list[str] = []
    for key, default in AMBIENCE_DEFAULTS.items():
        lo, hi = AMBIENCE_BOUNDS[key]
        value = src.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            if key in src:
                adjusted.append(key)
            value = default
        if value < lo:
            value = lo
            adjusted.append(key)
        elif value > hi:
            value = hi
            adjusted.append(key)
        clean[key] = float(value) if key == "gain" else int(value)
    return clean, adjusted


# ---------------------------------------------------------------------------
# Legacy PTT duplicate suppression
# ---------------------------------------------------------------------------

LEGACY_CALL_FILENAME_PREFIX = "voice_call_"
LEGACY_ASR_TTL_SEC = 90.0
# A legacy recording starts when the call sheet opens; a WS voice turn for the
# same words can never precede that.  Without a known call-open time only a
# short look-back is allowed so a previous call's turn cannot match.
CALL_OPEN_MAX_AGE_SEC = 180.0
FALLBACK_LOOKBACK_SEC = 20.0
DEFAULT_WAIT_SEC = 12.0
SIMILARITY_MIN = 0.6


def normalize_call_text(text: Any) -> str:
    return "".join(
        ch.lower()
        for ch in str(text or "")
        if not ch.isspace() and not unicodedata.category(ch).startswith(("P", "S"))
    )


def call_texts_match(a: str, b: str) -> bool:
    """Two ASR runs over the same speech: equal, contained, or close enough."""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIMILARITY_MIN


@dataclass
class LegacyClaim:
    contact_id: str
    norm: str
    anchor: float


@dataclass
class _VoiceTurn:
    at: float
    norm: str
    record: dict[str, Any] = field(default_factory=dict)


class LegacyCallDedupe:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wait_sec: float = DEFAULT_WAIT_SEC,
        step_sec: float = 0.2,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self.wait_sec = wait_sec
        self.step_sec = step_sec
        self._lock = threading.Lock()
        self._call_open: dict[str, float] = {}
        self._legacy_asr: dict[str, tuple[str, float]] = {}
        self._turns: dict[str, collections.deque[_VoiceTurn]] = {}

    def note_call_open(self, contact_id: str) -> None:
        with self._lock:
            self._call_open[contact_id] = self._clock()

    def note_legacy_asr(self, contact_id: str, filename: str, transcript: str) -> bool:
        if not str(filename or "").startswith(LEGACY_CALL_FILENAME_PREFIX):
            return False
        norm = normalize_call_text(transcript)
        if not norm:
            return False
        with self._lock:
            self._legacy_asr[contact_id] = (norm, self._clock())
        return True

    def note_voice_turn(self, contact_id: str, text: str) -> _VoiceTurn:
        turn = _VoiceTurn(at=self._clock(), norm=normalize_call_text(text))
        with self._lock:
            self._turns.setdefault(contact_id, collections.deque(maxlen=16)).append(turn)
        return turn

    def set_turn_record(self, turn: _VoiceTurn, record: dict[str, Any]) -> None:
        with self._lock:
            turn.record = dict(record or {})

    def claim_legacy_send(self, contact_id: str, text: str) -> Optional[LegacyClaim]:
        """One-shot: is this plain send the echo of a just-returned call ASR?"""
        norm = normalize_call_text(text)
        now = self._clock()
        with self._lock:
            pending = self._legacy_asr.get(contact_id)
            if not pending or not norm:
                return None
            pending_norm, asr_at = pending
            if now - asr_at > LEGACY_ASR_TTL_SEC:
                self._legacy_asr.pop(contact_id, None)
                return None
            if pending_norm != norm:
                return None
            self._legacy_asr.pop(contact_id, None)
            opened = self._call_open.get(contact_id)
            if opened is not None and 0.0 <= asr_at - opened <= CALL_OPEN_MAX_AGE_SEC:
                anchor = opened - 1.0
            else:
                anchor = asr_at - FALLBACK_LOOKBACK_SEC
            return LegacyClaim(contact_id=contact_id, norm=norm, anchor=anchor)

    def _matching_turn(self, claim: LegacyClaim) -> Optional[_VoiceTurn]:
        with self._lock:
            for turn in reversed(self._turns.get(claim.contact_id, ())):
                if turn.at < claim.anchor:
                    break
                if call_texts_match(claim.norm, turn.norm):
                    return turn
        return None

    def wait_for_voice_turn(
        self,
        claim: LegacyClaim,
        *,
        timeout: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Poll until a WS voice turn with matching text shows up.

        Returns a shallow copy of that turn's user record (``{}`` when the
        record is not written yet, e.g. the turn was refused as busy), or
        ``None`` when no matching voice turn arrived within the timeout.
        Elapsed time is measured on ``clock`` so tests can drive it.
        """
        limit = self.wait_sec if timeout is None else timeout
        deadline = self._clock() + limit
        while True:
            turn = self._matching_turn(claim)
            if turn is not None:
                # Give the voice turn a brief moment to persist its record so
                # the client can bind to a real ts; never required.
                grace_end = min(deadline, self._clock() + 2.0)
                while self._clock() < grace_end:
                    with self._lock:
                        if turn.record:
                            break
                    self._sleep(self.step_sec)
                with self._lock:
                    return dict(turn.record)
            if self._clock() >= deadline:
                return None
            self._sleep(self.step_sec)
