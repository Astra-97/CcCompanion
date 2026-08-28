#!/usr/bin/env python3
"""Fixed-contact stdio MCP bridge for AI-assisted Co-Reading.

This is intentionally not a general reading API.  The sole tool accepts a
bounded character count and an idempotency key; its contact identity, private
credential and proxy endpoint are loaded from one 0600 deployment file.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MAX_STDIN_LINE = 16 * 1024
DEFAULT_CREDENTIAL_ROOT = Path("/var/lib/cc-xia-relay/channel-state/reading-ai-bridges")
CONTACT_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
REQUEST_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _credential(contact_id: str) -> dict[str, str] | None:
    root = Path(os.environ.get("CC_COMPANION_READING_AI_MCP_CREDENTIAL_ROOT", str(DEFAULT_CREDENTIAL_ROOT)))
    path = root / f"{contact_id}.json"
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
            ):
                return None
            raw_bytes = os.read(fd, 16 * 1024 + 1)
        finally:
            os.close(fd)
        if len(raw_bytes) > 16 * 1024:
            return None
        raw = json.loads(raw_bytes.decode("utf-8"))
        endpoint, token, bound_contact = raw.get("endpoint"), raw.get("token"), raw.get("contactId")
        if (
            not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")) or len(endpoint) > 2048
            or not isinstance(token, str) or not 16 <= len(token) <= 4096
            or bound_contact != contact_id
        ):
            return None
        return {"endpoint": endpoint.rstrip("/"), "token": token}
    except (OSError, UnicodeError, ValueError, AttributeError):
        return None


def _tool() -> dict[str, Any]:
    return {
        "name": "continue_reading",
        "description": "Continue only the user-authorized reading snapshot for this fixed chat identity.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requestedChars", "requestId"],
            "properties": {
                "requestedChars": {"type": "integer", "minimum": 1, "maximum": 1000},
                "requestId": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,128}$"},
            },
        },
    }


def _call(credential: dict[str, str], contact_id: str, arguments: Any) -> tuple[bool, str]:
    if not isinstance(arguments, dict) or set(arguments) != {"requestedChars", "requestId"}:
        return False, "参数必须只有 requestedChars 与 requestId"
    requested, request_id = arguments.get("requestedChars"), arguments.get("requestId")
    if isinstance(requested, bool) or not isinstance(requested, int) or not 1 <= requested <= 1000 or not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        return False, "续读参数无效"
    payload = json.dumps({"requestedChars": requested, "requestId": request_id}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        credential["endpoint"] + "/reading/ai/continue", data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CC-Reading-AI-Contact": contact_id,
            "X-CC-Reading-AI-Token": credential["token"],
        },
    )
    try:
        with urllib.request.build_opener().open(request, timeout=25) as response:
            raw = response.read(32 * 1024)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False, "续读服务暂时不可用"
    try:
        value = json.loads(raw)
        text = value.get("text") if isinstance(value, dict) else None
        if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > 1000:
            return False, "续读服务返回无效内容"
        return True, text
    except (UnicodeError, ValueError, AttributeError):
        return False, "续读服务返回无效内容"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not CONTACT_RE.fullmatch(argv[1]):
        return 64
    contact_id = argv[1]
    credential = _credential(contact_id)
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > MAX_STDIN_LINE:
            continue
        try:
            message = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            continue
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": f"reading-continue-{contact_id}", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [_tool()]}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or params.get("name") != "continue_reading":
                answer = _error(request_id, -32601, "工具不存在")
                sys.stdout.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n"); sys.stdout.flush(); continue
            if credential is None:
                ok, content = False, "服务尚未为此固定身份配置"
            else:
                ok, content = _call(credential, contact_id, params.get("arguments"))
            result = {"content": [{"type": "text", "text": content}], "isError": not ok}
        elif method == "notifications/initialized":
            continue
        else:
            answer = _error(request_id, -32601, "方法不存在")
            sys.stdout.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n"); sys.stdout.flush(); continue
        if "id" in message:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
