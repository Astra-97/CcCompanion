"""Small, dependency-free ACP client for the local Kiro CLI (kiro 桥接 2026-09-09).

Forked from kimi_acp.py and adapted to ``kiro-cli acp``.  ACP uses one
JSON-RPC object per line over stdio.  This module deliberately keeps the wire
protocol away from the HTTP handler so Kiro has an independent session,
lifecycle and cancellation boundary.

Phase 1 scope: text chat + durable session continuity (session/new,
session/load, session/prompt, session/cancel).  Image blocks, forge and
quota bridges are later phases and intentionally absent here.

kiro 切模型 (2026-09-10): the client captures the ``models`` block from
session/new and session/load responses (``availableModels`` /
``currentModelId``), persists a sanitized catalog cache next to the session
pointer, and pins one allowlisted model via ``session/set_model``.  Kiro
2.21.2 answers ``session/set_model`` with an empty result even for unknown
ids and ``session/load`` resets to the persisted default, so the pin is
re-applied after every new/load and catalog membership is the only
validation available on this side of the wire.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable

KIRO_DEFAULT_COMMAND = str(Path.home() / ".local" / "bin" / "kiro-cli")
DEFAULT_KIRO_CWD = "/root/Karami-Workspace"


class KiroACPError(RuntimeError):
    pass


class KiroACPBusy(KiroACPError):
    pass


class KiroACPAuthRequired(KiroACPError):
    pass


class KiroACPQuotaExceeded(KiroACPError):
    pass


class KiroACPCancelled(KiroACPError):
    pass


@dataclass(frozen=True)
class KiroACPResult:
    text: str
    session_id: str
    stop_reason: str


def _update_kind(params: Any) -> dict[str, Any] | None:
    """Return the ACP update payload of a session notification, if any."""
    if not isinstance(params, dict):
        return None
    update = params.get("update")
    return update if isinstance(update, dict) else None


def _normalized_update_name(update: dict[str, Any]) -> str:
    """Normalize ``sessionUpdate`` across snake_case/camelCase spellings."""
    return re.sub(r"[_\-\s]", "", str(update.get("sessionUpdate") or "")).lower()


def _text_from_update(params: Any) -> str:
    """Return only assistant text from an ACP session/update notification."""
    update = _update_kind(params)
    if update is None or _normalized_update_name(update) != "agentmessagechunk":
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
    update = _update_kind(params)
    if update is None:
        return None
    kind = _normalized_update_name(update)
    if kind in {"agentthoughtchunk", "agentthought", "thinking"}:
        return {"kind": "activity", "label": "正在思考"}
    if kind in {"toolcall", "toolcallupdate", "tooluse"}:
        return {"kind": "activity", "label": "正在使用工具"}
    return None


# Auth/quota classification is message-based because Kiro's ACP error codes
# are not a published contract.  The patterns stay narrow so an ordinary
# provider failure never masquerades as a login or billing state.
_AUTH_MESSAGE_RE = re.compile(
    r"authentication required|not\s+logged\s+in|unauthorized|login required|\b401\b",
    re.IGNORECASE,
)
_QUOTA_MESSAGE_RE = re.compile(
    r"\b402\b|payment required|insufficient.{0,24}credit|out of credits|"
    r"quota exceeded|monthly limit|usage limit",
    re.IGNORECASE,
)
# kiro-cli refuses to answer even ``initialize`` when logged out; it exits
# with this stderr line instead.  Matched as a fixed pattern only — stderr
# content itself is never logged or surfaced.
_STDERR_AUTH_RE = re.compile(r"not logged in|please log in|login required", re.IGNORECASE)

# kiro 切模型 (2026-09-10): model ids ride the wire into session/set_model,
# so only this closed charset may ever leave the process.
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,119}\Z")
_MODEL_CATALOG_MAX_ENTRIES = 64
_MODEL_CATALOG_MAX_BYTES = 64 * 1024


def _valid_model_id(value: Any) -> str:
    model_id = str(value or "").strip()
    return model_id if _MODEL_ID_RE.fullmatch(model_id) else ""


def _clean_catalog_text(value: Any, maximum: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:maximum]


def _classified_rpc_error(method: str, error: Any) -> KiroACPError:
    """Map one JSON-RPC error object to a typed, payload-free failure."""
    code = error.get("code") if isinstance(error, dict) else None
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    if code == -32000 or _AUTH_MESSAGE_RE.search(message):
        return KiroACPAuthRequired("Kiro login is required")
    if _QUOTA_MESSAGE_RE.search(message):
        return KiroACPQuotaExceeded("Kiro credits or quota are exhausted")
    suffix = f" ({code})" if code else ""
    return KiroACPError(f"Kiro ACP {method} failed{suffix}")


class KiroACPClient:
    def __init__(
        self,
        *,
        command: str | Path = KIRO_DEFAULT_COMMAND,
        cwd: str | Path = DEFAULT_KIRO_CWD,
        state_path: str | Path,
        logger: logging.Logger | None = None,
        request_timeout: float = 30.0,
        prompt_timeout: float = 900.0,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        catalog_path: str | Path | None = None,
    ):
        self.command = str(Path(command).expanduser())
        self.cwd = Path(cwd).expanduser().resolve()
        self.state_path = Path(state_path).expanduser()
        # kiro 切模型 (2026-09-10): sanitized copy of the last models block
        # seen on the wire; the cache lets /kiro/preferences answer while the
        # ACP process is down.
        self.catalog_path = (
            Path(catalog_path).expanduser()
            if catalog_path is not None
            else self.state_path.with_name("kiro_models_cache.json")
        )
        self._catalog_lock = threading.Lock()
        self._available_models: list[dict[str, str]] = []
        self._current_model_id = ""
        self._pinned_model_id = ""
        self.logger = logger or logging.getLogger(__name__)
        self.request_timeout = max(1.0, float(request_timeout))
        self.prompt_timeout = max(self.request_timeout, float(prompt_timeout))
        self._popen_factory = popen_factory
        self._process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
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
        self._stderr_done = threading.Event()
        self._stderr_auth_hint = False
        self._initialized = False
        self._loaded_session_id = ""

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

    # ---------- kiro 切模型 (2026-09-10): model catalog + pinning ----------

    def _capture_model_catalog(self, result: dict[str, Any]) -> None:
        """Snapshot the sanitized ``models`` block of a session/new|load result."""
        models = result.get("models") if isinstance(result, dict) else None
        if not isinstance(models, dict):
            return
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        raw_entries = models.get("availableModels")
        for item in raw_entries if isinstance(raw_entries, list) else []:
            if not isinstance(item, dict):
                continue
            model_id = _valid_model_id(item.get("modelId"))
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            entries.append({
                "id": model_id,
                "name": _clean_catalog_text(item.get("name"), 120) or model_id,
                "description": _clean_catalog_text(item.get("description"), 240),
            })
            if len(entries) >= _MODEL_CATALOG_MAX_ENTRIES:
                break
        if not entries:
            return
        current = _valid_model_id(models.get("currentModelId"))
        with self._catalog_lock:
            self._available_models = entries
            if current:
                self._current_model_id = current
        try:
            self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.catalog_path.with_name(f".{self.catalog_path.name}.tmp.{os.getpid()}")
            tmp.write_text(
                json.dumps({"version": 1, "captured_at": int(time.time()), "models": entries},
                           ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.catalog_path)
        except OSError:
            self.logger.warning("Kiro model catalog cache write failed", exc_info=True)

    def _load_model_catalog_cache(self) -> list[dict[str, str]]:
        try:
            info = self.catalog_path.stat()
            if not info.st_mode or info.st_size > _MODEL_CATALOG_MAX_BYTES:
                return []
            raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                return []
            entries: list[dict[str, str]] = []
            seen: set[str] = set()
            raw_entries = raw.get("models")
            for item in raw_entries if isinstance(raw_entries, list) else []:
                if not isinstance(item, dict):
                    continue
                model_id = _valid_model_id(item.get("id"))
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                entries.append({
                    "id": model_id,
                    "name": _clean_catalog_text(item.get("name"), 120) or model_id,
                    "description": _clean_catalog_text(item.get("description"), 240),
                })
                if len(entries) >= _MODEL_CATALOG_MAX_ENTRIES:
                    break
            return entries
        except (OSError, ValueError):
            return []

    def available_models(self) -> tuple[list[dict[str, str]], str]:
        """Return (catalog entries, source) where source is live|cache|none."""
        with self._catalog_lock:
            if self._available_models:
                return [dict(entry) for entry in self._available_models], "live"
        cached = self._load_model_catalog_cache()
        return cached, ("cache" if cached else "none")

    def available_model_ids(self) -> tuple[str, ...]:
        entries, _source = self.available_models()
        return tuple(entry["id"] for entry in entries)

    def current_model_id(self) -> str:
        with self._catalog_lock:
            return self._current_model_id

    def pin_model(self, model_id: str) -> str:
        """Pin one catalog model for the next and current sessions (no RPC)."""
        clean = _valid_model_id(model_id)
        if not clean:
            raise KiroACPError("invalid Kiro model id")
        ids = self.available_model_ids()
        if ids and clean not in ids:
            raise KiroACPError("Kiro model is not in the available catalog")
        self._pinned_model_id = clean
        return clean

    def set_model(self, model_id: str) -> str:
        """Pin ``model_id`` and apply it to the loaded session right now."""
        clean = self.pin_model(model_id)
        session_id = self._loaded_session_id
        if not session_id or not self._process_alive():
            raise KiroACPError("Kiro ACP session was not prepared")
        self._request(
            "session/set_model",
            {"sessionId": session_id, "modelId": clean},
            timeout=self.request_timeout,
        )
        self._current_model_id = clean
        return clean

    def _apply_pinned_model(self) -> None:
        """Re-apply the pin after new/load; the wire does not persist it."""
        pinned = self._pinned_model_id
        if not pinned:
            return
        ids = self.available_model_ids()
        if ids and pinned not in ids:
            self.logger.warning("Kiro pinned model is no longer offered; keeping Kiro default")
            return
        if pinned == self.current_model_id():
            return
        try:
            self.set_model(pinned)
        except KiroACPError:
            # 钉选已持久化，下次 prepare 自愈；一次重放失败不该打死用户消息。
            self.logger.warning("Kiro pinned model re-apply failed; will retry on next prepare", exc_info=True)

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
                raise KiroACPError(f"Kiro ACP could not start: {exc}") from exc
            self._process = process
            self._process_generation += 1
            generation = self._process_generation
            self._initialized = False
            self._loaded_session_id = ""
            self._stderr_done = threading.Event()
            self._stderr_auth_hint = False
            self._reader = threading.Thread(
                target=self._read_stdout,
                args=(process, generation),
                name="kiro-acp-stdout",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._drain_stderr,
                args=(process,),
                name="kiro-acp-stderr",
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
            self._stderr_done.set()
            return
        # Never log stderr content: it may contain prompts, paths, or auth
        # data.  Only a fixed logged-out pattern becomes a boolean hint.
        try:
            for line in process.stderr:
                if _STDERR_AUTH_RE.search(line):
                    self._stderr_auth_hint = True
        except Exception:
            pass
        finally:
            self._stderr_done.set()

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
                # Kiro documents its stream as ``session/notification`` while
                # the ACP spec and Kimi use ``session/update``; accept both.
                if message.get("method") in {"session/update", "session/notification"}:
                    params = message.get("params")
                    if not isinstance(params, dict):
                        continue
                    session_id = str(params.get("sessionId") or "")
                    with self._active_lock:
                        matches = session_id == self._active_session_id
                        callback = self._active_update if matches else None
                        activity_callback = self._active_activity if matches else None
                    delta = _text_from_update(params)
                    if callback is not None and delta:
                        try:
                            callback(delta)
                        except Exception:
                            self.logger.warning("Kiro ACP update callback failed", exc_info=True)
                    activity = _activity_from_update(params)
                    if activity_callback is not None and activity is not None:
                        try:
                            activity_callback(activity)
                        except Exception:
                            self.logger.warning("Kiro ACP activity callback failed", exc_info=True)
                    continue
                # Kiro can ask its ACP client for permission. Select a bounded
                # one-turn approval; all other client-side requests fail closed.
                if "id" in message and message.get("method") == "session/request_permission":
                    self._answer_permission(message)
        finally:
            # A logged-out kiro-cli closes stdout without answering; wait for
            # the process and its stderr drain so the auth hint is settled
            # before pending requests are woken.
            try:
                process.wait(timeout=2)
            except Exception:
                pass
            self._stderr_done.wait(timeout=1.0)
            failure = "Kiro ACP login is required" if self._stderr_auth_hint else "Kiro ACP exited"
            with self._pending_lock:
                pending = [
                    value for value in self._pending.values()
                    if value[2] == generation
                ]
            for event, bucket, _pending_generation in pending:
                bucket.setdefault("failure", failure)
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
            raise KiroACPError("Kiro ACP is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise KiroACPError("Kiro ACP connection closed") from exc

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
                raise KiroACPError(f"Kiro ACP {method} timed out")
            if bucket.get("failure"):
                failure = str(bucket["failure"])
                if failure == "Kiro ACP login is required":
                    raise KiroACPAuthRequired("Kiro login is required")
                raise KiroACPError(failure)
            message = bucket.get("message")
            if not isinstance(message, dict):
                raise KiroACPError(f"Kiro ACP {method} returned no response")
            if message.get("error"):
                raise _classified_rpc_error(method, message["error"])
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def _load_existing_session(self, session_id: str) -> str:
        """Load one known session without changing the durable session pointer."""
        clean = self._valid_session_id(session_id)
        if not clean:
            raise KiroACPError("invalid Kiro session id")
        result = self._request(
            "session/load",
            {"cwd": str(self.cwd), "mcpServers": [], "sessionId": clean},
            timeout=self.request_timeout,
        )
        # ACP is not allowed to redirect this request to a different session.
        loaded = self._valid_session_id(result.get("sessionId") or clean)
        if not loaded or loaded != clean:
            raise KiroACPError("Kiro ACP loaded an unexpected session")
        self._capture_model_catalog(result)
        self._loaded_session_id = loaded
        return loaded

    def _new_session_id(self) -> str:
        result = self._request(
            "session/new",
            {"cwd": str(self.cwd), "mcpServers": []},
            timeout=self.request_timeout,
        )
        session_id = self._valid_session_id(result.get("sessionId"))
        if not session_id:
            raise KiroACPError("Kiro ACP did not return a session id")
        self._capture_model_catalog(result)
        self._loaded_session_id = session_id
        return session_id

    def _new_or_load_session(self) -> str:
        previous = self.load_session_id()
        if previous and previous == self._loaded_session_id and self._process_alive():
            return previous
        if previous:
            # Fail closed instead of silently replacing a conversation after
            # restart. A transient/load/auth failure must not make the next
            # user message start in an unrelated context; POST /kiro/new_session
            # is the explicit recovery path.
            return self._load_existing_session(previous)
        session_id = self._new_session_id()
        # The new pointer commits only after ACP confirmed a valid session id.
        self._save_session_id(session_id)
        return session_id

    def prepare_session(self, *, model: str | None = None) -> str:
        """Start the ACP process if needed and return the current session id.

        kiro 切模型 (2026-09-10): ``model`` pins the App-owned selection; the
        pin is re-applied after every new/load because Kiro persists neither
        set_model nor a read-back across session/load.
        """
        with self._prepare_lock:
            if model is not None:
                self.pin_model(model)
            self._start()
            session_id = self._new_or_load_session()
            self._apply_pinned_model()
            return session_id

    def new_session(self, *, model: str | None = None) -> str:
        """Explicitly abandon the persisted pointer and start a fresh session."""
        with self._prepare_lock:
            if model is not None:
                self.pin_model(model)
            self._start()
            session_id = self._new_session_id()
            self._save_session_id(session_id)
            self._apply_pinned_model()
            return session_id

    def prompt_existing(
        self,
        text: str,
        *,
        session_id: str,
        turn_id: str,
        on_update: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> KiroACPResult:
        session_id = str(session_id or "").strip()
        turn_id = str(turn_id or "").strip()
        if not session_id or not turn_id:
            raise KiroACPError("Kiro ACP exact session and turn identities are required")
        if not self._turn_lock.acquire(blocking=False):
            raise KiroACPBusy("Kiro is already handling another turn")
        try:
            if not self._process_alive() or self._loaded_session_id != session_id:
                raise KiroACPError("Kiro ACP session was not prepared")
            with self._active_lock:
                self._active_session_id = session_id
                self._active_turn_id = turn_id
                self._active_update = on_update
                self._active_activity = on_activity
            if cancel_event is not None and cancel_event.is_set():
                raise KiroACPCancelled("Kiro generation cancelled before prompt")
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

            worker = threading.Thread(target=request_prompt, name="kiro-acp-prompt", daemon=True)
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
                    raise KiroACPError("Kiro ACP prompt timed out")
            if cancel_event is not None and cancel_event.is_set() and not cancelled:
                self.cancel(turn_id, session_id)
                cancelled = True
            if cancelled:
                raise KiroACPCancelled("Kiro generation cancelled")
            error = outcome.get("error")
            if isinstance(error, Exception):
                raise error
            result = outcome.get("result")
            result = result if isinstance(result, dict) else {}
            return KiroACPResult(
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
            except KiroACPError:
                return False

    def close(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        self._loaded_session_id = ""
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
