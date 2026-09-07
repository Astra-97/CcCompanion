"""Lightweight acoustic analysis for voice messages.

Computes three display metrics, mirroring the 小红书 MCP ear summary style:

- 基频（音高）: median F0 over voiced frames via FFT autocorrelation,
  bucketed against the adult-female speech range (~165-255 Hz).
- 语速: transcript characters per second of *voiced* audio.
- 停顿次数: interior silence segments longer than the pause threshold.

``analyze_voice_acoustics`` handles ffmpeg wav conversion; the pure
``analyze_samples`` core takes a float waveform and is unit-testable
without any audio files or subprocesses.
"""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import numpy as np


FFMPEG_BIN = "/usr/bin/ffmpeg"
ANALYSIS_SAMPLE_RATE = 16000
DEFAULT_FFMPEG_TIMEOUT_SEC = 30.0

FRAME_MS = 40.0
HOP_MS = 20.0
F0_MIN_HZ = 50.0
F0_MAX_HZ = 400.0
# 成年女性说话基频正常区间，按此分档。
PITCH_NORMAL_LOW_HZ = 165.0
PITCH_NORMAL_HIGH_HZ = 255.0
PAUSE_MIN_SEC = 0.5
VOICED_RELATIVE_THRESHOLD = 0.15
AUTOCORR_VOICED_RATIO = 0.3

_CHAR_RE = re.compile(r"[0-9A-Za-z一-鿿]")


class VoiceAcousticsError(RuntimeError):
    """The audio could not be decoded or analysed."""


def count_speech_chars(text: str) -> int:
    """Count spoken characters (CJK + alphanumerics), excluding punctuation."""

    return len(_CHAR_RE.findall(str(text or "")))


def _decode_to_wav(audio_path: Path, *, timeout: float = DEFAULT_FFMPEG_TIMEOUT_SEC) -> Path:
    tmp = tempfile.NamedTemporaryFile(prefix="cc-voice-acoustics-", suffix=".wav", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        result = subprocess.run(
            [
                FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(audio_path),
                "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE), "-f", "wav",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise VoiceAcousticsError(f"ffmpeg_convert_failed: {type(exc).__name__}") from exc
    if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size <= 44:
        tmp_path.unlink(missing_ok=True)
        raise VoiceAcousticsError("ffmpeg_convert_failed")
    return tmp_path


def _read_wav_samples(wav_path: Path) -> tuple[np.ndarray, int]:
    try:
        with wave.open(str(wav_path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except Exception as exc:
        raise VoiceAcousticsError(f"wav_read_failed: {type(exc).__name__}") from exc
    if width != 2 or not raw:
        raise VoiceAcousticsError("wav_unsupported_format")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def _frame_energy(samples: np.ndarray, frame: int, hop: int) -> np.ndarray:
    if len(samples) < frame:
        return np.zeros(0)
    count = 1 + (len(samples) - frame) // hop
    index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    frames = samples[index]
    return np.sqrt(np.mean(frames * frames, axis=1))


def _f0_track(
    samples: np.ndarray,
    sample_rate: int,
    voiced: np.ndarray,
    frame: int,
    hop: int,
) -> list[float]:
    """FFT-autocorrelation F0 per voiced frame, in Hz."""

    lag_min = max(1, int(sample_rate / F0_MAX_HZ))
    lag_max = min(frame - 1, int(sample_rate / F0_MIN_HZ))
    if lag_max <= lag_min:
        return []
    track: list[float] = []
    for index in np.flatnonzero(voiced):
        segment = samples[index * hop : index * hop + frame]
        if len(segment) < frame:
            continue
        segment = segment - segment.mean()
        spectrum = np.fft.rfft(segment, n=2 * frame)
        autocorr = np.fft.irfft(spectrum * np.conj(spectrum))[:frame]
        if autocorr[0] <= 0:
            continue
        window = autocorr[lag_min : lag_max + 1] / autocorr[0]
        peak = lag_min + int(np.argmax(window))
        if autocorr[peak] / autocorr[0] < AUTOCORR_VOICED_RATIO:
            continue
        # Parabolic interpolation around the peak for sub-sample resolution.
        if 0 < peak < frame - 1:
            left, center, right = autocorr[peak - 1], autocorr[peak], autocorr[peak + 1]
            denom = left - 2.0 * center + right
            if abs(denom) > 1e-12:
                shift = 0.5 * (left - right) / denom
                if abs(shift) <= 1.0:
                    peak = peak + shift
        track.append(float(sample_rate) / float(peak))
    return track


def pitch_label(pitch_hz: float) -> str:
    if pitch_hz <= 0 or not math.isfinite(pitch_hz):
        return "未知"
    if pitch_hz < PITCH_NORMAL_LOW_HZ:
        return "偏低"
    if pitch_hz > PITCH_NORMAL_HIGH_HZ:
        return "偏高"
    return "正常"


def analyze_samples(
    samples: np.ndarray,
    sample_rate: int,
    transcript: str = "",
) -> dict[str, Any]:
    """Pure analysis core: waveform in, metric dict out."""

    samples = np.asarray(samples, dtype=np.float64)
    frame = int(sample_rate * FRAME_MS / 1000.0)
    hop = int(sample_rate * HOP_MS / 1000.0)
    duration_sec = len(samples) / float(sample_rate)

    energies = _frame_energy(samples, frame, hop)
    if energies.size == 0:
        raise VoiceAcousticsError("audio_too_short")
    peak = float(energies.max())
    if peak <= 1e-4:
        raise VoiceAcousticsError("audio_silent")
    threshold = max(1e-3, peak * VOICED_RELATIVE_THRESHOLD)
    voiced = energies >= threshold

    # 停顿：发声段之间、持续超过阈值的静音段（首尾静音不算）。
    pauses = 0
    first_voiced = int(np.argmax(voiced))
    last_voiced = int(len(voiced) - 1 - np.argmax(voiced[::-1]))
    run = 0
    pause_frames = int(PAUSE_MIN_SEC * 1000.0 / HOP_MS)
    for index in range(first_voiced, last_voiced + 1):
        if voiced[index]:
            if run >= pause_frames:
                pauses += 1
            run = 0
        else:
            run += 1

    voiced_sec = float(np.count_nonzero(voiced)) * hop / float(sample_rate)
    f0_values = _f0_track(samples, sample_rate, voiced, frame, hop)
    pitch_hz = float(np.median(f0_values)) if f0_values else 0.0

    chars = count_speech_chars(transcript)
    speech_rate = round(chars / voiced_sec, 1) if chars > 0 and voiced_sec > 0 else 0.0

    return {
        "pitch_hz": round(pitch_hz, 1),
        "pitch_label": pitch_label(pitch_hz),
        "speech_rate_cps": speech_rate,
        "speech_chars": chars,
        "pauses": pauses,
        "voiced_sec": round(voiced_sec, 2),
        "duration_sec": round(duration_sec, 2),
    }


def format_acoustics_summary(result: dict[str, Any]) -> str:
    """Render the one-line 中文短句, e.g. 「音高偏高，语速2.6字/秒，停顿0次」."""

    parts = [f"音高{result.get('pitch_label') or '未知'}"]
    rate = result.get("speech_rate_cps") or 0.0
    if rate > 0:
        parts.append(f"语速{rate}字/秒")
    parts.append(f"停顿{int(result.get('pauses') or 0)}次")
    return "，".join(parts)


def analyze_voice_acoustics(
    audio_path: str | Path,
    transcript: str = "",
    *,
    ffmpeg_timeout: float = DEFAULT_FFMPEG_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Decode any audio file via ffmpeg and return metrics + summary."""

    wav_path = _decode_to_wav(Path(audio_path), timeout=ffmpeg_timeout)
    try:
        samples, rate = _read_wav_samples(wav_path)
    finally:
        wav_path.unlink(missing_ok=True)
    result = analyze_samples(samples, rate, transcript)
    result["summary"] = format_acoustics_summary(result)
    return result
