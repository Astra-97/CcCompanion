"""Safe, configuration-backed catalog for inline ``[bqb:name]`` stickers.

The mobile clients deliberately never turn a model-provided URL into an image.
They ask the Companion server for this catalog and resolve a token only when its
name is an exact key in the returned list.  The server in turn derives every
URL from an operator-configured HTTPS base plus a filename in a manifest.

Manifests are intentionally tiny JSON documents, for example::

    {"version": 1, "stickers": [
      {"name": "憋不住笑了", "file": "憋不住笑了.gif"}
    ]}

They may be local files or HTTPS URLs configured by the operator.  A manifest
may not provide arbitrary image URLs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
import unicodedata
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


logger = logging.getLogger(__name__)

_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_STICKERS = 512
_MAX_NAME_CHARS = 80
_IMAGE_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp"}


def is_valid_sticker_name(value: Any) -> bool:
    """Return true only for an exact, safe token/catalog name.

    No trimming, case folding, or fuzzy matching is performed: ``[bqb:爱]``
    has to match exactly the name that the operator published in the catalog.
    """
    if not isinstance(value, str) or not (1 <= len(value) <= _MAX_NAME_CHARS):
        return False
    if value != unicodedata.normalize("NFC", value):
        return False
    if value != value.strip():
        return False
    forbidden = set("[]:/\\?#%")
    return all(unicodedata.category(ch)[0] != "C" and ch not in forbidden for ch in value)


class _NoRedirect(HTTPRedirectHandler):
    """Configured manifest downloads must not hop to an arbitrary URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _normalise_base_url(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return candidate.rstrip("/")


def _safe_catalog_entry(item: Any, base_url: str) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    filename = item.get("file")
    if not is_valid_sticker_name(name) or not isinstance(filename, str):
        return None
    if filename != unicodedata.normalize("NFC", filename) or "/" in filename or "\\" in filename:
        return None
    suffix = Path(filename).suffix.lower()
    stem = filename[: -len(suffix)] if suffix else ""
    # The filename is the source of truth on the static host.  Requiring the
    # exact name + extension makes traversal and arbitrary file references
    # impossible even when somebody accidentally publishes a bad manifest.
    if suffix not in _IMAGE_EXTENSIONS or stem != name:
        return None
    return {"name": name, "url": f"{base_url}/{quote(filename, safe='-._~()')}"}


@dataclass(frozen=True)
class _Source:
    manifest_path: Path | None
    manifest_url: str | None
    public_base_url: str


class StickerCatalogService:
    """Read and cache only explicitly configured sticker manifests."""

    def __init__(self, config: Any):
        cfg = config if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        try:
            self.cache_seconds = max(15, min(3600, int(cfg.get("cache_seconds", 300))))
        except (TypeError, ValueError):
            self.cache_seconds = 300
        try:
            self.max_items = max(1, min(_MAX_STICKERS, int(cfg.get("max_items", _MAX_STICKERS))))
        except (TypeError, ValueError):
            self.max_items = _MAX_STICKERS
        self.sources = self._parse_sources(cfg)
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, Any] = {"ok": True, "version": "disabled", "stickers": []}

    @staticmethod
    def _parse_sources(cfg: dict[str, Any]) -> tuple[_Source, ...]:
        raw_sources = cfg.get("sources")
        if not isinstance(raw_sources, list):
            raw_sources = [cfg]
        sources: list[_Source] = []
        for raw in raw_sources:
            if not isinstance(raw, dict):
                continue
            base_url = _normalise_base_url(raw.get("public_base_url"))
            if not base_url:
                continue
            raw_path = raw.get("manifest_path")
            manifest_path = Path(str(raw_path)).expanduser() if isinstance(raw_path, str) and raw_path.strip() else None
            manifest_url = _normalise_base_url(raw.get("manifest_url"))
            # A manifest endpoint is also HTTPS-only, but unlike an image base
            # it is allowed to end in .json and may have a path.
            if manifest_path is None and manifest_url is None:
                continue
            sources.append(_Source(manifest_path, manifest_url, base_url))
        return tuple(sources)

    @staticmethod
    def _read_source(source: _Source) -> Any:
        if source.manifest_path is not None:
            data = source.manifest_path.read_bytes()
        else:
            assert source.manifest_url is not None
            request = Request(source.manifest_url, headers={"Accept": "application/json"})
            opener = build_opener(_NoRedirect())
            with opener.open(request, timeout=5) as response:  # nosec B310: operator-configured HTTPS only
                data = response.read(_MAX_MANIFEST_BYTES + 1)
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ValueError("sticker manifest exceeds size limit")
        return json.loads(data.decode("utf-8"))

    def _build_catalog(self) -> dict[str, Any]:
        if not self.enabled or not self.sources:
            return {"ok": True, "version": "disabled", "stickers": []}
        stickers: list[dict[str, str]] = []
        seen_names: set[str] = set()
        for source in self.sources:
            try:
                raw_manifest = self._read_source(source)
            except Exception as exc:
                # One unavailable source does not make existing stickers turn
                # into arbitrary text/URLs.  Preserve any healthy sources.
                logger.warning("sticker catalog source unavailable: %s", exc)
                continue
            raw_items = raw_manifest.get("stickers") if isinstance(raw_manifest, dict) else raw_manifest
            if not isinstance(raw_items, list):
                continue
            for raw_item in raw_items:
                entry = _safe_catalog_entry(raw_item, source.public_base_url)
                if entry is None or entry["name"] in seen_names:
                    continue
                seen_names.add(entry["name"])
                stickers.append(entry)
                if len(stickers) >= self.max_items:
                    break
            if len(stickers) >= self.max_items:
                break
        fingerprint = hashlib.sha256(
            json.dumps(stickers, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return {"ok": True, "version": fingerprint, "stickers": stickers}

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at < self.cache_seconds:
                return dict(self._cached)
            self._cached = self._build_catalog()
            self._cached_at = now
            return dict(self._cached)
