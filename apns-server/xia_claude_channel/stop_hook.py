#!/usr/bin/env python3
"""Fail-closed Stop fallback for the isolated Xia Claude TUI only."""
from __future__ import annotations
import html, json, os, re, sys, time, urllib.request
from pathlib import Path
from typing import Any

URL = "http://127.0.0.1:8821/fallback"
CHANNEL_TAG_RE = re.compile(r"<channel\b([^>]*)>", re.I | re.S)
META_ATTR_RE = re.compile(r"(?:^|\s)metadata_json\s*=\s*([\"'])(.*?)\1", re.I | re.S)

def strings(value: Any):
    if isinstance(value, str): yield value
    elif isinstance(value, dict):
        for child in value.values(): yield from strings(child)
    elif isinstance(value, list):
        for child in value: yield from strings(child)

def record_role(record: dict[str, Any]) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    return str(message.get("role") or "")

def exact_meta_with_marker(records: list[dict[str, Any]]) -> tuple[dict[str, Any], int] | None:
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        if record_role(record) != "user": continue
        for text in strings(record):
            for attributes in reversed(CHANNEL_TAG_RE.findall(text)):
                match = META_ATTR_RE.search(attributes)
                if not match: continue
                raw = html.unescape(match.group(2))
                try: meta = json.loads(raw)
                except Exception: continue
                if meta.get("contact_id") == "ai-custom" and meta.get("provider") == "claude":
                    if (
                        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", str(meta.get("request_id") or ""))
                        and isinstance(meta.get("epoch"), int) and meta["epoch"] >= 0
                        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", str(meta.get("lease") or ""))
                    ):
                        return meta, index
    return None

def exact_meta(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    found = exact_meta_with_marker(records)
    return found[0] if found else None

def assistant_text(record: dict[str, Any]) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    if message.get("role") != "assistant": return ""
    content = message.get("content")
    if isinstance(content, str): return content.strip()
    if isinstance(content, list):
        return "".join(str(x.get("text") or "") for x in content if isinstance(x, dict) and x.get("type") == "text").strip()
    return ""

def load_stable(path: Path) -> list[dict[str, Any]]:
    previous = -1
    for _ in range(4):
        try: size = path.stat().st_size
        except OSError: return []
        if size == previous: break
        previous = size; time.sleep(0.15)
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try: value = json.loads(line)
            except Exception: continue
            if isinstance(value, dict): out.append(value)
    except OSError: return []
    return out

def main() -> int:
    try: hook = json.load(sys.stdin)
    except Exception: return 0
    transcript = Path(str(hook.get("transcript_path") or ""))
    if not transcript.is_absolute(): return 0
    records = load_stable(transcript)
    found = exact_meta_with_marker(records)
    if not found: return 0
    meta, marker = found
    if any(record_role(record) == "user" for record in records[marker + 1:]):
        return 0
    text = str(hook.get("last_assistant_message") or "").strip()
    if not text:
        # Only text after the exact correlated inbound channel record.
        text = "\n".join(filter(None, (assistant_text(r) for r in records[marker + 1:]))) if marker >= 0 else ""
    if not text: return 0
    token_file = Path(os.environ.get("XIA_CHANNEL_TOKEN_FILE", "/var/lib/cc-xia-relay/channel-state/channel.token"))
    try: token = token_file.read_text(encoding="utf-8").strip()
    except OSError: return 0
    body = json.dumps({"request_id": meta["request_id"], "epoch": meta["epoch"], "lease": meta["lease"], "text": text}).encode()
    request = urllib.request.Request(URL, data=body, method="POST", headers={"Content-Type": "application/json", "X-Auth-Token": token})
    try: urllib.request.urlopen(request, timeout=5).read()
    except Exception: pass
    return 0

if __name__ == "__main__": raise SystemExit(main())
