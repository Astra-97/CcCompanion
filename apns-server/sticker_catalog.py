"""Safe, configuration-backed catalog for inline ``[bqb:name]`` stickers.

The mobile clients deliberately never turn a model-provided URL into an image.
They ask the Companion server for this catalog and resolve a token only when its
name is an exact key in the returned list.  The server in turn derives every
URL from an operator-configured HTTPS base plus a filename in a manifest.

Manifests are intentionally tiny JSON documents, for example::

    {"version": 1, "category": {"id": "xiaodou", "name": "小黄豆"}, "stickers": [
      {"name": "小黄豆·抱抱·2", "file": "小黄豆·抱抱·2.gif", "label": "抱抱",
       "aliases": ["抱抱"]}
    ]}

``aliases`` are legacy ``[bqb:name]`` tokens kept so historical messages still
resolve after a 「分类·真名」 rename; they never affect URL construction and a
visible name always wins over any alias.

They may be local files or HTTPS URLs configured by the operator.  A manifest
may not provide arbitrary image URLs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
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
_MAX_ALIASES_PER_STICKER = 8
_MAX_CATEGORY_ID_CHARS = 48
_IMAGE_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp"}
# The configured static host can take >5s to answer through Cloudflare; a short
# timeout made the whole catalog vanish whenever a cache rebuild was unlucky.
_FETCH_TIMEOUT_SECONDS = 20
# Cloudflare's bot policy rejects urllib's default user agent on the existing
# static host. This is a fixed product identifier, not an auth credential.
STICKER_CATALOG_USER_AGENT = "CcCompanion-StickerCatalog/1.0"


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


def _safe_display_label(raw: Any, fallback: str) -> str:
    """Use an optional safe display label, otherwise preserve the token name.

    Labels are never part of the ``[bqb:name]`` protocol or URL construction.
    Treating malformed labels as absent preserves the sticker for older
    manifests while keeping untrusted display text out of the catalog.
    """
    return raw if is_valid_sticker_name(raw) else fallback


def _safe_aliases(raw: Any, name: str) -> list[str]:
    """Validate optional legacy ``[bqb:name]`` tokens kept after a rename.

    Aliases let clients resolve historical messages whose tokens predate the
    「分类·真名」 rename.  They follow the exact same safety rules as names,
    never participate in URL construction, and malformed entries are dropped
    individually so one bad alias cannot hide the sticker itself.
    """
    aliases: list[str] = []
    if not isinstance(raw, list):
        return aliases
    for candidate in raw:
        if (isinstance(candidate, str) and candidate != name
                and is_valid_sticker_name(candidate) and candidate not in aliases):
            aliases.append(candidate)
        if len(aliases) >= _MAX_ALIASES_PER_STICKER:
            break
    return aliases


def is_valid_category_id(value: Any) -> bool:
    """Return true only for a stable, wire-safe category identifier.

    Category IDs are server-defined data that clients may cache and group by;
    they are deliberately narrower than human-visible category names.  This
    also prevents a manifest from smuggling a control sequence into a future
    client implementation.
    """
    if not isinstance(value, str) or not (1 <= len(value) <= _MAX_CATEGORY_ID_CHARS):
        return False
    return all(
        ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch in {"-", "_"}
        for ch in value
    ) and value[0] not in {"-", "_"} and value[-1] not in {"-", "_"}


def _safe_category(raw: Any) -> dict[str, str] | None:
    """Validate category metadata as all-or-nothing optional data."""
    if not isinstance(raw, dict):
        return None
    category_id = raw.get("id")
    category_name = raw.get("name")
    if not is_valid_category_id(category_id) or not is_valid_sticker_name(category_name):
        return None
    return {"id": category_id, "name": category_name}


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


def _safe_catalog_entry(item: Any, base_url: str) -> dict[str, Any] | None:
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
    entry: dict[str, Any] = {"name": name, "url": f"{base_url}/{quote(filename, safe='-._~()')}"}
    label = _safe_display_label(item.get("label"), name)
    # Omit the field when it is equivalent to the protocol token.  That keeps
    # legacy payloads stable; newer clients fall back to `name` when absent.
    if label != name:
        entry["label"] = label
    aliases = _safe_aliases(item.get("aliases"), name)
    if aliases:
        entry["aliases"] = aliases
    return entry


@dataclass(frozen=True)
class _Source:
    manifest_path: Path | None
    manifest_url: str | None
    public_base_url: str
    category: dict[str, str] | None
    category_configured: bool


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
        self._last_good_manifests: dict[int, Any] = {}
        self._cached_at = float("-inf")
        self._cached: dict[str, Any] = {
            "ok": True,
            "version": "disabled",
            "categories": [],
            "stickers": [],
        }

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
            # Operator configuration takes priority over manifest metadata.  A
            # partial or malformed config category is not repaired from the
            # manifest: it yields no category, so a typo cannot silently put
            # stickers into an unexpected group.
            has_config_category = "category_id" in raw or "category_name" in raw
            category = None
            if has_config_category:
                category = _safe_category({
                    "id": raw.get("category_id"),
                    "name": raw.get("category_name"),
                })
            sources.append(_Source(
                manifest_path,
                manifest_url,
                base_url,
                category,
                has_config_category,
            ))
        return tuple(sources)

    @staticmethod
    def _read_source(source: _Source) -> Any:
        if source.manifest_path is not None:
            data = source.manifest_path.read_bytes()
        else:
            assert source.manifest_url is not None
            request = Request(
                source.manifest_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": STICKER_CATALOG_USER_AGENT,
                },
            )
            opener = build_opener(_NoRedirect())
            with opener.open(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # nosec B310: operator-configured HTTPS only
                data = response.read(_MAX_MANIFEST_BYTES + 1)
        if len(data) > _MAX_MANIFEST_BYTES:
            raise ValueError("sticker manifest exceeds size limit")
        return json.loads(data.decode("utf-8"))

    def _build_catalog(self) -> dict[str, Any]:
        if not self.enabled or not self.sources:
            return {"ok": True, "version": "disabled", "categories": [], "stickers": []}
        stickers: list[dict[str, Any]] = []
        categories: list[dict[str, str]] = []
        categories_by_id: dict[str, dict[str, str]] = {}
        seen_names: set[str] = set()
        for index, source in enumerate(self.sources):
            try:
                raw_manifest = self._read_source(source)
                self._last_good_manifests[index] = raw_manifest
            except Exception as exc:
                # One unavailable source does not make existing stickers turn
                # into arbitrary text/URLs.  Preserve any healthy sources, and
                # fall back to this source's last good manifest so a transient
                # timeout does not make its stickers vanish from clients.
                raw_manifest = self._last_good_manifests.get(index)
                if raw_manifest is None:
                    logger.warning("sticker catalog source unavailable: %s", exc)
                    continue
                logger.warning("sticker catalog source unavailable, serving last good manifest: %s", exc)
            raw_items = raw_manifest.get("stickers") if isinstance(raw_manifest, dict) else raw_manifest
            if not isinstance(raw_items, list):
                continue
            # A source category set in config wins over the manifest.  Without
            # source config, a manifest may opt into a category, but only with
            # a complete valid {id, name} object.  Bad metadata fails closed
            # to uncategorised stickers and can never affect the token/URL.
            category = source.category
            if category is None and not source.category_configured and isinstance(raw_manifest, dict):
                category = _safe_category(raw_manifest.get("category"))
            # The user-upload aggregate has multiple categories in one safe
            # manifest.  Those category IDs are still merely metadata: each
            # sticker has to point at an exact, validated entry below.
            manifest_categories: dict[str, dict[str, str]] = {}
            if category is None and not source.category_configured and isinstance(raw_manifest, dict):
                raw_categories = raw_manifest.get("categories")
                if isinstance(raw_categories, list):
                    for raw_category in raw_categories:
                        candidate = _safe_category(raw_category)
                        if candidate is not None and candidate["id"] not in manifest_categories:
                            manifest_categories[candidate["id"]] = candidate
            for raw_item in raw_items:
                entry = _safe_catalog_entry(raw_item, source.public_base_url)
                if entry is None or entry["name"] in seen_names:
                    continue
                item_category = category
                if item_category is None and manifest_categories and isinstance(raw_item, dict):
                    requested_id = raw_item.get("category_id")
                    item_category = manifest_categories.get(requested_id) if isinstance(requested_id, str) else None
                category_id = item_category["id"] if item_category is not None else None
                if category_id is not None:
                    # Return only categories that have a visible sticker.
                    # Equal ID/name pairs merge across sources.  An ID reused
                    # with a different name fails closed to uncategorised for
                    # the conflicting source: the sticker remains visible but
                    # is never silently placed in the first source's group.
                    registered_category = categories_by_id.get(category_id)
                    if registered_category is None:
                        categories_by_id[category_id] = item_category
                        categories.append(item_category)
                        entry["category_id"] = category_id
                    elif registered_category["name"] == item_category["name"]:
                        entry["category_id"] = category_id
                seen_names.add(entry["name"])
                stickers.append(entry)
                if len(stickers) >= self.max_items:
                    break
            if len(stickers) >= self.max_items:
                break
        # Names always win over aliases: an alias equal to any visible sticker
        # name, or already claimed by an earlier sticker, is dropped so a
        # legacy token can never become ambiguous or shadow a live name.
        visible_names = {entry["name"] for entry in stickers}
        claimed_aliases: set[str] = set()
        for entry in stickers:
            aliases = entry.get("aliases")
            if not aliases:
                continue
            kept = [alias for alias in aliases
                    if alias not in visible_names and alias not in claimed_aliases]
            claimed_aliases.update(kept)
            if kept:
                entry["aliases"] = kept
            else:
                del entry["aliases"]
        fingerprint = hashlib.sha256(
            json.dumps(
                {"categories": categories, "stickers": stickers},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return {"ok": True, "version": fingerprint, "categories": categories, "stickers": stickers}

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at < self.cache_seconds:
                return dict(self._cached)
            self._cached = self._build_catalog()
            self._cached_at = now
            return dict(self._cached)

    def invalidate(self) -> None:
        """Force the next request to fetch configured manifests again."""
        with self._lock:
            self._cached_at = float("-inf")

    def normalize_outgoing_text(self, text: str) -> str:
        """Fail-open ``normalize_bqb_tokens`` over the live catalog snapshot.

        A catalog that cannot be built (or a bug in the normalizer itself)
        must never eat or corrupt an assistant reply, so every failure path
        returns the original text untouched.
        """
        if not text or "[bqb" not in text:
            return text
        try:
            snapshot = self.snapshot()
        except Exception:
            logger.warning(
                "sticker catalog snapshot unavailable; leaving [bqb:] tokens untouched",
                exc_info=True,
            )
            return text
        try:
            return normalize_bqb_tokens(text, snapshot)
        except Exception:
            logger.warning(
                "bqb token normalization failed; leaving text untouched",
                exc_info=True,
            )
            return text


# Same token shape as the clients (windows-pwa sticker-protocol.js /
# StickerProtocol.kt), plus the full-width colon variant models keep emitting:
# ``[bqb：名字]`` would otherwise stay dead text.  No ``[``/``]``/newline
# inside, 1..80 chars: a greedy match must not swallow a second token, and
# half-written or nested text is never captured (catalog names themselves may
# never contain brackets per is_valid_sticker_name).
_BQB_TOKEN_RE = re.compile(r"\[bqb[:：]([^\[\]\r\n]{1,80})\]")
# 「哥哥熊」 is an exclusive pack: it may only be hit by an exact full name or
# an explicit ``哥哥熊·`` prefix.  Bare-true-name / wrong-prefix rescue must
# never rewrite a token into this pack — dead text beats a misfired sticker.
_EXCLUSIVE_CATEGORY_NAME = "哥哥熊"


def _token_truename(name: str) -> str:
    """Strip the leading ``分类·`` segment; the remainder is the true name."""
    parts = name.split("·")
    return "·".join(parts[1:]) if len(parts) > 1 else name


def normalize_bqb_tokens(text: str, snapshot: dict[str, Any]) -> str:
    """Best-effort rescue of ``[bqb:name]`` tokens in an outgoing AI message.

    For every token (half-width ``:`` or full-width ``：`` colon):

    1. exact catalog ``name`` hit → keep the name (colon normalized to ``:``);
    2. exact ``aliases`` hit → rewrite to that entry's ``name``;
    3. otherwise strip the (possibly wrong) category prefix and match the true
       name against every entry's name-truename / label / aliases:
       exactly one match → that entry's ``name``; several → prefer an entry
       whose ``label`` equals the true name, ties broken by catalog order;
       none → keep the token byte-for-byte (dead text over wrong sticker).

    「哥哥熊」 entries only participate in steps 2/3 when the token explicitly
    starts with ``哥哥熊·``; an explicit prefix searches inside that category.
    """
    if not text or "[bqb" not in text or not isinstance(snapshot, dict):
        return text
    entries = [
        entry
        for entry in (snapshot.get("stickers") or [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    if not entries:
        return text
    category_names = {
        category.get("id"): category.get("name")
        for category in (snapshot.get("categories") or [])
        if isinstance(category, dict)
    }

    def is_exclusive(entry: dict[str, Any]) -> bool:
        category_id = entry.get("category_id")
        return (
            category_id is not None
            and category_names.get(category_id) == _EXCLUSIVE_CATEGORY_NAME
        )

    by_name: dict[str, dict[str, Any]] = {}
    by_alias: dict[str, dict[str, Any]] = {}
    for entry in entries:
        by_name.setdefault(entry["name"], entry)
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str):
                by_alias.setdefault(alias, entry)

    def rewrite(match: re.Match[str]) -> str:
        raw = match.group(1)
        explicit_exclusive = (
            "·" in raw and raw.split("·", 1)[0] == _EXCLUSIVE_CATEGORY_NAME
        )
        resolved: str | None = None
        hit = by_name.get(raw)
        if hit is not None:
            # Exact full name always wins, 「哥哥熊·…」 included (rule 4).
            resolved = hit["name"]
        else:
            hit = by_alias.get(raw)
            if hit is not None and (explicit_exclusive or not is_exclusive(hit)):
                resolved = hit["name"]
            else:
                true_name = _token_truename(raw)
                if explicit_exclusive:
                    pool = [entry for entry in entries if is_exclusive(entry)]
                else:
                    pool = [entry for entry in entries if not is_exclusive(entry)]
                matches: list[dict[str, Any]] = []
                for entry in pool:
                    candidates = {_token_truename(entry["name"])}
                    label = entry.get("label")
                    if isinstance(label, str):
                        candidates.add(label)
                    for alias in entry.get("aliases") or []:
                        if isinstance(alias, str):
                            candidates.add(alias)
                    if true_name in candidates:
                        matches.append(entry)
                if matches:
                    label_hits = [
                        entry for entry in matches if entry.get("label") == true_name
                    ]
                    resolved = (label_hits or matches)[0]["name"]
        if resolved is None:
            # 宁缺毋滥: keep the original token exactly, colon included.
            return match.group(0)
        new_token = f"[bqb:{resolved}]"
        if new_token != match.group(0):
            logger.info("bqb token rewrite: %s -> %s", match.group(0), new_token)
        return new_token

    return _BQB_TOKEN_RE.sub(rewrite, text)
