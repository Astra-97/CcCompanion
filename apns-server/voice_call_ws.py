#!/usr/bin/env python3
"""Standalone turn-based voice-call WebSocket server.

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

from voice_protocol import (
    VOICE_INTERNAL_HEADER,
    VOICE_INTERNAL_TOKEN_PATH,
    VOICE_REPLY_SOURCE,
    VOICE_REPLY_TOKEN_FIELD,
    generate_voice_reply_token,
    load_or_create_voice_internal_token,
)

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
TURN_TERMINAL_SEND_TIMEOUT_SEC = float(
    _env_int("CC_VOICE_TURN_TERMINAL_SEND_TIMEOUT_SEC", 2)
)
TURN_TERMINAL_CLOSE_TIMEOUT_SEC = float(
    _env_int("CC_VOICE_TURN_TERMINAL_CLOSE_TIMEOUT_SEC", 1)
)

# TTS delivery deliberately runs only a little ahead of the phone's playback
# clock.  MiniMax can produce PCM much faster than real time; forwarding it at
# provider speed used to leave Android with 10-15 seconds queued in AudioTrack,
# so a reconnect or end-of-turn race sounded like speech was cut off.  Keep a
# small cushion for jitter without turning the handset into the backpressure
# buffer.  The first chunk is still sent immediately.
TTS_PACING_LEAD_SEC = max(
    0.0, float(_env_int("CC_VOICE_TTS_PACING_LEAD_MS", 200)) / 1000.0
)
TTS_BYTES_PER_SEC = SAMPLE_RATE * 2  # mono PCM s16le

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
SERVER_CAPABILITIES = (
    "thinking_frames",
    "app_ping",
    "error_severity",
    "turn_accepted_v1",
)
# How often to remind a thinking_frames client that the LLM is still chewing.
THINKING_TICK_SEC = float(_env_int("CC_VOICE_THINKING_TICK_SEC", 5))

ASR_TIMEOUT_SEC = 90
# A single absolute 90-second deadline used to include helper startup,
# provider generation *and* real-time delivery to the phone.  A healthy long
# reply could therefore be killed at 90 seconds.  Timeouts now describe the
# actual failure modes: no first frame, no next frame while actively reading,
# and a generous independent safety ceiling for the whole helper lifetime.
TTS_FIRST_FRAME_TIMEOUT_SEC = float(
    _env_int("CC_VOICE_TTS_FIRST_FRAME_TIMEOUT_SEC", 45)
)
TTS_FRAME_IDLE_TIMEOUT_SEC = float(
    _env_int("CC_VOICE_TTS_FRAME_IDLE_TIMEOUT_SEC", 30)
)
TTS_TOTAL_TIMEOUT_SEC = float(_env_int("CC_VOICE_TTS_TOTAL_TIMEOUT_SEC", 900))
# Compatibility alias for callers/tests that still patch the old name.  Its
# meaning is now the first-frame timeout, not an absolute stream deadline.
TTS_TIMEOUT_SEC = TTS_FIRST_FRAME_TIMEOUT_SEC
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
_background_tasks: set[asyncio.Task[Any]] = set()


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
    internal_voice: bool = False,
) -> dict[str, Any]:
    data = None
    headers = {"X-Auth-Token": AUTH_TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if internal_voice:
        headers[VOICE_INTERNAL_HEADER] = load_or_create_voice_internal_token(
            VOICE_INTERNAL_TOKEN_PATH
        )
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


def _send_live_contact_message(
    contact_id: str,
    text: str,
    voice_reply_token: str = "",
) -> str:
    payload: dict[str, Any] = {
        "text": text,
        "contact_id": contact_id,
    }
    internal_voice = contact_id == "xiaoke" and bool(voice_reply_token)
    if internal_voice:
        payload[VOICE_REPLY_TOKEN_FIELD] = voice_reply_token
    body = _request_json(
        "/chat/send",
        method="POST",
        payload=payload,
        timeout=20,
        internal_voice=internal_voice,
    )
    if not body.get("ok"):
        raise RuntimeError(str(body.get("error") or "chat send failed"))
    record = body.get("record") if isinstance(body.get("record"), dict) else {}
    ts = str(record.get("ts") or "")
    if not ts:
        raise RuntimeError("chat send returned no user timestamp")
    return ts


def _read_live_contact_records(contact_id: str, since_ts: str) -> list[dict[str, Any]]:
    params = {
        "contact_id": contact_id,
        "limit": 50,
    }
    if since_ts:
        params["since"] = since_ts
    query = urllib.parse.urlencode(params)
    body = _request_json(f"/chat/history?{query}", timeout=10)
    records = body.get("records")
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


LEGACY_LIVE_REPLY_SOURCES = {
    "cc-companion-channel",
    "ccc-stop-hook",
    "claude-code",
    "codex:kairos",
}


def _cancel_live_voice_reply(voice_reply_token: str) -> None:
    _request_json(
        "/voice-call/cancel",
        method="POST",
        payload={VOICE_REPLY_TOKEN_FIELD: voice_reply_token},
        timeout=5,
        internal_voice=True,
    )


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _background_tasks.add(task)

    def _finished(done: asyncio.Task[Any]) -> None:
        _background_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("background voice-call cleanup failed: %s", exc)

    task.add_done_callback(_finished)


def _schedule_voice_cancel(voice_reply_token: str) -> None:
    if not voice_reply_token:
        return
    _track_background_task(asyncio.create_task(
        asyncio.to_thread(_cancel_live_voice_reply, voice_reply_token)
    ))


async def _cancel_after_send(
    send_future: asyncio.Future[str],
    voice_reply_token: str,
) -> None:
    try:
        await asyncio.shield(send_future)
    except BaseException:
        # A transport exception does not prove that the server failed before
        # registering the turn.  Always issue the idempotent exact-token
        # cleanup after the blocking send has settled.
        pass
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _cancel_live_voice_reply, voice_reply_token)


async def _await_future_or_interrupt(
    future: asyncio.Future[Any],
    interrupt_event: asyncio.Event | None,
) -> tuple[bool, Any]:
    """Return ``(interrupted, result)`` without canceling blocking executor IO."""

    if interrupt_event is None:
        return False, await asyncio.shield(future)
    if interrupt_event.is_set():
        return True, None
    interrupt_wait = asyncio.create_task(interrupt_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {future, interrupt_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if interrupt_wait in done and interrupt_event.is_set():
            return True, None
        return False, future.result()
    finally:
        if not interrupt_wait.done():
            interrupt_wait.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await interrupt_wait


async def send_live_contact_and_wait_reply(
    contact_id: str,
    text: str,
    interrupt_event: asyncio.Event | None = None,
) -> str:
    """Send a transcript to a live CC Companion contact and wait for its next
    token-bound formal channel reply.

    Assistant records from the terminal stop hook, Claude transcript scraping,
    and older voice turns are deliberately ignored even if they arrive first.
    """
    if interrupt_event is not None and interrupt_event.is_set():
        return ""
    loop = asyncio.get_running_loop()
    voice_reply_token = generate_voice_reply_token() if contact_id == "xiaoke" else ""
    send_future: asyncio.Future[str] | None = None
    reply_received = False
    cancel_scheduled = False

    def schedule_exact_cancel(*, after_send: bool = False) -> None:
        """Schedule at most one server-side cancel for this generated token."""

        nonlocal cancel_scheduled
        if not voice_reply_token or cancel_scheduled or reply_received:
            return
        cancel_scheduled = True
        if after_send and send_future is not None:
            _track_background_task(
                asyncio.create_task(_cancel_after_send(send_future, voice_reply_token))
            )
        else:
            _schedule_voice_cancel(voice_reply_token)

    try:
        send_future = loop.run_in_executor(
            None, _send_live_contact_message, contact_id, text, voice_reply_token
        )
        interrupted, user_ts = await _await_future_or_interrupt(
            send_future,
            interrupt_event,
        )
        if interrupted:
            schedule_exact_cancel(after_send=not send_future.done())
            return ""
        user_ts = str(user_ts or "")
        deadline = time.monotonic() + LIVE_REPLY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if interrupt_event is not None and interrupt_event.is_set():
                schedule_exact_cancel()
                return ""
            # XiaoKe is fenced by the exact private token, so do not rely on the
            # millisecond timestamp cursor: a formal reply can share user_ts.
            since_ts = "" if contact_id == "xiaoke" else user_ts
            read_future = loop.run_in_executor(
                None, _read_live_contact_records, contact_id, since_ts
            )
            interrupted, records = await _await_future_or_interrupt(
                read_future,
                interrupt_event,
            )
            if interrupted:
                schedule_exact_cancel()
                return ""
            for rec in records:
                if rec.get("role") != "assistant":
                    continue
                source = str(rec.get("source") or "")
                if contact_id == "xiaoke":
                    if source != VOICE_REPLY_SOURCE:
                        continue
                    metadata = rec.get("metadata")
                    if not isinstance(metadata, dict):
                        continue
                    if metadata.get(VOICE_REPLY_TOKEN_FIELD) != voice_reply_token:
                        continue
                else:
                    # Kairos still uses the established App conversation path;
                    # there is no XiaoKe terminal protocol marker in that backend.
                    if source and source not in LEGACY_LIVE_REPLY_SOURCES:
                        continue
                reply_text = str(rec.get("text") or "").strip()
                if reply_text:
                    reply_received = True
                    return reply_text
            if interrupt_event is None:
                await asyncio.sleep(LIVE_REPLY_POLL_INTERVAL_SEC)
            else:
                try:
                    await asyncio.wait_for(
                        interrupt_event.wait(),
                        timeout=LIVE_REPLY_POLL_INTERVAL_SEC,
                    )
                    schedule_exact_cancel()
                    return ""
                except asyncio.TimeoutError:
                    pass
        schedule_exact_cancel()
        raise TimeoutError(f"timed out waiting for {contact_id} reply")
    except BaseException:
        # Session.run() cancels the current call task when the websocket ends.
        # The server may already have accepted the prompt, so cancellation and
        # every unexpected failure must clear that exact pending turn.  If the
        # send is still blocking, wait for it only in a retained background
        # task so websocket teardown remains immediate.
        schedule_exact_cancel(
            after_send=send_future is not None and not send_future.done()
        )
        raise
    finally:
        if not reply_received:
            schedule_exact_cancel(
                after_send=send_future is not None and not send_future.done()
            )


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


class _TtsReadInterrupted(Exception):
    """Internal control flow: the caller interrupted a blocked helper read."""


class _TtsSendInterrupted(Exception):
    """Internal control flow: the caller interrupted a blocked WS send."""


async def _read_exact_interruptibly(
    stream: asyncio.StreamReader,
    n: int,
    *,
    deadline: float,
    interrupt_event: Optional[asyncio.Event],
) -> bytes:
    """Read one frame segment against one absolute deadline.

    Header and payload callers pass the same deadline.  This prevents a slow
    producer from earning a fresh timeout merely by dribbling a header before
    stalling on its payload.
    """
    loop = asyncio.get_running_loop()
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    read_task = asyncio.create_task(_read_exact(stream, n))
    interrupt_task: Optional[asyncio.Task] = None
    waiters: set[asyncio.Task] = {read_task}
    if interrupt_event is not None:
        if interrupt_event.is_set():
            read_task.cancel()
            with contextlib.suppress(BaseException):
                await read_task
            raise _TtsReadInterrupted
        interrupt_task = asyncio.create_task(interrupt_event.wait())
        waiters.add(interrupt_task)
    try:
        done, _pending = await asyncio.wait(
            waiters,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError
        if interrupt_task is not None and interrupt_task in done:
            raise _TtsReadInterrupted
        return read_task.result()
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        for task in waiters:
            with contextlib.suppress(BaseException):
                await task


async def _send_tts_chunk_interruptibly(
    ws: ServerConnection,
    payload: bytes,
    *,
    interrupt_event: asyncio.Event,
    timeout: float,
) -> None:
    """Send one PCM frame while allowing an interrupt to win immediately."""
    if interrupt_event.is_set():
        raise _TtsSendInterrupted
    send_task = asyncio.create_task(ws.send(payload))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    waiters = {send_task, interrupt_task}
    try:
        done, _pending = await asyncio.wait(
            waiters,
            timeout=max(0.001, timeout),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError
        if interrupt_task in done:
            raise _TtsSendInterrupted
        send_task.result()
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        for task in waiters:
            with contextlib.suppress(BaseException):
                await task


async def stream_stackchan_tts(
    text: str,
    timeout: float,
    *,
    idle_timeout: float = TTS_FRAME_IDLE_TIMEOUT_SEC,
    total_timeout: float = TTS_TOTAL_TIMEOUT_SEC,
    interrupt_event: Optional[asyncio.Event] = None,
):
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
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        total_deadline = started_at + total_timeout
        first_pcm_deadline = min(total_deadline, started_at + timeout)
        saw_audio = False
        next_frame_deadline: Optional[float] = None
        while True:
            if interrupt_event is not None and interrupt_event.is_set():
                return
            now = loop.time()
            if now >= total_deadline:
                yield ("error", "tts_stream helper total timeout")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                return
            # META and unknown frames never refresh either phase.  Before the
            # first non-empty PCM, all header+payload reads share the original
            # first-PCM deadline.  Thereafter each complete valid PCM gives the
            # *next* complete frame one idle window; header and payload share it.
            if saw_audio:
                if next_frame_deadline is None:
                    next_frame_deadline = min(
                        total_deadline, loop.time() + max(0.001, idle_timeout)
                    )
                frame_deadline = next_frame_deadline
            else:
                frame_deadline = first_pcm_deadline
            try:
                header = await _read_exact_interruptibly(
                    proc.stdout,
                    7,
                    deadline=frame_deadline,
                    interrupt_event=interrupt_event,
                )
            except _TtsReadInterrupted:
                return
            except asyncio.TimeoutError:
                timed_out = "total" if loop.time() >= total_deadline else (
                    "frame idle" if saw_audio else "first frame"
                )
                yield ("error", f"tts_stream helper {timed_out} timeout")
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
            if length:
                try:
                    payload = await _read_exact_interruptibly(
                        proc.stdout,
                        length,
                        deadline=frame_deadline,
                        interrupt_event=interrupt_event,
                    )
                except _TtsReadInterrupted:
                    return
                except asyncio.TimeoutError:
                    timed_out = "total" if loop.time() >= total_deadline else (
                        "frame idle" if saw_audio else "first frame"
                    )
                    yield ("error", f"tts_stream helper {timed_out} timeout")
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    return
            else:
                payload = b""
            if length and len(payload) != length:
                yield ("error", "tts_stream truncated payload")
                return
            if ftype == _TTS_FRAME_META:
                try:
                    yield ("meta", json.loads(payload.decode("utf-8")))
                except Exception:
                    yield ("meta", {})
            elif ftype == _TTS_FRAME_PCM:
                if payload:
                    saw_audio = True
                    yield ("pcm", payload)
                    # Consumer-side playback pacing may suspend this generator;
                    # start the provider idle clock only when reading resumes.
                    next_frame_deadline = min(
                        total_deadline, loop.time() + max(0.001, idle_timeout)
                    )
                else:
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
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except (asyncio.TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=0.5)


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


class TurnTerminalDeliveryError(Exception):
    """An accepted turn could not deliver its terminal state."""


async def _force_reconnect_after_terminal_failure(
    ws: ServerConnection,
    *,
    terminal_type: str,
    turn_id: str,
) -> None:
    """Boundedly close, then abort if needed, so a muted client reconnects."""
    logger.warning(
        "voice_turn_terminal_failed type=%s turn_id=%s action=reconnect",
        terminal_type,
        turn_id or "-",
    )
    close = getattr(ws, "close", None)
    if callable(close) and getattr(ws, "close_code", None) is None:
        try:
            await asyncio.wait_for(
                close(code=1011, reason="turn terminal delivery failed"),
                timeout=max(0.05, TURN_TERMINAL_CLOSE_TIMEOUT_SEC),
            )
        except Exception:
            pass
    if getattr(ws, "close_code", None) is None:
        transport = getattr(ws, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()


async def send_turn_terminal_json(
    ws: ServerConnection,
    payload: dict[str, Any],
) -> None:
    """Deliver an accepted-turn terminal or force the socket to reconnect."""
    terminal_type = str(payload.get("type") or "unknown")
    turn_id = str(payload.get("turn_id") or "")
    try:
        await asyncio.wait_for(
            send_json(ws, payload),
            timeout=max(0.05, TURN_TERMINAL_SEND_TIMEOUT_SEC),
        )
    except Exception as exc:
        await _force_reconnect_after_terminal_failure(
            ws, terminal_type=terminal_type, turn_id=turn_id
        )
        raise TurnTerminalDeliveryError(
            f"{terminal_type} delivery failed"
        ) from exc


async def send_error(
    ws: ServerConnection,
    msg: str,
    *,
    fatal: bool = False,
    turn_id: str = "",
) -> None:
    """Send an error frame tagged with whether the *call* is over.

    Historically every error frame looked identical, so the Android client tore
    the whole call down when a single utterance failed ASR. ``fatal`` separates
    "this turn didn't work, say it again" from "this session can never work".
    Old clients ignore the extra key.
    """
    payload: dict[str, Any] = {"type": "error", "msg": msg, "fatal": fatal}
    if turn_id:
        payload["turn_id"] = turn_id
        await send_turn_terminal_json(ws, payload)
    else:
        await send_json(ws, payload)


async def send_turn_accepted(
    ws: ServerConnection,
    capabilities: frozenset,
    *,
    session_id: str,
    turn_id: str,
    speech_ms: int,
) -> bool:
    """Tell a capable client that endpointing is complete and it may mute.

    This must only be called *after* VAD has received its full trailing-silence
    window.  Sending it when speech merely starts would make a strict
    half-duplex client mute before the server can ever finish endpointing.
    """
    if "turn_accepted_v1" not in capabilities:
        return False
    await send_json(
        ws,
        {
            "type": "turn_accepted",
            "turn_id": turn_id,
            "speech_ms": max(0, int(speech_ms)),
        },
    )
    logger.info(
        "voice_turn_event=%s",
        json.dumps(
            {
                "event": "turn_accepted_sent",
                "session_id": session_id or "-",
                "turn_id": turn_id or "-",
                "speech_ms": max(0, int(speech_ms)),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )
    return True


TURN_RELEASE_REASONS = frozenset(
    {
        "audio_too_short",
        "asr_ghost_rejected",
        "asr_hallucination_rejected",
        "empty_transcript",
        "empty_reply",
        "interrupted_pre_tts",
        "tts_not_started",
        "internal_abort",
    }
)


async def send_turn_released(
    ws: ServerConnection,
    capabilities: frozenset,
    *,
    session_id: str,
    turn_id: str,
    reason_code: str,
) -> bool:
    """Release a negotiated accepted turn that produced no TTS terminal.

    ``tts_end``, ``tts_interrupted`` and ``error`` remain their own terminal
    frames.  This frame only closes silent/filtered/pre-TTS paths.  Old clients
    never receive it, and a socket already known closed is not written to.
    """
    if "turn_accepted_v1" not in capabilities:
        return False
    if getattr(ws, "close_code", None) is not None:
        return False
    safe_reason = (
        reason_code if reason_code in TURN_RELEASE_REASONS else "internal_abort"
    )
    await send_turn_terminal_json(
        ws,
        {
            "type": "turn_released",
            "turn_id": turn_id,
            "reason_code": safe_reason,
        },
    )
    logger.info(
        "voice_turn_event=%s",
        json.dumps(
            {
                "event": "turn_released_sent",
                "session_id": session_id or "-",
                "turn_id": turn_id or "-",
                "reason_code": safe_reason,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )
    return True


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


def _tts_pacing_delay(
    *,
    audio_started_at: float,
    now: float,
    bytes_sent: int,
    next_bytes: int,
    lead_sec: float,
) -> float:
    """Return how long to wait before sending the next PCM frame.

    The calculation includes ``next_bytes``, which bounds the queue after the
    send instead of merely before it.  A first frame smaller than the lead is
    immediate, preserving time-to-first-audio.
    """
    audio_after_send = (bytes_sent + next_bytes) / TTS_BYTES_PER_SEC
    playback_elapsed = max(0.0, now - audio_started_at)
    return max(0.0, audio_after_send - playback_elapsed - max(0.0, lead_sec))


def _tts_log_event(
    event: str,
    *,
    session_id: str,
    turn_id: str,
    info: dict[str, Any],
    reason: str = "",
    elapsed_ms: Optional[int] = None,
) -> None:
    """Write a machine-readable TTS lifecycle event without reply text."""
    payload: dict[str, Any] = {
        "event": event,
        "session_id": session_id or "-",
        "turn_id": turn_id or "-",
        "bytes": int(info.get("bytes") or 0),
        "frames": int(info.get("frames") or info.get("chunks") or 0),
        "elapsed_ms": int(
            (info.get("total_ms") or 0) if elapsed_ms is None else elapsed_ms
        ),
    }
    if reason:
        # Keep the journal useful without logging provider/user content.
        payload["reason"] = reason[:64]
    logger.info(
        "voice_tts_event=%s",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
    )


def _tts_error_reason(error: str) -> str:
    value = (error or "").lower()
    if "first frame timeout" in value:
        return "provider_first_frame_timeout"
    if "frame idle timeout" in value:
        return "provider_frame_idle_timeout"
    if "total timeout" in value:
        return "provider_total_timeout"
    if "send timeout" in value:
        return "ws_send_timeout"
    if "ws closed" in value:
        return "ws_closed"
    if "protocol" in value or "truncated" in value:
        return "helper_protocol"
    return "provider_or_helper"


async def send_tts_terminal_frame(
    ws: ServerConnection,
    frame_type: str,
    *,
    session_id: str,
    turn_id: str,
    info: dict[str, Any],
) -> None:
    """Send a backward-compatible terminal frame and record its lifecycle."""
    await send_turn_terminal_json(
        ws,
        {
            "type": frame_type,
            "turn_id": turn_id,
            "bytes": int(info.get("bytes") or 0),
            "frames": int(info.get("frames") or info.get("chunks") or 0),
            "elapsed_ms": int(info.get("total_ms") or 0),
        },
    )
    event = "tts_end_sent" if frame_type == "tts_end" else "tts_interrupted_sent"
    _tts_log_event(
        event, session_id=session_id, turn_id=turn_id, info=info
    )


async def stream_tts_from_helper(
    ws: ServerConnection,
    text: str,
    interrupt_event: asyncio.Event,
    *,
    timeout: float = TTS_TIMEOUT_SEC,
    idle_timeout: float = TTS_FRAME_IDLE_TIMEOUT_SEC,
    total_timeout: float = TTS_TOTAL_TIMEOUT_SEC,
    pacing_lead_sec: float = TTS_PACING_LEAD_SEC,
    session_id: str = "-",
    turn_id: str = "-",
) -> tuple[bool, dict[str, Any]]:
    """Real-streaming TTS path.

    Spawns the stackchan helper in ``tts_stream`` mode and forwards each PCM
    chunk to the WebSocket the moment it arrives from MiniMax. Returns
    ``(completed, info)`` where ``info`` records timing / chunk counts.

    The Android side accepts arbitrary-size binary frames and feeds them to
    ``AudioTrack`` in MODE_STREAM.  Provider chunks are capped and paced close
    to the playback clock: the first audio remains immediate, while the phone
    never becomes a many-seconds-deep queue for a fast provider.
    """
    info: dict[str, Any] = {
        "chunks": 0,
        "frames": 0,
        "bytes": 0,
        "first_chunk_ms": None,
        "total_ms": None,
        "sample_rate": SAMPLE_RATE,
        "source_sample_rate": None,
        "started": False,
        "last_ws_send_ms": None,
        "provider_end_ms": None,
    }
    t0 = time.monotonic()
    ratecv_state = None
    source_sr: Optional[int] = None
    started = False
    interrupted = False
    error: Optional[str] = None
    provider_ended = False
    audio_started_at: Optional[float] = None

    async for kind, data in stream_stackchan_tts(
        text,
        timeout=timeout,
        idle_timeout=idle_timeout,
        total_timeout=total_timeout,
        interrupt_event=interrupt_event,
    ):
        if interrupt_event.is_set():
            interrupted = True
            break
        if kind == "meta":
            source_sr = int((data or {}).get("sample_rate") or SAMPLE_RATE)
            info["source_sample_rate"] = source_sr
        elif kind == "pcm":
            if not started:
                try:
                    await send_json(
                        ws,
                        {
                            "type": "tts_start",
                            "sampleRate": SAMPLE_RATE,
                            "encoding": "pcm_s16le",
                            "channels": 1,
                            "sampleWidthBytes": 2,
                            "turn_id": turn_id,
                        },
                    )
                except ConnectionClosed:
                    error = "ws closed before tts_start"
                    break
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
                now = time.monotonic()
                if audio_started_at is None:
                    audio_started_at = now
                delay = _tts_pacing_delay(
                    audio_started_at=audio_started_at,
                    now=now,
                    bytes_sent=int(info["bytes"]),
                    next_bytes=len(part),
                    lead_sec=pacing_lead_sec,
                )
                if delay > 0:
                    try:
                        await asyncio.wait_for(interrupt_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    if interrupt_event.is_set():
                        interrupted = True
                        sent_all = False
                        break
                try:
                    # Guard the send: on a stalled client the TCP send buffer
                    # fills and a bare `await ws.send()` would hang this session
                    # forever.  Race it with the turn interrupt as well, so a
                    # user never waits up to the send timeout to stop playback.
                    await _send_tts_chunk_interruptibly(
                        ws,
                        part,
                        interrupt_event=interrupt_event,
                        timeout=WS_SEND_TIMEOUT_SEC,
                    )
                except _TtsSendInterrupted:
                    interrupted = True
                    sent_all = False
                    break
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
                info["frames"] += 1
                info["bytes"] += len(part)
                info["last_ws_send_ms"] = int((time.monotonic() - t0) * 1000)
            if not sent_all:
                break
        elif kind == "end":
            provider_ended = True
            info["total_ms"] = int((time.monotonic() - t0) * 1000)
            info["provider_end_ms"] = info["total_ms"]
            _tts_log_event(
                "provider_end",
                session_id=session_id,
                turn_id=turn_id,
                info=info,
            )
            break
        elif kind == "error":
            error = str(data)
            break

    if interrupt_event.is_set():
        interrupted = True
    info["total_ms"] = int((time.monotonic() - t0) * 1000)
    if info["frames"]:
        _tts_log_event(
            "last_ws_send",
            session_id=session_id,
            turn_id=turn_id,
            info=info,
            elapsed_ms=int(info.get("last_ws_send_ms") or 0),
        )
    if interrupted:
        _tts_log_event(
            "interrupted",
            session_id=session_id,
            turn_id=turn_id,
            info=info,
            reason="client_interrupt",
        )
    elif error:
        _tts_log_event(
            "error",
            session_id=session_id,
            turn_id=turn_id,
            info=info,
            reason=_tts_error_reason(error),
        )
    if error and not started:
        # Nothing was streamed — report the error to the caller.
        info["error"] = error
        return False, info
    if error:
        info["error"] = error
    completed = started and provider_ended and not interrupted and not error
    return completed, info


async def handle_utterance(
    ws: ServerConnection,
    pcm: bytes,
    interrupt_event: asyncio.Event,
    contact_id: str,
    speech_ms: int = 0,
    capabilities: frozenset = frozenset(),
    session_id: str = "-",
    turn_id: str = "-",
) -> None:
    """ASR -> LLM -> TTS for a single utterance. Caller has already cleared
    interrupt_event."""
    wav_path = Path(f"/tmp/voice_ws_{uuid.uuid4().hex}.wav")
    tts_path: Optional[Path] = None
    terminal_sent = False
    release_reason = "internal_abort"

    async def turn_error(msg: str, *, fatal: bool = False) -> None:
        nonlocal terminal_sent
        # Mark before awaiting: delivery failure already forces reconnect and
        # must not trigger a second, competing turn_released send in finally.
        terminal_sent = True
        await send_error(ws, msg, fatal=fatal, turn_id=turn_id)

    try:
        if len(pcm) < FRAME_BYTES * 5:  # < ~100ms — too short
            release_reason = "audio_too_short"
            return
        write_wav(wav_path, bytes(pcm))

        # --- ASR -------------------------------------------------------
        ok, payload = await run_stackchan(
            ["asr", "--input", str(wav_path)], timeout=ASR_TIMEOUT_SEC
        )
        if interrupt_event.is_set():
            release_reason = "interrupted_pre_tts"
            return
        if not ok:
            # Per-turn failure, not a dead call: say it again.
            await turn_error(f"asr failed: {payload.get('error') or 'unknown'}")
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
            release_reason = "asr_ghost_rejected"
            logger.info(
                "asr: dropped ghost transcript speech=%dms (<%dms)",
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
            release_reason = "asr_hallucination_rejected"
            logger.info("asr: dropped hallucination blacklist")
            return
        await send_json(
            ws, {"type": "asr_final", "text": transcript, "turn_id": turn_id}
        )
        if not transcript:
            release_reason = "empty_transcript"
            return

        # --- LLM -------------------------------------------------------
        if interrupt_event.is_set():
            release_reason = "interrupted_pre_tts"
            return
        # The LLM leg is the long one (a Claude Code turn is routinely 30-60s).
        # Keep a `thinking` drip going for clients that asked for it so neither
        # the phone nor the human has to guess whether the call is still alive.
        if contact_id == "ai-custom":
            try:
                mgr = get_ai_manager()
            except Exception as e:
                await turn_error(f"ai_chat init failed: {e}", fatal=True)
                return

            loop = asyncio.get_running_loop()
            try:
                async with thinking_ticker(ws, capabilities, interrupt_event):
                    result = await loop.run_in_executor(None, mgr.send_message, transcript)
            except Exception as e:
                await turn_error(f"llm failed: {e}")
                return
            if interrupt_event.is_set():
                release_reason = "interrupted_pre_tts"
                return
            if not isinstance(result, dict) or not result.get("ok"):
                err = (result or {}).get("error") if isinstance(result, dict) else "llm error"
                await turn_error(f"llm: {err}")
                return
            reply_text = str(result.get("reply") or "").strip()
        elif contact_id in {"xiaoke", "kairos"}:
            try:
                async with thinking_ticker(ws, capabilities, interrupt_event):
                    reply_text = await send_live_contact_and_wait_reply(
                        contact_id, transcript, interrupt_event
                    )
            except Exception as e:
                await turn_error(f"live chat: {e}")
                return
            if interrupt_event.is_set():
                release_reason = "interrupted_pre_tts"
                return
        else:
            await turn_error(
                f"voice call contact not supported: {contact_id}", fatal=True
            )
            return
        await send_json(
            ws, {"type": "reply_text", "text": reply_text, "turn_id": turn_id}
        )
        if not reply_text:
            release_reason = "empty_reply"
            return

        # --- TTS (real streaming) --------------------------------------
        if interrupt_event.is_set():
            release_reason = "interrupted_pre_tts"
            return
        completed, info = await stream_tts_from_helper(
            ws,
            reply_text,
            interrupt_event,
            timeout=TTS_FIRST_FRAME_TIMEOUT_SEC,
            idle_timeout=TTS_FRAME_IDLE_TIMEOUT_SEC,
            total_timeout=TTS_TOTAL_TIMEOUT_SEC,
            session_id=session_id,
            turn_id=turn_id,
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
            terminal_sent = True
            await send_tts_terminal_frame(
                ws,
                "tts_end",
                session_id=session_id,
                turn_id=turn_id,
                info=info,
            )
        elif interrupt_event.is_set():
            if info.get("started"):
                terminal_sent = True
                await send_tts_terminal_frame(
                    ws,
                    "tts_interrupted",
                    session_id=session_id,
                    turn_id=turn_id,
                    info=info,
                )
            else:
                # Never send tts_interrupted without a preceding tts_start;
                # Android intentionally ignores that orphan terminal.
                release_reason = "interrupted_pre_tts"
        elif info.get("error"):
            await turn_error(f"tts: {info['error']}")
        else:
            release_reason = "tts_not_started"
    finally:
        if not terminal_sent:
            await send_turn_released(
                ws,
                capabilities,
                session_id=session_id,
                turn_id=turn_id,
                reason_code=release_reason,
            )
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
        self.turn_active = False
        self.started = False
        self.close_reason: str = "unknown"
        self.opened_at = time.monotonic()
        # Random, short-lived correlation only.  It contains no contact,
        # transcript, account, device or peer information.
        self.session_id = uuid.uuid4().hex[:12]
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
                # Once VAD has accepted a turn, thinking + TTS are strictly
                # half-duplex.  New clients mute on `turn_accepted`; this
                # server-side guard also prevents late/in-flight frames (and
                # legacy-client echo) from becoming the next utterance.
                if self.turn_active:
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

            turn_id = uuid.uuid4().hex[:12]
            self.turn_active = True
            # Clear any stale interrupt from the previous turn *before* the
            # accepted frame.  A fresh interrupt racing with that send must
            # remain set; clearing afterwards used to swallow it.
            self.interrupt_event.clear()
            # Endpointing is complete here (including VAD_SILENCE_END_MS), so
            # it is now safe for a negotiated strict-half-duplex client to
            # close its mic.  Drain frames already queued behind the accepted
            # endpoint; they belong to this turn's trailing silence.
            while True:
                try:
                    self.audio_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await send_turn_accepted(
                self.ws,
                self.capabilities,
                session_id=self.session_id,
                turn_id=turn_id,
                speech_ms=speech_ms,
            )

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
                    self.session_id,
                    turn_id,
                )
            )
            try:
                await self.current_task
            except asyncio.CancelledError:
                pass
            except ConnectionClosed:
                return
            except TurnTerminalDeliveryError as exc:
                # The terminal helper has already closed/aborted the socket.
                # End this pipeline as well; continuing would leave a capable
                # Android client muted with no matching terminal frame.
                self.close_reason = str(exc)
                self.end_event.set()
                return
            except Exception:
                logger.exception("utterance handler crashed")
            finally:
                self.current_task = None
                self.turn_active = False

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
        "tts: pacing_lead=%dms first_frame_timeout=%ss frame_idle_timeout=%ss "
        "total_timeout=%ss",
        int(TTS_PACING_LEAD_SEC * 1000),
        TTS_FIRST_FRAME_TIMEOUT_SEC,
        TTS_FRAME_IDLE_TIMEOUT_SEC,
        TTS_TOTAL_TIMEOUT_SEC,
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
