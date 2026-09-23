#!/usr/bin/env python3
"""Fixed-contact stdio MCP bridge for AI-assisted Co-Reading.

This is intentionally not a general reading API.  The contact identity,
private credential and proxy endpoint are loaded from one 0600 deployment
file; no tool accepts a contact, URL, path or credential argument.

Tools (2026-09-23, Astra authorized private AI contacts to browse the shelf):

* ``list_bookshelf``   – shelf metadata only (title/author/chapters/size).
* ``list_chapters``    – one shelf book's chapter ids/titles/sizes, no text.
* ``continue_reading`` – at most 1000 UTF-16 units per call, either from this
  identity's current anchor or from a self-selected shelf book/chapter.

The server re-validates every book/chapter/offset, keeps the idempotency
ledger, writes only the reading system card, and never touches Astra's own
reading progress.
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
MAX_CONTINUE_RESPONSE = 32 * 1024
MAX_LISTING_RESPONSE = 512 * 1024
DEFAULT_CREDENTIAL_ROOT = Path("/var/lib/cc-xia-relay/channel-state/reading-ai-bridges")
CONTACT_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
REQUEST_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
# Mirrors the server's _READING_ID_RE; the server re-validates regardless.
READING_ID_RE = re.compile(r"[A-Za-z0-9._\-一-鿿]{1,128}")
MAX_OFFSET = 16_000_000
PRIVATE_ONLY = "仅限与方小南的私聊使用；在苹果幼稚园群聊里不要调用，也不要把书的正文贴进群。"


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


_ID_PATTERN = "^[A-Za-z0-9._\\-\\u4e00-\\u9fff]{1,128}$"


def _tool() -> dict[str, Any]:
    return {
        "name": "continue_reading",
        "description": (
            "续读方小南书架上的书，单次最多 1000 字。只传 requestedChars+requestId 时从你自己的当前锚点接着读；"
            "传 bookId 可以自己挑书架上任意一本（不带 chunkId 时从你在这本书的书签处继续，没有书签则从第一章开头）；"
            "再传 chunkId（和可选 anchorOffset，UTF-16 偏移）可以从任意章节/位置开始。"
            "requestId 是幂等键：同一个 requestId 重试会得到同一段结果，新读一段请换新的 requestId。"
            "你的续读不会改变方小南自己的阅读进度。" + PRIVATE_ONLY
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["requestedChars", "requestId"],
            "properties": {
                "requestedChars": {"type": "integer", "minimum": 1, "maximum": 1000},
                "requestId": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,128}$"},
                "bookId": {"type": "string", "pattern": _ID_PATTERN, "description": "list_bookshelf 返回的 bookId"},
                "chunkId": {"type": "string", "pattern": _ID_PATTERN, "description": "list_chapters 返回的 chunkId；需同时传 bookId"},
                "anchorOffset": {"type": "integer", "minimum": 0, "maximum": MAX_OFFSET, "description": "章节内 UTF-16 偏移；需同时传 chunkId"},
            },
        },
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_bookshelf",
            "description": (
                "列出方小南阅读器书架上的书（bookId/书名/作者/章节数/总字数），以及你自己的当前锚点和各书书签。"
                "只返回元数据，不含正文。" + PRIVATE_ONLY
            ),
            "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        },
        {
            "name": "list_chapters",
            "description": "列出书架上某本书的章节目录（chunkId/章节标题/字数）和你在这本书的书签，不含正文。" + PRIVATE_ONLY,
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bookId"],
                "properties": {"bookId": {"type": "string", "pattern": _ID_PATTERN}},
            },
        },
        _tool(),
    ]


def _post(credential: dict[str, str], contact_id: str, path: str, payload: dict[str, Any], limit: int) -> tuple[int, Any]:
    request = urllib.request.Request(
        credential["endpoint"] + path,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CC-Reading-AI-Contact": contact_id,
            "X-CC-Reading-AI-Token": credential["token"],
        },
    )
    try:
        with urllib.request.build_opener().open(request, timeout=25) as response:
            raw = response.read(limit + 1)
            status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(4096) if error.fp is not None else b""
        except OSError:
            raw = b""
        try:
            value = json.loads(raw) if raw else {}
        except (UnicodeError, ValueError):
            value = {}
        return error.code, value if isinstance(value, dict) else {}
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    if len(raw) > limit:
        return 0, None
    try:
        return status, json.loads(raw)
    except (UnicodeError, ValueError):
        return 0, None


_ERROR_TEXT = {
    401: "续读服务拒绝了这个身份",
    403: "这个身份没有被授权浏览书架/续读",
    404: "书架上没有这本书或这一章",
    409: "现在无法续读",
}


def _failure(status: int, value: Any) -> str:
    if status == 0 or value is None:
        return "续读服务暂时不可用"
    detail = value.get("error") if isinstance(value, dict) else None
    base = _ERROR_TEXT.get(status, "续读请求无效" if status == 400 else "续读服务暂时不可用")
    return f"{base}（{detail}）" if isinstance(detail, str) and len(detail) <= 200 else base


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and ".." not in value and bool(READING_ID_RE.fullmatch(value))


def _call(credential: dict[str, str], contact_id: str, arguments: Any) -> tuple[bool, str]:
    """continue_reading.  Returns (ok, excerpt-or-error)."""
    ok, text, _meta = _call_continue(credential, contact_id, arguments)
    return ok, text


def _call_continue(credential: dict[str, str], contact_id: str, arguments: Any) -> tuple[bool, str, str | None]:
    allowed = {"requestedChars", "requestId", "bookId", "chunkId", "anchorOffset"}
    if not isinstance(arguments, dict) or not {"requestedChars", "requestId"}.issubset(arguments) or set(arguments) - allowed:
        return False, "参数必须是 requestedChars、requestId，以及可选的 bookId/chunkId/anchorOffset", None
    requested, request_id = arguments.get("requestedChars"), arguments.get("requestId")
    if isinstance(requested, bool) or not isinstance(requested, int) or not 1 <= requested <= 1000 or not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        return False, "续读参数无效", None
    payload: dict[str, Any] = {"requestedChars": requested, "requestId": request_id}
    if "bookId" in arguments:
        if not _valid_id(arguments["bookId"]):
            return False, "bookId 无效", None
        payload["bookId"] = arguments["bookId"]
    if "chunkId" in arguments:
        if "bookId" not in payload or not _valid_id(arguments["chunkId"]):
            return False, "chunkId 无效（需同时传 bookId）", None
        payload["chunkId"] = arguments["chunkId"]
    if "anchorOffset" in arguments:
        offset = arguments["anchorOffset"]
        if "chunkId" not in payload or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_OFFSET:
            return False, "anchorOffset 无效（需同时传 chunkId）", None
        payload["anchorOffset"] = offset
    status, value = _post(credential, contact_id, "/reading/ai/continue", payload, MAX_CONTINUE_RESPONSE)
    if status != 200 or not isinstance(value, dict):
        return False, _failure(status, value), None
    text = value.get("text")
    if not isinstance(text, str) or len(text.encode("utf-16-le")) // 2 > 1000:
        return False, "续读服务返回无效内容", None
    to = value.get("to") if isinstance(value.get("to"), dict) else {}
    meta = "【《{}》· {} | 本次 {} 字 | 下次从 {}@{} 接着读{}】".format(
        str(value.get("bookTitle") or "")[:240], str(value.get("chapterTitle") or "")[:320],
        len(text.encode("utf-16-le")) // 2, str(to.get("chunkId") or "")[:128], to.get("anchorOffset"),
        " | 已读到全书末尾" if value.get("completed") else "",
    )
    return True, text, meta


def _call_listing(credential: dict[str, str], contact_id: str, name: str, arguments: Any) -> tuple[bool, str]:
    if name == "list_bookshelf":
        if arguments not in (None, {}):
            return False, "list_bookshelf 不接受参数"
        status, value = _post(credential, contact_id, "/reading/ai/shelf", {}, MAX_LISTING_RESPONSE)
    else:
        if not isinstance(arguments, dict) or set(arguments) != {"bookId"} or not _valid_id(arguments.get("bookId")):
            return False, "参数必须只有合法的 bookId"
        status, value = _post(credential, contact_id, "/reading/ai/chapters", {"bookId": arguments["bookId"]}, MAX_LISTING_RESPONSE)
    if status != 200 or not isinstance(value, dict):
        return False, _failure(status, value)
    return True, json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dispatch(credential: dict[str, str] | None, contact_id: str, name: Any, arguments: Any) -> dict[str, Any] | None:
    if name not in {"list_bookshelf", "list_chapters", "continue_reading"}:
        return None
    if credential is None:
        return {"content": [{"type": "text", "text": "服务尚未为此固定身份配置"}], "isError": True}
    if name == "continue_reading":
        ok, text, meta = _call_continue(credential, contact_id, arguments)
        content = [{"type": "text", "text": meta}] if ok and meta else []
        content.append({"type": "text", "text": text})
        return {"content": content, "isError": not ok}
    ok, text = _call_listing(credential, contact_id, name, arguments)
    return {"content": [{"type": "text", "text": text}], "isError": not ok}


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
            result: dict[str, Any] = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": f"reading-continue-{contact_id}", "version": "2"}}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            params = message.get("params")
            dispatched = _dispatch(credential, contact_id, params.get("name"), params.get("arguments")) if isinstance(params, dict) else None
            if dispatched is None:
                answer = _error(request_id, -32601, "工具不存在")
                sys.stdout.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n"); sys.stdout.flush(); continue
            result = dispatched
        elif method == "ping":
            result = {}
        elif isinstance(method, str) and method.startswith("notifications/"):
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
