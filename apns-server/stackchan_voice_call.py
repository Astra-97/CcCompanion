#!/usr/bin/env python3
"""Small bridge for CC Companion voice calls using StackChan xiaozhi providers.

Two TTS modes:
- ``tts``: legacy batch — synthesize full audio, write WAV/MP3 to disk, print
  JSON with stored filename. Used by ``apns-server``'s ``/voice/push`` push
  notification path (one-shot voice messages).
- ``tts_stream``: real streaming for live calls. Connects to MiniMax T2A v2
  SSE endpoint and yields PCM chunks as they arrive (no buffering, no WAV
  flattening). The chunks are framed onto stdout so ``voice_call_ws.py`` can
  consume them and push them straight onto the WebSocket. Frame layout:

      magic(2)=b"MM" | type(1) | length(4 BE) | payload(length)

  type=0 meta JSON (utf-8) — first frame, carries ``sample_rate``/``channels``.
  type=1 PCM chunk (16-bit signed little-endian mono at meta.sample_rate).
  type=2 end of stream (length=0).
  type=3 error JSON (utf-8) — caller should treat as fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import struct
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


def _minimax_httpstream_pcm_iter(tts, text: str):
    """Stream PCM chunks from MiniMax T2A v2 SSE.

    This is the real-streaming primitive: ``requests`` with ``stream=True``,
    parse SSE events as they arrive, ``yield`` raw PCM bytes for every chunk
    that has ``data.status == 1`` and a non-empty hex ``audio`` payload.

    Yields ``bytes`` (raw 16-bit signed LE mono PCM at ``tts.audio_setting
    .sample_rate``). Caller decides chunking / framing / resampling.
    """
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
            "Accept": "text/event-stream",
        },
        timeout=(10, 60),
        stream=True,
    )
    if response.status_code != 200:
        body = response.text[:200]
        raise RuntimeError(f"Minimax TTS failed: HTTP {response.status_code}: {body}")
    response.encoding = response.encoding or "utf-8"

    # SSE is line-delimited; events end with a blank line. ``iter_lines`` keeps
    # us moving as soon as the server flushes a line — that is what makes this
    # actual streaming. We accumulate ``data:`` lines until we hit a blank.
    pending: list[str] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", "ignore")
        if raw_line == "":
            if not pending:
                continue
            block = "\n".join(pending)
            pending = []
            if not block.startswith("data: "):
                continue
            try:
                data = json.loads(block[6:])
            except json.JSONDecodeError:
                continue
            base_resp = data.get("base_resp") or {}
            if base_resp and base_resp.get("status_code", 0) != 0:
                raise RuntimeError(str(base_resp.get("status_msg") or "Minimax TTS error"))
            d = data.get("data") or {}
            if d.get("status") == 1 and d.get("audio"):
                try:
                    yield bytes.fromhex(d["audio"])
                except ValueError:
                    continue
            # status == 2 is the final summary block; nothing to emit.
            continue
        pending.append(raw_line)

    # Flush any trailing block that didn't end with a blank line.
    if pending:
        block = "\n".join(pending)
        if block.startswith("data: "):
            try:
                data = json.loads(block[6:])
                d = data.get("data") or {}
                if d.get("status") == 1 and d.get("audio"):
                    yield bytes.fromhex(d["audio"])
            except (json.JSONDecodeError, ValueError):
                pass


def _minimax_httpstream_to_wav(tts, text: str, output_path: Path) -> None:
    """Collect the full stream into a single WAV file (batch path for push)."""
    pcm_data = bytearray()
    for chunk in _minimax_httpstream_pcm_iter(tts, text):
        pcm_data.extend(chunk)
    if not pcm_data:
        raise RuntimeError("Minimax TTS returned no audio")

    sample_rate = int(tts.audio_setting.get("sample_rate") or 24000)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm_data))


# --- Streaming protocol (stdout binary framing) -----------------------------

_FRAME_MAGIC = b"MM"
_FRAME_TYPE_META = 0
_FRAME_TYPE_PCM = 1
_FRAME_TYPE_END = 2
_FRAME_TYPE_ERROR = 3


def _write_frame(frame_type: int, payload: bytes = b"") -> None:
    """Write a length-prefixed binary frame to stdout."""
    out = sys.stdout.buffer
    header = _FRAME_MAGIC + bytes([frame_type]) + struct.pack(">I", len(payload))
    out.write(header)
    if payload:
        out.write(payload)
    out.flush()


def _run_tts_stream(text: str) -> int:
    """TTS in real-streaming mode. Yields PCM chunks straight to stdout as
    framed binary so the parent process can push them onto a WebSocket without
    waiting for synthesis to finish.

    Returns process exit code (0 = ok, 1 = error). Logs/errors go to stderr.
    """
    _prepare_imports()
    from config.settings import load_config
    from core.utils.modules_initialize import initialize_tts
    from core.utils.tts import MarkdownCleaner

    try:
        config = load_config()
        tts = initialize_tts(config)
        cleaned = MarkdownCleaner.clean_markdown(text.strip())
        if getattr(tts, "_correct_words_pattern", None):
            cleaned = tts._correct_words_pattern.sub(
                lambda m: tts.correct_words[m.group(0)], cleaned
            )

        # Only MinimaxTTSHTTPStream is wired for real streaming today. Other
        # providers fall back to batch synthesis (synthesize whole file, then
        # emit it as one chunk) so the protocol is still uniform.
        is_minimax_stream = tts.__class__.__module__.endswith("minimax_httpstream")
        sample_rate = int(getattr(tts, "audio_setting", {}).get("sample_rate") or 24000)

        _write_frame(
            _FRAME_TYPE_META,
            json.dumps(
                {
                    "sample_rate": sample_rate,
                    "channels": 1,
                    "sample_width": 2,
                    "format": "pcm_s16le",
                    "provider": tts.__class__.__module__.rsplit(".", 1)[-1],
                    "streaming": bool(is_minimax_stream),
                }
            ).encode("utf-8"),
        )

        chunk_count = 0
        if is_minimax_stream:
            for pcm in _minimax_httpstream_pcm_iter(tts, cleaned):
                if not pcm:
                    continue
                _write_frame(_FRAME_TYPE_PCM, pcm)
                chunk_count += 1
        else:
            # Batch fallback: synthesize to a temp file, then emit as one frame.
            ext = str(getattr(tts, "audio_file_type", "wav") or "wav").strip().lower().lstrip(".")
            if ext == "mpeg":
                ext = "mp3"
            tmp_path = Path(f"/tmp/tts_stream_{uuid.uuid4().hex}.{ext}")
            try:
                asyncio.run(tts.text_to_speak(cleaned, str(tmp_path)))
                if tmp_path.exists() and tmp_path.stat().st_size > 0:
                    # Decode to 16-bit LE mono at the provider's sample rate.
                    from pydub import AudioSegment

                    seg = AudioSegment.from_file(str(tmp_path))
                    seg = seg.set_channels(1).set_sample_width(2)
                    sample_rate = seg.frame_rate
                    # Re-emit meta with corrected sample rate.
                    _write_frame(
                        _FRAME_TYPE_META,
                        json.dumps(
                            {
                                "sample_rate": sample_rate,
                                "channels": 1,
                                "sample_width": 2,
                                "format": "pcm_s16le",
                                "provider": tts.__class__.__module__.rsplit(".", 1)[-1],
                                "streaming": False,
                            }
                        ).encode("utf-8"),
                    )
                    raw = seg.raw_data
                    # 40ms slices so the consumer still sees chunked frames.
                    slice_bytes = int(sample_rate * 2 * 40 / 1000)
                    for i in range(0, len(raw), slice_bytes):
                        _write_frame(_FRAME_TYPE_PCM, raw[i : i + slice_bytes])
                        chunk_count += 1
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if chunk_count == 0:
            error = "tts produced no audio"
            if is_minimax_stream:
                error = (
                    "MiniMax PCM streaming produced no audio; "
                    "provider may not support audio_setting.format=pcm for streaming"
                )
            _write_frame(
                _FRAME_TYPE_ERROR,
                json.dumps({"error": error}).encode("utf-8"),
            )
            return 1

        _write_frame(_FRAME_TYPE_END)
        return 0
    except Exception as exc:
        try:
            _write_frame(
                _FRAME_TYPE_ERROR,
                json.dumps({"error": str(exc)}).encode("utf-8"),
            )
        except Exception:
            print(f"tts_stream error: {exc}", file=sys.stderr, flush=True)
        return 1


async def _main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    asr_parser = sub.add_parser("asr")
    asr_parser.add_argument("--input", required=True)
    tts_parser = sub.add_parser("tts")
    tts_parser.add_argument("--text", required=True)
    tts_parser.add_argument("--output-dir", required=True)
    tts_stream_parser = sub.add_parser("tts_stream")
    tts_stream_parser.add_argument("--text", required=True)
    args = parser.parse_args()

    try:
        if args.command == "asr":
            _print_json(await _run_asr(Path(args.input)))
        elif args.command == "tts":
            _print_json(await _run_tts(args.text, Path(args.output_dir)))
        elif args.command == "tts_stream":
            return _run_tts_stream(args.text)
        return 0
    except Exception as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
