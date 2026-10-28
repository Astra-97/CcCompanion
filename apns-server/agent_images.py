"""Pictures an agent sends back to 方小南 (2026-10-28, Kiro first).

Kiro has no upload tool: to send a picture he puts one line
``[[CCC_IMAGE:/absolute/path/on/his/host.png]]`` in his reply.  The backend
(push.py ``_kiro_outbound_images``) takes every marker out of the visible
text, fetches the file from the agent's own host (Tokyo, over the same ssh
transport as ACP; see ``RemoteStdio.get_file``), checks the bytes really are
an image, stores it in the app attachment store like any other attachment
and shows it in the chat.  When the turn came in through WeCom the stored
paths also ride on the wecom outbox item (``files``), so the callback service
sends them as WeChat picture messages.

Pure helpers only (marker parsing, image sniffing, storing); no I/O policy.
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

MARKER_PREFIX = "[[CCC_IMAGE:"
# One marker = one picture.  Optional surrounding backticks (a model may
# format it as code) go with it.
MARKER_RE = re.compile(r"`?\[\[CCC_IMAGE:[ \t]*([^\]\n]{1,1024}?)[ \t]*\]\]`?")
_PARTIAL_TAIL_RE = re.compile(r"`?\[(?:\[(?:C(?:C(?:C(?:_(?:I(?:M(?:A(?:G(?:E(?::[^\]\n]*)?)?)?)?)?)?)?)?)?)?)?$")
MAX_IMAGES = 4
MAX_BYTES = 10 * 1024 * 1024  # WeCom image upload cap


def extract_markers(text: str, *, limit: int = MAX_IMAGES) -> tuple[str, list[str], int]:
    """-> (visible text without markers, unique paths in order (<= limit), extra count)."""
    raw = str(text or "")
    if MARKER_PREFIX not in raw:
        return raw, [], 0
    paths: list[str] = []
    extra = 0
    for match in MARKER_RE.finditer(raw):
        candidate = match.group(1).strip()
        if not candidate or candidate in paths:
            continue
        if len(paths) >= limit:
            extra += 1
            continue
        paths.append(candidate)
    cleaned = MARKER_RE.sub("", raw)
    # A marker on its own line leaves an empty line behind; keep paragraphs.
    lines = [line.rstrip() for line in cleaned.split("\n")]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return cleaned, paths, extra


def strip_markers(text: str) -> str:
    """Streaming drafts: hide complete markers and a half-written one at the end."""
    raw = str(text or "")
    if "[" not in raw:
        return raw
    out = MARKER_RE.sub("", raw)
    return _PARTIAL_TAIL_RE.sub("", out)


def sniff(header: bytes) -> tuple[str, str] | None:
    """(extension, media type) for jpeg/png/gif/webp bytes, else None."""
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def store_image(data: bytes, destination: Path, *, prefix: str, filename: str = "") -> dict[str, Any]:
    """Write verified image bytes into the attachment store (0600, atomic).

    Returns the same attachment record shape the app upload path uses.
    Raises ValueError for anything that is not a supported image.
    """
    if not data or len(data) > MAX_BYTES:
        raise ValueError("invalid image size")
    kind = sniff(data[:12])
    if kind is None:
        raise ValueError("not a jpeg/png/gif/webp image")
    extension, media_type = kind
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,15}", prefix):
        raise ValueError("invalid prefix")
    target_dir = destination.resolve(strict=True)
    stored_name = f"{prefix}-{secrets.token_hex(16)}{extension}"
    final = target_dir / stored_name
    tmp = target_dir / f".{stored_name}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    shown = Path(str(filename or "")).name
    stem = Path(shown).stem if shown else ""
    safe_stem = re.sub(r"[^\w.-]+", "_", stem)[:80].strip("._") or "image"
    return {
        "attachment_url": f"/attachments/{stored_name}",
        "filename": f"{safe_stem}{extension}",
        "type": "image",
        "media_type": media_type,
        "size": len(data),
        "stored_path": str(final),
    }
