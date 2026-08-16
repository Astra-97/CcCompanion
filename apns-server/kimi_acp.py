"""Small, dependency-free ACP client for the local Kimi Code CLI.

ACP uses one JSON-RPC object per line over stdio.  This module deliberately
keeps the wire protocol away from the HTTP handler so Kimi has an independent
session, lifecycle and cancellation boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable

KIMI_APP_MODEL = "kimi-code/k3-256k"
KIMI_APP_EFFORT = "high"
KIMI_APP_MODELS = frozenset({
    "kimi-code/k3-256k",
    "kimi-code/k3",
    "kimi-code/kimi-for-coding",
    "kimi-code/kimi-for-coding-highspeed",
})
KIMI_APP_EFFORTS = frozenset({"low", "high", "max"})
# Kimi Code 0.36.x renamed the ACP config option to ``thinking``.  Keep the
# older spellings because durable sessions created by earlier CLI releases may
# still report them after an upgrade.  Every support/current/set lookup must
# use this one allowlist so a schema change cannot be accepted on one path and
# then silently fail read-back on another.
KIMI_ACP_EFFORT_OPTION_IDS = frozenset({
    "thinking",
    "thinking_effort",
    "thinkingeffort",
    "reasoning_effort",
    "reasoningeffort",
    "effort",
})
DEFAULT_KIMI_CWD = "/root/Karami-Workspace"


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


def _activity_from_update(params: Any) -> dict[str, Any] | None:
    """Project an ACP update to one prompt-free activity event.

    ACP update payloads may contain tool arguments, tool output, paths and
    model reasoning.  The Android observer gets only this fixed vocabulary.
    In particular, no payload field is copied into the returned dictionary.
    """
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    kind = str(update.get("sessionUpdate") or "").strip().lower()
    if kind in {"agent_thought_chunk", "agent_thought", "thinking"}:
        return {"kind": "activity", "label": "正在思考"}
    if kind in {"tool_call", "tool_use"}:
        return {"kind": "activity", "label": "正在使用工具"}
    if kind in {"subagent_started", "subagent_start", "collaboration_started"}:
        return {
            "kind": "collaboration_worker",
            "worker_id": "kimi-subagent",
            "name": "Kimi 协作 worker",
            "status": "running",
            "count_delta": 1,
        }
    if kind in {"subagent_completed", "subagent_complete", "collaboration_completed"}:
        return {
            "kind": "collaboration_worker",
            "worker_id": "kimi-subagent",
            "name": "Kimi 协作 worker",
            "status": "completed",
            "count_delta": 0,
        }
    if kind in {"subagent_failed", "collaboration_failed"}:
        return {
            "kind": "collaboration_worker",
            "worker_id": "kimi-subagent",
            "name": "Kimi 协作 worker",
            "status": "failed",
            "count_delta": 0,
        }
    return None


class KimiACPClient:
    def __init__(
        self,
        *,
        command: str | Path = "/root/.kimi-code/bin/kimi",
        cwd: str | Path = DEFAULT_KIMI_CWD,
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
        # forge_new_session() holds this lock while it calls prepare_session()
        # for the new session; the nested prepare must be re-entrant.
        self._prepare_lock = threading.RLock()
        self._turn_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any], int]] = {}
        self._next_id = 1
        self._process_generation = 0
        self._active_lock = threading.Lock()
        self._active_session_id = ""
        self._active_turn_id = ""
        self._active_update: Callable[[str], None] | None = None
        self._active_activity: Callable[[dict[str, Any]], None] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._initialized = False
        self._loaded_session_id = ""
        self._app_model_session_id = ""
        self._app_effort_session_id = ""
        self._prepared_selection: dict[str, tuple[str, str]] = {}

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    def load_session_id(self) -> str:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return ""
            if payload.get("version") != 2:
                return ""
            session_id = str(payload.get("session_id") or "").strip()
            saved_cwd = str(payload.get("cwd") or "").strip()
            if not session_id or not saved_cwd:
                return ""
            try:
                canonical_saved_cwd = str(Path(saved_cwd).expanduser().resolve())
            except (OSError, RuntimeError):
                return ""
            return self._valid_session_id(session_id) if canonical_saved_cwd == str(self.cwd) else ""
        except (FileNotFoundError, OSError, ValueError):
            return ""

    def _save_session_id(self, session_id: str) -> None:
        session_id = self._valid_session_id(session_id)
        if not session_id:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(
                {
                    "version": 2,
                    "session_id": session_id,
                    "cwd": str(self.cwd),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    @staticmethod
    def _valid_session_id(value: Any) -> str:
        session_id = str(value or "").strip()
        if (
            not session_id
            or len(session_id) > 200
            or not all(char.isalnum() or char in {"-", "_"} for char in session_id)
        ):
            return ""
        return session_id

    def _session_cache_snapshot(self) -> tuple[str, str, str, dict[str, tuple[str, str]]]:
        """Snapshot only volatile ACP state; the persisted pointer is committed last."""
        return (
            self._loaded_session_id,
            self._app_model_session_id,
            self._app_effort_session_id,
            dict(self._prepared_selection),
        )

    def _restore_session_cache(
        self,
        snapshot: tuple[str, str, str, dict[str, tuple[str, str]]],
    ) -> None:
        (
            self._loaded_session_id,
            self._app_model_session_id,
            self._app_effort_session_id,
            prepared,
        ) = snapshot
        self._prepared_selection = dict(prepared)

    def _invalidate_session_confirmation(self, session_id: str) -> None:
        """Forget local preference confirmation after a failed ACP mutation.

        ``session/set_config_option`` may have applied one part of a selection
        before a later option fails its read-back.  Restoring the previous
        in-memory snapshot alone would then make the fast path trust values
        which only existed before that partial remote mutation.  Clearing the
        affected session's confirmation forces the next preparation to reload,
        pin and read back ACP's actual state.
        """
        clean = self._valid_session_id(session_id)
        if not clean:
            return
        self._prepared_selection.pop(clean, None)
        if self._app_model_session_id == clean:
            self._app_model_session_id = ""
        if self._app_effort_session_id == clean:
            self._app_effort_session_id = ""

    def _activate_uncommitted_session(self, session_id: str) -> None:
        """Make a candidate current in memory without moving the durable pointer."""
        self._loaded_session_id = session_id
        self._app_model_session_id = ""
        self._app_effort_session_id = ""
        self._prepared_selection.pop(session_id, None)

    def select_session_id(self, session_id: str) -> None:
        """Reject legacy non-atomic selection callers.

        A session pointer is durable user context.  Callers must use
        :meth:`prepare_existing_session`, which pins and reads back the model
        selection before committing that pointer.
        """
        if not self._valid_session_id(session_id):
            raise KimiACPError("invalid Kimi session id")
        raise KimiACPError("Kimi session selection must be prepared before it is committed")

    def prepared_selection(self, session_id: str | None = None) -> tuple[str, str] | None:
        """Return the selection that ACP itself confirmed for a session."""
        session = str(session_id or self.load_session_id() or "").strip()
        with self._prepare_lock:
            return self._prepared_selection.get(session)

    def list_local_sessions(self, *, limit: int = 48) -> list[dict[str, Any]]:
        """Read only local sessions whose stored cwd is exactly this workspace.

        This intentionally does not return a raw ACP/web object: paths,
        prompts, agents and tokens are all discarded before the HTTP layer can
        see them.
        """
        root = Path.home() / ".kimi-code" / "sessions"
        records: list[dict[str, Any]] = []
        try:
            for state_file in root.glob("wd_*/session_*/state.json"):
                try:
                    if not state_file.is_file() or state_file.stat().st_size > 128 * 1024:
                        continue
                    raw = json.loads(state_file.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        continue
                    cwd_value = raw.get("cwd") or raw.get("workDir") or ""
                    cwd = Path(str(cwd_value)).expanduser().resolve()
                    session_id = self._valid_session_id(raw.get("id"))
                    if not session_id:
                        directory_name = state_file.parent.name
                        prefix = "session_"
                        if directory_name.startswith(prefix):
                            session_id = self._valid_session_id(directory_name[len(prefix):])
                    if cwd != self.cwd or not session_id:
                        continue
                    updated = raw.get("updatedAt")
                    try:
                        updated_at = int(updated)
                    except (TypeError, ValueError):
                        try:
                            value = str(updated or "").strip()
                            if value.endswith("Z"):
                                value = value[:-1] + "+00:00"
                            parsed = datetime.fromisoformat(value)
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            updated_at = int(parsed.timestamp() * 1000)
                        except (TypeError, ValueError, OverflowError, OSError):
                            updated_at = 0
                    records.append({"session_id": session_id, "updated_at": max(0, updated_at)})
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
        except OSError:
            return []
        records.sort(key=lambda item: int(item["updated_at"]), reverse=True)
        return records[: max(1, min(int(limit), 96))]

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
            self._app_model_session_id = ""
            self._app_effort_session_id = ""
            self._prepared_selection = {}
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
                        activity_callback = (
                            self._active_activity if session_id == self._active_session_id else None
                        )
                    delta = _text_from_update(params)
                    if callback is not None and delta:
                        try:
                            callback(delta)
                        except Exception:
                            self.logger.warning("Kimi ACP update callback failed", exc_info=True)
                    activity = _activity_from_update(params)
                    if activity_callback is not None and activity is not None:
                        try:
                            activity_callback(activity)
                        except Exception:
                            self.logger.warning("Kimi ACP activity callback failed", exc_info=True)
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

    def _load_existing_session(self, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        """Load one known session without changing the durable session pointer."""
        clean = self._valid_session_id(session_id)
        if not clean:
            raise KimiACPError("invalid Kimi session id")
        common = {"cwd": str(self.cwd), "mcpServers": []}
        result = self._request(
            "session/load",
            {**common, "sessionId": clean},
            timeout=self.request_timeout,
        )
        # ACP is not allowed to redirect this request to a different session.
        loaded = self._valid_session_id(result.get("sessionId") or clean)
        if not loaded or loaded != clean:
            raise KimiACPError("Kimi ACP loaded an unexpected session")
        self._loaded_session_id = loaded
        options = result.get("configOptions")
        return loaded, options if isinstance(options, list) else []

    def _new_or_load_session(self, *, force_reload: bool = False) -> tuple[str, list[dict[str, Any]]]:
        previous = self.load_session_id()
        if (
            previous
            and previous == self._loaded_session_id
            and self._process_alive()
            and not force_reload
        ):
            return previous, []
        if previous:
            # Fail closed instead of silently replacing a conversation after
            # restart. A transient/load/auth failure must not make the next
            # user message start in an unrelated context.
            return self._load_existing_session(previous)
        common = {"cwd": str(self.cwd), "mcpServers": []}
        result = self._request("session/new", common, timeout=self.request_timeout)
        session_id = self._valid_session_id(result.get("sessionId"))
        if not session_id:
            raise KimiACPError("Kimi ACP did not return a session id")
        # The caller commits this pointer only after preference pin + readback.
        self._activate_uncommitted_session(session_id)
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

    @staticmethod
    def _option_by_ids(
        options: list[dict[str, Any]],
        ids: frozenset[str],
    ) -> dict[str, Any] | None:
        return next(
            (
                item for item in options
                if isinstance(item, dict) and str(item.get("id") or "").strip().lower() in ids
            ),
            None,
        )

    @classmethod
    def _option_is_value(
        cls,
        options: list[dict[str, Any]],
        *,
        ids: frozenset[str],
        value: str,
        require_current: bool,
    ) -> bool:
        option = cls._option_by_ids(options, ids)
        if not isinstance(option, dict) or value not in cls._select_option_values(option):
            return False
        return not require_current or str(option.get("currentValue") or "") == value

    @classmethod
    def _model_option_is_app_model(
        cls,
        options: list[dict[str, Any]],
        *,
        require_current: bool,
        model: str = KIMI_APP_MODEL,
    ) -> bool:
        return cls._option_is_value(
            options,
            ids=frozenset({"model"}),
            value=model,
            require_current=require_current,
        )

    @classmethod
    def _effort_option_is_app_effort(
        cls,
        options: list[dict[str, Any]],
        *,
        require_current: bool,
        effort: str,
    ) -> bool:
        return cls._option_is_value(
            options,
            ids=KIMI_ACP_EFFORT_OPTION_IDS,
            value=effort,
            require_current=require_current,
        )

    def _set_config_option(
        self,
        session_id: str,
        option: dict[str, Any],
        value: str,
    ) -> list[dict[str, Any]]:
        config_id = str(option.get("id") or "").strip()
        if not config_id:
            raise KimiACPError("Kimi ACP returned an invalid config option")
        result = self._request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=self.request_timeout,
        )
        updated = result.get("configOptions")
        return updated if isinstance(updated, list) else []

    def _set_session_preferences(
        self,
        session_id: str,
        config_options: list[dict[str, Any]],
        *,
        model: str,
        reasoning_effort: str | None,
    ) -> None:
        if model not in KIMI_APP_MODELS:
            raise KimiACPError("Kimi ACP model is not allowed for the App")
        if reasoning_effort is not None and reasoning_effort not in KIMI_APP_EFFORTS:
            raise KimiACPError("Kimi ACP reasoning effort is not allowed for the App")
        if (
            self._app_model_session_id == session_id
            and self._prepared_selection.get(session_id) == (model, reasoning_effort or "")
            and (
                reasoning_effort is None
                or self._app_effort_session_id == session_id
            )
        ):
            return

        options = config_options
        model_option = self._option_by_ids(options, frozenset({"model"}))
        if not self._model_option_is_app_model(options, require_current=False, model=model) or not isinstance(model_option, dict):
            raise KimiACPError("Kimi ACP session does not support the selected App model")
        if str(model_option.get("currentValue") or "") != model:
            options = self._set_config_option(session_id, model_option, model)
        if not self._model_option_is_app_model(options, require_current=True, model=model):
            raise KimiACPError("Kimi ACP did not confirm the selected App model")
        self._app_model_session_id = session_id

        if reasoning_effort is not None:
            effort_option = self._option_by_ids(options, KIMI_ACP_EFFORT_OPTION_IDS)
            if (
                not isinstance(effort_option, dict)
                or not self._effort_option_is_app_effort(
                    options,
                    require_current=False,
                    effort=reasoning_effort,
                )
            ):
                raise KimiACPError("Kimi ACP session does not support the selected App reasoning effort")
            if str(effort_option.get("currentValue") or "") != reasoning_effort:
                options = self._set_config_option(session_id, effort_option, reasoning_effort)
            if not self._effort_option_is_app_effort(
                options,
                require_current=True,
                effort=reasoning_effort,
            ):
                raise KimiACPError("Kimi ACP did not confirm the selected App reasoning effort")
            self._app_effort_session_id = session_id
            self._prepared_selection[session_id] = (model, reasoning_effort)
        else:
            # Compatibility for pre-preferences callers/tests. Production
            # paths always supply the selected effort and therefore require a
            # read-back confirmation above.
            self._prepared_selection[session_id] = (model, "")

    def _set_app_model(
        self,
        session_id: str,
        config_options: list[dict[str, Any]],
    ) -> None:
        """Backward-compatible model-only helper used by older callers."""
        self._set_session_preferences(
            session_id,
            config_options,
            model=KIMI_APP_MODEL,
            reasoning_effort=None,
        )

    def prepare_session(
        self,
        *,
        model: str = KIMI_APP_MODEL,
        reasoning_effort: str | None = None,
    ) -> str:
        """Start/load one session, pin its App selection, and confirm it."""
        with self._prepare_lock:
            self._start()
            snapshot = self._session_cache_snapshot()
            session_id = ""
            try:
                session_id, config_options = self._new_or_load_session()
                desired = (model, reasoning_effort or "")
                # An already-loaded session normally has no config options in
                # the fast path. Reload when the user changed a preference so
                # the new selection is pinned against ACP's current options.
                if not config_options and self._prepared_selection.get(session_id) != desired:
                    session_id, config_options = self._new_or_load_session(force_reload=True)
                self._set_session_preferences(
                    session_id,
                    config_options,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                # This is the durable commit point: before it, a failed pin or
                # read-back leaves the previous context pointer untouched.
                self._save_session_id(session_id)
                return session_id
            except Exception:
                self._restore_session_cache(snapshot)
                self._invalidate_session_confirmation(session_id)
                raise

    def prepare_existing_session(
        self,
        session_id: str,
        *,
        model: str,
        reasoning_effort: str,
    ) -> str:
        """Atomically select an authorized existing session.

        Loading, pinning and read-back all finish before the state file moves
        to ``session_id``.  Failure restores in-memory caches and preserves
        the old pointer for the next turn.
        """
        clean = self._valid_session_id(session_id)
        if not clean:
            raise KimiACPError("invalid Kimi session id")
        with self._prepare_lock:
            self._start()
            snapshot = self._session_cache_snapshot()
            loaded_session_id = ""
            try:
                loaded_session_id, config_options = self._load_existing_session(clean)
                self._activate_uncommitted_session(loaded_session_id)
                self._set_session_preferences(
                    loaded_session_id,
                    config_options,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                self._save_session_id(loaded_session_id)
                return loaded_session_id
            except Exception:
                self._restore_session_cache(snapshot)
                self._invalidate_session_confirmation(loaded_session_id)
                raise

    def new_session(self, *, model: str, reasoning_effort: str) -> str:
        """Create a fresh session, pin both preferences, and read them back."""
        with self._prepare_lock:
            self._start()
            snapshot = self._session_cache_snapshot()
            session_id = ""
            try:
                common = {"cwd": str(self.cwd), "mcpServers": []}
                result = self._request("session/new", common, timeout=self.request_timeout)
                session_id = self._valid_session_id(result.get("sessionId"))
                if not session_id:
                    raise KimiACPError("Kimi ACP did not return a session id")
                config_options = result.get("configOptions")
                self._activate_uncommitted_session(session_id)
                self._set_session_preferences(
                    session_id,
                    config_options if isinstance(config_options, list) else [],
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                self._save_session_id(session_id)
                return session_id
            except Exception:
                self._restore_session_cache(snapshot)
                self._invalidate_session_confirmation(session_id)
                raise

    def prompt_existing(
        self,
        text: str,
        *,
        session_id: str,
        turn_id: str,
        on_update: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
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
                or self._app_model_session_id != session_id
                or (
                    bool(self._prepared_selection.get(session_id, ("", ""))[1])
                    and self._app_effort_session_id != session_id
                )
            ):
                raise KimiACPError("Kimi ACP session was not prepared")
            with self._active_lock:
                self._active_session_id = session_id
                self._active_turn_id = turn_id
                self._active_update = on_update
                self._active_activity = on_activity
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
                self._active_activity = None
            self._turn_lock.release()

    def _prompt_and_collect_text(
        self,
        text: str,
        *,
        session_id: str,
        turn_id: str,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Send a prompt and return the complete assistant text.

        The ACP wire protocol delivers assistant text through
        ``session/update`` chunks; this helper drains them into a single
        string.
        """
        chunks: list[str] = []

        def on_update(delta: str) -> None:
            chunks.append(delta)

        self.prompt_existing(
            text,
            session_id=session_id,
            turn_id=turn_id,
            on_update=on_update,
            cancel_event=cancel_event,
        )
        return "".join(chunks)

    def forge_new_session(
        self,
        *,
        model: str = KIMI_APP_MODEL,
        reasoning_effort: str | None = None,
        summarize_prompt: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[str, str]:
        """Summarize the current session and start a fresh one seeded with it.

        Returns ``(new_session_id, summary_text)``.
        """
        with self._prepare_lock:
            snapshot = self._session_cache_snapshot()
            # 1. Ensure the existing session is loaded.
            old_session_id = self.load_session_id()
            new_session_id = ""
            if not old_session_id:
                raise KimiACPError("No existing Kimi session to forge from")
            try:
                self.prepare_session(model=model, reasoning_effort=reasoning_effort)
                old_session_id = self._loaded_session_id
                if not old_session_id:
                    raise KimiACPError("Kimi session not loaded")

                # 2. Ask Kimi to summarize the conversation so far.
                prompt = summarize_prompt or (
                    "请用一段话总结我们当前会话的所有关键上下文：任务目标、已完成的工作、"
                    "未完成的决策、重要的文件路径或代码位置。要足够详细，让我能在新会话中"
                    "无缝继续。用中文。"
                )
                turn_id = f"forge-summarize-{int(time.time() * 1000)}"
                summary = self._prompt_and_collect_text(
                    prompt,
                    session_id=old_session_id,
                    turn_id=turn_id,
                    cancel_event=cancel_event,
                )
                if not summary.strip():
                    raise KimiACPError("Kimi forge summary was empty")

                # 3. Pin a brand-new session, but do not publish it as the
                # active pointer until the inherited-context seed has landed.
                common = {"cwd": str(self.cwd), "mcpServers": []}
                result = self._request("session/new", common, timeout=self.request_timeout)
                new_session_id = self._valid_session_id(result.get("sessionId"))
                if not new_session_id:
                    raise KimiACPError("Kimi ACP did not return a new session id")
                config_options = result.get("configOptions")
                self._activate_uncommitted_session(new_session_id)
                self._set_session_preferences(
                    new_session_id,
                    config_options if isinstance(config_options, list) else [],
                    model=model,
                    reasoning_effort=reasoning_effort,
                )

                # 4. Seed the candidate. A cancelled/failed seed must leave
                # the durable pointer on the old conversation.
                seed_turn_id = f"forge-seed-{int(time.time() * 1000)}"
                seed_prompt = (
                    "【上下文继承】这是我们之前会话的总结，请牢记它，"
                    "后续所有请求都在此基础上继续：\n\n" + summary
                )
                self._prompt_and_collect_text(
                    seed_prompt,
                    session_id=new_session_id,
                    turn_id=seed_turn_id,
                    cancel_event=cancel_event,
                )
                self._save_session_id(new_session_id)
                return new_session_id, summary
            except Exception:
                self._restore_session_cache(snapshot)
                # Either the current session pin or the candidate session pin
                # can have partially changed ACP before raising.  Neither may
                # retain a stale local confirmation after rollback.
                self._invalidate_session_confirmation(old_session_id)
                self._invalidate_session_confirmation(new_session_id)
                raise

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
        self._app_model_session_id = ""
        self._app_effort_session_id = ""
        self._prepared_selection = {}
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
