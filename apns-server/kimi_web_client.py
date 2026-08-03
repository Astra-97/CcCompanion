"""Lightweight REST client for the local Kimi Code CLI web server.

This module is intentionally small: it only queries state that the ACP stdio
protocol does not expose (quota, context usage, session status).  The actual
conversation still goes through kim_acp.py because ACP handles tool approval
and streaming correctly in the current Kimi Code version.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import urllib.error
import urllib.request

DEFAULT_KIMI_BIN = "/root/.kimi-code/bin/kimi"
DEFAULT_KIMI_CODE_HOME = Path.home() / ".kimi-code"
DEFAULT_PORT = 58627
DEFAULT_HOST = "127.0.0.1"


class KimiWebError(RuntimeError):
    pass


class KimiWebClient:
    def __init__(
        self,
        *,
        command: str | Path = DEFAULT_KIMI_BIN,
        kimi_code_home: str | Path = DEFAULT_KIMI_CODE_HOME,
        port: int = DEFAULT_PORT,
        host: str = DEFAULT_HOST,
        logger: logging.Logger | None = None,
        start_timeout: float = 30.0,
    ):
        self.command = str(Path(command).expanduser())
        self.kimi_code_home = Path(kimi_code_home).expanduser()
        self.port = int(port)
        self.host = host
        self.base_url = f"http://{host}:{port}"
        self.logger = logger or logging.getLogger(__name__)
        self.start_timeout = max(5.0, float(start_timeout))
        self._process: subprocess.Popen[str] | None = None
        self._token = ""
        self._token_lock = threading.Lock()
        # start() owns this lock while waiting for readiness and uses close()
        # to clean up a failed child.  The cleanup must be re-entrant or a
        # startup timeout would deadlock while trying to acquire its own lock.
        self._start_lock = threading.RLock()
        self._owned_process = False

    @property
    def _server_token_path(self) -> Path:
        return self.kimi_code_home / "server.token"

    def _read_token(self) -> str:
        with self._token_lock:
            if self._token:
                return self._token
            try:
                token = self._server_token_path.read_text(encoding="utf-8").strip()
                self._token = token
                return token
            except (FileNotFoundError, OSError) as exc:
                raise KimiWebError(f"Cannot read Kimi server token: {exc}") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._read_token()}"}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = ""
            raise KimiWebError(f"Kimi web {method} {path} failed ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise KimiWebError(f"Kimi web {method} {path} unreachable: {reason}") from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise KimiWebError(f"Kimi web returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise KimiWebError("Kimi web returned non-object JSON")
        if payload.get("code") != 0:
            msg = payload.get("msg") or "unknown error"
            raise KimiWebError(f"Kimi web {method} {path} error: {msg}")
        return payload.get("data") if isinstance(payload.get("data"), dict) else {}

    def _is_server_responsive(self) -> bool:
        try:
            self._request("GET", "/api/v1/healthz", timeout=2.0)
            return True
        except KimiWebError:
            return False

    def _wait_for_server(self) -> None:
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self._is_server_responsive():
                return
            time.sleep(0.2)
        raise KimiWebError("Kimi web server did not become ready in time")

    def _try_reuse_existing_server(self) -> bool:
        """If another `kimi web` is already running on the port, reuse it."""
        if not self._is_server_responsive():
            return False
        try:
            self._read_token()
            self.logger.info("Reusing existing Kimi web server at %s", self.base_url)
            return True
        except KimiWebError:
            return False

    def start(self) -> None:
        """Start the web server if it is not already running."""
        with self._start_lock:
            if self._process is not None and self._process.poll() is None:
                return
            if self._try_reuse_existing_server():
                self._owned_process = False
                return
            self.logger.info("Starting Kimi web server at %s", self.base_url)
            env = os.environ.copy()
            env["KIMI_CODE_HOME"] = str(self.kimi_code_home)
            try:
                process = subprocess.Popen(
                    [self.command, "web", "--no-open", "--port", str(self.port), "--host", self.host],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            except Exception as exc:
                raise KimiWebError(f"Could not start Kimi web server: {exc}") from exc
            self._process = process
            self._owned_process = True
            try:
                self._wait_for_server()
            except Exception:
                self.close()
                raise
            self.logger.info("Kimi web server ready (pid %s)", process.pid)

    def close(self) -> None:
        with self._start_lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            if not self._owned_process:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    pass

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/healthz")

    def get_quota(self) -> dict[str, Any]:
        """Return managed-account quota, e.g. weekly/5h limits."""
        return self._request("GET", "/api/v1/oauth/usage")

    def list_sessions(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/sessions")
        items = data.get("items")
        return items if isinstance(items, list) else []

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise KimiWebError("session_id is required")
        return self._request(
            "GET",
            f"/api/v1/sessions/{session_id}/status",
            timeout=5.0,
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise KimiWebError("session_id is required")
        return self._request(
            "GET",
            f"/api/v1/sessions/{session_id}",
            timeout=5.0,
        )
