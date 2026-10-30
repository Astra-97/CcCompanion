#!/usr/bin/env python3
"""ElevenLabs TTS / STT for CC Companion voice calls (2026-10-21).

Stdlib only (urllib), so it can be imported both by apns-server's venv and by
``stackchan_voice_call.py`` running under the xiaozhi venv.

- Call TTS: voice ``zJkzOPmQZx3vFhVX8zNR`` + ``eleven_v4``, no voice_settings,
  no language_code (the text decides). Streaming PCM s16le mono for live calls,
  mp3 file for the batch ``/voice-call/tts`` endpoint.
- Call ASR: Scribe batch speech-to-text, ``scribe_v2`` with ``scribe_v1`` as
  model fallback, language auto-detect.

Callers keep the old StackChan providers as fallback when anything here raises.

Switch (no code edits needed):
- env ``CC_VOICE_ELEVENLABS=0`` (also off/false/no) disables it, or
- create the flag file ``state/voice_elevenlabs.disabled`` next to this module
  (picked up by the next call, no restart needed).

The API key is read at runtime from env ``ELEVENLABS_API_KEY`` or the key file
(``CC_VOICE_ELEVENLABS_KEY_FILE``, default ``/root/.elevenlabs_key``). It is
never logged or returned.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Iterator, Optional

HERE = Path(__file__).resolve().parent
API_BASE = "https://api.elevenlabs.io"
VOICE_ID = os.environ.get("CC_VOICE_ELEVENLABS_VOICE_ID", "zJkzOPmQZx3vFhVX8zNR")
TTS_MODEL_ID = os.environ.get("CC_VOICE_ELEVENLABS_TTS_MODEL", "eleven_v4")
STT_MODEL_IDS = ("scribe_v2", "scribe_v1")
DEFAULT_KEY_FILE = "/root/.elevenlabs_key"
DISABLED_FLAG_FILE = HERE / "state" / "voice_elevenlabs.disabled"
STREAM_SAMPLE_RATE = 16000
_OFF_VALUES = {"0", "off", "false", "no", "disabled"}


class ElevenLabsError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def load_api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    path = Path(os.environ.get("CC_VOICE_ELEVENLABS_KEY_FILE", DEFAULT_KEY_FILE))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def enabled() -> bool:
    if os.environ.get("CC_VOICE_ELEVENLABS", "").strip().lower() in _OFF_VALUES:
        return False
    if DISABLED_FLAG_FILE.exists():
        return False
    return bool(load_api_key())


def _http_error(exc: urllib.error.HTTPError) -> ElevenLabsError:
    try:
        body = exc.read(300).decode("utf-8", "replace")
    except Exception:
        body = ""
    return ElevenLabsError(f"ElevenLabs HTTP {exc.code}: {body}".strip(), status=exc.code)


def _request(path: str, *, data: bytes, content_type: str, accept: str, timeout: float):
    key = load_api_key()
    if not key:
        raise ElevenLabsError("ElevenLabs api key missing")
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method="POST",
        headers={"xi-api-key": key, "Content-Type": content_type, "Accept": accept},
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from None
    except urllib.error.URLError as exc:
        raise ElevenLabsError(f"ElevenLabs network error: {exc.reason}") from None


# --- TTS ---------------------------------------------------------------------

_AUDIO_TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z _-]{0,30}\]\s*")


def strip_audio_tags(text: str) -> str:
    """Drop ElevenLabs audio tags like ``[softly]`` so fallback voices don't read them."""
    return _AUDIO_TAG_RE.sub("", text).strip()


def _tts_body(text: str) -> bytes:
    # Deliberately no voice_settings and no language_code.
    return json.dumps({"text": text, "model_id": TTS_MODEL_ID}, ensure_ascii=False).encode("utf-8")


def align_pcm_chunks(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Re-chunk a byte stream so every yielded piece holds whole int16 samples."""
    carry = b""
    for chunk in chunks:
        if not chunk:
            continue
        buf = carry + chunk
        cut = len(buf) - (len(buf) % 2)
        carry = buf[cut:]
        if cut:
            yield buf[:cut]


def tts_pcm_stream(
    text: str, *, sample_rate: int = STREAM_SAMPLE_RATE, timeout: float = 30.0, read_size: int = 4096
) -> Iterator[bytes]:
    """Yield raw PCM s16le mono chunks at ``sample_rate`` as ElevenLabs streams them."""
    query = urllib.parse.urlencode({"output_format": f"pcm_{int(sample_rate)}"})
    resp = _request(
        f"/v1/text-to-speech/{VOICE_ID}/stream?{query}",
        data=_tts_body(text),
        content_type="application/json",
        accept="audio/pcm",
        timeout=timeout,
    )

    def _raw() -> Iterator[bytes]:
        with resp:
            reader = getattr(resp, "read1", None) or resp.read
            while True:
                chunk = reader(read_size)
                if not chunk:
                    break
                yield chunk

    yield from align_pcm_chunks(_raw())


def tts_to_file(text: str, output_dir: Path, *, timeout: float = 60.0) -> dict:
    """Synthesize ``text`` to an mp3 in ``output_dir``; return the helper payload shape."""
    query = urllib.parse.urlencode({"output_format": "mp3_44100_128"})
    resp = _request(
        f"/v1/text-to-speech/{VOICE_ID}?{query}",
        data=_tts_body(text),
        content_type="application/json",
        accept="audio/mpeg",
        timeout=timeout,
    )
    with resp:
        audio = resp.read()
    if not audio:
        raise ElevenLabsError("ElevenLabs TTS returned no audio")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"voice_call_{uuid.uuid4().hex}.mp3"
    output_path.write_bytes(audio)
    return {
        "ok": True,
        "text": text,
        "stored_name": output_path.name,
        "mime_type": "audio/mpeg",
        "bytes": len(audio),
        "provider": "elevenlabs",
    }


# --- STT ---------------------------------------------------------------------


def _multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----ccvoice" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def parse_stt_response(payload: object) -> str:
    """Extract the transcript from a Scribe response (single or multichannel)."""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    transcripts = payload.get("transcripts")
    if isinstance(transcripts, list):
        pieces = [str(t.get("text") or "").strip() for t in transcripts if isinstance(t, dict)]
        return " ".join(p for p in pieces if p)
    return ""


def _model_rejected(exc: ElevenLabsError) -> bool:
    return exc.status in (400, 404, 422)


def speech_to_text(input_path: Path, *, timeout: float = 60.0) -> tuple[str, str]:
    """Transcribe one utterance. Returns ``(transcript, model_id_used)``."""
    audio = Path(input_path).read_bytes()
    last_exc: Optional[ElevenLabsError] = None
    for model_id in STT_MODEL_IDS:
        body, content_type = _multipart(
            {"model_id": model_id, "tag_audio_events": "false"},
            "file",
            Path(input_path).name,
            audio,
        )
        try:
            resp = _request(
                "/v1/speech-to-text",
                data=body,
                content_type=content_type,
                accept="application/json",
                timeout=timeout,
            )
            with resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
            return parse_stt_response(payload), model_id
        except ElevenLabsError as exc:
            last_exc = exc
            if not _model_rejected(exc):
                raise
    assert last_exc is not None
    raise last_exc
