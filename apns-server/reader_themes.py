"""Server-owned reader theme palette for the Android co-reading screen.

The App ships built-in defaults identical to ``DEFAULT_READER_THEMES`` and
fetches ``GET /kimi/reader-themes`` when the reader opens.  Editing
``reader-themes.json`` next to ``push.py`` therefore re-tints every client
without an App release.  Any missing or malformed content falls back to the
built-in defaults per theme, so a bad edit can never break the reader.

Color values are ``[r, g, b]`` or ``[r, g, b, a]`` arrays with 8-bit channels
and a 0..1 alpha.  ``paper`` is either ``null`` (keep the live wallpaper
glass) or a 2-3 color vertical gradient for a paper surface.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
from typing import Any

READER_THEMES_VERSION = 1
DEFAULT_READER_THEME_ID = "green"

READER_THEMES_MAX_BYTES = 64 * 1024

DEFAULT_READER_THEMES: dict[str, Any] = {
    "version": READER_THEMES_VERSION,
    "default_theme": DEFAULT_READER_THEME_ID,
    "themes": {
        "glass": {
            "label": "玻璃",
            "paper": None,
            "paper_border": [255, 247, 238, 0.24],
            "ink": [247, 238, 229],
            "dim": [255, 240, 228, 0.68],
            "faint": [255, 238, 224, 0.44],
            "link": [143, 195, 232],
        },
        "green": {
            "label": "护眼绿",
            "paper": [[217, 242, 214], [213, 239, 210], [207, 234, 204]],
            "paper_border": [94, 140, 98, 0.30],
            "ink": [45, 70, 49],
            "dim": [95, 125, 99],
            "faint": [148, 174, 145],
            "link": [11, 125, 196],
        },
        "brown": {
            "label": "暖棕",
            "paper": [[239, 225, 198], [228, 210, 176], [221, 201, 164]],
            "paper_border": [140, 100, 58, 0.32],
            "ink": [75, 58, 34],
            "dim": [124, 102, 64],
            "faint": [166, 144, 106],
            "link": [62, 110, 158],
        },
    },
}

READER_THEME_IDS = tuple(DEFAULT_READER_THEMES["themes"].keys())

# Resolved once at import; tests substitute their own path explicitly.
READER_THEMES_CONFIG_PATH = Path(__file__).resolve().parent / "reader-themes.json"


def _valid_channel(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255


def _valid_alpha(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= float(value) <= 1.0
    )


def _valid_color(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) in (3, 4)
        and all(_valid_channel(channel) for channel in value[:3])
        and (len(value) == 3 or _valid_alpha(value[3]))
    )


def _valid_paper(value: Any) -> bool:
    return value is None or (
        isinstance(value, list)
        and 2 <= len(value) <= 3
        and all(_valid_color(stop) and len(stop) == 3 for stop in value)
    )


def _valid_theme(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    label = value.get("label")
    return (
        isinstance(label, str)
        and 0 < len(label.strip()) <= 24
        and _valid_paper(value.get("paper"))
        and _valid_color(value.get("paper_border"))
        and _valid_color(value.get("ink"))
        and _valid_color(value.get("dim"))
        and _valid_color(value.get("faint"))
        and _valid_color(value.get("link"))
    )


def _normalize_theme(theme: dict[str, Any]) -> dict[str, Any]:
    """Keep only the known fields so the payload stays a closed world."""
    normalized = {
        "label": str(theme["label"]).strip(),
        "paper": theme.get("paper"),
        "paper_border": theme["paper_border"],
        "ink": theme["ink"],
        "dim": theme["dim"],
        "faint": theme["faint"],
        "link": theme["link"],
    }
    return normalized


def load_reader_themes(path: str | Path = READER_THEMES_CONFIG_PATH) -> dict[str, Any]:
    """Return the theme payload, falling back to built-in defaults per theme."""
    payload = copy.deepcopy(DEFAULT_READER_THEMES)
    try:
        candidate = Path(path).expanduser()
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > READER_THEMES_MAX_BYTES:
            return payload
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return payload
    if not isinstance(raw, dict) or raw.get("version") != READER_THEMES_VERSION:
        return payload
    themes = raw.get("themes")
    if isinstance(themes, dict):
        for theme_id in READER_THEME_IDS:
            override = themes.get(theme_id)
            if _valid_theme(override):
                payload["themes"][theme_id] = _normalize_theme(override)
    default_theme = str(raw.get("default_theme") or "").strip().lower()
    if default_theme in READER_THEME_IDS:
        payload["default_theme"] = default_theme
    return payload


def reader_themes_config_path(server_cfg: dict[str, Any] | None = None) -> Path:
    """Allow config.toml to relocate the file; default sits next to push.py."""
    override = ""
    if isinstance(server_cfg, dict):
        override = str(server_cfg.get("reader_themes_config_path") or "").strip()
    if override:
        expanded = Path(os.path.expanduser(override))
        if expanded.is_absolute():
            return expanded
    return READER_THEMES_CONFIG_PATH
