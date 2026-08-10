#!/usr/bin/env python3
"""Fail-closed systemd wrapper for the shared Codex app-server daemon.

The official CLI can print enrollment material, so its output is always
captured and discarded.  More importantly, ``stop`` never calls the CLI until
the PID record and the target process have matched the remote-control daemon
twice.  A reused PID is therefore never handed to a destructive stop path.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Sequence


DEFAULT_CODEX_HOME = Path("/root/.codex")
DEFAULT_CODEX = DEFAULT_CODEX_HOME / "packages/standalone/current/bin/codex"
DEFAULT_LOCK = Path("/run/lock/codex-remote-control-service.lock")
DEFAULT_PROC_ROOT = Path("/proc")
DEFAULT_COMMAND_TIMEOUT = 45.0
DEFAULT_STATE_TIMEOUT = 15.0

# A takeover can stop and start the official daemon.  Each phase gets one CLI
# timeout and one state-settle timeout, so systemd's start budget must exceed
# 2 * (DEFAULT_COMMAND_TIMEOUT + DEFAULT_STATE_TIMEOUT).  A plain stop needs
# one such phase.  The tracked unit keeps another ten seconds for interpreter
# startup, flock acquisition, and systemd bookkeeping.
WORST_CASE_TAKEOVER_TIMEOUT = 2 * (DEFAULT_COMMAND_TIMEOUT + DEFAULT_STATE_TIMEOUT)
WORST_CASE_STOP_TIMEOUT = DEFAULT_COMMAND_TIMEOUT + DEFAULT_STATE_TIMEOUT
SYSTEMD_TIMEOUT_MARGIN = 10.0


@dataclass(frozen=True)
class ExecutableIdentity:
    device: int
    inode: int
    uid: int


@dataclass(frozen=True)
class PidRecord:
    pid: int
    file_device: int
    file_inode: int
    process_starttime: int | None
    argv: tuple[bytes, ...]
    executable: ExecutableIdentity | None
    process_uid: int | None
    cgroups: tuple[str, ...]

    def is_live_remote(self, managed: ExecutableIdentity, service_uid: int) -> bool:
        return (
            self.process_starttime is not None
            and b"app-server" in self.argv
            and b"--remote-control" in self.argv
            and self.executable is not None
            and (self.executable.device, self.executable.inode)
            == (managed.device, managed.inode)
            and self.process_uid == service_uid
        )


def daemon_pid_path(codex_home: Path) -> Path:
    return codex_home / "app-server-daemon/app-server.pid"


def trusted_executable_identity(path: Path, trust_root: Path) -> ExecutableIdentity | None:
    """Resolve a configured binary beneath a root-owned, non-writable anchor.

    Release payloads may be owned by their installer account (currently uid
    1001), while the systemd unit deliberately runs the daemon as root.  The
    payload owner is therefore independent from the process owner.  Safety is
    provided by an exact dev/inode match plus a chain where no directory or
    file is writable by group/other and every descendant is owned by either
    root or the payload owner.
    """
    try:
        anchor = trust_root.resolve(strict=True)
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
    return ExecutableIdentity(target_info.st_dev, target_info.st_ino, target_info.st_uid)


def _parse_starttime(stat_text: str) -> int:
    # Field 2 is parenthesized and may itself contain spaces or ')'.  The
    # fields after its final ')' begin at field 3; starttime is field 22.
    tail = stat_text[stat_text.rfind(")") + 2 :].split()
    return int(tail[19])


def _read_process(pid: int, proc_root: Path) -> tuple[
    int | None, tuple[bytes, ...], ExecutableIdentity | None, int | None, tuple[str, ...]
]:
    process_dir = proc_root / str(pid)
    try:
        stat_text = (process_dir / "stat").read_text(encoding="utf-8")
        starttime = _parse_starttime(stat_text)
        argv = tuple(
            part
            for part in (process_dir / "cmdline").read_bytes().split(b"\0")
            if part
        )
        exe_info = (process_dir / "exe").stat()
        proc_info = process_dir.stat()
        cgroups = process_cgroups(pid, proc_root)
        # The final starttime read binds argv/exe/uid/cgroup to the same PID
        # incarnation.  Any exec/reuse race is treated as an unknown identity.
        if _parse_starttime((process_dir / "stat").read_text(encoding="utf-8")) != starttime:
            return None, (), None, None, ()
    except (OSError, ValueError, IndexError):
        return None, (), None, None, ()
    return (
        starttime,
        argv,
        ExecutableIdentity(exe_info.st_dev, exe_info.st_ino, exe_info.st_uid),
        proc_info.st_uid,
        cgroups,
    )


def read_pid_record(codex_home: Path, proc_root: Path = DEFAULT_PROC_ROOT) -> PidRecord | None:
    path = daemon_pid_path(codex_home)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None
        raw = os.read(descriptor, 4097)
        if len(raw) > 4096:
            return None
        payload = json.loads(raw.decode("utf-8"))
        pid = int(payload["pid"])
        if pid <= 1:
            return None
        starttime, argv, executable, process_uid, cgroups = _read_process(pid, proc_root)
        # Ensure the pidfile itself did not change while /proc was sampled.
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 4097) != raw:
            return None
        return PidRecord(
            pid, info.st_dev, info.st_ino, starttime, argv, executable, process_uid, cgroups
        )
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)


def record_is_unchanged(
    expected: PidRecord,
    codex_home: Path,
    proc_root: Path = DEFAULT_PROC_ROOT,
) -> bool:
    return read_pid_record(codex_home, proc_root) == expected


def process_cgroups(pid: int, proc_root: Path = DEFAULT_PROC_ROOT) -> tuple[str, ...]:
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    paths = []
    for line in lines:
        parts = line.split(":", 2)
        # Prefer the unified hierarchy (v2) or systemd's named controller
        # (v1); unrelated controllers can legitimately share '/'.
        controllers = parts[1].split(",") if len(parts) == 3 else []
        is_systemd_hierarchy = len(parts) == 3 and parts[0] == "0" and parts[1] == ""
        is_systemd_hierarchy = is_systemd_hierarchy or "name=systemd" in controllers
        if is_systemd_hierarchy and parts[2].startswith("/"):
            paths.append(parts[2].rstrip("/") or "/")
    return tuple(paths)


def daemon_is_in_current_cgroup(record: PidRecord, proc_root: Path = DEFAULT_PROC_ROOT) -> bool:
    daemon_groups = set(record.cgroups)
    current_groups = set(process_cgroups(os.getpid(), proc_root))
    return bool(daemon_groups and current_groups and daemon_groups.intersection(current_groups))


def retire_stale_pid_record(
    expected: PidRecord,
    managed: ExecutableIdentity,
    service_uid: int,
    codex_home: Path,
    proc_root: Path = DEFAULT_PROC_ROOT,
) -> bool:
    """Remove only the exact stale record observed twice; never signal its PID."""
    if expected.is_live_remote(managed, service_uid) or not record_is_unchanged(
        expected, codex_home, proc_root
    ):
        return False
    path = daemon_pid_path(codex_home)
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) != (expected.file_device, expected.file_inode):
            return False
        path.unlink()
    except OSError:
        return False
    return True


def run_cli(codex: Path, codex_home: Path, action: str, timeout: float) -> bool:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        completed = subprocess.run(
            [str(codex), "remote-control", action, "--json"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def wait_for_state(
    codex_home: Path,
    managed: ExecutableIdentity,
    service_uid: int,
    running: bool,
    timeout: float,
    proc_root: Path = DEFAULT_PROC_ROOT,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = read_pid_record(codex_home, proc_root)
        if bool(record and record.is_live_remote(managed, service_uid)) == running:
            return True
        time.sleep(0.05)
    return False


def open_secure_lock(path: Path):
    """Open a root-owned regular 0600 lock without following symlinks."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PermissionError("unsafe recovery lock identity")
        return os.fdopen(descriptor, "a+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(DEFAULT_CODEX_HOME))),
    )
    parser.add_argument(
        "--codex",
        type=Path,
        default=Path(os.environ.get("CODEX_REMOTE_CODEX", str(DEFAULT_CODEX))),
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--proc-root", type=Path, default=DEFAULT_PROC_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--service-uid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--command-timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--state-timeout", type=float, default=DEFAULT_STATE_TIMEOUT,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    args.lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_file = open_secure_lock(args.lock)
    except OSError:
        print("Codex Remote Control: unsafe service lock", file=sys.stderr)
        return 1
    with lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        managed = trusted_executable_identity(args.codex, args.codex_home)
        if managed is None or args.service_uid < 0:
            print("Codex Remote Control: managed binary identity is unsafe", file=sys.stderr)
            return 1
        first = read_pid_record(args.codex_home, args.proc_root)
        if args.action == "status":
            return 0 if first is not None and first.is_live_remote(managed, args.service_uid) else 1

        if args.action == "stop":
            # Missing/dead/non-daemon records are already safely stopped.  Do
            # not pass their PID to the official stop implementation.
            if first is None or not first.is_live_remote(managed, args.service_uid):
                return 0
            if not record_is_unchanged(first, args.codex_home, args.proc_root):
                print("Codex Remote Control: daemon identity changed before stop", file=sys.stderr)
                return 1
        elif first is not None and first.is_live_remote(managed, args.service_uid):
            if daemon_is_in_current_cgroup(first, args.proc_root):
                return 0
            # An existing daemon outside this service cgroup would still die
            # with its previous owner.  Revalidate immediately before asking
            # the official CLI to stop it, then relaunch from this unit.
            if not record_is_unchanged(first, args.codex_home, args.proc_root):
                print("Codex Remote Control: daemon identity changed before takeover", file=sys.stderr)
                return 1
            if not run_cli(args.codex, args.codex_home, "stop", args.command_timeout):
                print("Codex Remote Control: takeover stop command failed", file=sys.stderr)
                return 1
            if not wait_for_state(
                args.codex_home, managed, args.service_uid, running=False,
                timeout=args.state_timeout, proc_root=args.proc_root
            ):
                print("Codex Remote Control: takeover stop health check failed", file=sys.stderr)
                return 1
        elif first is not None:
            if not retire_stale_pid_record(
                first, managed, args.service_uid, args.codex_home, args.proc_root
            ):
                print("Codex Remote Control: stale pid record changed before start", file=sys.stderr)
                return 1

        if not run_cli(args.codex, args.codex_home, args.action, args.command_timeout):
            print(f"Codex Remote Control: {args.action} command failed", file=sys.stderr)
            return 1
        if not wait_for_state(
            args.codex_home,
            managed,
            args.service_uid,
            running=args.action == "start",
            timeout=args.state_timeout,
            proc_root=args.proc_root,
        ):
            print(f"Codex Remote Control: {args.action} health check failed", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
