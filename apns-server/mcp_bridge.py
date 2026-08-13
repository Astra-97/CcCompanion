#!/usr/bin/env python3
"""Fixed-provider stdio MCP bridge for the Kairos and XiaoKe runtimes.

It is deliberately not a generic HTTP proxy: one positional provider id is
allow-listed, endpoints are compiled in, credentials are reread from a 0600
server file for every upstream request, and nothing is logged to stdout.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MAX_STDIN_LINE = 256 * 1024
MCP_TEST_TIMEOUT_SECONDS = 8
# McDonald's currently returns roughly 85 KiB for tools/list.  Keep the read
# bounded, but leave enough headroom for the official catalog to grow.
MCP_TEST_RESPONSE_LIMIT = 256 * 1024
PROVIDERS = {
    "luckin": {
        "endpoint": "https://gwmcp.lkcoffee.com/order/user/mcp",
        "default_token_path": "/var/lib/cc-xia-relay/channel-state/mcp-tokens/luckin.token",
        "env_path": "LUCKIN_MCP_TOKEN_PATH",
    },
    "mcdonalds": {
        "endpoint": "https://mcp.mcd.cn",
        "default_token_path": "/var/lib/cc-xia-relay/channel-state/mcp-tokens/mcdonalds.token",
        "env_path": "MCDONALDS_MCP_TOKEN_PATH",
    },
}
_session_id: str | None = None


def _token(provider_id: str) -> str:
    provider = PROVIDERS[provider_id]
    path = Path(os.environ.get(provider["env_path"], provider["default_token_path"]))
    try:
        if not path.is_file() or path.stat().st_size > 16 * 1024:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _first_mcp_message(raw: bytes, content_type: str) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace").strip()
    candidates = [text]
    if "text/event-stream" in content_type.lower():
        candidates = []
        data_lines: list[str] = []
        for line in text.splitlines():
            if not line:
                if data_lines:
                    candidates.append("\n".join(data_lines))
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            candidates.append("\n".join(data_lines))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("jsonrpc") == "2.0":
            return value
    return None


def forward(provider_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
    global _session_id
    token = _token(provider_id)
    request_id = message.get("id")
    if not token:
        return _error(request_id, -32000, "服务尚未在服务器端配置") if "id" in message else None
    raw_body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw_body) > MAX_STDIN_LINE:
        return _error(request_id, -32600, "MCP 请求过大") if "id" in message else None
    method = message.get("method")
    if method == "initialize":
        # A client may start a new session in the same bridge process.
        _session_id = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
        "User-Agent": "CcCompanion-MCP-Bridge/1",
    }
    if _session_id and method != "initialize":
        headers["Mcp-Session-Id"] = _session_id
    request = urllib.request.Request(
        PROVIDERS[provider_id]["endpoint"], data=raw_body, method="POST",
        headers=headers,
    )
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=MCP_TEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MCP_TEST_RESPONSE_LIMIT + 1)
            if len(raw) > MCP_TEST_RESPONSE_LIMIT:
                return _error(request_id, -32001, "上游响应过大") if "id" in message else None
            answer = _first_mcp_message(raw, response.headers.get("Content-Type", ""))
            if method == "initialize":
                candidate = response.headers.get("Mcp-Session-Id", "").strip()
                _session_id = candidate[:512] or None
            return answer or (_error(request_id, -32002, "上游返回无效 MCP 响应") if "id" in message else None)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            _session_id = None
            message_text = "MCP 会话已失效，请重新初始化"
        else:
            message_text = "服务拒绝了凭据" if error.code in {401, 403} else "服务暂时不可用"
        return _error(request_id, -32003, message_text) if "id" in message else None
    except (urllib.error.URLError, TimeoutError, OSError):
        return _error(request_id, -32004, "服务连接失败") if "id" in message else None


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in PROVIDERS:
        return 64
    provider_id = argv[1]
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > MAX_STDIN_LINE:
            continue
        try:
            message = json.loads(raw_line)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            continue
        result = forward(provider_id, message)
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
