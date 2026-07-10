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

try:
    import fcntl
except ImportError:  # pragma: no cover - production is Linux.
    fcntl = None  # type: ignore[assignment]


UpdateCallback = Callable[[str], None]
ActivityCallback = Callable[[str], None]
ThreadCallback = Callable[[str], None]
MarkerProvider = Callable[[str], tuple[str, int, int] | None]


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


@dataclass(frozen=True)
class CodexTurnResult:
    thread_id: str
    turn_id: str | None
    text: str
    status: str
    error: str | None = None


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
    connection_lost: bool = False
    interrupt_requested: bool = False
    interrupt_sent: bool = False
    agent_deltas: OrderedDict[str, str] = field(default_factory=OrderedDict)
    final_messages: OrderedDict[str, str] = field(default_factory=OrderedDict)
    completed_items: set[str] = field(default_factory=set)
    activity_items: set[str] = field(default_factory=set)
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
    """Own one long-lived ``codex app-server --listen stdio://`` child."""

    def __init__(
        self,
        *,
        codex_bin: str = "/usr/bin/codex",
        codex_home: str = "/root/.codex",
        command: Sequence[str] | None = None,
        logger: logging.Logger | None = None,
        request_timeout_sec: float = 30.0,
        interrupt_grace_sec: float = 10.0,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = str(Path(codex_home).expanduser())
        self.command = list(command) if command else None
        self.log = logger or logging.getLogger(__name__)
        self.request_timeout_sec = request_timeout_sec
        self.interrupt_grace_sec = max(0.1, float(interrupt_grace_sec))

        self._connect_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._turn_gate = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_request_id = 1
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._generation = 0
        self._initialized = False
        self._connection_error: BaseException | None = None
        self._active: _ActiveTurn | None = None
        self._last_markers: dict[str, tuple[str, int, int] | None] = {}
        self._marker_baselines: set[str] = set()

    def close(self) -> None:
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
        marker_provider: MarkerProvider | None = None,
        max_runtime_sec: float = 900.0,
    ) -> CodexTurnResult:
        if not self._turn_gate.acquire(blocking=False):
            raise CodexActiveTurnError()

        cwd = Path(cwd).expanduser().resolve()
        initial_thread_id = str(thread_id or "").strip() or None
        locks: list[_PromptProcessLock] = []
        active: _ActiveTurn | None = None
        resolved_thread_id = initial_thread_id
        try:
            initial_lock = _PromptProcessLock(initial_thread_id, cwd)
            if not initial_lock.acquire():
                raise CodexPromptLockBusy()
            locks.append(initial_lock)

            if self._rollout_changed(initial_thread_id, marker_provider):
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

            if cancel_event is not None and cancel_event.is_set():
                return self._interrupted_before_start(active)

            try:
                self._ensure_connected(cwd)
                resolved_thread_id, prior_turns = self._prepare_thread(
                    initial_thread_id,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                )
            except CodexAppBridgeError as exc:
                raise CodexAppBridgeError(
                    str(exc),
                    fallback_safe=True,
                    thread_id=initial_thread_id,
                ) from exc

            if resolved_thread_id != initial_thread_id:
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
            params = {
                "threadId": resolved_thread_id,
                "input": input_items,
                "cwd": str(cwd),
                "model": model,
                "effort": effort,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            }
            with active.condition:
                active.phase = "starting"
            try:
                start_result = self._rpc_request("turn/start", params)
            except _RPCError as exc:
                raise CodexAppBridgeError(
                    str(exc),
                    fallback_safe=True,
                    thread_id=resolved_thread_id,
                ) from exc
            except CodexAppBridgeError:
                if not self._reconnect_and_reconcile(
                    active,
                    prior_turn_ids=prior_turn_ids,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                ):
                    return self._uncertain_result(active, "turn/start acceptance is uncertain")
            else:
                turn = start_result.get("turn") if isinstance(start_result, dict) else None
                if not isinstance(turn, dict) or not str(turn.get("id") or "").strip():
                    return self._uncertain_result(active, "turn/start returned no turn id")
                self._apply_turn_snapshot(active, turn)

            if not active.turn_id:
                return self._uncertain_result(active, "unable to identify accepted turn")
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
        deadline = time.monotonic() + max_runtime_sec
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
                remaining = deadline - now
                if cancel_requested or remaining <= 0:
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
                    elif remaining > 0:
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
                if not self._reconnect_and_reconcile(
                    active,
                    prior_turn_ids=prior,
                    cwd=cwd,
                    model=model,
                    effort=effort,
                    require_known_turn=True,
                ):
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
    ) -> bool:
        thread_id = active.thread_id
        if not thread_id:
            return False
        try:
            with self._connect_lock:
                self._close_process_locked()
            self._ensure_connected(cwd)
            resumed_id, turns = self._prepare_thread(
                thread_id,
                cwd=cwd,
                model=model,
                effort=effort,
            )
        except CodexAppBridgeError:
            return False
        if resumed_id != thread_id:
            return False

        target: dict[str, Any] | None = None
        if active.turn_id:
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

    def _prepare_thread(
        self,
        thread_id: str | None,
        *,
        cwd: Path,
        model: str,
        effort: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        common = {
            "cwd": str(cwd),
            "model": model,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "config": {"model_reasoning_effort": effort},
        }
        if thread_id:
            params = {"threadId": thread_id, **common}
            result = self._rpc_request("thread/resume", params)
        else:
            params = {**common, "ephemeral": False}
            result = self._rpc_request("thread/start", params)
        thread = result.get("thread") if isinstance(result, dict) else None
        resolved = str(thread.get("id") or "").strip() if isinstance(thread, dict) else ""
        if not resolved:
            raise CodexAppBridgeError("thread start/resume returned no thread id")
        turns = thread.get("turns") if isinstance(thread, dict) else []
        turn_list = [turn for turn in (turns or []) if isinstance(turn, dict)]
        return resolved, turn_list

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

    def _ensure_connected(self, cwd: Path) -> None:
        with self._connect_lock:
            process = self._process
            if self._initialized and process is not None and process.poll() is None:
                return
            self._close_process_locked()
            self._start_process_locked(cwd)

    def _start_process_locked(self, cwd: Path) -> None:
        command = self.command or [self.codex_bin, "app-server", "--listen", "stdio://"]
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
            self._rpc_request("initialize", {
                "clientInfo": {
                    "name": "cc-companion-kairos",
                    "title": "CcCompanion Kairos",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            })
            self._rpc_notify("initialized", {})
        except Exception:
            self._close_process_locked()
            raise
        self._initialized = True
        self.log.info("Codex app-server bridge initialized pid=%s", process.pid)

    def _close_process_locked(self) -> None:
        process = self._process
        reader = self._reader
        self._process = None
        self._reader = None
        self._initialized = False
        self._connection_error = None
        if process is None:
            return
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
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self._fail_pending(_TransportError("app-server connection closed"))

    def _rpc_request(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
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
        if not pending.event.wait(timeout if timeout is not None else self.request_timeout_sec):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise _TransportError(f"timed out waiting for {method}")
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

    def _reader_disconnected(self, process: subprocess.Popen[str], generation: int) -> None:
        with self._state_lock:
            if self._process is not process or self._generation != generation:
                return
            error = _TransportError("Codex app-server stdio disconnected")
            self._connection_error = error
            self._initialized = False
            active = self._active
        self._fail_pending(error)
        if active is not None:
            with active.condition:
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
            if not active.turn_id and turn_id:
                active.turn_id = turn_id

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
            )
