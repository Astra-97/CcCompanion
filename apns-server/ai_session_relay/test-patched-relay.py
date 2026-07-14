#!/usr/bin/env python3
"""Mock-engine integration check for an already patched upstream checkout."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock


class JsonRequest:
    def __init__(self, value: dict) -> None:
        self.value = value

    async def json(self) -> dict:
        return self.value


async def response_json_lines(response) -> list[dict]:
    result = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        result.extend(json.loads(line) for line in chunk.splitlines() if line.strip())
    return result


async def run(source: Path, overlay: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "instance"
        workspace = root / "workspace"
        state = root / "state"
        bin_dir = Path(tmp) / "bin"
        for path in (workspace, state / "claude-home", state / "codex-home",
                     state / "runtime-home", bin_dir):
            path.mkdir(parents=True, exist_ok=True)
        for path in (root, workspace, state, state / "claude-home",
                     state / "codex-home", state / "runtime-home"):
            os.chmod(path, 0o700)
        for name in ("CLAUDE.md", "AGENTS.md"):
            (workspace / name).write_text("persona", encoding="utf-8")
            os.chmod(workspace / name, 0o600)
        (state / "empty-mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
        os.chmod(state / "empty-mcp.json", 0o600)
        for name in ("claude", "codex"):
            (bin_dir / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(bin_dir / name, 0o755)

        os.environ.update({
            "AI_RELAY_EXECUTION_MODE": "chat_only",
            "AI_RELAY_INSTANCE_ROOT": str(root),
            "AI_RELAY_WORKSPACE": str(workspace),
            "AI_RELAY_STATE_DIR": str(state),
            "AI_RELAY_HOST": "127.0.0.1",
            "CLAUDE_CONFIG_DIR": str(state / "claude-home"),
            "CODEX_HOME": str(state / "codex-home"),
            "HOME": str(state / "runtime-home"),
            "AI_RELAY_CLAUDE_BIN": str(bin_dir / "claude"),
            "AI_RELAY_CODEX_BIN": str(bin_dir / "codex"),
            "AI_RELAY_PROVIDER": "claude",
        })
        sys.path.insert(0, str(overlay))
        import security_policy

        calls = 0
        cc = types.ModuleType("cc_headless")

        async def stream_headless(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            yield {"text_delta": "answer"}
            yield {"done": True, "full": "answer", "parts": [{"type": "text", "text": "answer"}]}

        cc.stream_headless = stream_headless
        codex = types.ModuleType("codex_engine")
        codex.list_models = lambda: []
        sys.modules["cc_headless"] = cc
        sys.modules["codex_engine"] = codex
        uvicorn = types.ModuleType("uvicorn")
        uvicorn.run = lambda *_args, **_kwargs: None
        sys.modules["uvicorn"] = uvicorn

        fastapi = types.ModuleType("fastapi")

        class FastAPI:
            def __init__(self, **_kwargs):
                pass

            def get(self, _path):
                return lambda func: func

            def post(self, _path):
                return lambda func: func

        class Request:
            pass

        class JSONResponse:
            def __init__(self, content, status_code=200):
                self.body = json.dumps(content).encode()
                self.status_code = status_code

        class StreamingResponse:
            def __init__(self, body_iterator, media_type=None):
                self.body_iterator = body_iterator
                self.media_type = media_type
                self.status_code = 200

        responses = types.ModuleType("fastapi.responses")
        responses.JSONResponse = JSONResponse
        responses.StreamingResponse = StreamingResponse
        fastapi.FastAPI = FastAPI
        fastapi.Request = Request
        sys.modules["fastapi"] = fastapi
        sys.modules["fastapi.responses"] = responses

        spec = importlib.util.spec_from_file_location("restricted_relay_integration", source / "relay_server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with mock.patch.object(security_policy, "_mount_is_read_only", return_value=True):
            spec.loader.exec_module(module)

        payload = {"text": "hello", "provider": "claude", "execution_mode": "chat_only",
                   "request_id": "stable-1"}
        first = await module.chat_stream(JsonRequest(payload))
        first_lines = await response_json_lines(first)
        assert first_lines[-1]["request_status"] == "completed"
        assert calls == 1
        second = await module.chat_stream(JsonRequest(payload))
        second_lines = await response_json_lines(second)
        assert second_lines == [first_lines[-1]]
        assert calls == 1, "completed replay called the engine twice"

        ledger = module.REQUEST_LEDGER.load()
        module.REQUEST_LEDGER.accept(ledger, "claude", 0, "stable-crash", "old-process")
        ledger = module.REQUEST_LEDGER.load()
        rec = module.REQUEST_LEDGER.find_request(ledger, "stable-crash")
        module.REQUEST_LEDGER.transition(ledger, rec, "running")
        (state / "headless_last_session").write_text("stale", encoding="utf-8")
        module._CRASH_RECONCILED = False
        crashed = await module.chat_stream(JsonRequest({**payload, "request_id": "stable-crash"}))
        assert crashed.status_code == 409
        body = json.loads(bytes(crashed.body))
        assert body["code"] == "request_uncertain" and body["retryable"] is False
        assert calls == 1, "crashed request was submitted again"
        assert not (state / "headless_last_session").exists()
        relay_state = json.loads((state / "relay_state.json").read_text(encoding="utf-8"))
        assert relay_state["pending_switch"] is True and relay_state["epoch"] >= 1
        terminal = module.REQUEST_LEDGER.find_request(module.REQUEST_LEDGER.load(), "stable-crash")
        assert terminal["status"] == "uncertain"

        module.REQUEST_LEDGER_FILE.write_text("corrupt", encoding="utf-8")
        corrupt = await module.chat_stream(JsonRequest({**payload, "request_id": "stable-corrupt"}))
        assert corrupt.status_code == 409
        corrupt_body = json.loads(bytes(corrupt.body))
        assert corrupt_body["code"] == "request_uncertain"
        assert corrupt_body["retryable"] is False
        assert calls == 1, "corrupt ledger reached the engine"


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    source = Path(sys.argv[1]).resolve()
    overlay = Path(__file__).resolve().parent
    asyncio.run(run(source, overlay))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
