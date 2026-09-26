"""Leased interactive Kiro terminal for the App's 「终端」 tab (kiro 对齐 CC r3, 2026-09-26).

Kiro's App chat runs through a resident ``kiro-cli acp`` process.  The
terminal tab instead runs ``kiro-cli chat --resume-id <production session>`` in
one bridge-owned tmux pane.  Both are *writers* of the same on-disk Kiro
session, so they must strictly alternate (single writer):

* kiro-cli itself refuses to open a session another live process holds
  (``<id>.lock`` → ACP ``session/load`` fails, the TUI prints "Session is active
  in another process"; both verified against 2.21.2 in an isolated session).
  That lock is the last line of defence, not the protocol.
* The protocol is the handler's handoff (push.py): a terminal acquire first
  reserves Kiro under ``kiro_turn_lock`` (no ACP turn/prepare may be running),
  closes the ACP process so its session lock and in-memory state are dropped,
  and only then launches the TUI.  A chat send reserves Kiro, then asks this
  bridge to release an *idle* TUI before ACP ``session/load``s the session —
  which therefore sees every terminal turn.  A TUI mid-turn refuses the
  handoff; the App message is rejected, never interleaved.

TUI turn state is read from Kiro's own session files: each user turn appends
one ``Prompt`` record to ``<id>.jsonl`` when it starts, and one
``user_turn_metadatas`` entry to ``<id>.json`` when it ends (including Ctrl-C
interruptions).  More prompts than finished turns means a turn is running.

Pane/session identities never leave this module except the public alias; the
App only receives an opaque lease bound to the exact owned pane.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import time
from typing import Any, Callable

KIRO_TERMINAL_ALIAS = "kiro"
KIRO_TERMINAL_TMUX_SESSION = "ccc-kiro-terminal"
KIRO_TERMINAL_OWNER_OPTION = "@ccc_kiro_terminal_owner"
KIRO_TERMINAL_OWNER_VALUE = "cccompanion:kiro-terminal:v1"
KIRO_TERMINAL_SESSION_OPTION = "@ccc_kiro_terminal_session"
KIRO_SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
_PANE_RE = re.compile(r"%[0-9]+")
_MODEL_RE = re.compile(r"[A-Za-z0-9._:\-]{1,120}")
_EFFORT_VALUES = frozenset({"low", "medium", "high", "xhigh", "max"})
# kiro-cli writes a turn's Prompt record only once the request is under way,
# so an Enter from the App is treated as a running turn until the session
# files show it finished (or, for slash commands/empty input that never
# become a turn, until this grace expires).
KIRO_TERMINAL_SUBMIT_GRACE_SECONDS = 30.0
# 2.21.2 TUI footer while a turn runs ("Kiro is working · Type to steer").
_TUI_WORKING_RE = re.compile(r"Kiro is working", re.IGNORECASE)

logger = logging.getLogger(__name__)


class KiroTerminalUnavailable(RuntimeError):
    """Safe failure while opening or driving the Kiro console."""


class KiroTerminalBusy(KiroTerminalUnavailable):
    """The single-writer handoff is not possible right now."""


class KiroTerminalNoActiveSession(KiroTerminalUnavailable):
    """There is no durable Kiro session pointer to resume."""


class KiroSessionTurnProbe:
    """Read whether a Kiro session has a user turn in flight, from its files.

    ``busy(session_id)`` returns True/False, or None when the files cannot be
    read (callers treat None as busy: fail closed).  The ``.jsonl`` log can be
    many MB, so Prompt records are counted incrementally from a cached offset.
    """

    def __init__(self, sessions_dir: Path = KIRO_SESSIONS_DIR) -> None:
        self.sessions_dir = Path(sessions_dir)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, int, int]] = {}  # id -> (inode, offset, prompts)

    def _prompt_count(self, session_id: str) -> int | None:
        path = self.sessions_dir / f"{session_id}.jsonl"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return 0
        except OSError:
            return None
        with self._lock:
            inode, offset, count = self._cache.get(session_id, (stat.st_ino, 0, 0))
            if inode != stat.st_ino or stat.st_size < offset:
                inode, offset, count = stat.st_ino, 0, 0
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
            except OSError:
                return None
            # Only complete lines are consumed; a record being written is
            # picked up by the next poll.
            end = data.rfind(b"\n") + 1
            for raw in data[:end].splitlines():
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("kind") == "Prompt":
                    count += 1
            self._cache[session_id] = (inode, offset + end, count)
            while len(self._cache) > 16:
                self._cache.pop(next(iter(self._cache)))
            return count

    def _finished_turns(self, session_id: str) -> int | None:
        path = self.sessions_dir / f"{session_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            return None
        try:
            turns = payload["session_state"]["conversation_metadata"]["user_turn_metadatas"]
        except (KeyError, TypeError):
            return 0
        return len(turns) if isinstance(turns, list) else None

    def counts(self, session_id: str) -> tuple[int, int] | None:
        """``(prompts started, turns finished)`` or None when unreadable."""
        if not _SESSION_ID_RE.fullmatch(str(session_id or "")):
            return None
        # Read the finished count first: a turn that ends between the two
        # reads then looks busy for one poll (safe), never the reverse.
        finished = self._finished_turns(session_id)
        prompts = self._prompt_count(session_id)
        if finished is None or prompts is None:
            return None
        return prompts, finished

    def busy(self, session_id: str) -> bool | None:
        counts = self.counts(session_id)
        return None if counts is None else counts[0] > counts[1]


class KiroTerminalBridge:
    """Own at most one ``kiro-cli chat --resume-id`` TUI in a private tmux pane."""

    def __init__(
        self,
        *,
        command: str | Path = Path.home() / ".local" / "bin" / "kiro-cli",
        cwd: str | Path = "/root/Karami-Workspace",
        tmux_session: str = KIRO_TERMINAL_TMUX_SESSION,
        idle_seconds: float = 60.0,
        sessions_dir: Path = KIRO_SESSIONS_DIR,
        runner: Any = subprocess.run,
        process_killer: Any = os.kill,
        shutdown_wait_seconds: float = 2.0,
        resume_wait_seconds: float = 10.0,
        turn_probe: KiroSessionTurnProbe | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.command = Path(command).expanduser()
        self.cwd = Path(cwd).expanduser()
        self.tmux_session = tmux_session
        self.idle_seconds = max(5.0, float(idle_seconds))
        self.sessions_dir = Path(sessions_dir)
        self._run = runner
        self._process_killer = process_killer
        self.shutdown_wait_seconds = max(0.05, min(float(shutdown_wait_seconds), 5.0))
        self.resume_wait_seconds = max(0.5, float(resume_wait_seconds))
        self.turn_probe = turn_probe or KiroSessionTurnProbe(self.sessions_dir)
        self._sleep = sleep
        self._lock = threading.RLock()
        # Serializes every App terminal operation (capture/paste/keys/release)
        # with the reaper and the chat handoff, so a pane is never killed
        # between a paste and its Enter.
        self._input_lock = threading.Lock()
        self._lease: str | None = None
        self._lease_pane: str | None = None
        self._session_id: str | None = None
        self._timer: threading.Timer | None = None
        self._last_activity = 0.0
        # (monotonic time, prompts, finished) at the last App Enter.
        self._submit_marker: tuple[float, int, int] | None = None

    # ---------- tmux primitives ----------

    def input_transaction(self):
        return self._input_lock

    def _run_tmux(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(argv, capture_output=True, text=True, timeout=5)

    def _has_session_locked(self) -> bool:
        return self._run_tmux(["tmux", "has-session", "-t", f"={self.tmux_session}"]).returncode == 0

    def _owns_session_locked(self) -> bool:
        result = self._run_tmux([
            "tmux", "show-options", "-v", "-t", self.tmux_session, KIRO_TERMINAL_OWNER_OPTION,
        ])
        return result.returncode == 0 and result.stdout.rstrip("\r\n") == KIRO_TERMINAL_OWNER_VALUE

    @staticmethod
    def _fingerprint(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _bound_fingerprint_locked(self) -> str:
        result = self._run_tmux([
            "tmux", "show-options", "-v", "-t", self.tmux_session, KIRO_TERMINAL_SESSION_OPTION,
        ])
        value = result.stdout.rstrip("\r\n")
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{64}", value) else ""

    def _pane_status_locked(self) -> tuple[str, bool, int]:
        result = self._run_tmux([
            "tmux", "display-message", "-p", "-t", self.tmux_session,
            "#{pane_id}|#{pane_dead}|#{pane_pid}",
        ])
        fields = result.stdout.rstrip("\r\n").split("|")
        if (
            result.returncode != 0
            or len(fields) != 3
            or not _PANE_RE.fullmatch(fields[0])
            or fields[1] not in {"0", "1"}
            or not fields[2].isdigit()
        ):
            raise KiroTerminalUnavailable("Kiro 终端 pane 状态异常")
        return fields[0], fields[1] == "1", int(fields[2])

    def _owned_pane_locked(self) -> tuple[str, bool, int] | None:
        """The bridge-owned pane (even without an in-memory lease), else None."""
        if not self._has_session_locked():
            return None
        if not self._owns_session_locked():
            raise KiroTerminalUnavailable("Kiro 终端名称已被其他会话占用")
        return self._pane_status_locked()

    def _schedule_reaper_locked(self, delay: float | None = None) -> None:
        if self._timer is not None:
            return
        self._timer = threading.Timer(delay or self.idle_seconds, self._reap_if_idle)
        self._timer.daemon = True
        self._timer.start()

    def _touch_locked(self) -> None:
        self._last_activity = time.monotonic()
        self._schedule_reaper_locked()

    def touch(self) -> None:
        with self._lock:
            if self._lease is not None:
                self._touch_locked()

    def _clear_lease_locked(self) -> None:
        self._lease = None
        self._lease_pane = None
        self._session_id = None
        self._submit_marker = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _shutdown_exact_pane_locked(self, expected_pane: str, pane_pid: int, pane_dead: bool) -> bool:
        """SIGTERM the verified pane's process group, then kill that exact pane."""
        if not _PANE_RE.fullmatch(expected_pane):
            raise KiroTerminalUnavailable("Kiro 终端 pane 身份异常")
        if not pane_dead and pane_pid > 1:
            try:
                # The pane process is a session leader; kiro-cli's chat child
                # shares its process group.  A graceful TERM lets it flush the
                # session and remove its lock.
                self._process_killer(-pane_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    self._process_killer(pane_pid, signal.SIGTERM)
                except OSError:
                    pass
            deadline = time.monotonic() + self.shutdown_wait_seconds
            while time.monotonic() < deadline:
                self._sleep(0.05)
                try:
                    current = self._owned_pane_locked()
                except KiroTerminalUnavailable:
                    return True
                if current is None:
                    return True
                if not hmac.compare_digest(current[0], expected_pane):
                    return False
                if current[1]:
                    break
        current = self._owned_pane_locked()
        if current is None:
            return True
        if not hmac.compare_digest(current[0], expected_pane):
            return False
        # Kill the bridge's own tmux session (exact-name match, ownership
        # re-verified above).  It only ever contains this one pane.
        result = self._run_tmux(["tmux", "kill-session", "-t", f"={self.tmux_session}"])
        if result.returncode != 0 and self._has_session_locked():
            raise KiroTerminalUnavailable("Kiro 终端释放失败")
        return True

    # ---------- session state ----------

    def session_busy(self, session_id: str) -> bool | None:
        return self.turn_probe.busy(session_id)

    def mark_submitted(self) -> None:
        """Record an App Enter into the owned TUI (see SUBMIT_GRACE)."""
        with self._lock:
            counts = self.turn_probe.counts(self._session_id) if self._session_id else None
            prompts, finished = counts if counts is not None else (0, 0)
            self._submit_marker = (time.monotonic(), prompts, finished)

    def _tui_busy_locked(self, session_id: str, pane_id: str | None) -> bool:
        """Conservative: True unless the TUI is positively idle."""
        counts = self.turn_probe.counts(session_id)
        if counts is None or counts[0] > counts[1]:
            return True
        marker = self._submit_marker
        if marker is not None and session_id == self._session_id:
            submitted_at, _prompts, finished_before = marker
            if counts[1] > finished_before:
                self._submit_marker = None  # that turn has finished
            elif time.monotonic() - submitted_at < KIRO_TERMINAL_SUBMIT_GRACE_SECONDS:
                return True
            else:
                self._submit_marker = None
        if pane_id:
            try:
                screen = self._run_tmux(["tmux", "capture-pane", "-t", pane_id, "-p"])
            except Exception:
                return True
            if screen.returncode != 0:
                return True
            tail = "\n".join(screen.stdout.rstrip().splitlines()[-8:])
            if _TUI_WORKING_RE.search(tail):
                return True
        return False

    def tui_busy(self) -> bool:
        """Whether the owned TUI may be mid-turn (for the capture state)."""
        with self._lock:
            if not self._session_id:
                return False
            try:
                pane = self._verified_live_pane_locked()
            except KiroTerminalUnavailable:
                return True
            return pane is not None and self._tui_busy_locked(self._session_id, pane)

    def _lock_owner_pid(self, session_id: str) -> int:
        try:
            payload = json.loads((self.sessions_dir / f"{session_id}.lock").read_text(encoding="utf-8"))
            pid = int(payload.get("pid") or 0)
            return pid if pid > 1 else 0
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    @staticmethod
    def _parent_pid(pid: int) -> int:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            return int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return 0

    @classmethod
    def _belongs_to_pane(cls, pid: int, pane_pid: int) -> bool:
        """``pid`` is the pane process or one of its descendants.

        kiro-cli 2.21.2's TUI (bun) runs its session engine as a
        ``kiro-cli-chat acp`` grandchild in its own process group; that
        grandchild is the lock owner, so ancestry — not pgid — is checked.
        """
        current = pid
        for _ in range(8):
            if current == pane_pid:
                return True
            if current <= 1:
                return False
            current = cls._parent_pid(current)
        return False

    def _wait_for_resume_locked(self, session_id: str, pane_pid: int) -> bool:
        """True once the session lock belongs to this pane's process group.

        If the resume failed (e.g. another process still held the session)
        the TUI falls back to an *unrelated new* chat; it must never be left
        running for the App to type into.
        """
        deadline = time.monotonic() + self.resume_wait_seconds
        while time.monotonic() < deadline:
            owner = self._lock_owner_pid(session_id)
            if owner and self._belongs_to_pane(owner, pane_pid):
                return True
            try:
                current = self._owned_pane_locked()
            except KiroTerminalUnavailable:
                return False
            if current is None or current[1]:
                return False
            self._sleep(0.2)
        return False

    def _launch_argv(self, session_id: str, model: str | None, effort: str | None) -> list[str]:
        argv = [str(self.command), "chat", "--resume-id", session_id]
        # Same model/effort as the App's Kiro preferences; never changes them.
        if model and _MODEL_RE.fullmatch(model):
            argv += ["--model", model]
        if effort and effort in _EFFORT_VALUES:
            argv += ["--effort", effort]
        return argv

    def ensure(self, session_id: str, *, model: str | None = None, effort: str | None = None) -> str:
        """Return the owned pane running the TUI for exactly ``session_id``.

        The caller guarantees the ACP process is closed and no ACP turn can
        start (terminal acquire reservation).
        """
        if not _SESSION_ID_RE.fullmatch(str(session_id or "")):
            raise KiroTerminalNoActiveSession("Kiro 当前没有可恢复的会话")
        expected = self._fingerprint(session_id)
        with self._lock:
            try:
                current = self._owned_pane_locked()
            except KiroTerminalUnavailable:
                raise
            except Exception as exc:
                raise KiroTerminalUnavailable("tmux 状态不可用") from exc
            if current is not None:
                pane_id, pane_dead, pane_pid = current
                if not pane_dead and hmac.compare_digest(self._bound_fingerprint_locked(), expected):
                    if self._lease is None or self._lease_pane != pane_id:
                        # Post-restart adoption or pane replacement: new generation.
                        self._lease = secrets.token_urlsafe(32)
                        self._lease_pane = pane_id
                    self._session_id = session_id
                    self._touch_locked()
                    return pane_id
                # Dead pane, or a TUI bound to an older session pointer: never
                # interrupt a turn this bridge knows is still running there.
                if (
                    not pane_dead
                    and self._session_id
                    and hmac.compare_digest(self._bound_fingerprint_locked(), self._fingerprint(self._session_id))
                    and self._tui_busy_locked(self._session_id, pane_id)
                ):
                    raise KiroTerminalBusy("Kiro 终端里还有一轮在进行")
                self._shutdown_exact_pane_locked(pane_id, pane_pid, pane_dead)
                self._clear_lease_locked()
            if not self.command.is_file() or not os.access(self.command, os.X_OK):
                raise KiroTerminalUnavailable("kiro-cli 未安装")
            if not self.cwd.is_dir():
                raise KiroTerminalUnavailable("Kiro 终端工作区不可用")
            # A harmless staging pane is created and marked before respawn, so
            # this bridge never adopts or kills a foreign tmux session.
            created = self._run_tmux([
                "tmux", "new-session", "-d", "-s", self.tmux_session,
                "-c", str(self.cwd), "-x", "160", "-y", "48", "/bin/sleep", "30",
            ])
            if created.returncode != 0:
                raise KiroTerminalUnavailable("Kiro 终端启动失败")
            marked = self._run_tmux([
                "tmux", "set-option", "-t", self.tmux_session,
                KIRO_TERMINAL_OWNER_OPTION, KIRO_TERMINAL_OWNER_VALUE,
            ])
            bound = self._run_tmux([
                "tmux", "set-option", "-t", self.tmux_session,
                KIRO_TERMINAL_SESSION_OPTION, expected,
            ])
            if (
                marked.returncode != 0
                or bound.returncode != 0
                or not self._owns_session_locked()
                or not hmac.compare_digest(self._bound_fingerprint_locked(), expected)
            ):
                self._kill_staging_locked()
                raise KiroTerminalUnavailable("Kiro 终端归属标记失败")
            launched = self._run_tmux([
                "tmux", "respawn-pane", "-k", "-t", self.tmux_session, "-c", str(self.cwd),
                "/usr/bin/env", "CCC_KIRO_TERMINAL_BRIDGE=1",
                *self._launch_argv(session_id, model, effort),
            ])
            if launched.returncode != 0:
                self._kill_staging_locked()
                raise KiroTerminalUnavailable("Kiro 终端启动失败")
            pane_id, pane_dead, pane_pid = self._pane_status_locked()
            if pane_dead or not self._wait_for_resume_locked(session_id, pane_pid):
                try:
                    current = self._owned_pane_locked()
                    if current is not None:
                        self._shutdown_exact_pane_locked(*current)
                except KiroTerminalUnavailable:
                    logger.warning("Kiro terminal cleanup after failed resume was not confirmed")
                raise KiroTerminalUnavailable("Kiro 终端没能接上当前会话")
            self._lease = secrets.token_urlsafe(32)
            self._lease_pane = pane_id
            self._session_id = session_id
            self._touch_locked()
            return pane_id

    def _kill_staging_locked(self) -> None:
        try:
            current = self._owned_pane_locked()
            if current is not None:
                self._shutdown_exact_pane_locked(*current)
        except KiroTerminalUnavailable:
            pass

    def lease_for_pane(self, pane_id: str) -> str:
        with self._lock:
            current = self._owned_pane_locked()
            if (
                current is None
                or current[1]
                or not hmac.compare_digest(current[0], str(pane_id))
                or self._lease is None
                or self._lease_pane is None
                or not hmac.compare_digest(current[0], self._lease_pane)
            ):
                raise KiroTerminalUnavailable("Kiro 终端已切换")
            self._touch_locked()
            return self._lease

    def has_live_pane(self) -> bool:
        with self._lock:
            try:
                current = self._owned_pane_locked()
            except Exception:
                return True  # unknown ownership: fail closed
            return current is not None and not current[1]

    def capture(self, pane_id: str, lines: int) -> str:
        with self._lock:
            current = self._owned_pane_locked()
            if current is None or not hmac.compare_digest(current[0], pane_id):
                raise KiroTerminalUnavailable("Kiro 终端已切换")
            result = self._run_tmux([
                "tmux", "capture-pane", "-t", pane_id, "-p", "-S", str(-max(1, min(int(lines), 2000))),
            ])
            if result.returncode != 0:
                raise KiroTerminalUnavailable("Kiro 终端捕获失败")
            self._touch_locked()
            return result.stdout

    def _verified_live_pane_locked(self) -> str | None:
        current = self._owned_pane_locked()
        if (
            current is None
            or current[1]
            or self._lease_pane is None
            or not hmac.compare_digest(current[0], self._lease_pane)
        ):
            return None
        return current[0]

    def send_control_key(self, key_name: str) -> bool:
        with self._lock:
            pane_id = self._verified_live_pane_locked()
            if pane_id is None:
                return False
            result = self._run_tmux(["tmux", "send-keys", "-t", pane_id, key_name])
            if result.returncode != 0:
                raise KiroTerminalUnavailable("Kiro 终端按键失败")
            if key_name == "Enter":
                self.mark_submitted()
            self._touch_locked()
            return True

    def send_text(self, text: str, enter: bool) -> bool:
        with self._lock:
            pane_id = self._verified_live_pane_locked()
            if pane_id is None:
                return False
            buffer_name: str | None = None
            try:
                if text:
                    buffer_name = f"ccc-kiro-{secrets.token_hex(8)}"
                    load = self._run_tmux(["tmux", "set-buffer", "-b", buffer_name, "--", text])
                    if load.returncode != 0:
                        raise KiroTerminalUnavailable("Kiro 终端输入失败")
                    paste = self._run_tmux([
                        "tmux", "paste-buffer", "-b", buffer_name, "-t", pane_id, "-p", "-d",
                    ])
                    if paste.returncode != 0:
                        raise KiroTerminalUnavailable("Kiro 终端输入失败")
                    buffer_name = None  # -d deleted it
                if enter:
                    if text:
                        # Let the TUI finish consuming a bracketed paste first.
                        self._sleep(0.15)
                    submit = self._run_tmux(["tmux", "send-keys", "-t", pane_id, "Enter"])
                    if submit.returncode != 0:
                        raise KiroTerminalUnavailable("Kiro 终端按键失败")
                    self.mark_submitted()
            finally:
                if buffer_name:
                    try:
                        self._run_tmux(["tmux", "delete-buffer", "-b", buffer_name])
                    except Exception:
                        pass
            self._touch_locked()
            return True

    def resize(self, columns: int, rows: int) -> bool:
        with self._lock:
            pane_id = self._verified_live_pane_locked()
            if pane_id is None:
                return False
            result = self._run_tmux([
                "tmux", "resize-window", "-t", pane_id, "-x", str(int(columns)), "-y", str(int(rows)),
            ])
            if result.returncode != 0:
                raise KiroTerminalUnavailable("Kiro 终端尺寸更新失败")
            self._touch_locked()
            return True

    # ---------- release paths ----------

    def release(self, lease: str | None) -> bool:
        """App release of its exact lease; stale leases are harmless no-ops."""
        candidate = str(lease or "")
        with self._lock:
            if not self._lease or not self._lease_pane or not hmac.compare_digest(candidate, self._lease):
                return False
            current = self._owned_pane_locked()
            released = True
            if current is not None and hmac.compare_digest(current[0], self._lease_pane):
                released = self._shutdown_exact_pane_locked(*current)
            if released:
                self._clear_lease_locked()
            return released

    def release_for_writer(self, session_id: str) -> bool:
        """Hand the session to ACP: tear down an idle TUI, refuse a busy one.

        Works without an in-memory lease (service restart): tmux ownership and
        the bound session fingerprint identify the pane.  A TUI bound to a
        different session is no conflict for ``session_id`` but is still torn
        down when idle, so at most one Kiro writer process exists.
        """
        with self._lock:
            try:
                current = self._owned_pane_locked()
            except KiroTerminalUnavailable as exc:
                raise KiroTerminalBusy("Kiro 终端状态未确认") from exc
            if current is None:
                self._clear_lease_locked()
                return True
            pane_id, pane_dead, pane_pid = current
            if not pane_dead:
                bound = self._bound_fingerprint_locked()
                if not session_id or not hmac.compare_digest(bound, self._fingerprint(session_id)):
                    # The TUI holds a different session (or an unreadable
                    # binding): it is no writer of ``session_id``.  Leave it
                    # to the reaper/next acquire rather than interrupting it.
                    if bound:
                        return True
                    raise KiroTerminalBusy("Kiro 终端状态未确认")
                if self._tui_busy_locked(session_id, pane_id):
                    raise KiroTerminalBusy("Kiro 终端里还有一轮在进行")
            released = self._shutdown_exact_pane_locked(pane_id, pane_pid, pane_dead)
            if released:
                self._clear_lease_locked()
            return released

    def release_for_shutdown(self) -> bool:
        with self._lock:
            try:
                current = self._owned_pane_locked()
            except KiroTerminalUnavailable:
                return False
            released = True
            if current is not None:
                released = self._shutdown_exact_pane_locked(*current)
            if released:
                self._clear_lease_locked()
            return released

    def _reap_if_idle(self) -> None:
        """Tear down a TUI nobody has polled for ``idle_seconds`` once idle."""
        with self._input_lock:
            with self._lock:
                self._timer = None
                if self._lease is None:
                    return
                remaining = self.idle_seconds - (time.monotonic() - self._last_activity)
                if remaining > 0:
                    self._schedule_reaper_locked(remaining)
                    return
                try:
                    current = self._owned_pane_locked()
                    if current is None:
                        self._clear_lease_locked()
                        return
                    if not current[1] and self._session_id and self._tui_busy_locked(self._session_id, current[0]):
                        # A detached terminal turn keeps running; check again later.
                        self._schedule_reaper_locked(self.idle_seconds)
                        return
                    if self._shutdown_exact_pane_locked(*current):
                        self._clear_lease_locked()
                except Exception:
                    logger.warning("Kiro terminal idle reaper failed", exc_info=True)
                    self._schedule_reaper_locked(self.idle_seconds)
