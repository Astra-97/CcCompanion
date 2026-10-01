"""语音附件时长探测（2026-10-01 语音消息真实时长改造）。

wav 用标准库 ``wave`` 直接算（帧数 / 采样率）；其它容器（mp3/m4a 等）
靠系统自带的 ``ffprobe`` 读容器元数据。任何一步失败都返回 0——调用方
（``push.py`` 的 ``_handle_voice_push``）把 0 当「时长未知」处理，App 端
再在音频下载进缓存后用 MediaMetadataRetriever 实测兜底，主流程绝不因
探测失败中断。不新增任何 Python 依赖。
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

_WAV_MIME_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})
_FFPROBE_TIMEOUT_SEC = 15


def audio_duration_ms(path, mime_type: str = "") -> int:
    """返回音频时长（毫秒）；探测不出来返回 0。"""
    target = Path(path)
    try:
        if not target.exists() or not target.is_file():
            return 0
    except OSError:
        return 0
    is_wav = (
        target.suffix.lower() == ".wav"
        or str(mime_type or "").strip().lower() in _WAV_MIME_TYPES
    )
    if is_wav:
        wav_ms = _wav_duration_ms(target)
        if wav_ms > 0:
            return wav_ms
        # wav 头损坏等情况继续走 ffprobe 兜底。
    return _ffprobe_duration_ms(target)


def _wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav_file:
            rate = wav_file.getframerate()
            if rate <= 0:
                return 0
            return max(0, int(round(wav_file.getnframes() * 1000.0 / rate)))
    except Exception:
        return 0


def _ffprobe_duration_ms(path: Path) -> int:
    if not shutil.which("ffprobe"):
        return 0
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_SEC,
            check=False,
        )
        return max(0, int(round(float(result.stdout.strip()) * 1000)))
    except Exception:
        return 0
