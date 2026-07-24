"""Small, dependency-free ACP client for the local Kimi Code CLI.

ACP uses one JSON-RPC object per line over stdio.  This module deliberately
keeps the wire protocol away from the HTTP handler so Kimi has an independent
session, lifecycle and cancellation boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable

KIMI_APP_MODEL = "kimi-code/kimi-for-coding-highspeed"


class KimiACPError(RuntimeError):
    pass


class KimiACPBusy(KimiACPError):
    pass


class KimiACPAuthRequired(KimiACPError):
    pass


class KimiACPCancelled(KimiACPError):
    pass


@dataclass(frozen=True)
class KimiACPResult:
    text: str
    session_id: str
    stop_reason: str


def _text_from_update(params: Any) -> str:
    """Return only assistant text from an ACP session/update notification."""
    if not isinstance(params, dict):
        return ""
    update = params.get("update")
    if not isinstance(update, dict):
        return ""
    if update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return ""
    return str(content.get("text") or "")


class KimiACPClient:
    def __init__(
        self,
        *,
        command: str | Path = "/root/.kimi-code/bin/kimi",
        cwd: str | Path = "/root/Windows-Codex-TG",
        state_path: str | Path,
        logger: logging.Logger | None = None,
        request_timeout: float = 30.0,
        prompt_timeout: float = 900.0,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ):
        self.command = str(Path(command).expanduser())
        self.cwd = Path(cwd).expanduser().resolve()
        self.state_path = Path(state_path).expanduser()
        self.logger = logger or logging.getLogger(__name__)
        self.request_timeout = max(1.0, float(request_timeout))
        self.prompt_timeout = max(self.request_timeout, float(prompt_timeout))
        self._popen_factory = popen_factory
        self._process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any], int]] = {}
        self._next_id = 1
        self._process_generation = 0
        self._active_lock = threading.Lock()
        self._active_session_id = ""
        self._active_turn_id = ""
        self._active_update: Callable[[str], None] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._initialized = False
        self._loaded_session_id = ""
        self._highspeed_model_session_id = ""

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    def _load_session_id(self) -> str:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return str(payload.get("session_id") or "").strip() if isinstance(payload, dict) else ""
        except (FileNotFoundError, OSError, ValueError):
            return ""

    def _save_session_id(self, session_id: str) -> None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"version": 1, "session_id": session_id}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    def _process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start(self) -> None:
        with self._start_lock:
            if self._process_alive() and self._initialized:
                return
            self.close()
            try:
                process = self._popen_factory(
                    [self.command, "acp"],
                    cwd=str(self.cwd),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception as exc:
                raise KimiACPError(f"Kimi ACP could not start: {exc}") from exc
            self._process = process
            self._process_generation += 1
            generation = self._process_generation
            self._initialized = False
            self._loaded_session_id = ""
            self._highspeed_model_session_id = ""
            self._reader = threading.Thread(
                target=self._read_stdout,
                args=(process, generation),
                name="kimi-acp-stdout",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._drain_stderr,
                args=(process,),
                name="kimi-acp-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
            self._request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "CcCompanion", "version": "1"},
                },
                timeout=self.request_timeout,
                ensure_started=False,
            )
            self._initialized = True

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        # Never log stderr content: it may contain prompts, paths, or auth data.
        try:
            for _line in process.stderr:
                pass
        except Exception:
            pass

    def _read_stdout(self, process: subprocess.Popen[str], generation: int) -> None:
        if process.stdout is None:
            return
        try:
            for raw in process.stdout:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    try:
                        request_id = int(message["id"])
                    except (TypeError, ValueError):
                        continue
                    with self._pending_lock:
                        pending = self._pending.get(request_id)
                    if pending is not None and pending[2] == generation:
                        event, bucket, _pending_generation = pending
                        bucket["message"] = message
                        event.set()
                    continue
                if generation != self._process_generation:
                    continue
                if message.get("method") == "session/update":
                    params = message.get("params")
                    if not isinstance(params, dict):
                        continue
                    session_id = str(params.get("sessionId") or "")
                    with self._active_lock:
                        callback = self._active_update if session_id == self._active_session_id else None
                    delta = _text_from_update(params)
                    if callback is not None and delta:
                        try:
                            callback(delta)
                        except Exception:
                            self.logger.warning("Kimi ACP update callback failed", exc_info=True)
                    continue
                # Kimi can ask its ACP client for permission. Select a bounded
                # one-turn approval; all other client-side requests fail closed.
                if "id" in message and message.get("method") == "session/request_permission":
                    self._answer_permission(message)
        finally:
            with self._pending_lock:
                pending = [
                    value for value in self._pending.values()
                    if value[2] == generation
                ]
            for event, bucket, _pending_generation in pending:
                bucket.setdefault("failure", "Kimi ACP exited")
                event.set()

    def _answer_permission(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        options = params.get("options") if isinstance(params, dict) else None
        tool_call = params.get("toolCall") if isinstance(params, dict) else None
        tool_call_id = (
            str(tool_call.get("toolCallId") or "").strip()
            if isinstance(tool_call, dict)
            else ""
        )
        option_id = ""
        if tool_call_id and isinstance(options, list):
            allow_once = [
                option for option in options
                if isinstance(option, dict) and option.get("kind") == "allow_once"
            ]
            has_reject = any(
                isinstance(option, dict)
                and option.get("kind") in {"reject_once", "reject_always"}
                for option in options
            )
            # Standard tool approval has one allow-once choice plus a reject
            # choice. Multiple allow-once options represent a question or plan
            # decision; choosing one without Astra seeing it would be unsafe.
            if len(allow_once) == 1 and has_reject:
                option_id = str(allow_once[0].get("optionId") or "")
        result: dict[str, Any]
        if option_id:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        self._write({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise KimiACPError("Kimi ACP is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise KimiACPError("Kimi ACP connection closed") from exc

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        ensure_started: bool = True,
    ) -> dict[str, Any]:
        if ensure_started:
            self._start()
        with self._pending_lock:
            generation = self._process_generation
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            bucket: dict[str, Any] = {}
            self._pending[request_id] = (event, bucket, generation)
        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if not event.wait(timeout):
                raise KimiACPError(f"Kimi ACP {method} timed out")
            if bucket.get("failure"):
                raise KimiACPError(str(bucket["failure"]))
            message = bucket.get("message")
            if not isinstance(message, dict):
                raise KimiACPError(f"Kimi ACP {method} returned no response")
            if message.get("error"):
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                error_message = str(error.get("message") or "") if isinstance(error, dict) else ""
                if code == -32000 or error_message.strip().lower() == "authentication required":
                    raise KimiACPAuthRequired("Kimi login is required")
                raise KimiACPError(f"Kimi ACP {method} failed" + (f" ({code})" if code else ""))
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _new_or_load_session(self) -> tuple[str, list[dict[str, Any]]]:
        previous = self._load_session_id()
        if previous and previous == self._loaded_session_id and self._process_alive():
            return previous, []
        common = {"cwd": str(self.cwd), "mcpServers": []}
        if previous:
            # Fail closed instead of silently replacing a conversation after
            # restart. A transient/load/auth failure must not make the next
            # user message start in an unrelated context.
            result = self._request(
                "session/load",
                {**common, "sessionId": previous},
                timeout=self.request_timeout,
            )
            loaded = str(result.get("sessionId") or previous)
            self._loaded_session_id = loaded
            options = result.get("configOptions")
            return loaded, options if isinstance(options, list) else []
        result = self._request("session/new", common, timeout=self.request_timeout)
        session_id = str(result.get("sessionId") or "").strip()
        if not session_id:
            raise KimiACPError("Kimi ACP did not return a session id")
        self._save_session_id(session_id)
        self._loaded_session_id = session_id
        options = result.get("configOptions")
        return session_id, options if isinstance(options, list) else []

    @staticmethod
    def _select_option_values(option: dict[str, Any]) -> set[str]:
        values: set[str] = set()
        pending = list(option.get("options") or [])
        while pending:
            item = pending.pop()
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            if value:
                values.add(value)
            nested = item.get("options")
            if isinstance(nested, list):
                pending.extend(nested)
        return values

    @classmethod
    def _model_option_is_highspeed(
        cls,
        options: list[dict[str, Any]],
        *,
        require_current: bool,
    ) -> bool:
        model = next(
            (
                item for item in options
                if isinstance(item, dict) and str(item.get("id") or "") == "model"
            ),
            None,
        )
        if not isinstance(model, dict):
            return False
        if KIMI_APP_MODEL not in cls._select_option_values(model):
            return False
        return (
            not require_current
            or str(model.get("currentValue") or "") == KIMI_APP_MODEL
        )

    def _set_highspeed_model(
        self,
        session_id: str,
        config_options: list[dict[str, Any]],
    ) -> None:
        if self._highspeed_model_session_id == session_id:
            return
        if not self._model_option_is_highspeed(config_options, require_current=False):
            raise KimiACPError("Kimi ACP session does not support the App model")
        current = next(
            (
                item for item in config_options
                if isinstance(item, dict) and str(item.get("id") or "") == "model"
            ),
            {},
        )
        if str(current.get("currentValue") or "") == KIMI_APP_MODEL:
            self._highspeed_model_session_id = session_id
            return
        result = self._request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": KIMI_APP_MODEL},
            timeout=self.request_timeout,
        )
        updated = result.get("configOptions")
        updated_options = updated if isinstance(updated, list) else []
        if not self._model_option_is_highspeed(updated_options, require_current=True):
            raise KimiACPError("Kimi ACP did not confirm the App model")
        self._highspeed_model_session_id = session_id

    def prepare_session(self) -> str:
        """Start/load one session and pin the App-only high-speed model."""
        with self._prepare_lock:
            self._start()
            session_id, config_options = self._new_or_load_session()
            self._set_highspeed_model(session_id, config_options)
            self._save_session_id(session_id)
            return session_id

    def prompt_existing(
        self,
        text: str,
        *,
        session_id: str,
        turn_id: str,
        on_update: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> KimiACPResult:
        session_id = str(session_id or "").strip()
        turn_id = str(turn_id or "").strip()
        if not session_id or not turn_id:
            raise KimiACPError("Kimi ACP exact session and turn identities are required")
        if not self._turn_lock.acquire(blocking=False):
            raise KimiACPBusy("Kimi is already handling another turn")
        try:
            if (
                not self._process_alive()
                or self._loaded_session_id != session_id
                or self._highspeed_model_session_id != session_id
            ):
                raise KimiACPError("Kimi ACP session was not prepared")
            with self._active_lock:
                self._active_session_id = session_id
                self._active_turn_id = turn_id
                self._active_update = on_update
            if cancel_event is not None and cancel_event.is_set():
                raise KimiACPCancelled("Kimi generation cancelled before prompt")
            finished = threading.Event()
            outcome: dict[str, Any] = {}

            def request_prompt() -> None:
                try:
                    outcome["result"] = self._request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                        timeout=self.prompt_timeout,
                    )
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    finished.set()

            worker = threading.Thread(target=request_prompt, name="kimi-acp-prompt", daemon=True)
            worker.start()
            deadline = time.monotonic() + self.prompt_timeout
            cancelled = False
            while not finished.wait(0.1):
                if cancel_event is not None and cancel_event.is_set() and not cancelled:
                    self.cancel(turn_id, session_id)
                    cancelled = True
                if time.monotonic() >= deadline:
                    if not cancelled:
                        self.cancel(turn_id, session_id)
                    raise KimiACPError("Kimi ACP prompt timed out")
            if cancel_event is not None and cancel_event.is_set() and not cancelled:
                self.cancel(turn_id, session_id)
                cancelled = True
            if cancelled:
                raise KimiACPCancelled("Kimi generation cancelled")
            error = outcome.get("error")
            if isinstance(error, Exception):
                raise error
            result = outcome.get("result")
            result = result if isinstance(result, dict) else {}
            return KimiACPResult(
                text="",
                session_id=session_id,
                stop_reason=str(result.get("stopReason") or ""),
            )
        finally:
            with self._active_lock:
                self._active_session_id = ""
                self._active_turn_id = ""
                self._active_update = None
            self._turn_lock.release()

    def cancel(self, turn_id: str, session_id: str) -> bool:
        expected_turn = str(turn_id or "").strip()
        expected_session = str(session_id or "").strip()
        if not expected_turn or not expected_session:
            return False
        with self._active_lock:
            if (
                self._active_turn_id != expected_turn
                or self._active_session_id != expected_session
                or not self._process_alive()
            ):
                return False
            try:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": expected_session},
                    }
                )
                return True
            except KimiACPError:
                return False

    def close(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        self._loaded_session_id = ""
        self._highspeed_model_session_id = ""
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass
