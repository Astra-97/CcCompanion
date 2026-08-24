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
import stat
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
# Most activity is still represented by a short, display-safe label.  A
# collaboration item can additionally carry a sanitised worker identity and
# lifecycle state so the client can show a compact worker card without ever
# receiving the delegated prompt or tool payload.
ActivityValue = str | dict[str, Any]
ActivityCallback = Callable[[ActivityValue], None]
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
QIAOKAIROS_REMOTE_COMPAT_LOCK_OWNER = "qiaokairos-interactive"
QIAOKAIROS_REMOTE_SCRIPT = Path("/root/Windows-Codex-TG/scripts/qiaokairos.py")
CODEX_RELEASE_TRUST_ANCHOR = Path("/root/.codex")
_QIAOKAIROS_LOCK_METADATA_KEYS = frozenset({
    "pid", "pid_starttime", "uid", "owner", "started_at", "session_id",
    "cwd", "codex_bin", "supervisor_cwd", "supervisor_exe", "supervisor_argv",
    "process_identity",
})


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
class _DaemonProcessIdentity:
    pid: int
    pidfile_device: int
    pidfile_inode: int
    starttime: int
    argv: tuple[bytes, ...]
    executable_device: int
    executable_inode: int
    executable_uid: int
    process_uid: int
    cgroups: tuple[str, ...]


@dataclass(frozen=True)
class _ProcSnapshot:
    pid: int
    starttime: int
    uid: int
    argv: tuple[str, ...]
    executable: str
    cwd: str


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
    worker_items: dict[str, tuple[str, str]] = field(default_factory=dict)
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
        try:
            lock_file = _open_trusted_prompt_lock(self.path, migrate_private=True)
        except OSError as exc:
            raise CodexAppBridgeError(
                "Codex prompt lock path is not trusted",
                fallback_safe=True,
            ) from exc
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


def _open_trusted_prompt_lock(path: Path, *, migrate_private: bool = False) -> Any:
    """Open one prompt lock without following an attacker-controlled file.

    The lock directory is normally root-owned below ``/tmp``.  We accept an
    override for tests and deployments, but only when its final directory and
    the lock inode are owned by this service uid and cannot be written by a
    group or another user.  The returned fd pins the exact inode whose flock
    and metadata are inspected, closing the rename/replacement race.
    """
    root = path.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_stat = root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise OSError("untrusted Codex prompt lock directory")

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        # This compatibility lock is a Linux service feature.  On a platform
        # without O_NOFOLLOW it is safer to fail closed than read an arbitrary
        # target via a symlink.
        raise OSError("O_NOFOLLOW is required for Codex prompt locks")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    root_fd = os.open(root, directory_flags | getattr(os, "O_CLOEXEC", 0))
    try:
        opened_root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(opened_root_stat.st_mode)
            or opened_root_stat.st_uid != os.geteuid()
            or opened_root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise OSError("untrusted Codex prompt lock directory")
        if stat.S_IMODE(opened_root_stat.st_mode) != 0o700:
            if not migrate_private:
                raise OSError("Codex prompt lock directory is not private")
            os.fchmod(root_fd, 0o700)
            if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
                raise OSError("unable to secure Codex prompt lock directory")
        # Resolve the lock name relative to the opened directory rather than
        # reopening ``path``.  A rename after the checks above cannot switch
        # this fd to another directory or another lock inode.
        fd = os.open(path.name, flags | nofollow, 0o600, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        file_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise OSError("untrusted Codex prompt lock file")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            if not migrate_private:
                raise OSError("Codex prompt lock file is not private")
            os.fchmod(fd, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise OSError("unable to secure Codex prompt lock file")
        return os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _read_qiaokairos_lock_metadata(lock_file: Any) -> dict[str, Any] | None:
    """Read a bounded, non-secret qiaokairos compatibility lock record."""
    try:
        lock_file.seek(0)
        raw = lock_file.read(1025)
        # This is intentionally small so a malicious legacy lock cannot make
        # the service ingest arbitrary lock contents.  It contains identities,
        # never credentials or prompt material.
        if len(raw) > 1024:
            return None
        metadata = json.loads(raw)
        if not isinstance(metadata, dict) or set(metadata) != _QIAOKAIROS_LOCK_METADATA_KEYS:
            return None
        if (
            type(metadata["pid"]) is not int
            or type(metadata["pid_starttime"]) is not int
            or type(metadata["uid"]) is not int
            or type(metadata["started_at"]) is not int
            or not isinstance(metadata["owner"], str)
            or not all(isinstance(metadata[key], str) for key in (
                "session_id", "cwd", "codex_bin", "supervisor_cwd", "supervisor_exe",
                "process_identity",
            ))
            or not isinstance(metadata["supervisor_argv"], list)
            or not metadata["supervisor_argv"]
            or len(metadata["supervisor_argv"]) > 16
            or any(
                not isinstance(argument, str) or len(argument) > 512
                for argument in metadata["supervisor_argv"]
            )
            or sum(len(argument) for argument in metadata["supervisor_argv"]) > 1024
        ):
            return None
        return metadata
    except Exception:
        return None


def _canonical_trusted_regular_path(value: str | Path) -> str | None:
    """Resolve one configured executable/script only if its inode is safe."""
    try:
        path = Path(value).expanduser().resolve(strict=True)
        path_stat = path.stat()
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != os.geteuid()
        or path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        return None
    return str(path)


def _trusted_codex_release_executable(value: str | Path) -> tuple[str, int, int] | None:
    """Resolve a configured Codex shim through its protected release chain."""
    try:
        anchor = CODEX_RELEASE_TRUST_ANCHOR.resolve(strict=True)
        target = Path(value).expanduser().resolve(strict=True)
        anchor_info = anchor.stat()
        target_info = target.stat()
        relative = target.relative_to(anchor)
    except (OSError, RuntimeError, ValueError):
        return None
    if (
        not stat.S_ISDIR(anchor_info.st_mode)
        or anchor_info.st_uid != 0
        or anchor_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not stat.S_ISREG(target_info.st_mode)
        or not target_info.st_mode & stat.S_IXUSR
    ):
        return None
    allowed_owners = {0, target_info.st_uid}
    current = anchor
    try:
        for index, part in enumerate(relative.parts):
            current = current / part
            info = current.stat()
            is_target = index == len(relative.parts) - 1
            if (
                info.st_uid not in allowed_owners
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or (is_target and not stat.S_ISREG(info.st_mode))
                or (not is_target and not stat.S_ISDIR(info.st_mode))
            ):
                return None
    except OSError:
        return None
    return str(target), target_info.st_dev, target_info.st_ino


def _same_trusted_codex_release(value: str | Path, expected: tuple[str, int, int]) -> bool:
    candidate = _trusted_codex_release_executable(value)
    return candidate is not None and candidate == expected


def _allowed_qiaokairos_supervisor_argv(
    argv: tuple[str, ...],
    *,
    executable: str,
    expected_script: str,
) -> bool:
    """Allow only Python/shebang forms that execute the trusted qia script."""
    if len(argv) < 2 or not argv[1].startswith("/"):
        return False
    interpreter = _canonical_trusted_regular_path(executable)
    script = _canonical_trusted_regular_path(argv[1])
    if not interpreter or script != expected_script:
        return False
    interpreter_name = Path(interpreter).name
    if not interpreter_name.startswith("python"):
        return False
    argv0 = argv[0]
    if argv0.startswith("/"):
        interpreter_ok = _canonical_trusted_regular_path(argv0) == interpreter
    else:
        interpreter_ok = argv0 in {interpreter_name, "python3", "python"}
    if not interpreter_ok:
        return False
    # qiaokairos has no positional arguments.  Keep its hidden maintenance
    # switches explicit too, so a metadata-matching Python invocation cannot
    # smuggle another interpreter mode or arbitrary script argument.
    value_options = {"--state", "--session-root", "--shared-name", "--codex-bin"}
    seen_no_wait = False
    index = 2
    while index < len(argv):
        option = argv[index]
        if option == "--no-wait" and not seen_no_wait:
            seen_no_wait = True
            index += 1
            continue
        if option in value_options and index + 1 < len(argv) and argv[index + 1]:
            index += 2
            continue
        return False
    return True


def _read_proc_snapshot(pid: int) -> _ProcSnapshot | None:
    """Take one self-consistent /proc identity snapshot, or fail closed."""
    if pid <= 0:
        return None
    proc = Path("/proc") / str(pid)
    try:
        proc_stat = proc.stat()
        raw_stat = (proc / "stat").read_text(encoding="utf-8", errors="strict")
        close = raw_stat.rfind(")")
        fields = raw_stat[close + 2:].split()
        # /proc/<pid>/stat field 22 is starttime; after the closing ')' this
        # becomes index 19 because field 3 is the first remaining token.
        starttime = int(fields[19])
        argv = tuple(
            part.decode("utf-8", errors="surrogateescape")
            for part in (proc / "cmdline").read_bytes().split(b"\0")
            if part
        )
        executable = os.readlink(proc / "exe")
        cwd = os.readlink(proc / "cwd")
        # Guard PID reuse or an exit/restart while fields are read.
        confirm = (proc / "stat").read_text(encoding="utf-8", errors="strict")
        confirm_close = confirm.rfind(")")
        if int(confirm[confirm_close + 2:].split()[19]) != starttime:
            return None
        return _ProcSnapshot(
            pid=pid,
            starttime=starttime,
            uid=proc_stat.st_uid,
            argv=argv,
            executable=executable,
            cwd=cwd,
        )
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _flock_holder_pids(lock_file: Any) -> list[int] | None:
    """Return unique kernel FLOCK holders for this exact opened lock inode."""
    try:
        file_stat = os.fstat(lock_file.fileno())
        device = f"{os.major(file_stat.st_dev):02x}:{os.minor(file_stat.st_dev):02x}:{file_stat.st_ino}"
        holders: set[int] = set()
        for raw_line in Path("/proc/locks").read_text(encoding="ascii", errors="strict").splitlines():
            fields = raw_line.split()
            # e.g. "12: FLOCK ADVISORY WRITE 123 00:02:99 0 EOF".
            if len(fields) < 6 or fields[1] != "FLOCK" or fields[5] != device:
                continue
            pid = int(fields[4])
            if pid <= 0:
                return None
            holders.add(pid)
        return sorted(holders)
    except (OSError, ValueError, UnicodeError):
        return None


def _direct_child_pids(pid: int) -> list[int] | None:
    try:
        raw = (Path("/proc") / str(pid) / "task" / str(pid) / "children").read_text(
            encoding="ascii", errors="strict",
        )
        children = [int(item) for item in raw.split()]
        return children if all(child > 0 for child in children) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _qiaokairos_lock_holder_is_verified(
    lock_file: Any,
    *,
    metadata: dict[str, Any],
    session_id: str | None,
    cwd: Path,
    expected_codex_bin: str | Path | None,
) -> bool:
    """Verify the held flock belongs to the real shared-daemon TUI.

    This is a defense against a same-uid process writing a plausible JSON
    marker.  It deliberately relies on Linux's kernel lock table plus /proc
    identities.  A hostile root can control both by definition; the trust
    boundary is the root-owned service/scripts and kernel, not another root
    principal on this host.
    """
    if metadata.get("owner") != QIAOKAIROS_REMOTE_COMPAT_LOCK_OWNER:
        return False
    expected_session = str(session_id or "").strip()
    try:
        expected_cwd = str(Path(cwd).expanduser().resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    expected_binary = (
        _trusted_codex_release_executable(expected_codex_bin)
        if expected_codex_bin is not None else None
    )
    expected_script = _canonical_trusted_regular_path(QIAOKAIROS_REMOTE_SCRIPT)
    if not expected_session or not expected_binary or not expected_script:
        return False
    if (
        metadata["session_id"] != expected_session
        or metadata["cwd"] != expected_cwd
        or metadata["codex_bin"] != expected_binary[0]
    ):
        return False
    pid = metadata["pid"]
    holders_before = _flock_holder_pids(lock_file)
    if holders_before != [pid]:
        return False
    supervisor = _read_proc_snapshot(pid)
    recorded_argv = tuple(metadata["supervisor_argv"])
    allowed_supervisor_argv = bool(supervisor and _allowed_qiaokairos_supervisor_argv(
        recorded_argv,
        executable=supervisor.executable,
        expected_script=expected_script,
    ))
    if supervisor is None or (
        supervisor.starttime != metadata["pid_starttime"]
        or supervisor.uid != os.geteuid()
        or supervisor.uid != metadata["uid"]
        or supervisor.executable != metadata["supervisor_exe"]
        or supervisor.cwd != metadata["supervisor_cwd"]
        or metadata["process_identity"] != f"{pid}:{supervisor.starttime}"
        or supervisor.argv != recorded_argv
        or not allowed_supervisor_argv
    ):
        return False
    children = _direct_child_pids(pid)
    if children is None or len(children) != 1:
        return False
    child = _read_proc_snapshot(children[0])
    expected_argv_tail = (
        "resume", "--remote", "unix://", "--include-non-interactive",
        "--cd", expected_cwd, expected_session,
    )
    if child is None or (
        child.uid != os.geteuid()
        or child.cwd != expected_cwd
        or not _same_trusted_codex_release(child.executable, expected_binary)
        or not child.argv
        or not _same_trusted_codex_release(child.argv[0], expected_binary)
        or child.argv[1:] != expected_argv_tail
    ):
        return False
    # Re-read the lock table after all process inspection.  If any PID exited,
    # was replaced, or lock ownership changed during the check, stay busy.
    return _flock_holder_pids(lock_file) == [pid]


def prompt_lock_is_busy(
    session_id: str | None,
    cwd: Path,
    *,
    ignore_owner: str | None = None,
    expected_codex_bin: str | Path | None = None,
) -> bool:
    """Check the legacy flock, optionally recognizing one trusted holder.

    ``ignore_owner`` is intentionally narrow: only a currently held lock on
    the same securely opened inode whose complete minimal metadata exactly
    matches the supplied owner is ignored.  Missing/corrupt/legacy metadata
    remains busy so a legacy ``codex exec`` writer is never raced.
    """
    if fcntl is None:
        return True
    path = prompt_lock_path(session_id, cwd)
    lock_file = None
    try:
        lock_file = _open_trusted_prompt_lock(path, migrate_private=True)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            metadata = _read_qiaokairos_lock_metadata(lock_file)
            compatible = (
                ignore_owner == QIAOKAIROS_REMOTE_COMPAT_LOCK_OWNER
                and metadata is not None
                and _qiaokairos_lock_holder_is_verified(
                    lock_file,
                    metadata=metadata,
                    session_id=session_id,
                    cwd=cwd,
                    expected_codex_bin=expected_codex_bin,
                )
            )
            return not compatible
        finally:
            if lock_file is not None:
                lock_file.close()
    except Exception:
        if lock_file is not None:
            lock_file.close()
        # A lock we cannot safely inspect is not evidence that it is safe to
        # race.  Fail closed; callers will retry after the normal queue delay.
        return True
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()
    return False


class _DaemonRecoveryLock:
    """A short-lived cross-process lock for shared-daemon recovery.

    The app-server is intentionally shared by CcCompanion, Remote, and the
    TUI.  A bridge-local mutex is therefore insufficient: two service
    instances can otherwise both decide that an old pid is stale and issue
    competing supervisor restarts.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def acquire(
        self,
        *,
        deadline: float,
        cancel_requested: Callable[[], bool] | None = None,
        closing: threading.Event | None = None,
    ) -> bool:
        if fcntl is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags, 0o600)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                os.close(descriptor)
                descriptor = None
                return False
            lock_file = os.fdopen(descriptor, "a+", encoding="utf-8")
            descriptor = None
        except (OSError, ValueError):
            if descriptor is not None:
                os.close(descriptor)
            return False
        try:
            while True:
                if (closing is not None and closing.is_set()) or (
                    cancel_requested is not None and cancel_requested()
                ):
                    lock_file.close()
                    raise _RecoveryCancelled("Codex daemon recovery cancelled", fallback_safe=True)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._file = lock_file
                    return True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        lock_file.close()
                        return False
                    if closing is not None:
                        closing.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                    else:
                        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except Exception:
            if self._file is not lock_file:
                lock_file.close()
            raise

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


@dataclass
class _DaemonRecoveryBudget:
    """One pre-submit recovery scope may restart the shared daemon once."""

    restart_limit: int = 1
    restart_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def claim_restart(self) -> bool:
        with self._lock:
            if self.restart_count >= self.restart_limit:
                return False
            self.restart_count += 1
            return True


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
        daemon_supervisor_command: Sequence[str] | None = None,
        daemon_recovery_lock_path: str | None = None,
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
        # The daemon must be owned outside cc-companion.service's cgroup.
        # ``systemctl`` starts the tracked fail-closed supervisor in deploy/.
        # A custom command is only for explicit deployments/tests; it receives
        # either ``start`` or ``restart`` as its final argument.
        self.daemon_supervisor_unit = (
            "codex-remote-control.service" if daemon_supervisor_command is None else None
        )
        # Must stay aligned with User=root and --service-uid=0 in the tracked
        # supervisor unit.  Binary ownership is deliberately independent.
        self.daemon_supervisor_uid = 0
        self.daemon_supervisor_command = list(daemon_supervisor_command or (
            "systemctl", "{action}", self.daemon_supervisor_unit,
        ))
        self.daemon_recovery_lock_path = Path(
            daemon_recovery_lock_path
            or (Path(self.codex_home) / "app-server-daemon" / "recovery.lock")
        ).expanduser()
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
        self._daemon_repair_requested = False
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
        recovery_budget = _DaemonRecoveryBudget()
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
                    recovery_budget=recovery_budget,
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
                if self.command is None and self.daemon_autostart:
                    # A socket can accept the WebSocket handshake and even
                    # initialize before a dead daemon immediately closes it.
                    # Ask the next pre-submit attempt to inspect the daemon
                    # pid instead of treating that as an ordinary reconnect.
                    self._daemon_repair_requested = True
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
        recovery_budget: _DaemonRecoveryBudget | None = None,
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
                recovery_budget=recovery_budget,
            )

    def _start_process_locked(
        self,
        cwd: Path,
        *,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        recovery_budget: _DaemonRecoveryBudget | None = None,
    ) -> None:
        if self.command is None:
            self._start_daemon_connection_locked(
                cwd,
                recovery_deadline=recovery_deadline,
                cancel_requested=cancel_requested,
                recovery_budget=recovery_budget,
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

    def _daemon_pid_state(self) -> str:
        """Return ``dead``, ``live``, or ``unknown`` without trusting a pid file.

        Only ``dead`` authorizes a supervisor restart.  A malformed file,
        inaccessible /proc entry, or permission failure is deliberately
        ``unknown`` and therefore never stopped by this bridge.  A live PID
        whose argv is not the remote-control app-server is a *proven stale*
        pid record, rather than a healthy daemon: it is safe to restart the
        independently-owned supervisor because the bridge never signals that
        unrelated process itself.
        """
        identity = self._read_daemon_identity()
        if identity is not None:
            return "live" if self._daemon_identity_is_managed(identity) else "dead"
        pid_path = Path(self.codex_home) / "app-server-daemon" / "app-server.pid"
        try:
            payload = json.loads(pid_path.read_text(encoding="utf-8"))
            pid = int(payload.get("pid"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return "unknown"
        if pid <= 1:
            return "unknown"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "unknown"
        except OSError:
            return "unknown"
        return "unknown"

    def _read_daemon_pid(self) -> int | None:
        try:
            payload = json.loads(
                (Path(self.codex_home) / "app-server-daemon" / "app-server.pid").read_text(
                    encoding="utf-8"
                )
            )
            pid = int(payload.get("pid"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return pid if pid > 1 else None

    @staticmethod
    def _process_cgroups(pid: int) -> tuple[str, ...]:
        try:
            lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        paths = []
        for line in lines:
            parts = line.split(":", 2)
            controllers = parts[1].split(",") if len(parts) == 3 else []
            is_systemd_hierarchy = len(parts) == 3 and parts[0] == "0" and parts[1] == ""
            is_systemd_hierarchy = is_systemd_hierarchy or "name=systemd" in controllers
            if len(parts) == 3 and is_systemd_hierarchy and parts[2].startswith("/"):
                paths.append(parts[2].rstrip("/") or "/")
        return tuple(paths)

    @staticmethod
    def _parse_process_starttime(payload: str) -> int:
        tail = payload[payload.rfind(")") + 2 :].split()
        return int(tail[19])

    def _managed_daemon_executable(self) -> tuple[int, int, int] | None:
        anchor_path = Path(self.codex_home)
        path = anchor_path / "packages/standalone/current/bin/codex"
        try:
            anchor = anchor_path.resolve(strict=True)
            resolved = path.resolve(strict=True)
            anchor_info = anchor.stat()
            target_info = resolved.stat()
            relative = resolved.relative_to(anchor)
            if (
                not stat.S_ISDIR(anchor_info.st_mode)
                or anchor_info.st_uid != 0
                or anchor_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not stat.S_ISREG(target_info.st_mode)
                or not target_info.st_mode & stat.S_IXUSR
            ):
                return None
            allowed_owners = {0, target_info.st_uid}
            current = anchor
            for index, part in enumerate(relative.parts):
                current = current / part
                info = current.stat()
                is_target = index == len(relative.parts) - 1
                if info.st_uid not in allowed_owners:
                    return None
                if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return None
                if (is_target and not stat.S_ISREG(info.st_mode)) or (
                    not is_target and not stat.S_ISDIR(info.st_mode)
                ):
                    return None
        except (OSError, ValueError):
            return None
        return target_info.st_dev, target_info.st_ino, target_info.st_uid

    def _daemon_identity_is_managed(self, identity: _DaemonProcessIdentity) -> bool:
        managed = self._managed_daemon_executable()
        return (
            managed is not None
            and (identity.executable_device, identity.executable_inode, identity.executable_uid)
            == managed
            and identity.process_uid == self.daemon_supervisor_uid
            and b"app-server" in identity.argv
            and b"--remote-control" in identity.argv
        )

    def _read_daemon_identity(self) -> _DaemonProcessIdentity | None:
        """Read one PID/starttime/argv/exe/uid/cgroup identity snapshot."""
        pid_path = Path(self.codex_home) / "app-server-daemon" / "app-server.pid"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(pid_path, flags)
        except OSError:
            return None
        try:
            pidfile_info = os.fstat(descriptor)
            if not stat.S_ISREG(pidfile_info.st_mode) or pidfile_info.st_uid != 0:
                return None
            raw = os.read(descriptor, 4097)
            if len(raw) > 4096:
                return None
            pid = int(json.loads(raw.decode("utf-8"))["pid"])
            if pid <= 1:
                return None
            process_dir = Path(f"/proc/{pid}")
            first_starttime = self._parse_process_starttime(
                (process_dir / "stat").read_text(encoding="utf-8")
            )
            argv = tuple(
                part for part in (process_dir / "cmdline").read_bytes().split(b"\0") if part
            )
            exe_info = (process_dir / "exe").stat()
            process_uid = process_dir.stat().st_uid
            cgroups = self._process_cgroups(pid)
            second_starttime = self._parse_process_starttime(
                (process_dir / "stat").read_text(encoding="utf-8")
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            if second_starttime != first_starttime or os.read(descriptor, 4097) != raw:
                return None
            return _DaemonProcessIdentity(
                pid=pid,
                pidfile_device=pidfile_info.st_dev,
                pidfile_inode=pidfile_info.st_ino,
                starttime=first_starttime,
                argv=argv,
                executable_device=exe_info.st_dev,
                executable_inode=exe_info.st_ino,
                executable_uid=exe_info.st_uid,
                process_uid=process_uid,
                cgroups=cgroups,
            )
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError, IndexError):
            return None
        finally:
            os.close(descriptor)

    def _query_systemd_unit(
        self,
        *arguments: str,
        deadline: float,
    ) -> subprocess.CompletedProcess[str] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return subprocess.run(
                ["systemctl", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=max(0.01, min(2.0, remaining)),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _systemd_start_contract_ready(self, deadline: float) -> bool:
        unit = self.daemon_supervisor_unit
        if unit is None:
            return True
        result = self._query_systemd_unit("is-enabled", "--quiet", unit, deadline=deadline)
        return result is not None and result.returncode == 0

    def _systemd_takeover_verified(self, deadline: float) -> bool:
        """Bind stable unit state and cgroup to one complete daemon snapshot."""
        unit = self.daemon_supervisor_unit
        if unit is None:
            return self._daemon_pid_state() == "live"
        def query_properties() -> dict[str, str] | None:
            result = self._query_systemd_unit(
                "show", unit, "--property=ActiveState", "--property=UnitFileState",
                "--property=ControlGroup", "--no-pager", deadline=deadline,
            )
            if result is None or result.returncode != 0:
                return None
            properties: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    properties[key] = value
            return properties

        before = query_properties()
        identity = self._read_daemon_identity()
        after = query_properties()
        if before is None or before != after or identity is None:
            return False
        if before.get("ActiveState") != "active":
            return False
        if before.get("UnitFileState") not in {"enabled", "enabled-runtime", "static"}:
            return False
        if not self._daemon_identity_is_managed(identity):
            return False
        control_group = before.get("ControlGroup", "").rstrip("/")
        if not control_group.startswith("/"):
            return False
        return any(
            path == control_group or path.startswith(control_group + "/")
            for path in identity.cgroups
        )

    def _supervisor_command(self, action: str) -> list[str]:
        if action not in {"start", "restart"}:
            raise ValueError("unsupported daemon supervisor action")
        command = [str(part) for part in self.daemon_supervisor_command]
        if "{action}" in command:
            return [action if part == "{action}" else part for part in command]
        return [*command, action]

    def _run_supervisor_action_locked(
        self,
        action: str,
        *,
        cwd: Path,
        env: dict[str, str],
        deadline: float,
        cancel_requested: Callable[[], bool] | None,
        recovery_budget: _DaemonRecoveryBudget | None = None,
    ) -> bool:
        """Run the externally-owned daemon supervisor under a flock.

        A timed-out contender leaves recovery to the lock holder rather than
        issuing a second restart.  Once it acquires the lock it always
        rechecks the pid state, which prevents a stale observation from
        stopping a daemon another client just repaired.
        """
        lock = _DaemonRecoveryLock(self.daemon_recovery_lock_path)
        if not lock.acquire(
            deadline=deadline,
            cancel_requested=cancel_requested,
            closing=self._closing,
        ):
            return False
        try:
            self._raise_if_recovery_cancelled(cancel_requested)
            state = self._daemon_pid_state()
            if action == "restart":
                if state != "dead":
                    self.log.info("shared Codex daemon repair skipped: pid state=%s", state)
                    return state == "live" and self._systemd_takeover_verified(deadline)
                if recovery_budget is not None and not recovery_budget.claim_restart():
                    self.log.warning("shared Codex daemon restart budget exhausted")
                    return False
            elif state == "live":
                return self._systemd_takeover_verified(deadline)
            if not self._systemd_start_contract_ready(deadline):
                self.log.warning("shared Codex daemon supervisor is not enabled")
                return False
            self._run_daemon_start_interruptible(
                self._supervisor_command(action),
                cwd=str(cwd),
                env=env,
                deadline=deadline,
                cancel_requested=cancel_requested,
            )
            if not self._systemd_takeover_verified(deadline):
                self.log.warning("shared Codex daemon supervisor takeover verification failed")
                return False
            return True
        except _RecoveryCancelled:
            raise
        except Exception as exc:
            self.log.warning(
                "shared Codex daemon supervisor %s failed: %s",
                action,
                type(exc).__name__,
            )
            return False
        finally:
            lock.release()

    def _start_daemon_connection_locked(
        self,
        cwd: Path,
        *,
        recovery_deadline: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        recovery_budget: _DaemonRecoveryBudget | None = None,
    ) -> None:
        env = os.environ.copy()
        env["CODEX_HOME"] = self.codex_home
        deadline = (
            recovery_deadline
            if recovery_deadline is not None
            else time.monotonic() + self.daemon_recovery_timeout_sec
        )
        budget = recovery_budget or _DaemonRecoveryBudget()

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
                # ``thread/resume`` returns the persisted thread snapshot in
                # one frame. Long-lived Kairos sessions can legitimately
                # exceed websockets' 1 MiB default even while their model
                # context remains healthy. Keep a finite local-transport
                # ceiling so oversized snapshots don't masquerade as daemon
                # disconnects, while still bounding memory use.
                max_size=64 * 1024 * 1024,
                # The listener is local. Keep the handshake slice short so a
                # half-open socket cannot make cancel/close wait for seconds;
                # the outer recovery loop can safely try again.
                open_timeout=max(0.01, min(0.25, self.request_timeout_sec, remaining)),
                close_timeout=1.0,
            )

        first_error: BaseException | None = None
        last_error: BaseException | None = None
        websocket: Any = None
        force_repair = self._daemon_repair_requested
        self._daemon_repair_requested = False
        while websocket is None:
            self._raise_if_recovery_cancelled(cancel_requested)
            if time.monotonic() >= deadline:
                break
            if self.daemon_supervisor_unit is not None and self.daemon_autostart:
                ownership_state = self._daemon_pid_state()
                if ownership_state == "live":
                    if not self._systemd_takeover_verified(deadline):
                        raise _TransportError(
                            "shared Codex daemon is not owned by its enabled systemd supervisor",
                            fallback_safe=True,
                        )
                else:
                    action = "restart" if ownership_state == "dead" else "start"
                    if not self._run_supervisor_action_locked(
                        action,
                        cwd=cwd,
                        env=env,
                        deadline=min(deadline, time.monotonic() + self.daemon_start_timeout_sec),
                        cancel_requested=cancel_requested,
                        recovery_budget=budget,
                    ):
                        raise _TransportError(
                            "shared Codex daemon supervisor contract is unavailable",
                            fallback_safe=True,
                        )
                    force_repair = False
            if force_repair and self.daemon_autostart:
                # Never restart a daemon merely because a connection dropped:
                # Remote/TUI may own a live turn.  Only a pid file that proves
                # its process is gone authorizes the supervisor restart.
                if self._daemon_pid_state() == "dead":
                    self._run_supervisor_action_locked(
                        "restart",
                        cwd=cwd,
                        env=env,
                        deadline=min(deadline, time.monotonic() + self.daemon_start_timeout_sec),
                        cancel_requested=cancel_requested,
                        recovery_budget=budget,
                    )
                else:
                    self.log.info("shared Codex daemon repair skipped: pid is live or unknown")
                force_repair = False
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
            state = self._daemon_pid_state()
            if state == "dead":
                # A stale pid/socket needs a real restart.  `start` alone is
                # commonly a no-op for a still-active oneshot supervisor.
                started = self._run_supervisor_action_locked(
                    "restart",
                    cwd=cwd,
                    env=env,
                    deadline=min(deadline, time.monotonic() + self.daemon_start_timeout_sec),
                    cancel_requested=cancel_requested,
                    recovery_budget=budget,
                )
            elif state == "unknown":
                # Missing/corrupt state cannot prove ownership, so only ask
                # the independent supervisor to start; never stop/restart.
                started = self._run_supervisor_action_locked(
                    "start",
                    cwd=cwd,
                    env=env,
                    deadline=min(deadline, time.monotonic() + self.daemon_start_timeout_sec),
                    cancel_requested=cancel_requested,
                )
            else:
                # A live daemon might simply be between listener handoffs.
                # Waiting is safer than disrupting another official client.
                started = False
            if not started:
                last_error = last_error or _TransportError(
                    f"shared Codex daemon unavailable (pid state={state})"
                )
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
        if item_type in {"collabAgentToolCall", "subAgentActivity"}:
            worker_activity = self._collaboration_activity(item, completed=completed)
            if worker_activity is not None:
                with active.condition:
                    previous_worker = active.worker_items.get(item_id) if item_id else None
                    if previous_worker is not None:
                        worker_activity["worker_id"], worker_activity["name"] = previous_worker
                    elif item_id:
                        active.worker_items[item_id] = (
                            str(worker_activity["worker_id"]),
                            str(worker_activity["name"]),
                        )
                    if not completed:
                        activity_key = item_id or (
                            f"{item_type}:{worker_activity['worker_id']}"
                        )
                        if activity_key in active.activity_items:
                            return
                        active.activity_items.add(activity_key)
                self._safe_callback(active.on_activity, worker_activity)
            # The worker record is the only client-facing representation for
            # collaboration items, preventing a duplicate generic tool row.
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
    def _collaboration_activity(item: dict[str, Any], *, completed: bool) -> dict[str, Any] | None:
        """Return a bounded, prompt-free worker lifecycle record.

        App-server versions have used a few different field spellings.  Only
        a conservative identifier-shaped value is ever surfaced; task text,
        commentary, command output and arbitrary display labels are ignored.
        Older payloads therefore degrade to the generic ``协作 worker`` row.
        """
        item_type = str(item.get("type") or "")
        if item_type not in {"collabAgentToolCall", "subAgentActivity"}:
            return None

        candidate: Any = None
        for key in ("agentName", "agent_name", "workerName", "worker_name", "taskName", "task_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value
                break
        if candidate is None:
            agent = item.get("agent")
            if isinstance(agent, dict):
                for key in ("name", "agentName", "agent_name", "taskName", "task_name"):
                    value = agent.get(key)
                    if isinstance(value, str) and value.strip():
                        candidate = value
                        break

        worker_id, name = CodexAppBridge._safe_worker_identity_name(candidate, item_id=str(item.get("id") or ""))
        raw_status = str(item.get("status") or item.get("state") or item.get("phase") or "").lower()
        if completed:
            status = "failed" if raw_status in {"failed", "error"} or item.get("error") else "completed"
        else:
            status = "failed" if raw_status in {"failed", "error"} else "running"
        return {
            "kind": "collaboration_worker",
            "worker_id": worker_id,
            "name": name,
            "status": status,
            # A started item is one unit of visible worker activity.  A final
            # item updates status only, so it cannot inflate the count.
            "count_delta": 0 if completed else 1,
        }

    @staticmethod
    def _safe_worker_identity_name(value: Any, *, item_id: str = "") -> tuple[str, str]:
        """Return a canonical aggregation key and a safe, non-colliding label."""
        raw = str(value or "").strip().replace("\\", "/")
        segments = [segment for segment in raw.strip("/").split("/") if segment]
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        safe_segments = bool(segments) and all(
            len(segment) <= 80
            and segment[0] in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            and all(char in allowed for char in segment)
            and segment not in {".", ".."}
            for segment in segments
        )
        canonical = "/".join(segments) if safe_segments else ""
        if canonical and len(canonical) <= 160:
            display = canonical.removeprefix("root/")
            return canonical, display

        # The hash differentiates anonymous/unsafe workers without disclosing
        # their rejected label. item_id keeps truly anonymous concurrent items
        # distinct and remains stable across notification replay.
        basis = raw or f"item:{item_id}" or "anonymous"
        digest = hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()[:16]
        return f"anonymous-{digest}", f"协作 worker-{digest[:8]}"

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
    def _safe_callback(callback: Callable[[ActivityValue], None] | None, value: ActivityValue) -> None:
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
