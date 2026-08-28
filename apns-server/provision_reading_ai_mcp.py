#!/usr/bin/env python3
"""Stage the fixed-contact Co-Reading MCP bridge without provisioning secrets."""
from __future__ import annotations

import argparse
import os
import re
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
from pathlib import Path


BRIDGE_NAME = "cc-companion-reading-ai-mcp"
CONTACT_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path); os.chmod(path, mode)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _prepared_codex_config(path: Path, contact_id: str) -> bytes | None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try: parsed = tomllib.loads(text) if text else {}
    except (ValueError, TypeError) as error: raise RuntimeError("refusing invalid Codex TOML") from error
    servers = parsed.get("mcp_servers", {})
    if not isinstance(servers, dict): raise RuntimeError("refusing invalid Codex mcp_servers table")
    name = f"reading_continue_{contact_id}"
    desired = {"command": f"/usr/local/libexec/{BRIDGE_NAME}", "args": [contact_id]}
    current = servers.get(name)
    if current is not None and current != desired: raise RuntimeError(f"refusing to replace [mcp_servers.{name}]")
    # A second registration pointing at this executable can silently bind a
    # different fixed identity.  Provisioning deliberately refuses that
    # cross-bridge overwrite instead of guessing which configuration wins.
    for existing_name, existing in servers.items():
        if existing_name == name or not isinstance(existing, dict):
            continue
        if existing.get("command") == desired["command"]:
            raise RuntimeError(f"refusing existing reading bridge registration: {existing_name}")
    if current is not None:
        return None
    return (text + f'\n[mcp_servers.{name}]\ncommand = "/usr/local/libexec/{BRIDGE_NAME}"\nargs = ["{contact_id}"]\n').encode("utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--contact", required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("reading_ai_mcp_bridge.py"))
    parser.add_argument("--bridge", type=Path, default=Path("/usr/local/libexec") / BRIDGE_NAME)
    parser.add_argument("--codex-config", type=Path, default=Path("/root/.codex/config.toml"))
    args = parser.parse_args(argv)
    contact = args.contact.strip().lower()
    if not CONTACT_RE.fullmatch(contact): parser.error("invalid fixed contact")
    actions = [f"install bridge at {args.bridge}", f"append fixed {contact} registration to {args.codex_config}", "leave all credentials absent; install a separate 0600 credential before enabling"]
    if not args.apply:
        print("Dry run; no files changed:\n- " + "\n- ".join(actions)); return 0
    # Validate every target before changing any one of them.  A partial
    # install would otherwise leave a live bridge without its matching config
    # (or vice versa), and must be repaired manually rather than overwritten.
    data = args.source.read_bytes()
    if not data.startswith(b"#!/usr/bin/env python3"): raise RuntimeError("bridge source has unexpected format")
    config_data = _prepared_codex_config(args.codex_config, contact)
    if args.bridge.exists() and args.bridge.read_bytes() != data:
        raise RuntimeError(f"refusing to replace a different bridge at {args.bridge}")
    _atomic_write(args.bridge, data, 0o755)
    if config_data is not None:
        _atomic_write(args.codex_config, config_data, 0o600)
    print("Installed bridge registration; no service or session was restarted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
