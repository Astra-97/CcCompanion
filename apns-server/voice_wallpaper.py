"""Custom call-screen wallpaper (voice-wallpaper/), server-hosted.

Same idea as voice-ambience/: the App asks ``GET /voice-call/wallpaper-config``
at call start, compares ``sha256`` with its cache and only then downloads
``GET /voice-call/wallpaper``.  No wallpaper -> ``sha256 == ""`` and the App
draws its built-in night-light scene.  Every request re-reads the directory, so
dropping/removing a file takes effect on the next call without a restart.

Storage is one file ``wallpaper.<ext>`` (jpg/png/webp, sniffed from the bytes,
never trusted from headers) plus an optional ``config.json`` with ``dim``
(0.0-0.9 scrim alpha drawn over the image for readability).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

MAX_BYTES = 8 * 1024 * 1024
DEFAULT_DIM = 0.45
DIM_BOUNDS = (0.0, 0.9)
EXT_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
_STEM = "wallpaper"


def sniff_image_ext(data: bytes) -> Optional[str]:
    """Return jpg/png/webp from magic bytes, or None for anything else."""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class WallpaperStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def current_file(self) -> Optional[Path]:
        for ext in EXT_CONTENT_TYPES:
            path = self.directory / f"{_STEM}.{ext}"
            if path.is_file() and path.stat().st_size > 0:
                return path
        return None

    def sha(self) -> str:
        path = self.current_file()
        if path is None:
            return ""
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()[:16]

    def dim(self) -> float:
        try:
            raw = json.loads((self.directory / "config.json").read_text(encoding="utf-8"))
        except Exception:
            return DEFAULT_DIM
        value = raw.get("dim", DEFAULT_DIM) if isinstance(raw, dict) else DEFAULT_DIM
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return DEFAULT_DIM
        lo, hi = DIM_BOUNDS
        return float(min(hi, max(lo, value)))

    def config(self) -> dict[str, Any]:
        sha = self.sha()
        return {
            "ok": True,
            "sha256": sha,
            "url": "/voice-call/wallpaper" if sha else "",
            "dim": self.dim(),
        }

    def save(self, data: bytes) -> dict[str, Any]:
        """Atomically replace the wallpaper; raises ValueError on bad input."""
        if not data:
            raise ValueError("empty")
        if len(data) > MAX_BYTES:
            raise ValueError("too_large")
        ext = sniff_image_ext(data)
        if ext is None:
            raise ValueError("unsupported_media_type")
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{_STEM}.{ext}"
        fd, tmp = tempfile.mkstemp(prefix=".wallpaper-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        for other in EXT_CONTENT_TYPES:
            if other != ext:
                try:
                    (self.directory / f"{_STEM}.{other}").unlink()
                except FileNotFoundError:
                    pass
        return self.config()

    def clear(self) -> dict[str, Any]:
        for ext in EXT_CONTENT_TYPES:
            try:
                (self.directory / f"{_STEM}.{ext}").unlink()
            except FileNotFoundError:
                pass
        return self.config()
