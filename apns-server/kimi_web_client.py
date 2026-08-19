"""Authenticated server-side adapter for the local Kimi Code Web API.

CcCompanion owns the Web-session pointer and consumes its REST/WebSocket
events server-side.  The loopback bearer credential never crosses this module
boundary; phones only see the existing redacted chat/status projections.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import urllib.error
import urllib.parse
import urllib.request

DEFAULT_KIMI_BIN = "/root/.kimi-code/bin/kimi"
DEFAULT_KIMI_CODE_HOME = Path.home() / ".kimi-code"
DEFAULT_PORT = 58627
DEFAULT_HOST = "127.0.0.1"
DEFAULT_KIMI_CWD = "/root/Karami-Workspace"


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
        state_path: str | Path | None = None,
        cwd: str | Path = DEFAULT_KIMI_CWD,
    ):
        self.command = str(Path(command).expanduser())
        self.kimi_code_home = Path(kimi_code_home).expanduser()
        self.port = int(port)
        self.host = host
        self.base_url = f"http://{host}:{port}"
        self.logger = logger or logging.getLogger(__name__)
        self.start_timeout = max(5.0, float(start_timeout))
        self.state_path = Path(state_path).expanduser() if state_path is not None else None
        self.cwd = str(Path(cwd).expanduser().resolve())
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

    def get_userinfo(self) -> dict[str, Any]:
        """Return Kimi's account payload for server-side allowlist projection.

        Callers must never return this raw object: it can include display name,
        avatar and account identifiers.  The App status endpoint projects only
        the public subscription level fields it explicitly understands.
        """
        return self._request("GET", "/api/v1/oauth/userinfo")

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

    # Web-only Kimi chat API -------------------------------------------------
    # The local Web server remains loopback-only.  These methods are the sole
    # place its bearer credential is read, and callers receive only selected
    # session/prompt data—not a URL or token they could relay to a phone.

    @staticmethod
    def _valid_session_id(value: Any) -> str:
        session_id = str(value or "").strip()
        return session_id if 0 < len(session_id) <= 200 and all(
            char.isalnum() or char in {"-", "_"} for char in session_id
        ) else ""

    def load_active_session_id(self) -> str:
        if self.state_path is None:
            return ""
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return ""
            if str(payload.get("cwd") or "") != self.cwd:
                return ""
            return self._valid_session_id(payload.get("session_id"))
        except (FileNotFoundError, OSError, ValueError):
            return ""

    def save_active_session_id(self, session_id: str) -> None:
        session_id = self._valid_session_id(session_id)
        if not session_id or self.state_path is None:
            raise KimiWebError("valid Kimi Web session and state path are required")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp.{os.getpid()}")
        temporary.write_text(json.dumps({
            "version": 1,
            "session_id": session_id,
            "cwd": self.cwd,
        }, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_path)

    def create_session(
        self,
        *,
        title: str = "CcCompanion Kimi",
        model: str = "",
        thinking: str = "",
        permission_mode: str = "manual",
    ) -> str:
        payload: dict[str, Any] = {
            "title": str(title or "CcCompanion Kimi").strip()[:120] or "CcCompanion Kimi",
            "metadata": {"cwd": self.cwd},
        }
        agent_config: dict[str, Any] = {}
        if model.strip():
            agent_config["model"] = model.strip()
        if thinking.strip():
            agent_config["thinking"] = thinking.strip()
        if permission_mode in {"manual", "yolo", "auto"}:
            agent_config["permission_mode"] = permission_mode
        if agent_config:
            payload["agent_config"] = agent_config
        data = self._request("POST", "/api/v1/sessions", data=payload)
        session_id = self._valid_session_id(data.get("id") or data.get("session_id"))
        if not session_id:
            raise KimiWebError("Kimi Web did not return a session id")
        self.save_active_session_id(session_id)
        return session_id

    def ensure_active_session(self, *, model: str = "", thinking: str = "") -> str:
        """Use the Web-only pointer or create a fresh Web session.

        The ACP pointer is deliberately never imported or overwritten.  It
        remains a rollback record while new App turns start in an explicitly
        separate Web-owned session.
        """
        self.start()
        session_id = self.load_active_session_id()
        if session_id:
            try:
                status = self.get_session_status(session_id)
            except KimiWebError:
                # A stale/deleted pointer is recoverable; retain the ACP
                # pointer untouched and make a fresh Web session below.
                status = {}
            if status:
                if bool(status.get("busy")):
                    raise KimiWebError("Kimi Web session is already busy")
                return session_id
        return self.create_session(model=model, thinking=thinking)

    def submit_prompt(
        self,
        session_id: str,
        text: str,
        *,
        model: str = "",
        thinking: str = "",
        permission_mode: str = "manual",
    ) -> dict[str, Any]:
        session_id = self._valid_session_id(session_id)
        clean_text = str(text or "").strip()
        if not session_id or not clean_text:
            raise KimiWebError("session id and prompt text are required")
        payload: dict[str, Any] = {"content": [{"type": "text", "text": clean_text}]}
        if model.strip():
            payload["model"] = model.strip()
        if thinking.strip():
            payload["thinking"] = thinking.strip()
        if permission_mode in {"manual", "yolo", "auto"}:
            payload["permission_mode"] = permission_mode
        submitted = self._request("POST", f"/api/v1/sessions/{session_id}/prompts", data=payload)
        prompt_id = str(submitted.get("prompt_id") or submitted.get("id") or "").strip()
        if prompt_id:
            return submitted
        # The documented submit response can expose only user_message_id.
        # Resolve it immediately to the server-generated prompt id so Stop
        # can remain exactly-fenced rather than cancelling a whole session.
        user_message_id = str(submitted.get("user_message_id") or "").strip()
        prompts = self.list_prompts(session_id)
        active = prompts.get("active") if isinstance(prompts, dict) else None
        if isinstance(active, dict):
            candidate = str(active.get("prompt_id") or "").strip()
            if candidate and (
                not user_message_id
                or str(active.get("user_message_id") or "").strip() == user_message_id
            ):
                submitted = dict(submitted)
                submitted["prompt_id"] = candidate
        return submitted

    def abort_prompt(self, session_id: str, prompt_id: str) -> dict[str, Any]:
        session_id = self._valid_session_id(session_id)
        prompt = str(prompt_id or "").strip()
        if not session_id or not prompt or len(prompt) > 200:
            raise KimiWebError("session id and prompt id are required")
        return self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/prompts/{urllib.parse.quote(prompt, safe='')}:abort",
            data={},
        )

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        session_id = self._valid_session_id(session_id)
        if not session_id:
            raise KimiWebError("session id is required")
        data = self._request("GET", f"/api/v1/sessions/{session_id}/messages")
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def list_prompts(self, session_id: str) -> dict[str, Any]:
        session_id = self._valid_session_id(session_id)
        if not session_id:
            raise KimiWebError("session id is required")
        return self._request("GET", f"/api/v1/sessions/{session_id}/prompts")

    def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        session_id = self._valid_session_id(session_id)
        if not session_id:
            raise KimiWebError("session id is required")
        data = self._request("GET", f"/api/v1/sessions/{session_id}/approvals")
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def approve_once(self, session_id: str, approval_id: str) -> None:
        session_id = self._valid_session_id(session_id)
        approval_id = str(approval_id or "").strip()
        if not session_id or not approval_id or len(approval_id) > 200:
            raise KimiWebError("session id and approval id are required")
        # Deliberately omit `scope: session`: Web's default is a decision for
        # this pending approval only, matching ACP's bounded allow-once rule.
        self._request(
            "POST",
            f"/api/v1/sessions/{session_id}/approvals/{urllib.parse.quote(approval_id, safe='')}",
            data={"decision": "approved"},
        )

    def get_snapshot(self, session_id: str) -> dict[str, Any]:
        session_id = self._valid_session_id(session_id)
        if not session_id:
            raise KimiWebError("session id is required")
        return self._request("GET", f"/api/v1/sessions/{session_id}/snapshot")

    def stream_session(
        self,
        session_id: str,
        *,
        on_event: Any,
        stop_event: threading.Event,
        on_ready: Any | None = None,
        timeout: float = 900.0,
    ) -> None:
        """Subscribe to one Web session's authenticated event stream.

        ``on_event`` receives decoded JSON only for the selected session.  It
        is intentionally server-side: Android receives the existing redacted
        chat draft/SSE projection and never gets Kimi Web's bearer token.
        """
        session_id = self._valid_session_id(session_id)
        if not session_id:
            raise KimiWebError("session id is required")
        try:
            from websockets.sync.client import connect
        except Exception as exc:
            raise KimiWebError("Python websockets client is unavailable") from exc
        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        ws_url = f"{ws_url}/api/v1/ws?client_id=cc-companion-{uuid.uuid4().hex}"
        headers = {"Authorization": f"Bearer {self._read_token()}"}
        deadline = time.monotonic() + max(5.0, float(timeout))
        try:
            with connect(ws_url, additional_headers=headers, open_timeout=8, close_timeout=2) as socket:
                client_id = f"cc-companion-{uuid.uuid4().hex}"

                def send_request(frame_type: str, payload: dict[str, Any]) -> str:
                    request_id = uuid.uuid4().hex
                    socket.send(json.dumps({
                        "type": frame_type,
                        "id": request_id,
                        "payload": payload,
                    }))
                    return request_id

                def send_pong(nonce: str) -> None:
                    # Pong is a nonce acknowledgement, rather than an RPC.
                    socket.send(json.dumps({"type": "pong", "payload": {"nonce": nonce}}))

                hello_id = send_request("client_hello", {
                    "client_id": client_id,
                    "subscriptions": [session_id],
                })
                subscribe_id = send_request("subscribe", {"session_ids": [session_id]})
                pending_acks = {hello_id, subscribe_id}
                ready = False
                # The server may start publishing while the subscribe RPC is
                # still being acknowledged.  Those frames are not evidence
                # that this client owns a ready subscription: retain only the
                # selected session and replay it after both exact ACKs.
                buffered_frames: list[dict[str, Any]] = []

                def deliver(frame: dict[str, Any]) -> None:
                    try:
                        on_event(frame)
                    except Exception:
                        self.logger.warning("Kimi Web session callback failed", exc_info=True)

                def publish_ready() -> None:
                    nonlocal ready
                    if ready:
                        return
                    ready = True
                    if callable(on_ready):
                        on_ready()
                    while buffered_frames:
                        deliver(buffered_frames.pop(0))

                while not stop_event.is_set() and time.monotonic() < deadline:
                    try:
                        raw = socket.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    try:
                        frame = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("type") == "ping":
                        payload = frame.get("payload")
                        nonce = payload.get("nonce") if isinstance(payload, dict) else None
                        if isinstance(nonce, str) and nonce:
                            send_pong(nonce)
                        continue
                    if frame.get("type") == "ack":
                        request_id = str(frame.get("id") or "")
                        if request_id in pending_acks:
                            if frame.get("code") != 0:
                                raise KimiWebError("Kimi Web subscription was rejected")
                            pending_acks.discard(request_id)
                        if not pending_acks and not ready:
                            publish_ready()
                        continue
                    # AsyncAPI deliberately puts the concrete event type at
                    # the top level (e.g. assistant.delta), not in a generic
                    # session_event wrapper.
                    if frame.get("session_id") != session_id:
                        continue
                    if not ready:
                        buffered_frames.append(frame)
                        continue
                    deliver(frame)
        except KimiWebError:
            raise
        except Exception as exc:
            raise KimiWebError(f"Kimi Web stream failed: {type(exc).__name__}") from exc
