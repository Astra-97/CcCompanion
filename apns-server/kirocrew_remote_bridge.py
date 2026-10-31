"""Kiro Crew gateway bridge, run ON Tokyo as user kiro (2026-10-31).

push.py never ships this file anywhere: ``kirocrew_client.py`` reads it and
runs it as ``python3 -c <this source> <port> <op>`` over the kiro ssh
transport, with the request JSON on stdin.  So the bridge always matches the
backend version and nothing has to be deployed to Tokyo.

Why a bridge instead of an ssh port forward: the gateway binds loopback only
and authenticates internal callers with ``X-Internal-Secret``, a random value
it writes (0600, owner kiro) to ``~/.kiro/crew/run/gateway-<port>-127.0.0.1.secret``
on every start.  Reading it here means the secret never leaves Tokyo, never
appears in an argv, a log line or on 216, and a gateway restart is handled by
re-reading the file once on 401/403.

Stdlib only (Tokyo's system python3).  Output is JSON lines on stdout:

* ``get`` / ``post`` / ``patch`` / ``delete``: one line ``{"status": <http status>, "body": <json or text>}``
* ``chat``: ``{"t": "d", "c": "<text delta>"}`` lines while the reply streams,
  ``{"t": "final", "c": "<whole reply>"}`` when the assistant row lands,
  ``{"t": "ev", "type": ..., "c": ..., "cls": ...}`` for other rows (tools,
  errors, notices), then exactly one terminal line: ``{"t": "done"}`` or
  ``{"t": "err", "status": <int>, "body": ...}``.
"""
import http.client
import json
import os
import sys

HOME = os.path.expanduser("~")
RUN_DIR = os.path.join(HOME, ".kiro", "crew", "run")
ALLOWED_PREFIXES = ("/api/", "/v1/")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def secret(port):
    for name in (f"gateway-{port}-127.0.0.1.secret", f"gateway-{port}.secret"):
        value = _read(os.path.join(RUN_DIR, name))
        if value:
            return value
    return _read(os.path.join(HOME, ".kiro", "crew", ".local_secret"))


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _decode(raw):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text[:2000]


def open_request(port, method, path, body, timeout):
    """-> (connection, response); retries once with a re-read secret on 401/403."""
    payload = None if body is None else json.dumps(body).encode("utf-8")
    for attempt in (0, 1):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        headers = {"X-Internal-Secret": secret(port), "Accept": "application/json, text/event-stream"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        if resp.status in (401, 403) and attempt == 0:
            resp.read()
            conn.close()
            continue
        return conn, resp
    raise RuntimeError("unreachable")


def do_simple(port, method, req):
    path = str(req.get("path") or "")
    if not path.startswith(ALLOWED_PREFIXES) or "\n" in path or " " in path:
        emit({"status": 400, "body": "bridge: path not allowed"})
        return
    try:
        conn, resp = open_request(port, method, path, req.get("body") if method in ("POST", "PATCH") else None,
                                  float(req.get("timeout") or 30))
        raw = resp.read()
        conn.close()
        emit({"status": resp.status, "body": _decode(raw)})
    except (OSError, http.client.HTTPException) as exc:
        emit({"status": 0, "body": f"bridge: {type(exc).__name__}"})


def do_chat(port, req):
    """POST /api/chat (the dashboard's own send) and relay its SSE rows.

    Used instead of the OpenAI-compatible endpoint because that one streams
    every ``chunk`` row AND the final ``assistant`` row into the same delta
    channel (the reply arrives twice) and hides tool / error rows.
    """
    body = {
        "message": str(req.get("text") or ""),
        "agent": str(req.get("agent") or ""),
        "slot": str(req.get("slot") or ""),
    }
    try:
        conn, resp = open_request(port, "POST", "/api/chat", body, float(req.get("timeout") or 14400))
    except (OSError, http.client.HTTPException) as exc:
        emit({"t": "err", "status": 0, "body": f"bridge: {type(exc).__name__}"})
        return
    ctype = resp.getheader("Content-Type", "") or ""
    if resp.status != 200 or "event-stream" not in ctype:
        emit({"t": "err", "status": resp.status, "body": _decode(resp.read())})
        conn.close()
        return
    finished = False
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                finished = True
                break
            try:
                row = json.loads(data)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            kind = str(row.get("type") or "")
            content = row.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False) if content is not None else ""
            if kind == "chunk":
                if content:
                    emit({"t": "d", "c": content})
            elif kind == "assistant":
                emit({"t": "final", "c": content})
            elif kind == "user":
                continue
            else:
                cls = row.get("cls")
                emit({"t": "ev", "type": kind[:40], "c": content[:600],
                      "cls": cls[:200] if isinstance(cls, str) else ""})
    except (OSError, http.client.HTTPException) as exc:
        emit({"t": "err", "status": 0, "body": f"bridge: {type(exc).__name__}"})
        return
    finally:
        conn.close()
    emit({"t": "done"} if finished else {"t": "err", "status": 0, "body": "bridge: stream ended early"})


def main():
    port = int(sys.argv[1])
    op = sys.argv[2]
    raw = sys.stdin.read()
    req = json.loads(raw) if raw.strip() else {}
    if op == "chat":
        do_chat(port, req)
    elif op == "get":
        do_simple(port, "GET", req)
    elif op == "post":
        do_simple(port, "POST", req)
    elif op == "delete":
        do_simple(port, "DELETE", req)
    elif op == "patch":
        do_simple(port, "PATCH", req)
    else:
        emit({"status": 400, "body": "bridge: unknown op"})


if __name__ == "__main__":
    main()
