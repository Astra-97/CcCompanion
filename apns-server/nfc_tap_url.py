#!/usr/bin/env python3
"""Print the URL to write onto the NFC "tap to summon 小克" tag.

Installed as /usr/local/bin/nfc-tap-url (symlink).  The secret lives only in
a 0600 file outside git (default /root/.config/cc-companion/nfc_tap_secret,
override with CC_COMPANION_NFC_TAP_SECRET_FILE) and is read at runtime.

  nfc-tap-url            print the tag URL
  nfc-tap-url --init     create the secret file if missing, then print
  nfc-tap-url --rotate   replace the secret (old tags stop working), print
  nfc-tap-url --test     print the URL with &test=1 (notification marked test)
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

DEFAULT_SECRET_FILE = Path("/root/.config/cc-companion/nfc_tap_secret")
DEFAULT_BASE = "https://companion-vps2.xiaonancaleb.xyz"
CONFIG = Path(__file__).resolve().parent / "config.toml"


def secret_path() -> Path:
    raw = os.environ.get("CC_COMPANION_NFC_TAP_SECRET_FILE", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_SECRET_FILE


def write_secret(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (secrets.token_urlsafe(18) + "\n").encode())
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
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    path = secret_path()
    if args.rotate or (args.init and not path.exists()):
        write_secret(path)
    try:
        secret = path.read_text("utf-8").strip()
    except OSError:
        print(f"no secret at {path}; run: nfc-tap-url --init", file=sys.stderr)
        return 1
    if len(secret) < 16:
        print(f"secret at {path} too short; run: nfc-tap-url --rotate", file=sys.stderr)
        return 1
    url = f"{base_url()}/nfc/tap?k={quote(secret, safe='')}"
    if args.test:
        url += "&test=1"
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
