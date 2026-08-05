"""Persistent Codex app-server v2 bridge for the CcCompanion Kairos chat."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Sequence
import uuid

from websockets.sync.client import unix_connect

try:
    import fcntl
except ImportError:  # pragma: no cover - production is Linux.
    fcntl = None  # type: ignore[assignment]


UpdateCallback = Callable[[str], None]
ActivityCallback = Callable[[str], None]
ThreadCallback = Callable[[str], None]
TurnAcceptedCallback = Callable[[str], None]
MarkerProvider = Callable[[str], tuple[str, int, int] | None]

OBSERVER_PHASE_LABELS = {
    "preparing": "正在准备",
    "starting": "正在启动",
    "running": "正在处理",
    "completed": "处理完成",
    "interrupted": "已中断",
    "failed": "处理失败",
}
OBSERVER_ITEM_LABELS = {
    "agentMessage": "正在整理回复（内容已隐藏）",
    "reasoning": "正在分析（思考内容已隐藏）",
    "commandExecution": "运行命令（参数与输出已隐藏）",
    "fileChange": "修改文件（路径与内容已隐藏）",
    "mcpToolCall": "调用工具（名称与参数已隐藏）",
    "dynamicToolCall": "调用工具（名称与参数已隐藏）",
    "collabAgentToolCall": "调用协作代理（任务内容已隐藏）",
    "subAgentActivity": "协作代理处理中（详情已隐藏）",
    "webSearch": "搜索资料（查询内容已隐藏）",
    "imageView": "查看图片（路径已隐藏）",
    "imageGeneration": "生成图片（提示词已隐藏）",
    "contextCompaction": "整理会话上下文（内容已隐藏）",
    "sleep": "等待外部步骤完成",
}
OBSERVER_EVENT_LABELS = frozenset({
    "已接收任务，正在准备",
    "正在启动本轮处理",
    "开始处理",
    *OBSERVER_ITEM_LABELS.values(),
})
OBSERVER_PHASES = frozenset(OBSERVER_PHASE_LABELS.values())


class CodexAppBridgeError(RuntimeError):
    """Base bridge failure with an explicit legacy-fallback safety signal."""

    def __init__(
        self,
        message: str,
        *,
        fallback_safe: bool = False,
        uncertain: bool = False,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.fallback_safe = fallback_safe
        self.uncertain = uncertain
        self.thread_id = thread_id
        self.turn_id = turn_id


class CodexPromptLockBusy(CodexAppBridgeError):
    def __init__(self) -> None:
        super().__init__("Codex session is locked by another process", fallback_safe=True)


class CodexActiveTurnError(CodexAppBridgeError):
    def __init__(self) -> None:
        super().__init__("Codex app-server bridge already has an active turn")


class _RPCError(CodexAppBridgeError):
    def __init__(self, method: str, error: Any) -> None:
        code = error.get("code") if isinstance(error, dict) else None
        super().__init__(f"app-server rejected {method} (code={code})")
        self.method = method
        self.error = error


class _TransportError(CodexAppBridgeError):
    pass


class _RecoveryCancelled(CodexAppBridgeError):
    pass


@dataclass(frozen=True)
class CodexTokenUsageBreakdown:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    @classmethod
    def from_payload(cls, payload: Any) -> CodexTokenUsageBreakdown | None:
        if not isinstance(payload, dict):
            return None
        fields = {
            "input_tokens": payload.get("inputTokens"),
            "cached_input_tokens": payload.get("cachedInputTokens"),
            "output_tokens": payload.get("outputTokens"),
            "reasoning_output_tokens": payload.get("reasoningOutputTokens"),
            "total_tokens": payload.get("totalTokens"),
        }
        if any(not isinstance(value, int) or isinstance(value, bool) for value in fields.values()):
            return None
        return cls(**fields)


@dataclass(frozen=True)
class CodexThreadTokenUsage:
    total: CodexTokenUsageBreakdown
    last: CodexTokenUsageBreakdown
    model_context_window: int | None

    @classmethod
    def from_payload(cls, payload: Any) -> CodexThreadTokenUsage | None:
        if not isinstance(payload, dict):
            return None
        total = CodexTokenUsageBreakdown.from_payload(payload.get("total"))
        last = CodexTokenUsageBreakdown.from_payload(payload.get("last"))
        model_context_window = payload.get("modelContextWindow")
        if total is None or last is None:
            return None
        if model_context_window is not None and (
            not isinstance(model_context_window, int) or isinstance(model_context_window, bool)
        ):
            return None
        return cls(
            total=total,
            last=last,
            model_context_window=model_context_window,
        )


@dataclass(frozen=True)
class CodexTurnResult:
    thread_id: str
    turn_id: str | None
    text: str
    status: str
    error: str | None = None
    token_usage: CodexThreadTokenUsage | None = None
    context_compacted: bool = False


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Any = None
    transport_error: BaseException | None = None


@dataclass
class _ActiveTurn:
    thread_id: str | None
    on_update: UpdateCallback | None
    on_activity: ActivityCallback | None
    started_at: float = field(default_factory=time.time)
    phase: str = "preparing"
    turn_id: str | None = None
    status: str | None = None
    error: str | None = None
    token_usage: CodexThreadTokenUsage | None = None
    context_compacted: bool = False
    connection_lost: bool = False
    connection_generation: int | None = None
    interrupt_requested: bool = False
    interrupt_sent: bool = False
    agent_deltas: OrderedDict[str, str] = field(default_factory=OrderedDict)
    final_messages: OrderedDict[str, str] = field(default_factory=OrderedDict)
    completed_items: set[str] = field(default_factory=set)
    activity_items: set[str] = field(default_factory=set)
    observer_events: list[tuple[int, str]] = field(default_factory=list)
    observer_event_keys: set[str] = field(default_factory=set)
    pending_notifications: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)


class _PromptProcessLock:
    def __init__(self, session_id: str | None, cwd: Path) -> None:
        self.session_id = str(session_id or "").strip() or None
        self.cwd = cwd
        self.path = prompt_lock_path(self.session_id, cwd)
        self._file: Any = None

    def acquire(self) -> bool:
        if fcntl is None:
            raise CodexAppBridgeError(
                "fcntl is required for the Codex cross-process prompt lock",
                fallback_safe=True,
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return False
        except Exception:
            lock_file.close()
            raise
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(json.dumps({
            "pid": os.getpid(),
            "cwd": str(self.cwd),
            "session_id": self.session_id or "",
            "started_at": int(time.time()),
            "owner": "cc-companion-app-server",
        }, separators=(",", ":")))
        lock_file.flush()
        self._file = lock_file
        return True

    def release(self) -> None:
        lock_file = self._file
        self._file = None
        if lock_file is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def prompt_lock_path(session_id: str | None, cwd: Path) -> Path:
    """Match Windows-Codex-TG CodexRunner._prompt_lock_path exactly."""
    cleaned_session_id = str(session_id or "").strip()
    if cleaned_session_id:
        identity = f"session:{cleaned_session_id}"
    else:
        try:
            cwd_text = str(Path(cwd).expanduser().resolve())
        except Exception:
            cwd_text = str(cwd)
        identity = f"cwd:{cwd_text}"
    lock_root = Path(
        os.environ.get("CODEX_PROMPT_LOCK_DIR", "/tmp/windows-codex-tg-session-locks")
    ).expanduser()
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return lock_root / f"{digest}.lock"


def prompt_lock_is_busy(session_id: str | None, cwd: Path) -> bool:
    if fcntl is None:
        return False
    path = prompt_lock_path(session_id, cwd)
    lock_file = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if lock_file is not None:
            lock_file.close()
        return True
    except Exception:
        if lock_file is not None:
            lock_file.close()
        return False
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()
    return False


class CodexAppBridge:
    """Keep one client connection to Codex's shared app-server daemon.

    Production uses the official local Unix/WebSocket control socket so Remote,
    the Codex TUI, and CcCompanion all submit through one app-server process.
    ``command`` remains an explicit stdio transport override for tests and
    emergency rollback.
    """

    def __init__(
        self,
        *,
        codex_bin: str = "/usr/bin/codex",
        codex_home: str = "/root/.codex",
        command: Sequence[str] | None = None,
        logger: logging.Logger | None = None,
        request_timeout_sec: float = 30.0,
        interrupt_grace_sec: float = 10.0,
        model_auto_compact_token_limit: int | None = None,
        daemon_socket_path: str | None = None,
        daemon_autostart: bool = True,
        daemon_recovery_timeout_sec: float = 90.0,
        daemon_start_timeout_sec: float = 20.0,
        daemon_connect_retry_sec: float = 0.25,
        pre_submit_reconnect_attempts: int = 2,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = str(Path(codex_home).expanduser())
        self.command = list(command) if command else None
        self.daemon_socket_path = Path(
            daemon_socket_path
            or (Path(self.codex_home) / "app-server-control" / "app-server-control.sock")
        ).expanduser()
        self.daemon_autostart = bool(daemon_autostart)
        self.daemon_recovery_timeout_sec = max(0.05, float(daemon_recovery_timeout_sec))
        self.daemon_start_timeout_sec = max(0.05, float(daemon_start_timeout_sec))
        self.daemon_connect_retry_sec = max(0.01, float(daemon_connect_retry_sec))
        self.pre_submit_reconnect_attempts = max(0, int(pre_submit_reconnect_attempts))
        self.log = logger or logging.getLogger(__name__)
        self.request_timeout_sec = request_timeout_sec
        self.interrupt_grace_sec = max(0.1, float(interrupt_grace_sec))
        self.model_auto_compact_token_limit = (
            int(model_auto_compact_token_limit)
            if model_auto_compact_token_limit is not None
            else None
        )

        self._connect_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._turn_gate = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_request_id = 1
        self._process: subprocess.Popen[str] | None = None
        self._websocket: Any = None
        self._reader: threading.Thread | None = None
        self._generation = 0
        self._initialized = False
        self._connection_error: BaseException | None = None
        self._active: _ActiveTurn | None = None
        self._last_markers: dict[str, tuple[str, int, int] | None] = {}
        self._marker_baselines: set[str] = set()
        self._closing = threading.Event()

    def close(self) -> None:
        # Wake an in-flight daemon recovery before waiting for its connect lock.
        self._closing.set()
        self.interrupt_active(timeout=1.0)
        with self._connect_lock:
            self._close_process_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            active = self._active
            process = self._process
            if active is None:
                return {
                    "busy": False,
                    "pid": process.pid if process and process.poll() is None else None,
                    "thread_id": None,
                    "turn_id": None,
                    "phase": None,
                    "started_at": None,
                }
            return {
                "busy": True,
                "pid": process.pid if process and process.poll() is None else None,
                "thread_id": active.thread_id,
                "turn_id": active.turn_id,
                "phase": active.phase,
                "started_at": active.started_at,
            }

    def list_models(
        self,
        *,
        cwd: Path,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Return the complete picker-visible app-server model catalog.

        This uses the same authenticated local app-server connection as turns,
        creates/resumes no thread, and never substitutes a hard-coded catalog.
        """

        cwd = Path(cwd).expanduser().resolve()
        deadline = time.monotonic() + max(0.1, float(timeout))
        # The shared-daemon transport accepts an end-to-end recovery deadline;
        # the legacy stdio bridge predates that keyword.  Keep the committed
        # method valid on both implementations without giving daemon recovery
        # an unbounded window beyond this request.
        if hasattr(self, "daemon_recovery_timeout_sec"):
            self._ensure_connected(cwd, recovery_deadline=deadline)
        else:
            self._ensure_connected(cwd)
        data: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        cursor: str | None = None
        for _page in range(20):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportError("timed out waiting for model/list")
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = self._rpc_request("model/list", params, timeout=remaining)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise CodexAppBridgeError("model/list returned an invalid result")
            for item in result["data"]:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or item.get("model") or "").strip()
                if item_id and item_id not in seen_ids:
                    data.append(item)
                    seen_ids.add(item_id)
            next_cursor = result.get("nextCursor")
            if next_cursor is None or next_cursor == "":
                return {"data": data, "nextCursor": None}
            if not isinstance(next_cursor, str) or next_cursor == cursor:
                raise CodexAppBridgeError("model/list returned an invalid cursor")
            cursor = next_cursor
        raise CodexAppBridgeError("model/list exceeded the pagination limit")

    def observer_snapshot(self) -> dict[str, Any]:
        """Return a deliberately low-detail view suitable for the App terminal.

        This is a separate API from :meth:`snapshot` so callers cannot
        accidentally expose process/thread identifiers.  Observer events are
        generated from an allowlist of app-server item *types* only; command
        arguments, paths, tool parameters, model text, and user input never
        enter this buffer.
        """
        with self._state_lock:
            active = self._active
        if active is None:
            return {"busy": False, "phase": None, "events": []}
        with active.condition:
            return {
                "busy": True,
                "phase": self._observer_phase(active.phase),
                "events": [
                    {"elapsed_seconds": elapsed, "label": label}
                    for elapsed, label in active.observer_events
                ],
            }

    def interrupt_active(self, *, timeout: float = 3.0) -> bool:
        with self._state_lock:
            active = self._active
        if active is None:
            return False
        with active.condition:
            if not active.thread_id or not active.turn_id:
                active.interrupt_requested = True
                active.condition.notify_all()
                return True
            if active.interrupt_sent:
                return True
            active.interrupt_requested = True
            active.interrupt_sent = True
            thread_id = active.thread_id
            turn_id = active.turn_id
        try:
            self._rpc_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=timeout,
            )
        except CodexAppBridgeError:
            with active.condition:
                active.interrupt_sent = False
            return False
        return True

    def run_turn(
        self,
        *,
        thread_id: str | None,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        image_paths: Sequence[Path] = (),
        cancel_event: threading.Event | None = None,
        on_update: UpdateCallback | None = None,
        on_activity: ActivityCallback | None = None,
        on_thread: ThreadCallback | None = None,
        on_turn_accepted: TurnAcceptedCallback | None = None,
        marker_provider: MarkerProvider | None = None,
        max_runtime_sec: float = 0,
    ) -> CodexTurnResult:
        if not self._turn_gate.acquire(blocking=False):
            raise CodexActiveTurnError()

        cwd = Path(cwd).expanduser().resolve()
        initial_thread_id = str(thread_id or "").strip() or None
        locks: list[_PromptProcessLock] = []
        active: _ActiveTurn | None = None
        resolved_thread_id = initial_thread_id
        try:
            # The official daemon is the single writer for Remote, TUI, and
            # CcCompanion.  The legacy flock is retained only for the explicit
            # stdio transport, which can still coexist with older CodexRunner
            # callers during rollback.
            if self.command is not None:
                initial_lock = _PromptProcessLock(initial_thread_id, cwd)
                if not initial_lock.acquire():
                    raise CodexPromptLockBusy()
                locks.append(initial_lock)

            if self.command is not None and self._rollout_changed(
                initial_thread_id,
                marker_provider,
            ):
                self.log.info("Codex rollout changed outside app-server; reloading thread")
                with self._connect_lock:
                    self._close_process_locked()

            active = _ActiveTurn(
                thread_id=initial_thread_id,
                on_update=on_update,
                on_activity=on_activity,
            )
            with self._state_lock:
                self._active = active
            self._record_observer_event(active, "已接收任务，正在准备", key="phase:preparing")

            if cancel_event is not None and cancel_event.is_set():
                return self._interrupted_before_start(active)

            recovery_deadline = time.monotonic() + self.daemon_recovery_timeout_sec
            cancel_requested = lambda: (
                self._closing.is_set()
                or active.interrupt_requested
                or (cancel_event is not None and cancel_event.is_set())
            )
            try:
                resolved_thread_id, prior_turns = self._prepare_thread_resilient(
                    initial_thread_id,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                    active=active,
                    recovery_deadline=recovery_deadline,
                    cancel_requested=cancel_requested,
                )
            except _RecoveryCancelled:
                return self._interrupted_before_start(active)
            except CodexAppBridgeError as exc:
                raise CodexAppBridgeError(
                    str(exc),
                    fallback_safe=True,
                    thread_id=initial_thread_id,
                ) from exc

            if self.command is not None and resolved_thread_id != initial_thread_id:
                thread_lock = _PromptProcessLock(resolved_thread_id, cwd)
                if not thread_lock.acquire():
                    raise CodexPromptLockBusy()
                locks.append(thread_lock)
            active.thread_id = resolved_thread_id
            prior_turn_ids = {
                str(turn.get("id")) for turn in prior_turns if str(turn.get("id") or "")
            }
            self._remember_marker(resolved_thread_id, marker_provider)
            if on_thread is not None:
                on_thread(resolved_thread_id)

            with active.condition:
                cancelled_before_start = active.interrupt_requested or (
                    cancel_event is not None and cancel_event.is_set()
                )
            if cancelled_before_start:
                return self._interrupted_before_start(active)

            input_items: list[dict[str, Any]] = [{
                "type": "text",
                "text": prompt,
                "text_elements": [],
            }]
            input_items.extend({
                "type": "localImage",
                "path": str(Path(path).expanduser().resolve()),
            } for path in image_paths)
            client_user_message_id = str(uuid.uuid4())
            steer_turn = next(
                (turn for turn in prior_turns if self._turn_is_in_progress(turn)),
                None,
            ) if self.command is None else None
            params = {
                "threadId": resolved_thread_id,
                "input": input_items,
                "clientUserMessageId": client_user_message_id,
                "cwd": str(cwd),
                "model": model,
                "effort": effort,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            }
            with active.condition:
                active.phase = "starting"
            self._record_observer_event(active, "正在启动本轮处理", key="phase:starting")
            try:
                if steer_turn is not None:
                    expected_turn_id = str(steer_turn.get("id") or "").strip()
                    if not expected_turn_id:
                        raise CodexPromptLockBusy()
                    # Use the protocol's active-turn precondition so input from
                    # Android joins the exact turn observed by thread/resume;
                    # it can never be steered into a different racing turn.
                    active.turn_id = expected_turn_id
                    steer_result = self._rpc_request("turn/steer", {
                        "threadId": resolved_thread_id,
                        "input": input_items,
                        "expectedTurnId": expected_turn_id,
                        "clientUserMessageId": client_user_message_id,
                    }, cancel_requested=cancel_requested)
                    returned_turn_id = str(
                        steer_result.get("turnId") if isinstance(steer_result, dict) else ""
                    ).strip()
                    if returned_turn_id != expected_turn_id:
                        return self._uncertain_result(
                            active,
                            "turn/steer returned an unexpected turn id",
                        )
                    start_result = {
                        "turn": {
                            "id": returned_turn_id,
                            "status": str(steer_turn.get("status") or "inProgress"),
                            "items": [],
                        }
                    }
                else:
                    # If another official client starts a normal turn after the
                    # snapshot, turn/start applies Codex's canonical same-turn
                    # steering behavior. Non-steerable turns are retried below.
                    start_result = self._rpc_request(
                        "turn/start",
                        params,
                        cancel_requested=cancel_requested,
                    )
            except _RPCError as exc:
                if self.command is None and self._rpc_error_is_active_turn(exc):
                    # Review/compact turns reject steering. Keep the Android
                    # message queued until the daemon accepts it safely.
                    raise CodexPromptLockBusy() from exc
                raise CodexAppBridgeError(
                    str(exc),
                    fallback_safe=True,
                    thread_id=resolved_thread_id,
                ) from exc
            except CodexAppBridgeError as exc:
                if isinstance(exc, _RecoveryCancelled) and active.turn_id:
                    # turn/steer has an exact pre-existing turn id even when
                    # its response never arrives. A plain cancel_event must
                    # stop that known turn before the UI returns uncertain.
                    # turn/start has no id here and is deliberately not
                    # interrupted by guesswork.
                    self.interrupt_active(timeout=min(0.5, self.request_timeout_sec))
                try:
                    reconciled = self._reconnect_and_reconcile(
                        active,
                        prior_turn_ids=prior_turn_ids,
                        cwd=cwd,
                        model=model,
                        effort=effort,
                        required_client_user_message_id=client_user_message_id,
                        recovery_deadline=recovery_deadline,
                        cancel_requested=cancel_requested,
                    )
                except _RecoveryCancelled:
                    return self._uncertain_result(
                        active,
                        "turn/start acceptance is uncertain after cancellation",
                    )
                if not reconciled:
                    return self._uncertain_result(active, "turn/start acceptance is uncertain")
            else:
                turn = start_result.get("turn") if isinstance(start_result, dict) else None
                if not isinstance(turn, dict) or not str(turn.get("id") or "").strip():
                    return self._uncertain_result(active, "turn/start returned no turn id")
                self._apply_turn_snapshot(active, turn)

            if not active.turn_id:
                return self._uncertain_result(active, "unable to identify accepted turn")
            if on_turn_accepted is not None:
                try:
                    on_turn_accepted(resolved_thread_id)
                except Exception:
                    self.log.exception("Codex turn-accepted callback failed")
            with active.condition:
                cancelled_while_starting = active.interrupt_requested or (
                    cancel_event is not None and cancel_event.is_set()
                )
            if cancelled_while_starting:
                # JSON-RPC requests cannot be recalled after they are written. Interrupt at
                # the first point the server exposes the new turn id, before consuming its
                # buffered output or entering the normal polling loop.
                self.interrupt_active(timeout=3.0)
            with active.condition:
                pending_notifications = list(active.pending_notifications)
                active.pending_notifications.clear()
                if active.status not in {"completed", "interrupted", "failed"}:
                    active.phase = "running"
            self._record_observer_event(active, "开始处理", key="phase:running")
            for method, notification_params in pending_notifications:
                self._handle_notification(method, notification_params)
            result = self._wait_for_turn(
                active,
                cwd=cwd,
                model=model,
                effort=effort,
                cancel_event=cancel_event,
                max_runtime_sec=max_runtime_sec,
            )
            self._remember_marker(resolved_thread_id, marker_provider)
            return result
        finally:
            with self._state_lock:
                if self._active is active:
                    self._active = None
            for process_lock in reversed(locks):
                process_lock.release()
            self._turn_gate.release()

    def _prepare_thread_resilient(
        self,
        thread_id: str | None,
        *,
        cwd: Path,
        model: str,
        effort: str,
        active: _ActiveTurn | None = None,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Reconnect only before any user input can have been submitted.

        ``thread/resume`` is read/attach-like and retrying ``thread/start`` can
        at worst leave an empty thread. Neither path executes the user prompt.
        Once ``turn/start`` or ``turn/steer`` is written, run_turn switches to
        exact clientUserMessageId reconciliation and never blindly resubmits.
        """
        deadline = (
            recovery_deadline
            if recovery_deadline is not None
            else time.monotonic() + self.daemon_recovery_timeout_sec
        )
        attempts = 1 if self.command is not None else self.pre_submit_reconnect_attempts + 1
        last_error: _TransportError | None = None
        for attempt in range(attempts):
            self._raise_if_recovery_cancelled(cancel_requested)
            if time.monotonic() >= deadline:
                raise _TransportError(
                    "unable to recover shared Codex app-server daemon before timeout",
                    fallback_safe=True,
                ) from last_error
            try:
                self._ensure_connected(
                    cwd,
                    recovery_deadline=deadline,
                    cancel_requested=cancel_requested,
                )
                prepared = self._prepare_thread(
                    thread_id,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                    timeout=max(0.01, deadline - time.monotonic()),
                    cancel_requested=cancel_requested,
                )
                self._mark_active_connection_ready(active)
                return prepared
            except _TransportError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
                self.log.warning(
                    "Codex app-server disconnected before prompt submission; "
                    "reconnecting (%d/%d)",
                    attempt + 1,
                    attempts - 1,
                )
                with self._connect_lock:
                    self._close_process_locked()
        assert last_error is not None
        raise last_error

    def _wait_for_turn(
        self,
        active: _ActiveTurn,
        *,
        cwd: Path,
        model: str,
        effort: str,
        cancel_event: threading.Event | None,
        max_runtime_sec: float,
    ) -> CodexTurnResult:
        runtime_limit_sec = float(max_runtime_sec)
        deadline = (
            time.monotonic() + runtime_limit_sec
            if runtime_limit_sec > 0
            else None
        )
        interrupt_deadline: float | None = None
        reconnected = False
        while True:
            should_interrupt = False
            force_stop = False
            with active.condition:
                if active.status in {"completed", "interrupted", "failed"}:
                    return self._result_from_active(active)
                cancel_requested = active.interrupt_requested or (
                    cancel_event is not None and cancel_event.is_set()
                )
                connection_lost = active.connection_lost
                now = time.monotonic()
                remaining = deadline - now if deadline is not None else None
                if cancel_requested or (remaining is not None and remaining <= 0):
                    if interrupt_deadline is None:
                        interrupt_deadline = now + self.interrupt_grace_sec
                    if now >= interrupt_deadline:
                        force_stop = True
                    elif not active.interrupt_sent:
                        should_interrupt = True
                if not should_interrupt and not force_stop and not connection_lost:
                    wait_for = 0.2
                    if interrupt_deadline is not None:
                        wait_for = min(wait_for, max(0.01, interrupt_deadline - now))
                    elif remaining is not None and remaining > 0:
                        wait_for = min(wait_for, remaining)
                    active.condition.wait(timeout=wait_for)
                    continue

            if force_stop:
                with self._connect_lock:
                    self._close_process_locked()
                return self._uncertain_result(active, "turn did not stop after interrupt request")

            if should_interrupt:
                self.interrupt_active(timeout=3.0)
                continue

            if connection_lost:
                if reconnected:
                    return self._uncertain_result(active, "app-server disconnected twice")
                reconnected = True
                prior = {active.turn_id} if active.turn_id else set()
                recovery_deadline = time.monotonic() + self.daemon_recovery_timeout_sec
                recovery_cancel_requested = lambda: (
                    self._closing.is_set()
                    or active.interrupt_requested
                    or (cancel_event is not None and cancel_event.is_set())
                )
                try:
                    reconciled = self._reconnect_and_reconcile(
                        active,
                        prior_turn_ids=prior,
                        cwd=cwd,
                        model=model,
                        effort=effort,
                        require_known_turn=True,
                        recovery_deadline=recovery_deadline,
                        cancel_requested=recovery_cancel_requested,
                    )
                except _RecoveryCancelled:
                    return self._uncertain_result(
                        active,
                        "active turn recovery cancelled",
                    )
                if not reconciled:
                    return self._uncertain_result(active, "active turn could not be reconciled")

    def _reconnect_and_reconcile(
        self,
        active: _ActiveTurn,
        *,
        prior_turn_ids: set[str],
        cwd: Path,
        model: str,
        effort: str,
        require_known_turn: bool = False,
        required_client_user_message_id: str | None = None,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> bool:
        thread_id = active.thread_id
        if not thread_id:
            return False
        try:
            with self._connect_lock:
                self._close_process_locked()
            resumed_id, turns = self._prepare_thread_resilient(
                thread_id,
                cwd=cwd,
                model=model,
                effort=effort,
                active=active,
                recovery_deadline=recovery_deadline,
                cancel_requested=cancel_requested,
            )
        except _RecoveryCancelled:
            raise
        except CodexAppBridgeError:
            return False
        if resumed_id != thread_id:
            return False

        target: dict[str, Any] | None = None
        if required_client_user_message_id:
            candidates = [
                turn for turn in turns
                if self._turn_contains_client_message(
                    turn,
                    required_client_user_message_id,
                )
            ]
            if active.turn_id:
                candidates = [
                    turn for turn in candidates
                    if str(turn.get("id") or "") == active.turn_id
                ]
            if len(candidates) == 1:
                target = candidates[0]
        elif active.turn_id:
            target = next((turn for turn in turns if turn.get("id") == active.turn_id), None)
        elif not require_known_turn:
            candidates = [turn for turn in turns if str(turn.get("id") or "") not in prior_turn_ids]
            if len(candidates) == 1:
                target = candidates[0]
        if target is None:
            return False
        with active.condition:
            active.connection_lost = False
        self._apply_turn_snapshot(active, target)
        return True

    def _mark_active_connection_ready(self, active: _ActiveTurn | None) -> None:
        if active is None:
            return
        with self._state_lock:
            if self._active is not active:
                return
            generation = self._generation
        with active.condition:
            active.connection_generation = generation
            active.connection_lost = False
            active.condition.notify_all()

    @staticmethod
    def _turn_contains_client_message(
        turn: dict[str, Any],
        client_user_message_id: str,
    ) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("type") == "userMessage"
            and str(item.get("clientId") or "") == client_user_message_id
            for item in (turn.get("items") or [])
        )

    def _prepare_thread(
        self,
        thread_id: str | None,
        *,
        cwd: Path,
        model: str,
        effort: str,
        timeout: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        thread_config: dict[str, Any] = {"model_reasoning_effort": effort}
        if self.model_auto_compact_token_limit is not None:
            thread_config["model_auto_compact_token_limit"] = self.model_auto_compact_token_limit
        common = {
            "cwd": str(cwd),
            "model": model,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "config": thread_config,
        }
        if thread_id:
            params = {"threadId": thread_id, **common}
            result = self._rpc_request(
                "thread/resume",
                params,
                timeout=timeout,
                cancel_requested=cancel_requested,
            )
        else:
            params = {**common, "ephemeral": False}
            result = self._rpc_request(
                "thread/start",
                params,
                timeout=timeout,
                cancel_requested=cancel_requested,
            )
        thread = result.get("thread") if isinstance(result, dict) else None
        resolved = str(thread.get("id") or "").strip() if isinstance(thread, dict) else ""
        if not resolved:
            raise CodexAppBridgeError("thread start/resume returned no thread id")
        turns = thread.get("turns") if isinstance(thread, dict) else []
        turn_list = [turn for turn in (turns or []) if isinstance(turn, dict)]
        return resolved, turn_list

    @staticmethod
    def _turn_is_in_progress(turn: dict[str, Any]) -> bool:
        status = str(turn.get("status") or "").strip().lower().replace("_", "")
        return status in {"inprogress", "running", "starting", "pending"}

    @staticmethod
    def _rpc_error_is_active_turn(error: _RPCError) -> bool:
        try:
            payload = json.dumps(error.error, ensure_ascii=False, sort_keys=True).lower()
        except Exception:
            payload = str(error.error).lower()
        return any(marker in payload for marker in (
            "active turn",
            "active_turn",
            "turn in progress",
            "turn_in_progress",
            "already running",
            "thread is busy",
            "thread_busy",
            "activeturnnotsteerable",
            "active_turn_not_steerable",
            "expectedturnid",
            "expected_turn_id",
            "expected turn",
        ))

    def _rollout_changed(
        self,
        thread_id: str | None,
        marker_provider: MarkerProvider | None,
    ) -> bool:
        if not thread_id or marker_provider is None:
            return False
        current = self._get_marker(thread_id, marker_provider)
        with self._state_lock:
            if thread_id not in self._marker_baselines:
                return False
            previous = self._last_markers.get(thread_id)
        return current is None or current != previous

    def _remember_marker(self, thread_id: str, marker_provider: MarkerProvider | None) -> None:
        if marker_provider is None:
            return
        marker = self._get_marker(thread_id, marker_provider)
        with self._state_lock:
            self._last_markers[thread_id] = marker
            self._marker_baselines.add(thread_id)

    @staticmethod
    def _get_marker(thread_id: str, marker_provider: MarkerProvider) -> tuple[str, int, int] | None:
        try:
            marker = marker_provider(thread_id)
        except Exception:
            return None
        if marker is None:
            return None
        return str(marker[0]), int(marker[1]), int(marker[2])

    def _ensure_connected(
        self,
        cwd: Path,
        *,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._raise_if_recovery_cancelled(cancel_requested)
        with self._connect_lock:
            self._raise_if_recovery_cancelled(cancel_requested)
            process = self._process
            if self._initialized and (
                self._websocket is not None
                or (process is not None and process.poll() is None)
            ):
                return
            self._close_process_locked()
            self._start_process_locked(
                cwd,
                recovery_deadline=recovery_deadline,
                cancel_requested=cancel_requested,
            )

    def _start_process_locked(
        self,
        cwd: Path,
        *,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        if self.command is None:
            self._start_daemon_connection_locked(
                cwd,
                recovery_deadline=recovery_deadline,
                cancel_requested=cancel_requested,
            )
            return

        self._raise_if_recovery_cancelled(cancel_requested)
        command = self.command
        env = os.environ.copy()
        env["CODEX_HOME"] = self.codex_home
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=True,
            )
        except Exception as exc:
            raise _TransportError("unable to start Codex app-server", fallback_safe=True) from exc
        self._generation += 1
        generation = self._generation
        self._process = process
        self._connection_error = None
        self._initialized = False
        reader = threading.Thread(
            target=self._reader_loop,
            args=(process, generation),
            name="codex-app-server-reader",
            daemon=True,
        )
        self._reader = reader
        reader.start()
        try:
            self._rpc_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "cc-companion-kairos",
                        "title": "CcCompanion Kairos",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                cancel_requested=cancel_requested,
            )
            self._rpc_notify("initialized", {})
        except Exception:
            self._close_process_locked()
            raise
        self._initialized = True
        self.log.info("Codex app-server bridge initialized pid=%s", process.pid)

    def _start_daemon_connection_locked(
        self,
        cwd: Path,
        *,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        env = os.environ.copy()
        env["CODEX_HOME"] = self.codex_home
        deadline = (
            recovery_deadline
            if recovery_deadline is not None
            else time.monotonic() + self.daemon_recovery_timeout_sec
        )

        def connect() -> Any:
            # Codex's Unix listener currently doesn't negotiate
            # permessage-deflate; websockets enables it by default.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("daemon recovery deadline expired")
            return unix_connect(
                path=str(self.daemon_socket_path),
                uri="ws://localhost/",
                compression=None,
                # The listener is local. Keep the handshake slice short so a
                # half-open socket cannot make cancel/close wait for seconds;
                # the outer recovery loop can safely try again.
                open_timeout=max(0.01, min(0.25, self.request_timeout_sec, remaining)),
                close_timeout=1.0,
            )

        first_error: BaseException | None = None
        last_error: BaseException | None = None
        websocket: Any = None
        while websocket is None:
            self._raise_if_recovery_cancelled(cancel_requested)
            if time.monotonic() >= deadline:
                break
            try:
                websocket = connect()
                break
            except Exception as exc:
                first_error = first_error or exc
                last_error = exc
            if not self.daemon_autostart:
                raise _TransportError(
                    "unable to connect to shared Codex app-server daemon",
                    fallback_safe=True,
                ) from first_error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # A concurrent `daemon restart` may hold Codex's startup lock
                # while its old PID is settling. The command can time out even
                # though the lock becomes usable moments later, so failure here
                # is not terminal; reconnect and retry until the bounded
                # recovery deadline.
                self._run_daemon_start_interruptible(
                    [self.codex_bin, "remote-control", "start", "--json"],
                    cwd=str(cwd),
                    env=env,
                    deadline=min(deadline, time.monotonic() + self.daemon_start_timeout_sec),
                    cancel_requested=cancel_requested,
                )
            except Exception as exc:
                if isinstance(exc, _RecoveryCancelled):
                    raise
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._interruptible_recovery_wait(
                    min(self.daemon_connect_retry_sec, remaining),
                    cancel_requested,
                )
        if websocket is None:
            raise _TransportError(
                "unable to recover shared Codex app-server daemon before timeout",
                fallback_safe=True,
            ) from (last_error or first_error)

        try:
            self._raise_if_recovery_cancelled(cancel_requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportError(
                    "unable to recover shared Codex app-server daemon before timeout",
                    fallback_safe=True,
                )
        except Exception:
            try:
                websocket.close()
            except Exception:
                pass
            raise
        self._generation += 1
        generation = self._generation
        self._websocket = websocket
        self._connection_error = None
        self._initialized = False
        reader = threading.Thread(
            target=self._websocket_reader_loop,
            args=(websocket, generation),
            name="codex-app-server-daemon-reader",
            daemon=True,
        )
        self._reader = reader
        reader.start()
        try:
            self._initialize_connection(
                timeout=max(0.01, min(self.request_timeout_sec, remaining)),
                cancel_requested=cancel_requested,
            )
        except Exception:
            self._close_process_locked()
            raise
        self._initialized = True
        self.log.info("Codex app-server bridge connected to shared daemon")

    def _run_daemon_start_interruptible(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str],
        deadline: float,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            while True:
                self._raise_if_recovery_cancelled(cancel_requested)
                return_code = process.poll()
                if return_code is not None:
                    if return_code != 0:
                        raise subprocess.CalledProcessError(return_code, list(command))
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(list(command), self.daemon_start_timeout_sec)
                self._interruptible_recovery_wait(min(0.05, remaining), cancel_requested)
        except BaseException:
            self._terminate_helper_process(process)
            raise
        finally:
            if process.poll() is not None:
                try:
                    process.wait(timeout=0)
                except Exception:
                    pass

    @staticmethod
    def _terminate_helper_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=0.2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                process.kill()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass

    def _interruptible_recovery_wait(
        self,
        delay: float,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            self._raise_if_recovery_cancelled(cancel_requested)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._closing.wait(timeout=min(0.05, remaining))

    def _raise_if_recovery_cancelled(
        self,
        cancel_requested: Callable[[], bool] | None,
    ) -> None:
        if self._closing.is_set() or (
            cancel_requested is not None and cancel_requested()
        ):
            raise _RecoveryCancelled("Codex daemon recovery cancelled", fallback_safe=True)

    def _initialize_connection(
        self,
        *,
        timeout: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._rpc_request("initialize", {
            "clientInfo": {
                "name": "cc-companion-kairos",
                "title": "CcCompanion Kairos",
                "version": "1",
            },
            "capabilities": {"experimentalApi": True},
        }, timeout=timeout, cancel_requested=cancel_requested)
        self._rpc_notify("initialized", {})

    def _close_process_locked(self) -> None:
        process = self._process
        websocket = self._websocket
        reader = self._reader
        self._process = None
        self._websocket = None
        self._reader = None
        self._initialized = False
        self._connection_error = None
        if websocket is not None:
            try:
                websocket.close()
            except Exception:
                pass
        if process is not None:
            process_group_id = process.pid
            if process.poll() is None:
                try:
                    os.killpg(process_group_id, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                if process.poll() is None:
                    process.kill()
            if process.poll() is None:
                process.wait(timeout=2.0)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.5)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
        self._fail_pending(_TransportError("app-server connection closed"))

    def _rpc_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Any:
        with self._pending_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        try:
            self._write_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except Exception as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise _TransportError(f"failed to send {method}") from exc
        wait_timeout = timeout if timeout is not None else self.request_timeout_sec
        deadline = time.monotonic() + max(0.0, wait_timeout)
        while not pending.event.is_set():
            try:
                self._raise_if_recovery_cancelled(cancel_requested)
            except _RecoveryCancelled:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise _TransportError(f"timed out waiting for {method}")
            pending.event.wait(min(0.05, remaining))
        if pending.transport_error is not None:
            raise _TransportError(f"connection lost while waiting for {method}") from pending.transport_error
        if pending.error is not None:
            raise _RPCError(method, pending.error)
        return pending.result

    def _rpc_notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write_json(message)

    def _write_json(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            websocket = self._websocket
            if websocket is not None:
                try:
                    websocket.send(encoded)
                except Exception as exc:
                    raise _TransportError("app-server daemon is not connected") from exc
                return
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise _TransportError("app-server is not connected")
            process.stdin.write(encoded + "\n")
            process.stdin.flush()

    def _reader_loop(self, process: subprocess.Popen[str], generation: int) -> None:
        stdout = process.stdout
        if stdout is None:
            self._reader_disconnected(process, generation)
            return
        try:
            for line in stdout:
                try:
                    message = json.loads(line)
                except Exception:
                    self.log.warning("Codex app-server emitted invalid JSON")
                    continue
                if isinstance(message, dict):
                    self._dispatch_message(message)
        finally:
            self._reader_disconnected(process, generation)

    def _websocket_reader_loop(self, websocket: Any, generation: int) -> None:
        try:
            while True:
                raw = websocket.recv()
                if raw is None:
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not isinstance(raw, str):
                    self.log.warning("Codex app-server emitted a non-text WebSocket frame")
                    continue
                try:
                    message = json.loads(raw)
                except Exception:
                    self.log.warning("Codex app-server emitted invalid JSON")
                    continue
                if isinstance(message, dict):
                    self._dispatch_message(message)
        except Exception:
            pass
        finally:
            self._reader_disconnected(websocket, generation)

    def _reader_disconnected(self, connection: Any, generation: int) -> None:
        with self._state_lock:
            if (
                self._generation != generation
                or (self._process is not connection and self._websocket is not connection)
            ):
                return
            error = _TransportError("Codex app-server connection disconnected")
            self._connection_error = error
            self._initialized = False
            active = self._active
        self._fail_pending(error)
        if active is not None:
            with active.condition:
                # A delayed reader from an older generation must not poison a
                # thread that has already prepared successfully on a new
                # connection.
                if (
                    active.connection_generation is None
                    or active.connection_generation == generation
                ):
                    active.connection_lost = True
                    active.condition.notify_all()
        self.log.warning("Codex app-server bridge disconnected")

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.transport_error = error
            pending.event.set()

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            request_id = message.get("id")
            with self._pending_lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            pending.error = message.get("error")
            pending.result = message.get("result")
            pending.event.set()
            return
        if "id" in message and "method" in message:
            self._respond_to_server_request(message)
            return
        method = str(message.get("method") or "")
        params = message.get("params")
        if method and isinstance(params, dict):
            self._handle_notification(method, params)

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        if method == "currentTime/read":
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"currentTimeAt": int(time.time())},
            }
        elif method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"decision": "decline"}}
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"decision": "denied"}}
        elif method == "item/tool/requestUserInput":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"answers": {}}}
        elif method == "mcpServer/elicitation/request":
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"action": "cancel", "content": None},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "client method not supported"},
            }
        try:
            self._write_json(response)
        except Exception:
            pass

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        with self._state_lock:
            active = self._active
        if active is None:
            return
        with active.condition:
            if active.phase == "preparing":
                return
            if active.phase == "starting":
                if len(active.pending_notifications) < 2000:
                    active.pending_notifications.append((method, dict(params)))
                return
        thread_id = str(params.get("threadId") or "")
        if active.thread_id and thread_id and active.thread_id != thread_id:
            return
        turn_id = str(params.get("turnId") or "")
        turn_payload = params.get("turn")
        if not turn_id and isinstance(turn_payload, dict):
            turn_id = str(turn_payload.get("id") or "")
        with active.condition:
            if active.turn_id and turn_id and active.turn_id != turn_id:
                return
            if (
                not active.turn_id
                and turn_id
                and method not in {"thread/tokenUsage/updated", "thread/compacted"}
            ):
                active.turn_id = turn_id

        if method in {"thread/tokenUsage/updated", "thread/compacted"}:
            # These thread-level notifications can be replayed by thread/resume.
            # Require both IDs to match the accepted active turn exactly.
            if not active.thread_id or thread_id != active.thread_id:
                return
            with active.condition:
                if not active.turn_id or turn_id != active.turn_id:
                    return
                if method == "thread/compacted":
                    active.context_compacted = True
                    return
            token_usage = CodexThreadTokenUsage.from_payload(params.get("tokenUsage"))
            if token_usage is not None:
                with active.condition:
                    active.token_usage = token_usage
            return

        if method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            if item_id and delta:
                with active.condition:
                    active.agent_deltas[item_id] = active.agent_deltas.get(item_id, "") + delta
                    draft = "\n\n".join(active.agent_deltas.values())
                self._safe_callback(active.on_update, draft)
            return
        if method == "item/started":
            item = params.get("item")
            if isinstance(item, dict):
                self._handle_item(active, item, completed=False)
            return
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                self._handle_item(active, item, completed=True)
            return
        if method == "turn/started" and isinstance(turn_payload, dict):
            self._apply_turn_snapshot(active, turn_payload)
            return
        if method == "turn/completed" and isinstance(turn_payload, dict):
            self._apply_turn_snapshot(active, turn_payload)
            return
        if method == "error" and not bool(params.get("willRetry")):
            error = params.get("error")
            if isinstance(error, dict):
                with active.condition:
                    active.error = str(error.get("message") or "Codex turn failed")[:500]

    def _apply_turn_snapshot(self, active: _ActiveTurn, turn: dict[str, Any]) -> None:
        turn_id = str(turn.get("id") or "")
        with active.condition:
            if active.turn_id and turn_id and active.turn_id != turn_id:
                return
            if turn_id:
                active.turn_id = turn_id
        for item in turn.get("items") or []:
            if isinstance(item, dict):
                self._handle_item(active, item, completed=True)
        status = str(turn.get("status") or "")
        if status in {"completed", "interrupted", "failed"}:
            error = turn.get("error")
            with active.condition:
                active.status = status
                active.phase = status
                if isinstance(error, dict) and error.get("message"):
                    active.error = str(error.get("message"))[:500]
                active.condition.notify_all()

    def _handle_item(self, active: _ActiveTurn, item: dict[str, Any], *, completed: bool) -> None:
        item_id = str(item.get("id") or "")
        item_type = str(item.get("type") or "")
        if completed and item_id:
            with active.condition:
                if item_id in active.completed_items:
                    return
                active.completed_items.add(item_id)
        if completed and item_type == "contextCompaction":
            with active.condition:
                active.context_compacted = True
        if not completed:
            observer_label = self._observer_activity_label(item_type)
            if observer_label:
                self._record_observer_event(
                    active,
                    observer_label,
                    key=f"item:{item_id}" if item_id else None,
                )
        if item_type == "agentMessage":
            phase = item.get("phase")
            if completed and phase in {None, "final_answer"}:
                text = str(item.get("text") or "")
                if text:
                    with active.condition:
                        active.final_messages[item_id or f"final-{len(active.final_messages)}"] = text
                        active.agent_deltas[item_id or f"final-{len(active.agent_deltas)}"] = text
                        draft = "\n\n".join(active.final_messages.values())
                    self._safe_callback(active.on_update, draft)
            return
        if item_type == "reasoning":
            if completed:
                summary = "\n".join(str(part) for part in (item.get("summary") or []) if str(part).strip())
                if summary:
                    self._safe_callback(active.on_activity, f"思考摘要：{summary}")
            return
        if completed:
            return
        label = self._activity_label(item)
        if not label:
            return
        with active.condition:
            activity_key = item_id or f"{item_type}:{label}"
            if activity_key in active.activity_items:
                return
            active.activity_items.add(activity_key)
        self._safe_callback(active.on_activity, label)

    @staticmethod
    def _activity_label(item: dict[str, Any]) -> str:
        item_type = str(item.get("type") or "")
        if item_type == "commandExecution":
            return "运行命令"
        if item_type == "fileChange":
            return "修改文件"
        if item_type == "mcpToolCall":
            name = "/".join(part for part in (str(item.get("server") or ""), str(item.get("tool") or "")) if part)
            return f"调用 {name}" if name else "调用 MCP 工具"
        if item_type == "dynamicToolCall":
            name = "/".join(part for part in (str(item.get("namespace") or ""), str(item.get("tool") or "")) if part)
            return f"调用 {name}" if name else "调用工具"
        return {
            "collabAgentToolCall": "调用协作代理",
            "subAgentActivity": "协作代理处理中",
            "webSearch": "搜索网页",
            "imageView": "查看图片",
            "imageGeneration": "生成图片",
            "contextCompaction": "整理上下文",
            "sleep": "等待",
        }.get(item_type, "")

    @staticmethod
    def _observer_activity_label(item_type: str) -> str:
        """Map only trusted item types to terminal-safe status text."""
        return OBSERVER_ITEM_LABELS.get(str(item_type or ""), "")

    @staticmethod
    def _observer_phase(phase: str | None) -> str:
        return OBSERVER_PHASE_LABELS.get(str(phase or ""), "正在处理")

    @staticmethod
    def _record_observer_event(
        active: _ActiveTurn,
        label: str,
        *,
        key: str | None = None,
    ) -> None:
        """Append bounded, pre-redacted observer text without raw payloads."""
        with active.condition:
            if key and key in active.observer_event_keys:
                return
            if key:
                if len(active.observer_event_keys) >= 160:
                    active.observer_event_keys.clear()
                active.observer_event_keys.add(key)
            elapsed = max(0, int(time.time() - active.started_at))
            active.observer_events.append((elapsed, str(label)))
            if len(active.observer_events) > 40:
                del active.observer_events[:-40]

    @staticmethod
    def _safe_callback(callback: Callable[[str], None] | None, value: str) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            return

    @staticmethod
    def _result_from_active(active: _ActiveTurn) -> CodexTurnResult:
        with active.condition:
            text = "\n\n".join(active.final_messages.values()).strip()
            return CodexTurnResult(
                thread_id=str(active.thread_id or ""),
                turn_id=active.turn_id,
                text=text,
                status=str(active.status or "failed"),
                error=active.error,
                token_usage=active.token_usage,
                context_compacted=active.context_compacted,
            )

    @staticmethod
    def _interrupted_before_start(active: _ActiveTurn) -> CodexTurnResult:
        with active.condition:
            active.status = "interrupted"
            active.phase = "interrupted"
        return CodexTurnResult(
            thread_id=str(active.thread_id or ""),
            turn_id=None,
            text="",
            status="interrupted",
        )

    @staticmethod
    def _uncertain_result(active: _ActiveTurn, detail: str) -> CodexTurnResult:
        with active.condition:
            text = "\n\n".join(active.final_messages.values()).strip()
            active.status = "uncertain"
            active.phase = "uncertain"
            return CodexTurnResult(
                thread_id=str(active.thread_id or ""),
                turn_id=active.turn_id,
                text=text,
                status="uncertain",
                error=detail,
                token_usage=active.token_usage,
                context_compacted=active.context_compacted,
            )
