#!/usr/bin/env python3
"""No-thread Codex app-server/model-list preflight; never prints catalog/auth."""

from __future__ import annotations

import json
import importlib.util
import os
import select
import subprocess
import sys
import time
from types import SimpleNamespace


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    policy_path, binary = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("relay_preflight_policy", policy_path)
    if spec is None or spec.loader is None:
        return 2
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    command = module.codex_args(SimpleNamespace(codex_bin=binary))
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ["HOME"],
            "CODEX_HOME": os.environ["CODEX_HOME"],
        },
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "cc-xia-relay-preflight", "version": "1"}
        }})
        deadline = time.monotonic() + 20
        initialized = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], min(1, deadline - time.monotonic()))
            if not ready:
                if proc.poll() is not None:
                    return 1
                continue
            line = proc.stdout.readline()
            if not line:
                return 1
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1 and not initialized:
                if not isinstance(message.get("result"), dict):
                    return 1
                send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                send({"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}})
                initialized = True
            elif message.get("id") == 2:
                return 0 if isinstance(message.get("result"), dict) else 1
        return 1
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
