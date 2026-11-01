"""Pictures for pi RPC prompts — 哈基米 ``hajiki`` (2026-11-01).

pi's ``prompt`` command takes ``images: [{"type": "image", "data": <base64>,
"mimeType": "image/png"}]`` (pi docs rpc-commands.md, ``ImageContent``).  The
picture travels inside one JSONL line over ssh stdin to Tokyo, so it is
shrunk here first: longest side <= ``MAX_SIDE`` px, at most ``MAX_BYTES``
(pi resizes Gemini images to 2000 px / 4.5 MB itself; doing it here keeps the
line small).  EXIF orientation is applied.  Nothing is logged.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

MAX_SIDE = 2000
MAX_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 50 * 1024 * 1024
JPEG_QUALITY = 85
# Sent untouched when Pillow cannot decode them but Gemini can (phone HEIC);
# a JPEG/PNG/WebP that Pillow cannot open is broken and refused.
RAW_OK = {"image/heic", "image/heif"}


def _sniff(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""
_KEEP_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def _encode(data: bytes, mime: str) -> dict[str, Any]:
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


def image_content(path: str | Path, media_type: str = "") -> dict[str, Any]:
    """One stored picture -> pi ImageContent.  ValueError when unusable."""
    source = Path(path)
    size = source.stat().st_size
    if size <= 0 or size > MAX_SOURCE_BYTES:
        raise ValueError("image size out of range")
    data = source.read_bytes()
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow is in requirements.txt
        # Without Pillow: pass small JPEG/PNG/WebP through as they are (pi
        # resizes for the model itself); anything else is refused.
        sniffed = _sniff(data)
        if sniffed and len(data) <= MAX_BYTES:
            return _encode(data, sniffed)
        Image = None  # type: ignore[assignment]
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                fmt = str(probe.format or "").upper()
                try:
                    rotated = int(probe.getexif().get(0x0112, 1) or 1) != 1
                except Exception:
                    rotated = False
                img = ImageOps.exif_transpose(probe)
                img.load()
        except Exception:
            img = None
        if img is not None:
            fits = max(img.size) <= MAX_SIDE and len(data) <= MAX_BYTES and not rotated
            if fits and fmt in _KEEP_FORMATS:
                return _encode(data, _KEEP_FORMATS[fmt])
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                out = io.BytesIO()
                img.convert("RGBA").save(out, format="PNG", optimize=True)
                if out.tell() <= MAX_BYTES:
                    return _encode(out.getvalue(), "image/png")
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = flat
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            if out.tell() > MAX_BYTES:
                raise ValueError("image still too large after resizing")
            return _encode(out.getvalue(), "image/jpeg")
    mime = str(media_type or "").split(";", 1)[0].strip().lower()
    if mime in RAW_OK and len(data) <= MAX_BYTES:
        return _encode(data, mime)
    raise ValueError("unsupported image")
