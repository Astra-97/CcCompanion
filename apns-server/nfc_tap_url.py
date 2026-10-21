#!/usr/bin/env python3
"""Print / rotate the URL to write onto an NFC crab tag.

Installed as /usr/local/bin/nfc-tap-url (symlink).  Keys live only in a 0600
file outside git (default /root/.config/cc-companion/nfc_tags.json, override
with CC_COMPANION_NFC_TAGS_FILE) and are read at runtime:

  {"tags": {"bedside": {"key", "name", "location", "action": "summon"},
            "door":    {"key", "name", "location", "action": "home"}}}

On first run the legacy single-key file (nfc_tap_secret) is migrated in as
"bedside" so the tag already written keeps working.

  nfc-tap-url [TAG]            print TAG's URL (default: bedside)
  nfc-tap-url TAG --init       create TAG's key if missing, then print
  nfc-tap-url TAG --rotate     replace TAG's key (that tag must be rewritten)
  nfc-tap-url TAG --test       print the URL with &test=1 (no pill, devices dry-run)
  nfc-tap-url --list           list tags (no keys)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

CONF_DIR = Path("/root/.config/cc-companion")
DEFAULT_TAGS_FILE = CONF_DIR / "nfc_tags.json"
LEGACY_SECRET_FILE = CONF_DIR / "nfc_tap_secret"
DEFAULT_BASE = "https://companion-vps2.xiaonancaleb.xyz"
CONFIG = Path(__file__).resolve().parent / "config.toml"

PROFILES = {
    "bedside": {"name": "床头螃蟹", "location": "床头", "action": "summon"},
    "door": {"name": "门口螃蟹", "location": "门口", "action": "home"},
}


def tags_path() -> Path:
    raw = os.environ.get("CC_COMPANION_NFC_TAGS_FILE", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_TAGS_FILE


def load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    data: dict = {"tags": {}}
    try:
        legacy = LEGACY_SECRET_FILE.read_text("utf-8").strip()
    except OSError:
        legacy = ""
    if len(legacy) >= 16:
        data["tags"]["bedside"] = {"key": legacy, **PROFILES["bedside"]}
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def base_url() -> str:
    try:
        m = re.search(r'^\s*server_url\s*=\s*"(https://[^"]+)"', CONFIG.read_text("utf-8"), re.M)
        if m:
            return m.group(1).rstrip("/")
    except OSError:
        pass
    return DEFAULT_BASE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag", nargs="?", default="bedside")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", args.tag):
        print("bad tag id", file=sys.stderr)
        return 2
    path = tags_path()
    data = load(path)
    tags = data.setdefault("tags", {})
    dirty = not path.exists() and bool(tags)
    if args.list:
        for tid, prof in tags.items():
            print(f"{tid}\t{prof.get('name')}\t{prof.get('location')}\t{prof.get('action')}")
        if dirty:
            save(path, data)
        return 0
    if args.rotate or (args.init and args.tag not in tags):
        prof = tags.get(args.tag) or {**PROFILES.get(args.tag, {"name": args.tag, "location": args.tag, "action": "summon"})}
        prof["key"] = secrets.token_urlsafe(18)
        tags[args.tag] = prof
        dirty = True
    if dirty:
        save(path, data)
    prof = tags.get(args.tag)
    if not prof or len(str(prof.get("key") or "")) < 16:
        print(f"no key for tag '{args.tag}'; run: nfc-tap-url {args.tag} --init", file=sys.stderr)
        return 1
    url = f"{base_url()}/nfc/tap?k={quote(prof['key'], safe='')}"
    if args.test:
        url += "&test=1"
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
