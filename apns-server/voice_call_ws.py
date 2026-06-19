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
import contextlib
import io
import json
import logging
import os
import struct
import sys
import time
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

VAD_RMS_SPEAK = 500          # >= this -> definitely speaking
VAD_RMS_SILENCE = 300        # < this -> silence candidate
VAD_SILENCE_END_MS = 700     # 700ms of silence after speech => utterance end
MAX_UTTERANCE_MS = 30_000    # safety cap

ASR_TIMEOUT_SEC = 90
TTS_TIMEOUT_SEC = 90

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
        self.is_speaking = False
        self.has_spoken = False
        self.silence_ms = 0
        self.total_ms = 0

    def feed(self, frame: bytes) -> bool:
        """Returns True when the utterance has just ended (caller should flush)."""
        rms = frame_rms(frame)
        self.total_ms += FRAME_MS

        if rms >= VAD_RMS_SPEAK:
            self.is_speaking = True
            self.has_spoken = True
            self.silence_ms = 0
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
            self.buf.extend(frame)

        if self.has_spoken and self.total_ms >= MAX_UTTERANCE_MS:
            return True
        return False

    def reset(self) -> None:
        self.buf.clear()
        self.is_speaking = False
        self.has_spoken = False
        self.silence_ms = 0
        self.total_ms = 0


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


async def send_json(ws: ServerConnection, payload: dict[str, Any]) -> None:
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    except ConnectionClosed:
        raise


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
                    await ws.send(part)
                except ConnectionClosed:
                    error = "ws closed"
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
            await send_json(
                ws, {"type": "error", "msg": f"asr failed: {payload.get('error') or 'unknown'}"}
            )
            return
        transcript = str(payload.get("transcript") or "").strip()
        await send_json(ws, {"type": "asr_final", "text": transcript})
        if not transcript:
            return

        # --- LLM -------------------------------------------------------
        if interrupt_event.is_set():
            return
        try:
            mgr = get_ai_manager()
        except Exception as e:
            await send_json(ws, {"type": "error", "msg": f"ai_chat init failed: {e}"})
            return

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, mgr.send_message, transcript)
        except Exception as e:
            await send_json(ws, {"type": "error", "msg": f"llm failed: {e}"})
            return
        if interrupt_event.is_set():
            return
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") if isinstance(result, dict) else "llm error"
            await send_json(ws, {"type": "error", "msg": f"llm: {err}"})
            return
        reply_text = str(result.get("reply") or "").strip()
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
            await send_json(ws, {"type": "error", "msg": f"tts: {info['error']}"})
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

    async def recv_loop(self) -> None:
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
                    await send_json(
                        self.ws,
                        {"type": "error", "msg": f"only {SAMPLE_RATE}Hz supported (got {sr})"},
                    )
                    self.end_event.set()
                    return
                cid = str(msg.get("contact_id") or "xiaoke").strip().lower()
                self.contact_id = cid or "xiaoke"
                self.started = True
                await send_json(self.ws, {"type": "ready"})
            elif mtype == "interrupt":
                self.interrupt_event.set()
            elif mtype == "end":
                self.end_event.set()
                return

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
            self.utterance.reset()
            self.interrupt_event.clear()

            # Run ASR/LLM/TTS as a cancellable task so a fresh `interrupt`
            # arriving mid-stream cleanly aborts the send loop.
            self.current_task = asyncio.create_task(
                handle_utterance(self.ws, pcm, self.interrupt_event, self.contact_id)
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
        logger.warning("ws auth failed peer=%s", ws.remote_address)
        await ws.close(code=4401, reason="unauthorized")
        return

    sess = Session(ws)
    try:
        await sess.run()
    except ConnectionClosed:
        pass
    except Exception:
        logger.exception("session crashed")


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

    async with serve(
        ws_handler,
        WS_HOST,
        WS_PORT,
        max_size=2 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
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
