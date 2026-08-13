#!/usr/bin/env python3
"""Stage, then explicitly install the shared food-order MCP runtime.

Run with ``--apply`` only during a maintenance window.  It intentionally does
not restart Codex, the dedicated XiaoKe Claude channel, or the app server:
existing sessions discover MCP tools only after their next/new session.
"""
from __future__ import annotations

import argparse
import os
import pwd
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
from pathlib import Path


BRIDGE_NAME = "cc-companion-mcp-bridge"
PROVIDERS = ("luckin", "mcdonalds")
CANONICAL_TOKEN_ROOT = Path("/var/lib/cc-xia-relay/channel-state/mcp-tokens")
LEGACY_TOKEN_PATHS = {
    "luckin": Path("/root/.my-coffee/LUCKIN_MCP_TOKEN"),
    "mcdonalds": Path("/root/.my-coffee/MCDONALDS_MCP_TOKEN"),
}


def _bridge_toml(provider: str) -> str:
    return (
        f'\n[mcp_servers.{provider}]\n'
        'command = "/usr/local/libexec/cc-companion-mcp-bridge"\n'
        f'args = ["{provider}"]\n'
    )


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def _append_codex_config(path: Path) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        parsed = tomllib.loads(text) if text else {}
    except (ValueError, TypeError) as error:
        raise RuntimeError("refusing to edit invalid Codex TOML") from error
    registered = parsed.get("mcp_servers", {})
    if not isinstance(registered, dict):
        raise RuntimeError("refusing invalid Codex mcp_servers table")
    for provider in PROVIDERS:
        heading = f"[mcp_servers.{provider}]"
        desired = {
            "command": "/usr/local/libexec/cc-companion-mcp-bridge",
            "args": [provider],
        }
        current = registered.get(provider)
        if current is not None and current != desired:
            raise RuntimeError(f"refusing to replace existing {heading}")
        if current is None:
            text += _bridge_toml(provider)
    _atomic_write(path, text.encode("utf-8"), 0o600)


def _install_bridge(source: Path, target: Path) -> None:
    data = source.read_bytes()
    if not data.startswith(b"#!/usr/bin/env python3"):
        raise RuntimeError("bridge source has an unexpected format")
    _atomic_write(target, data, 0o755)


def _read_legacy_token(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
            return b""
        return path.read_bytes().strip()
    except OSError:
        return b""


def _prepare_canonical_tokens(root: Path) -> None:
    """Create only the fixed relay-owned directory and import no-overwrite."""
    account = pwd.getpwnam("cc-xia-relay")
    root.mkdir(parents=True, exist_ok=True)
    info = root.lstat()
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("canonical MCP token root is unsafe")
    os.chown(root, account.pw_uid, account.pw_gid)
    os.chmod(root, 0o700)
    for provider in PROVIDERS:
        target = root / f"{provider}.token"
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"canonical {provider} token is unsafe")
            continue
        token = _read_legacy_token(LEGACY_TOKEN_PATHS[provider])
        if token:
            _atomic_write(target, token + b"\n", 0o600)
            os.chown(target, account.pw_uid, account.pw_gid)
    # Legacy sources are intentionally left untouched for rollback/audit.


def _install_xia_runtime_templates(source_dir: Path, target_dir: Path) -> None:
    """Deploy exactly the files read by the production XiaoKe launcher."""
    for name in (".mcp.json.in", "settings.json"):
        source = source_dir / name
        data = source.read_bytes()
        # Validate before publishing a strict-MCP config into the live install.
        import json
        json.loads(data.decode("utf-8"))
        _atomic_write(target_dir / name, data, 0o644)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the explicitly staged installation")
    parser.add_argument("--source", type=Path, default=Path(__file__).with_name("mcp_bridge.py"))
    parser.add_argument("--bridge", type=Path, default=Path("/usr/local/libexec") / BRIDGE_NAME)
    parser.add_argument("--codex-config", type=Path, default=Path("/root/.codex/config.toml"))
    parser.add_argument("--xia-source-dir", type=Path, default=Path(__file__).parent / "xia_claude_channel")
    parser.add_argument("--xia-install-dir", type=Path, default=Path("/opt/cc-xia-claude-channel"))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("must run as root")
    actions = [
        f"install fixed stdio bridge at {args.bridge}",
        f"append missing fixed bridge registrations to {args.codex_config}",
        f"create relay-owned canonical token root at {CANONICAL_TOKEN_ROOT} and import missing legacy tokens",
        f"deploy XiaoKe MCP template/settings to {args.xia_install_dir}, then start a new/restarted XiaoKe session",
        "start a new/restarted Kairos Codex session for tool discovery",
    ]
    if not args.apply:
        print("Dry run; no files changed:\n- " + "\n- ".join(actions))
        return 0
    _install_bridge(args.source, args.bridge)
    _append_codex_config(args.codex_config)
    _prepare_canonical_tokens(CANONICAL_TOKEN_ROOT)
    _install_xia_runtime_templates(args.xia_source_dir, args.xia_install_dir)
    print("Installed configuration; no services or sessions were restarted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
