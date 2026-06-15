#!/usr/bin/env python3
"""Small bridge for CC Companion voice calls using StackChan xiaozhi providers."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
import uuid
import wave
from pathlib import Path


XIAOZHI_ROOT = Path(os.environ.get("STACKCHAN_XIAOZHI_ROOT", "/root/stackchan-server-lite/main/xiaozhi-server"))
XIAOZHI_PYTHON = Path(os.environ.get("STACKCHAN_XIAOZHI_PYTHON", str(XIAOZHI_ROOT / ".venv/bin/python")))


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _prepare_imports() -> None:
    os.chdir(XIAOZHI_ROOT)
    root = str(XIAOZHI_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _audio_to_pcm_frames(input_path: Path) -> tuple[list[bytes], int, float]:
    from pydub import AudioSegment

    audio = AudioSegment.from_file(str(input_path), parameters=["-nostdin"])
    duration_ms = len(audio)
    dbfs = float(audio.dBFS) if audio.dBFS != float("-inf") else -120.0
    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    raw = audio.raw_data
    frame_size = 1920
    frames = [raw[i : i + frame_size] for i in range(0, len(raw), frame_size) if raw[i : i + frame_size]]
    return frames, duration_ms, dbfs


def _normalize_transcript(value) -> str:
    if isinstance(value, dict):
        return str(value.get("content") or value.get("text") or "").strip()
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                return str(decoded.get("content") or decoded.get("text") or text).strip()
        except Exception:
            return text
    return text


async def _run_asr(input_path: Path) -> dict:
    _prepare_imports()
    from config.settings import load_config
    from core.utils.modules_initialize import initialize_asr

    pcm_frames, duration_ms, dbfs = _audio_to_pcm_frames(input_path)
    if not pcm_frames:
        return {"ok": False, "error": "empty audio"}
    if duration_ms < 500 or dbfs < -52:
        return {"ok": True, "transcript": ""}
    config = load_config()
    asr = initialize_asr(config)
    transcript, _file_path = await asr.speech_to_text_wrapper(pcm_frames, f"app_voice_{uuid.uuid4().hex}", "pcm")
    normalized = _normalize_transcript(transcript)
    return {"ok": bool(normalized), "transcript": normalized}


async def _run_tts(text: str, output_dir: Path) -> dict:
    _prepare_imports()
    from config.settings import load_config
    from core.utils.modules_initialize import initialize_tts
    from core.utils.tts import MarkdownCleaner

    config = load_config()
    tts = initialize_tts(config)
    cleaned = MarkdownCleaner.clean_markdown(text.strip())
    if getattr(tts, "_correct_words_pattern", None):
        cleaned = tts._correct_words_pattern.sub(lambda m: tts.correct_words[m.group(0)], cleaned)
    if tts.__class__.__module__.endswith("minimax_httpstream"):
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"voice_call_{uuid.uuid4().hex}.wav"
        _minimax_httpstream_to_wav(tts, cleaned, output_path)
        return {
            "ok": True,
            "text": text,
            "stored_name": output_path.name,
            "mime_type": "audio/wav",
            "bytes": output_path.stat().st_size,
        }

    ext = str(getattr(tts, "audio_file_type", "wav") or "wav").strip().lower().lstrip(".")
    if ext == "mpeg":
        ext = "mp3"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"voice_call_{uuid.uuid4().hex}.{ext}"
    await tts.text_to_speak(cleaned, str(output_path))
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return {"ok": False, "error": "tts generated no audio"}
    mime_type = mimetypes.guess_type(str(output_path))[0] or ("audio/mpeg" if ext == "mp3" else "audio/wav")
    return {
        "ok": True,
        "text": text,
        "stored_name": output_path.name,
        "mime_type": mime_type,
        "bytes": output_path.stat().st_size,
    }


def _minimax_httpstream_to_wav(tts, text: str, output_path: Path) -> None:
    import requests

    payload = {
        "model": tts.model,
        "text": text,
        "stream": True,
        "voice_setting": tts.voice_setting,
        "pronunciation_dict": tts.pronunciation_dict,
        "audio_setting": {**tts.audio_setting, "format": "pcm"},
    }
    if isinstance(tts.timber_weights, list) and tts.timber_weights:
        payload["timber_weights"] = tts.timber_weights
        payload["voice_setting"]["voice_id"] = ""
    response = requests.post(
        tts.api_url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + tts.api_key,
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Minimax TTS failed: HTTP {response.status_code}")

    pcm_data = bytearray()
    for data_block in response.content.decode("utf-8", errors="ignore").split("\n\n"):
        if not data_block.startswith("data: "):
            continue
        try:
            data = json.loads(data_block[6:])
            base_resp = data.get("base_resp", {})
            if base_resp and base_resp.get("status_code", 0) != 0:
                raise RuntimeError(str(base_resp.get("status_msg") or "Minimax TTS error"))
            if data.get("data", {}).get("status") == 1 and data.get("data", {}).get("audio"):
                pcm_data.extend(bytes.fromhex(data["data"]["audio"]))
        except json.JSONDecodeError:
            continue
    if not pcm_data:
        raise RuntimeError("Minimax TTS returned no audio")

    sample_rate = int(tts.audio_setting.get("sample_rate") or 24000)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm_data))


async def _main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    asr_parser = sub.add_parser("asr")
    asr_parser.add_argument("--input", required=True)
    tts_parser = sub.add_parser("tts")
    tts_parser.add_argument("--text", required=True)
    tts_parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    try:
        if args.command == "asr":
            _print_json(await _run_asr(Path(args.input)))
        elif args.command == "tts":
            _print_json(await _run_tts(args.text, Path(args.output_dir)))
        return 0
    except Exception as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
