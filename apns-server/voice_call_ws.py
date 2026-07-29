#!/usr/bin/env python3
"""Standalone full-duplex voice-call WebSocket server.

Endpoint: ws://0.0.0.0:8765/voice-call/stream

Wire protocol — see README in the parent project. JSON text frames + binary PCM
mixed on the same WS connection. Pipeline is batch ASR/TTS with fast handoff:

    PCM in -> VAD -> utterance-end -> stackchan ASR -> AIChatManager.send_message
        -> stackchan TTS -> stream WAV back as 20ms PCM chunks
        -> watch for {"type":"interrupt"} mid-stream -> cancel cleanly

The server intentionally does NOT touch push.py — it shares the same shared
secret for auth, but everything else is a separate process on its own systemd
unit (cc-voice-ws.service) and its own port (8765).
"""

from __future__ import annotations

import asyncio
import audioop
import collections
import contextlib
import io
import json
import logging
import os
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.asyncio.server import ServerConnection, serve

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
STACKCHAN_HELPER = HERE / "stackchan_voice_call.py"
STACKCHAN_PYTHON = Path(
    os.environ.get(
        "STACKCHAN_XIAOZHI_PYTHON",
        "/root/stackchan-server-lite/main/xiaozhi-server/.venv/bin/python",
    )
)

WS_HOST = "0.0.0.0"
WS_PORT = 8765
WS_PATH = "/voice-call/stream"

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(SAMPLE_RATE * 2 * FRAME_MS / 1000)  # int16 mono 20ms = 640 bytes
MAX_TTS_CHUNK_MS = 100
MAX_TTS_CHUNK_BYTES = int(SAMPLE_RATE * 2 * MAX_TTS_CHUNK_MS / 1000)

def _env_int(name: str, default: int) -> int:
    """Read an int tuning knob from the environment.

    Every VAD / keepalive constant below is env-tunable so the call feel can be
    dialled in from the systemd unit without editing (and redeploying) code.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger_boot = logging.getLogger("voice_call_ws")
        logger_boot.warning("bad int for %s=%r, using default %d", name, raw, default)
        return default


VAD_RMS_SPEAK = _env_int("CC_VOICE_VAD_RMS_SPEAK", 500)      # >= this -> definitely speaking
VAD_RMS_SILENCE = _env_int("CC_VOICE_VAD_RMS_SILENCE", 300)  # < this -> silence candidate
# Silence after speech that ends an utterance. Was 700ms, which cut people off
# mid-sentence every time they paused to think or took a breath. The reference
# implementation we benchmarked against settled on 1800ms for exactly this
# reason; 1200ms is our compromise between "doesn't interrupt me" and "answers
# quickly".
VAD_SILENCE_END_MS = _env_int("CC_VOICE_VAD_SILENCE_END_MS", 1200)
MAX_UTTERANCE_MS = _env_int("CC_VOICE_MAX_UTTERANCE_MS", 30_000)  # safety cap
VAD_MIN_SPEECH_MS = _env_int("CC_VOICE_VAD_MIN_SPEECH_MS", 400)   # anti ASR hallucination
ASR_SINGLE_CHAR_MIN_MS = _env_int("CC_VOICE_ASR_SINGLE_CHAR_MIN_MS", 900)
# Pre-roll: how much audio *before* the VAD fires we keep and prepend to the
# utterance. Without this the quiet onset of the first syllable is thrown away
# (frames below VAD_RMS_SPEAK were simply dropped), which makes the ASR mishear
# or drop the start of every sentence.
VAD_PREROLL_MS = _env_int("CC_VOICE_VAD_PREROLL_MS", 300)
VAD_PREROLL_FRAMES = max(0, VAD_PREROLL_MS // FRAME_MS)

# WebSocket keepalive. The previous 10s/10s pair was the single biggest cause of
# dropped calls: an Android client that is busy with AudioTrack, briefly dozing,
# or on a flaky mobile link routinely needs more than 10s to turn a ping around,
# and websockets then kills the session with 1011 "keepalive ping timeout".
WS_PING_INTERVAL = _env_int("CC_VOICE_WS_PING_INTERVAL", 20)
WS_PING_TIMEOUT = _env_int("CC_VOICE_WS_PING_TIMEOUT", 60)
WS_CLOSE_TIMEOUT = _env_int("CC_VOICE_WS_CLOSE_TIMEOUT", 10)
# Hard ceiling on a single ws.send(); a stalled client must not wedge the whole
# session forever on a full TCP send buffer.
WS_SEND_TIMEOUT_SEC = float(_env_int("CC_VOICE_WS_SEND_TIMEOUT_SEC", 30))

# Anti ASR hallucination (third line of defense): classic Whisper-style
# hallucination phrases emitted on silence/noise. Matched against the
# transcript with whitespace/punctuation stripped and lowercased.
ASR_HALLUCINATION_BLACKLIST = frozenset(
    {
        "谢谢大家",
        "谢谢观看",
        "感谢观看",
        "谢谢收看",
        "感谢收看",
        "谢谢聆听",
        "感谢聆听",
        "请不吝点赞订阅转发打赏支持明镜与点点栏目",
        "优优独播剧场yoyotelevisionseriesexclusive",
        "字幕由amaraorg社群提供",
        "字幕志愿者",
        "by索兰娅",
        "明镜需要您的支持",
        "多謝觀看",
        "謝謝觀看",
        "thankyouforwatching",
        "thanksforwatching",
        "pleasesubscribe",
    }
)
# Substring markers: any transcript containing these is a subtitle-credit hallucination.
ASR_HALLUCINATION_SUBSTRINGS = ("字幕由", "amara.org")

# --- Protocol capability negotiation ------------------------------------
# The client lists what it understands in its `start` frame; we only send frame
# types it asked for. That is the whole point: an APK built before a frame type
# existed must keep seeing exactly the stream it was written against, so the
# protocol can grow without a forced app update.
SERVER_CAPABILITIES = ("thinking_frames", "app_ping", "error_severity")
# How often to remind a thinking_frames client that the LLM is still chewing.
THINKING_TICK_SEC = float(_env_int("CC_VOICE_THINKING_TICK_SEC", 5))

ASR_TIMEOUT_SEC = 90
TTS_TIMEOUT_SEC = 90
LIVE_REPLY_TIMEOUT_SEC = 180
# Halved from 1.0s: this poll is a loopback HTTP call, and the old interval added
# up to a full second of dead air to *every* turn after the reply had already
# landed.
LIVE_REPLY_POLL_INTERVAL_SEC = 0.5
PUSH_HTTP_BASE = "http://127.0.0.1:8291"

logger = logging.getLogger("voice_call_ws")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _load_auth_token() -> tuple[str, str]:
    """Return (token, source-description). Tries:

    1. env CC_VOICE_WS_TOKEN
    2. state/auth_token file
    3. state/config.json -> voice_call_ws.token  (legacy / spec hint)
    4. config.toml -> server.shared_secret  (same token as push.py)
    """
    env_tok = os.environ.get("CC_VOICE_WS_TOKEN", "").strip()
    if env_tok:
        return env_tok, "env:CC_VOICE_WS_TOKEN"

    state_token_file = STATE_DIR / "auth_token"
    if state_token_file.exists():
        try:
            tok = state_token_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok, f"file:{state_token_file}"
        except Exception:
            logger.warning("failed to read %s", state_token_file)

    cfg_json = STATE_DIR / "config.json"
    if cfg_json.exists():
        try:
            with cfg_json.open("r", encoding="utf-8") as f:
                data = json.load(f)
            tok = (((data or {}).get("voice_call_ws") or {}).get("token") or "").strip()
            if tok:
                return tok, "state/config.json:voice_call_ws.token"
        except Exception:
            logger.warning("failed to parse state/config.json")

    # Fall back to the push.py shared_secret from config.toml.
    cfg_toml = HERE / "config.toml"
    if cfg_toml.exists():
        tomllib = None
        try:
            import tomllib  # py311+
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # type: ignore
            except ModuleNotFoundError:  # pragma: no cover
                tomllib = None  # type: ignore[assignment]
        if tomllib is not None:
            try:
                with cfg_toml.open("rb") as f:
                    data = tomllib.load(f)
                tok = (((data or {}).get("server") or {}).get("shared_secret") or "").strip()
                if tok:
                    return tok, "config.toml:server.shared_secret"
            except Exception:
                logger.warning("failed to parse config.toml")

    return "", "<none>"


AUTH_TOKEN, AUTH_SOURCE = _load_auth_token()


# ---------------------------------------------------------------------------
# AI chat manager (lazy import to keep startup fast and avoid hard dependency
# during smoke tests when ai_chat config might be incomplete).
# ---------------------------------------------------------------------------

_ai_mgr: Any = None


def get_ai_manager() -> Any:
    global _ai_mgr
    if _ai_mgr is None:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from ai_chat import AIChatManager  # type: ignore

        _ai_mgr = AIChatManager(STATE_DIR)
    return _ai_mgr


def _request_json(
    path: str,
    *,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 15,
) -> dict[str, Any]:
    data = None
    headers = {"X-Auth-Token": AUTH_TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{PUSH_HTTP_BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            body = json.loads(raw) if raw.strip() else {}
            if not isinstance(body, dict):
                return {"ok": False, "error": "unexpected json response"}
            if not (200 <= int(resp.status) < 300):
                body.setdefault("ok", False)
                body.setdefault("error", f"http {resp.status}")
            return body
    except urllib.error.HTTPError as e:
        raw = e.read(4096).decode("utf-8", "replace")
        try:
            body = json.loads(raw) if raw.strip() else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        body.setdefault("ok", False)
        body.setdefault("error", f"http {e.code}: {raw[:300]}")
        return body
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _send_live_contact_message(contact_id: str, text: str) -> str:
    body = _request_json(
        "/chat/send",
        method="POST",
        payload={
            "text": text,
            "contact_id": contact_id,
            "source": "android-app:voice-call",
        },
        timeout=20,
    )
    if not body.get("ok"):
        raise RuntimeError(str(body.get("error") or "chat send failed"))
    record = body.get("record") if isinstance(body.get("record"), dict) else {}
    ts = str(record.get("ts") or "")
    if not ts:
        raise RuntimeError("chat send returned no user timestamp")
    return ts


def _read_live_contact_records(contact_id: str, since_ts: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "contact_id": contact_id,
        "since": since_ts,
        "limit": 50,
    })
    body = _request_json(f"/chat/history?{query}", timeout=10)
    records = body.get("records")
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


# Assistant records whose source is a real conversational reply. Anything else
# (memory-recall cards, leaving-check / schedule-remind pushes, ...) shares
# role=assistant in chat history but must never be spoken as the call reply.
LIVE_REPLY_SOURCES = {"cc-companion-channel", "ccc-stop-hook", "claude-code"}


async def send_live_contact_and_wait_reply(contact_id: str, text: str) -> str:
    """Send a transcript to a live CC Companion contact and wait for its next
    assistant message. Used by voice calls for xiaoke/kairos-style contacts.
    """
    loop = asyncio.get_running_loop()
    user_ts = await loop.run_in_executor(
        None, _send_live_contact_message, contact_id, text
    )
    deadline = time.monotonic() + LIVE_REPLY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        records = await loop.run_in_executor(
            None, _read_live_contact_records, contact_id, user_ts
        )
        for rec in records:
            if rec.get("role") != "assistant":
                continue
            source = str(rec.get("source") or "")
            if source and source not in LIVE_REPLY_SOURCES:
                continue
            reply_text = str(rec.get("text") or "").strip()
            if reply_text:
                return reply_text
        await asyncio.sleep(LIVE_REPLY_POLL_INTERVAL_SEC)
    raise TimeoutError(f"timed out waiting for {contact_id} reply")


# ---------------------------------------------------------------------------
# Stackchan ASR / TTS via subprocess (asyncio-friendly)
# ---------------------------------------------------------------------------


async def run_stackchan(args: list[str], timeout: float) -> tuple[bool, dict[str, Any]]:
    """Run stackchan_voice_call.py helper, return (ok, payload). Mirrors
    push.py:_run_stackchan_voice_helper but async."""
    cmd = [str(STACKCHAN_PYTHON), str(STACKCHAN_HELPER), *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(HERE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return False, {"ok": False, "error": f"spawn failed: {e}"}

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return False, {"ok": False, "error": "stackchan helper timed out"}

    stdout = (stdout_b.decode("utf-8", "replace") or "").strip()
    stderr = (stderr_b.decode("utf-8", "replace") or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except Exception:
        payload = {"ok": False, "error": stdout or stderr or "invalid helper output"}
    if proc.returncode != 0 and not payload.get("error"):
        payload["error"] = stderr or f"helper exited {proc.returncode}"
    return proc.returncode == 0 and bool(payload.get("ok")), payload


# Framed binary protocol from stackchan_voice_call.py:tts_stream.
# See that file for the layout — magic("MM") + type(1) + length(4 BE) + payload.
_TTS_FRAME_MAGIC = b"MM"
_TTS_FRAME_META = 0
_TTS_FRAME_PCM = 1
_TTS_FRAME_END = 2
_TTS_FRAME_ERROR = 3


async def _read_exact(stream: asyncio.StreamReader, n: int) -> bytes:
    """Like readexactly but returns b'' on clean EOF instead of raising."""
    try:
        return await stream.readexactly(n)
    except asyncio.IncompleteReadError as e:
        return bytes(e.partial)


async def stream_stackchan_tts(text: str, timeout: float):
    """Spawn ``stackchan_voice_call.py tts_stream`` and yield framed events.

    Yields ``("meta", dict)`` once, then ``("pcm", bytes)`` per chunk, then
    finally ``("end", None)``. On failure yields ``("error", str)`` and stops.

    The helper writes binary frames to stdout. We read length-prefixed frames
    one at a time so the first PCM chunk reaches the caller (and from there the
    WebSocket) as soon as MiniMax flushes it.
    """
    cmd = [str(STACKCHAN_PYTHON), str(STACKCHAN_HELPER), "tts_stream", "--text", text]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(HERE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        yield ("error", f"spawn failed: {e}")
        return

    stderr_buf: list[bytes] = []

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_buf.append(line)

    stderr_task = asyncio.create_task(drain_stderr())

    try:
        assert proc.stdout is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                yield ("error", "tts_stream helper timed out")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return
            try:
                header = await asyncio.wait_for(
                    _read_exact(proc.stdout, 7), timeout=remaining
                )
            except asyncio.TimeoutError:
                yield ("error", "tts_stream helper timed out")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return
            if len(header) < 7:
                # Clean EOF before END frame — treat as error.
                err = b"".join(stderr_buf).decode("utf-8", "replace").strip()
                yield ("error", err or "tts_stream helper exited unexpectedly")
                return
            if header[:2] != _TTS_FRAME_MAGIC:
                yield ("error", f"tts_stream protocol error: bad magic {header[:2]!r}")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return
            ftype = header[2]
            (length,) = struct.unpack(">I", header[3:7])
            if length > 16 * 1024 * 1024:
                yield ("error", f"tts_stream protocol error: huge frame {length}")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return
            payload = await _read_exact(proc.stdout, length) if length else b""
            if length and len(payload) != length:
                yield ("error", "tts_stream truncated payload")
                return
            if ftype == _TTS_FRAME_META:
                try:
                    yield ("meta", json.loads(payload.decode("utf-8")))
                except Exception:
                    yield ("meta", {})
            elif ftype == _TTS_FRAME_PCM:
                yield ("pcm", payload)
            elif ftype == _TTS_FRAME_END:
                yield ("end", None)
                return
            elif ftype == _TTS_FRAME_ERROR:
                try:
                    info = json.loads(payload.decode("utf-8"))
                    yield ("error", str(info.get("error") or info))
                except Exception:
                    yield ("error", payload.decode("utf-8", "replace") or "tts_stream error")
                return
            else:
                # Unknown frame type — skip but log.
                logger.warning("tts_stream unknown frame type %d", ftype)
    finally:
        with contextlib.suppress(asyncio.CancelledError):
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task
        with contextlib.suppress(ProcessLookupError):
            if proc.returncode is None:
                proc.terminate()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(proc.wait(), timeout=2)


def write_wav(path: Path, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def load_tts_audio_as_pcm16(path: Path) -> bytes:
    """Read a WAV (or MP3 via pydub) and return 16kHz int16 mono PCM bytes."""
    suffix = path.suffix.lower()
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                ch = wf.getnchannels()
                sw = wf.getsampwidth()
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
        except wave.Error:
            raw = b""
            ch = sr = sw = 0
        if raw and sw == 2 and ch == 1 and sr == SAMPLE_RATE:
            return raw
        if raw:
            # Convert sample width to 16-bit if needed.
            if sw != 2:
                raw = audioop.lin2lin(raw, sw, 2)
                sw = 2
            # Mix down to mono if needed.
            if ch == 2:
                raw = audioop.tomono(raw, sw, 0.5, 0.5)
                ch = 1
            # Resample.
            if sr != SAMPLE_RATE:
                raw, _ = audioop.ratecv(raw, sw, ch, sr, SAMPLE_RATE, None)
            return raw

    # Fallback path (mp3 / ogg / unusual wav) -> pydub.
    try:
        from pydub import AudioSegment  # type: ignore

        seg = AudioSegment.from_file(str(path))
        seg = seg.set_channels(1).set_frame_rate(SAMPLE_RATE).set_sample_width(2)
        return seg.raw_data
    except Exception as e:
        logger.warning("pydub decode failed for %s: %s", path, e)
        return b""


# ---------------------------------------------------------------------------
# Per-frame VAD
# ---------------------------------------------------------------------------


def frame_rms(frame: bytes) -> int:
    if len(frame) < 2:
        return 0
    try:
        return int(audioop.rms(frame, 2))
    except audioop.error:
        return 0


class Utterance:
    """Accumulates speech frames + tracks end-of-utterance via energy VAD."""

    def __init__(self) -> None:
        self.buf = bytearray()
        # Rolling window of pre-speech frames, prepended to the utterance when
        # the VAD finally fires so we don't clip the first syllable.
        self.preroll: collections.deque[bytes] = collections.deque(
            maxlen=VAD_PREROLL_FRAMES or 1
        )
        self.is_speaking = False
        self.has_spoken = False
        self.silence_ms = 0
        self.total_ms = 0
        self.speech_ms = 0  # active (non-silence) speech duration

    def feed(self, frame: bytes) -> bool:
        """Returns True when the utterance has just ended (caller should flush)."""
        rms = frame_rms(frame)
        # NOTE: total_ms deliberately counts *buffered* audio only, i.e. it does
        # not start ticking until speech has actually begun. It used to be
        # incremented on every frame including room silence, so after ~30s of
        # quiet in a call total_ms had already passed MAX_UTTERANCE_MS; the very
        # first frame of the next thing you said set has_spoken and instantly
        # tripped the safety cap, yielding a 20ms "utterance" that the
        # min-speech gate then discarded. The call went deaf and you had to
        # repeat yourself. See the regression test in _voice_vad_test.py.
        if self.has_spoken:
            self.total_ms += FRAME_MS

        if rms >= VAD_RMS_SPEAK:
            if not self.is_speaking:
                # Onset — flush the pre-roll so the attack of the word survives.
                for prev in self.preroll:
                    self.buf.extend(prev)
                self.total_ms += len(self.preroll) * FRAME_MS
                self.preroll.clear()
            self.is_speaking = True
            self.has_spoken = True
            self.silence_ms = 0
            self.speech_ms += FRAME_MS
            self.buf.extend(frame)
        elif self.is_speaking and rms < VAD_RMS_SILENCE:
            # In a silence run after speech started.
            self.silence_ms += FRAME_MS
            self.buf.extend(frame)
            if self.silence_ms >= VAD_SILENCE_END_MS:
                return True
        elif self.is_speaking:
            # Mid-range; still counts as speech.
            self.silence_ms = 0
            self.speech_ms += FRAME_MS
            self.buf.extend(frame)
        else:
            # Not speaking yet — keep the frame as pre-roll context only.
            if VAD_PREROLL_FRAMES:
                self.preroll.append(frame)

        if self.has_spoken and self.total_ms >= MAX_UTTERANCE_MS:
            return True
        return False

    def reset(self) -> None:
        self.buf.clear()
        self.preroll.clear()
        self.is_speaking = False
        self.has_spoken = False
        self.silence_ms = 0
        self.total_ms = 0
        self.speech_ms = 0


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


async def send_json(ws: ServerConnection, payload: dict[str, Any]) -> None:
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    except ConnectionClosed:
        raise


async def send_error(ws: ServerConnection, msg: str, *, fatal: bool = False) -> None:
    """Send an error frame tagged with whether the *call* is over.

    Historically every error frame looked identical, so the Android client tore
    the whole call down when a single utterance failed ASR. ``fatal`` separates
    "this turn didn't work, say it again" from "this session can never work".
    Old clients ignore the extra key.
    """
    await send_json(ws, {"type": "error", "msg": msg, "fatal": fatal})


@contextlib.asynccontextmanager
async def thinking_ticker(
    ws: ServerConnection,
    capabilities: frozenset,
    interrupt_event: asyncio.Event,
):
    """Emit periodic ``{"type": "thinking"}`` frames while the LLM is working.

    A Claude Code turn regularly takes tens of seconds. With no traffic in that
    window the phone cannot tell "still thinking" from "socket quietly died",
    and the user cannot tell it from "he hung up".

    Only sent to clients that advertised ``thinking_frames`` in their hello, so
    an old APK sees byte-for-byte the stream it has always seen.
    """
    if "thinking_frames" not in capabilities:
        yield
        return

    t0 = time.monotonic()

    async def _tick() -> None:
        try:
            while True:
                if interrupt_event.is_set():
                    return
                await send_json(
                    ws,
                    {
                        "type": "thinking",
                        "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    },
                )
                await asyncio.sleep(THINKING_TICK_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    task = asyncio.create_task(_tick(), name="ws-thinking")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


async def stream_tts_audio(
    ws: ServerConnection,
    pcm: bytes,
    interrupt_event: asyncio.Event,
) -> bool:
    """Push PCM in 20ms chunks. Returns True if completed, False if interrupted.

    Legacy helper kept around in case some path still wants to stream a full
    pre-rendered PCM buffer. The live-call path now uses
    :func:`stream_tts_from_helper` instead, which sends chunks as they arrive
    from MiniMax without waiting for synthesis to finish.
    """
    await send_json(ws, {"type": "tts_start", "sampleRate": SAMPLE_RATE})
    for i in range(0, len(pcm), FRAME_BYTES):
        if interrupt_event.is_set():
            return False
        chunk = pcm[i : i + FRAME_BYTES]
        try:
            await ws.send(chunk)
        except ConnectionClosed:
            return False
        # Pace ~real-time. 18ms not 20ms to keep the pipe slightly ahead.
        await asyncio.sleep(0.018)
    return True


async def stream_tts_from_helper(
    ws: ServerConnection,
    text: str,
    interrupt_event: asyncio.Event,
    *,
    timeout: float = TTS_TIMEOUT_SEC,
) -> tuple[bool, dict[str, Any]]:
    """Real-streaming TTS path.

    Spawns the stackchan helper in ``tts_stream`` mode and forwards each PCM
    chunk to the WebSocket the moment it arrives from MiniMax. Returns
    ``(completed, info)`` where ``info`` records timing / chunk counts.

    The Android side already accepts arbitrary-size binary frames and feeds
    them to ``AudioTrack`` in MODE_STREAM, so we don't need to slice into 20ms
    pieces here — passing through MiniMax's own chunking minimizes first-audio
    latency.
    """
    info: dict[str, Any] = {
        "chunks": 0,
        "bytes": 0,
        "first_chunk_ms": None,
        "total_ms": None,
        "sample_rate": SAMPLE_RATE,
        "source_sample_rate": None,
        "started": False,
    }
    t0 = time.monotonic()
    ratecv_state = None
    source_sr: Optional[int] = None
    started = False
    interrupted = False
    error: Optional[str] = None

    async for kind, data in stream_stackchan_tts(text, timeout=timeout):
        if interrupt_event.is_set():
            interrupted = True
            break
        if kind == "meta":
            source_sr = int((data or {}).get("sample_rate") or SAMPLE_RATE)
            info["source_sample_rate"] = source_sr
        elif kind == "pcm":
            if not started:
                await send_json(ws, {"type": "tts_start", "sampleRate": SAMPLE_RATE})
                started = True
                info["started"] = True
            pcm_chunk: bytes = data  # type: ignore[assignment]
            if not pcm_chunk:
                continue
            # Resample to 16kHz mono int16 if MiniMax handed us another rate.
            if source_sr and source_sr != SAMPLE_RATE:
                pcm_chunk, ratecv_state = audioop.ratecv(
                    pcm_chunk, 2, 1, source_sr, SAMPLE_RATE, ratecv_state
                )
            sent_all = True
            for offset in range(0, len(pcm_chunk), MAX_TTS_CHUNK_BYTES):
                if interrupt_event.is_set():
                    interrupted = True
                    sent_all = False
                    break
                part = pcm_chunk[offset : offset + MAX_TTS_CHUNK_BYTES]
                try:
                    # Guard the send: on a stalled client the TCP send buffer
                    # fills and a bare `await ws.send()` would hang this session
                    # forever with no diagnostic.
                    await asyncio.wait_for(ws.send(part), timeout=WS_SEND_TIMEOUT_SEC)
                except ConnectionClosed:
                    error = "ws closed"
                    sent_all = False
                    break
                except asyncio.TimeoutError:
                    logger.warning(
                        "tts send stalled >%.0fs, abandoning stream", WS_SEND_TIMEOUT_SEC
                    )
                    error = "ws send timeout"
                    sent_all = False
                    break
                if info["first_chunk_ms"] is None:
                    info["first_chunk_ms"] = int((time.monotonic() - t0) * 1000)
                info["chunks"] += 1
                info["bytes"] += len(part)
            if not sent_all:
                break
        elif kind == "end":
            break
        elif kind == "error":
            error = str(data)
            break

    info["total_ms"] = int((time.monotonic() - t0) * 1000)
    if error and not started:
        # Nothing was streamed — report the error to the caller.
        info["error"] = error
        return False, info
    if error:
        info["error"] = error
    completed = started and not interrupted and not error
    return completed, info


async def handle_utterance(
    ws: ServerConnection,
    pcm: bytes,
    interrupt_event: asyncio.Event,
    contact_id: str,
    speech_ms: int = 0,
    capabilities: frozenset = frozenset(),
) -> None:
    """ASR -> LLM -> TTS for a single utterance. Caller has already cleared
    interrupt_event."""
    if len(pcm) < FRAME_BYTES * 5:  # < ~100ms — too short
        return

    wav_path = Path(f"/tmp/voice_ws_{uuid.uuid4().hex}.wav")
    tts_path: Optional[Path] = None
    try:
        write_wav(wav_path, bytes(pcm))

        # --- ASR -------------------------------------------------------
        ok, payload = await run_stackchan(
            ["asr", "--input", str(wav_path)], timeout=ASR_TIMEOUT_SEC
        )
        if interrupt_event.is_set():
            return
        if not ok:
            # Per-turn failure, not a dead call: say it again.
            await send_error(ws, f"asr failed: {payload.get('error') or 'unknown'}")
            return
        transcript = str(payload.get("transcript") or "").strip()
        # Anti ASR hallucination (second line of defense): a single-character
        # transcript from a very short burst is almost always a ghost word
        # ("好" etc.) hallucinated from breath/reverb noise.
        core = "".join(
            ch
            for ch in transcript
            if not ch.isspace() and not unicodedata.category(ch).startswith("P")
        )
        if len(core) <= 1 and speech_ms < ASR_SINGLE_CHAR_MIN_MS:
            logger.info(
                "asr: dropped ghost transcript %r speech=%dms (<%dms)",
                transcript,
                speech_ms,
                ASR_SINGLE_CHAR_MIN_MS,
            )
            return
        # Anti ASR hallucination (third line of defense): well-known Whisper
        # hallucination phrases ("谢谢大家", subtitle credits, ...) produced
        # from silence/noise regardless of speech duration.
        core_lower = core.lower()
        transcript_lower = transcript.lower()
        if core_lower in ASR_HALLUCINATION_BLACKLIST or any(
            marker in transcript_lower for marker in ASR_HALLUCINATION_SUBSTRINGS
        ):
            logger.info("asr: dropped hallucination blacklist %r", transcript)
            return
        await send_json(ws, {"type": "asr_final", "text": transcript})
        if not transcript:
            return

        # --- LLM -------------------------------------------------------
        if interrupt_event.is_set():
            return
        # The LLM leg is the long one (a Claude Code turn is routinely 30-60s).
        # Keep a `thinking` drip going for clients that asked for it so neither
        # the phone nor the human has to guess whether the call is still alive.
        if contact_id == "ai-custom":
            try:
                mgr = get_ai_manager()
            except Exception as e:
                await send_error(ws, f"ai_chat init failed: {e}", fatal=True)
                return

            loop = asyncio.get_running_loop()
            try:
                async with thinking_ticker(ws, capabilities, interrupt_event):
                    result = await loop.run_in_executor(None, mgr.send_message, transcript)
            except Exception as e:
                await send_error(ws, f"llm failed: {e}")
                return
            if interrupt_event.is_set():
                return
            if not isinstance(result, dict) or not result.get("ok"):
                err = (result or {}).get("error") if isinstance(result, dict) else "llm error"
                await send_error(ws, f"llm: {err}")
                return
            reply_text = str(result.get("reply") or "").strip()
        elif contact_id in {"xiaoke", "kairos"}:
            try:
                async with thinking_ticker(ws, capabilities, interrupt_event):
                    reply_text = await send_live_contact_and_wait_reply(
                        contact_id, transcript
                    )
            except Exception as e:
                await send_error(ws, f"live chat: {e}")
                return
            if interrupt_event.is_set():
                return
        else:
            await send_error(
                ws, f"voice call contact not supported: {contact_id}", fatal=True
            )
            return
        await send_json(ws, {"type": "reply_text", "text": reply_text})
        if not reply_text:
            return

        # --- TTS (real streaming) --------------------------------------
        if interrupt_event.is_set():
            return
        completed, info = await stream_tts_from_helper(
            ws, reply_text, interrupt_event, timeout=TTS_TIMEOUT_SEC
        )
        if info.get("started"):
            logger.info(
                "tts_stream first_chunk=%sms total=%sms chunks=%s bytes=%s src_sr=%s",
                info.get("first_chunk_ms"),
                info.get("total_ms"),
                info.get("chunks"),
                info.get("bytes"),
                info.get("source_sample_rate"),
            )
        if completed:
            await send_json(ws, {"type": "tts_end"})
        elif interrupt_event.is_set():
            await send_json(ws, {"type": "tts_interrupted"})
        elif info.get("error"):
            await send_error(ws, f"tts: {info['error']}")
        else:
            # Started but ws closed mid-stream — nothing to report.
            pass
    finally:
        with contextlib.suppress(Exception):
            wav_path.unlink(missing_ok=True)
        if tts_path is not None:
            with contextlib.suppress(Exception):
                tts_path.unlink(missing_ok=True)


class Session:
    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.contact_id = "xiaoke"
        self.sample_rate = SAMPLE_RATE
        self.utterance = Utterance()
        self.audio_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=512)
        self.interrupt_event = asyncio.Event()
        self.end_event = asyncio.Event()
        self.current_task: Optional[asyncio.Task] = None
        self.started = False
        self.close_reason: str = "unknown"
        self.opened_at = time.monotonic()
        # Negotiated in the `start` frame; empty means "old client, send it
        # nothing it wasn't built for".
        self.capabilities: frozenset = frozenset()
        self.client_name: str = ""
        self.client_version: str = ""

    async def recv_loop(self) -> None:
        try:
            await self._recv_loop_inner()
        except ConnectionClosed as e:
            # Swallow here and record the reason. Previously this propagated out
            # of the task and, because asyncio.wait(FIRST_COMPLETED) never
            # retrieved it, showed up as a bare "Task exception was never
            # retrieved" traceback with no session context.
            rcvd = getattr(self.ws, "close_code", None)
            self.close_reason = (
                f"{type(e).__name__} code={rcvd} reason={getattr(self.ws, 'close_reason', '')!r}"
            )
            self.end_event.set()

    async def _recv_loop_inner(self) -> None:
        async for message in self.ws:
            if isinstance(message, (bytes, bytearray)):
                if not self.started:
                    continue
                data = bytes(message)
                # Slice into FRAME_BYTES chunks if client sends bigger.
                for i in range(0, len(data), FRAME_BYTES):
                    frame = data[i : i + FRAME_BYTES]
                    if len(frame) == FRAME_BYTES:
                        try:
                            self.audio_q.put_nowait(frame)
                        except asyncio.QueueFull:
                            # Drop oldest to avoid runaway memory.
                            with contextlib.suppress(asyncio.QueueEmpty):
                                self.audio_q.get_nowait()
                            with contextlib.suppress(asyncio.QueueFull):
                                self.audio_q.put_nowait(frame)
                continue

            # text frame
            try:
                msg = json.loads(message)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "start":
                sr = int(msg.get("sampleRate") or SAMPLE_RATE)
                if sr != SAMPLE_RATE:
                    await send_error(
                        self.ws, f"only {SAMPLE_RATE}Hz supported (got {sr})", fatal=True
                    )
                    self.end_event.set()
                    return
                cid = str(msg.get("contact_id") or "xiaoke").strip().lower()
                self.contact_id = cid or "xiaoke"
                raw_caps = msg.get("capabilities")
                if isinstance(raw_caps, list):
                    self.capabilities = frozenset(
                        str(c) for c in raw_caps if isinstance(c, (str, int))
                    )
                self.client_name = str(msg.get("client") or "")[:32]
                self.client_version = str(msg.get("client_version") or "")[:32]
                self.started = True
                logger.info(
                    "ws session start contact=%s sample_rate=%d client=%s/%s caps=%s "
                    "resumed=%s peer=%s",
                    self.contact_id,
                    sr,
                    self.client_name or "?",
                    self.client_version or "?",
                    sorted(self.capabilities) or "-",
                    bool(msg.get("resumed")),
                    self.ws.remote_address,
                )
                await send_json(
                    self.ws,
                    {"type": "ready", "capabilities": list(SERVER_CAPABILITIES)},
                )
            elif mtype == "ping":
                # Application-level heartbeat. The protocol-level ping/pong is
                # handled inside websockets, but that only proves the *socket*
                # is alive; this proves the event loop is still servicing this
                # session, and it gives the phone inbound traffic to measure
                # its own half-open-link watchdog against.
                await send_json(
                    self.ws, {"type": "pong", "t": msg.get("t")}
                )
            elif mtype == "interrupt":
                self.interrupt_event.set()
            elif mtype == "end":
                self.end_event.set()
                return
            else:
                # Forward compatibility in the other direction: a newer client
                # may send frames this build predates. Never close over one.
                logger.debug("ignoring unknown client frame type=%r", mtype)

    async def pipeline_loop(self) -> None:
        """Pulls audio frames, runs VAD, dispatches utterances to handle_utterance."""
        while not self.end_event.is_set():
            try:
                frame = await asyncio.wait_for(self.audio_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if frame is None:
                return

            ended = self.utterance.feed(frame)
            if not ended:
                continue

            pcm = bytes(self.utterance.buf)
            speech_ms = self.utterance.speech_ms
            self.utterance.reset()

            # Anti ASR hallucination: too little active speech means the "VAD
            # trigger" was likely a breath / reverb pop after mic reopen.
            if speech_ms < VAD_MIN_SPEECH_MS:
                logger.info(
                    "vad: dropped short utterance speech=%dms (<%dms) bytes=%d",
                    speech_ms,
                    VAD_MIN_SPEECH_MS,
                    len(pcm),
                )
                continue

            self.interrupt_event.clear()

            # Run ASR/LLM/TTS as a cancellable task so a fresh `interrupt`
            # arriving mid-stream cleanly aborts the send loop.
            self.current_task = asyncio.create_task(
                handle_utterance(
                    self.ws,
                    pcm,
                    self.interrupt_event,
                    self.contact_id,
                    speech_ms,
                    self.capabilities,
                )
            )
            try:
                await self.current_task
            except asyncio.CancelledError:
                pass
            except ConnectionClosed:
                return
            except Exception:
                logger.exception("utterance handler crashed")
            finally:
                self.current_task = None

    async def run(self) -> None:
        recv = asyncio.create_task(self.recv_loop(), name="ws-recv")
        pipe = asyncio.create_task(self.pipeline_loop(), name="ws-pipeline")
        end_waiter = asyncio.create_task(self.end_event.wait(), name="ws-end")

        done, pending = await asyncio.wait(
            {recv, pipe, end_waiter}, return_when=asyncio.FIRST_COMPLETED
        )

        # Retrieve exceptions from whatever finished so asyncio never reports
        # "Task exception was never retrieved", and so we know why we're closing.
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc is not None and self.close_reason == "unknown":
                self.close_reason = f"{t.get_name()}: {type(exc).__name__}: {exc}"
        if self.close_reason == "unknown":
            if end_waiter in done:
                self.close_reason = "client sent end"
            elif recv in done:
                self.close_reason = "client closed stream"
            elif pipe in done:
                self.close_reason = "pipeline finished"

        # Tear down.
        self.end_event.set()
        if self.current_task is not None:
            self.current_task.cancel()
        for t in pending:
            t.cancel()
        for t in pending:
            with contextlib.suppress(BaseException):
                await t


# ---------------------------------------------------------------------------
# WebSocket entrypoint
# ---------------------------------------------------------------------------


async def ws_handler(ws: ServerConnection) -> None:
    request = ws.request
    path = request.path if request is not None else ""
    # strip query string for path match
    base = path.split("?", 1)[0]
    if base != WS_PATH:
        await ws.close(code=1008, reason="bad path")
        return

    # Auth — accept the token from either an HTTP header on the handshake or
    # from a `?token=` query param (mobile clients often can't set custom WS
    # handshake headers).
    headers = request.headers if request is not None else {}
    client_tok = headers.get("X-Auth-Token") or headers.get("X-Auth") or ""
    if not client_tok and "?" in path:
        from urllib.parse import parse_qs

        qs = parse_qs(path.split("?", 1)[1])
        client_tok = (qs.get("token") or [""])[0]

    if not AUTH_TOKEN or client_tok != AUTH_TOKEN:
        # Log header presence to disambiguate "token wrong" vs "header stripped
        # by proxy" — Android sends X-Auth-Token, but some intermediaries lower-
        # case-only or drop unknown headers. We accept ?token= as a fallback.
        seen_headers = sorted(
            [k for k in headers.keys() if k.lower().startswith(("x-auth", "authorization"))]
        )
        logger.warning(
            "ws auth failed peer=%s have_token=%s auth_headers=%s ua=%r",
            ws.remote_address,
            bool(client_tok),
            seen_headers,
            headers.get("User-Agent", ""),
        )
        await ws.close(code=4401, reason="unauthorized")
        return

    logger.info(
        "ws auth ok peer=%s ua=%r",
        ws.remote_address,
        headers.get("User-Agent", "")[:80],
    )
    sess = Session(ws)
    try:
        await sess.run()
    except ConnectionClosed as e:
        sess.close_reason = f"{type(e).__name__}: {e}"
    except Exception as e:
        logger.exception("session crashed")
        sess.close_reason = f"crash: {type(e).__name__}: {e}"
    # Log *why* the call ended and how long it lasted. Without this a dropped
    # call was indistinguishable from a normal hangup in the journal.
    logger.info(
        "ws session ended peer=%s duration=%.1fs started=%s close_code=%s why=%s",
        ws.remote_address,
        time.monotonic() - sess.opened_at,
        sess.started,
        getattr(ws, "close_code", None),
        sess.close_reason,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def amain() -> None:
    if not AUTH_TOKEN:
        logger.error(
            "no auth token configured — set CC_VOICE_WS_TOKEN, state/auth_token, "
            "state/config.json:voice_call_ws.token, or config.toml:server.shared_secret"
        )
        # Still serve, but every connection will be rejected.
    logger.info("auth source: %s", AUTH_SOURCE)
    logger.info("listening on ws://%s:%d%s", WS_HOST, WS_PORT, WS_PATH)
    logger.info(
        "keepalive: ping_interval=%ss ping_timeout=%ss close_timeout=%ss send_timeout=%ss",
        WS_PING_INTERVAL,
        WS_PING_TIMEOUT,
        WS_CLOSE_TIMEOUT,
        WS_SEND_TIMEOUT_SEC,
    )
    logger.info(
        "vad: speak_rms=%d silence_rms=%d silence_end=%dms preroll=%dms "
        "min_speech=%dms max_utterance=%dms",
        VAD_RMS_SPEAK,
        VAD_RMS_SILENCE,
        VAD_SILENCE_END_MS,
        VAD_PREROLL_MS,
        VAD_MIN_SPEECH_MS,
        MAX_UTTERANCE_MS,
    )

    async with serve(
        ws_handler,
        WS_HOST,
        WS_PORT,
        max_size=2 * 1024 * 1024,
        # Keepalive tuned for phones, not datacentres. A handset that is busy
        # with AudioTrack, briefly dozing, or on a weak mobile link regularly
        # takes well over 10s to turn a ping around; the old 10s/10s pair killed
        # those calls outright with 1011 "keepalive ping timeout". We still ping
        # often enough to keep NAT/proxy state warm, but we wait a full minute
        # before declaring the phone dead.
        ping_interval=WS_PING_INTERVAL,
        ping_timeout=WS_PING_TIMEOUT,
        close_timeout=WS_CLOSE_TIMEOUT,
    ):
        await asyncio.Future()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
