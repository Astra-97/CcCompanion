"""Voice-message ASR via SiliconFlow SenseVoiceSmall.

The model returns rich transcriptions whose ``<|...|>`` tags carry the
language, one emotion marker (e.g. ``<|HAPPY|>``) and any audio-event
markers (e.g. ``<|Laughter|>``).  ``transcribe_voice_audio`` performs the
HTTP call; ``parse_sensevoice_output`` is the pure parser kept separate so
the tag grammar can be regression-tested without any network access.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx


SILICONFLOW_TRANSCRIPTION_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SENSEVOICE_MODEL = "FunAudioLLM/SenseVoiceSmall"

VOICE_MESSAGE_MAX_BYTES = 10 * 1024 * 1024
VOICE_MESSAGE_AUDIO_EXTENSIONS = frozenset({
    ".m4a", ".mp4", ".aac", ".wav", ".mp3", ".ogg", ".opus", ".webm",
    ".amr", ".flac",
})
VOICE_MESSAGE_MAX_DURATION_MS = 10 * 60 * 1000
DEFAULT_ASR_TIMEOUT_SEC = 60.0

_TAG_RE = re.compile(r"<\|([A-Za-z0-9_]+)\|>")

_LANGUAGES = frozenset({"zh", "en", "yue", "ja", "ko", "auto"})
_EMOTIONS = frozenset({
    "HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED",
})
_EVENTS = frozenset({
    "Speech", "Applause", "BGM", "Laughter", "Cry", "Sneeze", "Breath",
    "Cough",
})


class VoiceAsrError(RuntimeError):
    """The transcription request failed or returned an unusable payload."""


def parse_sensevoice_output(raw: Any) -> dict[str, Any]:
    """Split a SenseVoice rich transcription into text + emotion + events.

    Unknown tags are stripped from the visible text but otherwise ignored,
    so a provider-side vocabulary update never breaks the chat pipeline.
    """

    text = str(raw or "")
    language = ""
    emotion = ""
    events: list[str] = []
    for tag in _TAG_RE.findall(text):
        if tag in _LANGUAGES and not language:
            language = tag
        elif tag in _EMOTIONS and not emotion:
            emotion = tag
        elif tag in _EVENTS and tag not in events and tag != "Speech":
            events.append(tag)
    clean = _TAG_RE.sub("", text).strip()
    return {
        "text": clean,
        "language": language,
        "emotion": emotion,
        "events": events,
        "raw": text.strip(),
    }


def voice_message_api_key() -> str:
    """Read the SiliconFlow credential from the process environment only."""

    return str(os.environ.get("SILICONFLOW_API_KEY") or "").strip()


def transcribe_voice_audio(
    audio_path: str | Path,
    *,
    api_key: str,
    timeout: float = DEFAULT_ASR_TIMEOUT_SEC,
    url: str = SILICONFLOW_TRANSCRIPTION_URL,
    model: str = SENSEVOICE_MODEL,
) -> dict[str, Any]:
    """Upload one audio file to SiliconFlow and return the parsed result."""

    if not api_key:
        raise VoiceAsrError("siliconflow_api_key_missing")
    path = Path(audio_path)
    try:
        with path.open("rb") as handle:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": model},
                files={"file": (path.name, handle, "application/octet-stream")},
                timeout=timeout,
            )
    except VoiceAsrError:
        raise
    except Exception as exc:
        raise VoiceAsrError(f"siliconflow_request_failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise VoiceAsrError(f"siliconflow_http_{response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise VoiceAsrError("siliconflow_bad_json") from exc
    raw_text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise VoiceAsrError("siliconflow_empty_transcript")
    return parse_sensevoice_output(raw_text)
