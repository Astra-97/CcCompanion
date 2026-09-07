"""Gemini「精听」for voice messages via OpenRouter.

Sends the audio (converted to mp3 when needed) to
``google/gemini-2.5-flash`` with an audio-understanding prompt and parses
four prefixed lines out of the reply: 转写核对 / 细粒度情绪 / 声学特征 /
一句话总结.  Any failure raises :class:`VoiceListenError`; the caller
degrades silently so the voice-message pipeline is never blocked.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx


OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_LISTEN_MODEL = "google/gemini-2.5-flash"
DEFAULT_LISTEN_TIMEOUT_SEC = 60.0
DEFAULT_FFMPEG_TIMEOUT_SEC = 30.0
FFMPEG_BIN = "/usr/bin/ffmpeg"
# OpenRouter 侧按 UA 拦截，伪装成 curl。
REQUEST_USER_AGENT = "curl/7.81.0"

LISTEN_PROMPT = (
    "你是一名细致的语音聆听助手。请仔细听这段语音，严格按以下四行格式回答"
    "（每行以给定前缀开头，不要输出其它内容）：\n"
    "转写核对：<语音里说的原文；听不清就写「无法辨认」>\n"
    "细粒度情绪：<情绪与语气细节，如能量高低、语速缓急、颤抖、犹豫、撒娇、疲惫等>\n"
    "声学特征：<嗓音声学描述，如音高高低、鼻音、沙哑、气息声、耳语、回声等；没有特别之处就写「无明显特征」>\n"
    "一句话总结：<用一句话概括说话人当下的状态>\n"
)

_PREFIX_FIELDS = {
    "转写核对": "transcript_check",
    "细粒度情绪": "emotion_detail",
    "声学特征": "acoustic_desc",
    "一句话总结": "summary",
}


class VoiceListenError(RuntimeError):
    """The Gemini listen request failed or returned an unusable payload."""


def openrouter_api_key() -> str:
    """Read the OpenRouter credential from the process environment only."""

    return str(os.environ.get("OPENROUTER_API_KEY") or "").strip()


def parse_listen_output(raw: Any) -> dict[str, str]:
    """Split the four prefixed lines; missing prefixes leave fields empty."""

    result = {field: "" for field in _PREFIX_FIELDS.values()}
    text = str(raw or "")
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        for prefix, field in _PREFIX_FIELDS.items():
            for sep in ("：", ":"):
                head = f"{prefix}{sep}"
                if line.startswith(head) and not result[field]:
                    result[field] = line[len(head):].strip()
    return result


def _ensure_mp3(audio_path: Path, *, ffmpeg_timeout: float) -> tuple[Path, bool]:
    """Return an mp3 path; converts via ffmpeg into a temp file when needed."""

    if audio_path.suffix.lower() == ".mp3":
        return audio_path, False
    tmp = tempfile.NamedTemporaryFile(prefix="cc-voice-listen-", suffix=".mp3", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(audio_path), str(tmp_path)],
            capture_output=True,
            timeout=ffmpeg_timeout,
            check=False,
        )
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise VoiceListenError(f"ffmpeg_convert_failed: {type(exc).__name__}") from exc
    if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise VoiceListenError("ffmpeg_convert_failed")
    return tmp_path, True


def listen_voice_audio(
    audio_path: str | Path,
    *,
    api_key: str,
    timeout: float = DEFAULT_LISTEN_TIMEOUT_SEC,
    ffmpeg_timeout: float = DEFAULT_FFMPEG_TIMEOUT_SEC,
    url: str = OPENROUTER_CHAT_URL,
    model: str = GEMINI_LISTEN_MODEL,
    prompt: str = LISTEN_PROMPT,
) -> dict[str, str]:
    """Run Gemini audio understanding over one voice-message file."""

    if not api_key:
        raise VoiceListenError("openrouter_api_key_missing")
    mp3_path, is_temp = _ensure_mp3(Path(audio_path), ffmpeg_timeout=ffmpeg_timeout)
    try:
        encoded = base64.b64encode(mp3_path.read_bytes()).decode("ascii")
    except Exception as exc:
        raise VoiceListenError(f"audio_read_failed: {type(exc).__name__}") from exc
    finally:
        if is_temp:
            mp3_path.unlink(missing_ok=True)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio", "input_audio": {"data": encoded, "format": "mp3"}},
                ],
            }
        ],
    }
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": REQUEST_USER_AGENT,
            },
            json=payload,
            timeout=timeout,
        )
    except Exception as exc:
        raise VoiceListenError(f"openrouter_request_failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise VoiceListenError(f"openrouter_http_{response.status_code}")
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise VoiceListenError("openrouter_bad_payload") from exc
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    result = parse_listen_output(content)
    if not any(result.values()):
        raise VoiceListenError("openrouter_unparseable_reply")
    result["raw"] = str(content or "").strip()
    return result
