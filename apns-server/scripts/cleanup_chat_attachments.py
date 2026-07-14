#!/usr/bin/env python3
"""Crash-recoverable retention cleanup for the flat chat attachment store.

The CLI is read-only unless ``--apply`` is supplied.  It never traverses
subdirectories and never touches chat history, appearance assets, avatars,
wallpapers, or ``state/uploads``.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "tokens" / "attachments"
NANOSECONDS_PER_DAY = 86_400 * 1_000_000_000
QUARANTINE_PREFIX = ".cc-attachment-cleanup-quarantine-"
JOURNAL_PREFIX = ".cc-attachment-cleanup-journal-"
RENAME_NOREPLACE = 1
JOURNAL_VERSION = 1
MAX_JOURNAL_BYTES = 4096
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_IDENTITY_KEYS = ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns")


class CleanupError(RuntimeError):
    """Raised when a fail-closed safety check prevents cleanup."""


@dataclass
class CleanupSummary:
    scanned: int = 0
    candidates: int = 0
    removed: int = 0
    candidate_bytes: int = 0
    removed_bytes: int = 0
    skipped_non_regular: int = 0
    skipped_reserved: int = 0
    skipped_recovery_protected: int = 0
    skipped_changed: int = 0
    errors: int = 0
    journals_seen: int = 0
    recovery_removed: int = 0
    recovery_removed_bytes: int = 0
    recovery_restored: int = 0
    recovery_cleared: int = 0
    recovery_pending: int = 0
    journal_retained: int = 0
    scan_limit_reached: bool = False
    delete_limit_reached: bool = False
    recovery_limit_reached: bool = False


def _identity(value: os.stat_result) -> dict[str, int]:
    return {
        "dev": value.st_dev,
        "ino": value.st_ino,
        "mode": value.st_mode,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def _matches_quarantined_identity(
    value: os.stat_result, expected: dict[str, int]
) -> bool:
    """Match an inode after rename, which legitimately advances its ctime."""
    actual = _identity(value)
    return (
        all(actual[key] == expected[key] for key in _IDENTITY_KEYS if key != "ctime_ns")
        and actual["ctime_ns"] >= expected["ctime_ns"]
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _open_checked_root(root: Path) -> int:
    try:
        before = root.lstat()
    except OSError as exc:
        raise CleanupError(f"attachment root is unavailable: {exc.strerror}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise CleanupError("attachment root must not be a symlink")
    if not stat.S_ISDIR(before.st_mode):
        raise CleanupError("attachment root is not a directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise CleanupError(f"cannot open attachment root safely: {exc.strerror}") from exc
    after = os.fstat(root_fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(root_fd)
        raise CleanupError("attachment root changed while it was opened")
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(root_fd)
        raise CleanupError("another attachment cleanup is already running") from exc
    return root_fd


def _rename_noreplace(source: str, destination: str, *, dir_fd: int) -> None:
    """Atomically rename within ``dir_fd`` without clobbering destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
        dir_fd, os.fsencode(source), dir_fd, os.fsencode(destination), RENAME_NOREPLACE
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _fsync_directory(root_fd: int) -> None:
    os.fsync(root_fd)


def _is_safe_basename(name: object) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name.encode("utf-8")) <= 255
        and name not in {".", ".."}
        and "/" not in name
        and "\x00" not in name
    )


def _is_reserved(name: str) -> bool:
    return name.startswith(QUARANTINE_PREFIX) or name.startswith(JOURNAL_PREFIX)


def _names_for_token(token: str) -> tuple[str, str]:
    return f"{QUARANTINE_PREFIX}{token}", f"{JOURNAL_PREFIX}{token}.json"


def _write_journal(root_fd: int, original: str, expected: dict[str, int]) -> tuple[str, str]:
    """Durably create a private intent journal before any attachment rename."""
    for _attempt in range(8):
        token = uuid.uuid4().hex
        quarantine, journal = _names_for_token(token)
        payload = json.dumps(
            {
                "version": JOURNAL_VERSION,
                "original": original,
                "quarantine": quarantine,
                "expected": expected,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            journal_fd = os.open(journal, flags, 0o600, dir_fd=root_fd)
        except FileExistsError:
            continue
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(journal_fd, payload[offset:])
            os.fsync(journal_fd)
        except BaseException:
            os.close(journal_fd)
            try:
                os.unlink(journal, dir_fd=root_fd)
                _fsync_directory(root_fd)
            except OSError:
                pass
            raise
        else:
            os.close(journal_fd)
        _fsync_directory(root_fd)
        return quarantine, journal
    raise CleanupError("could not allocate a unique recovery journal")


def _remove_journal(root_fd: int, journal: str) -> None:
    os.unlink(journal, dir_fd=root_fd)
    _fsync_directory(root_fd)


def _load_journal(root_fd: int, journal: str) -> dict[str, object]:
    match = re.fullmatch(re.escape(JOURNAL_PREFIX) + r"([0-9a-f]{32})\.json", journal)
    if not match:
        raise CleanupError("invalid recovery journal name")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(journal, flags, dir_fd=root_fd)
    try:
        journal_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(journal_stat.st_mode)
            or journal_stat.st_nlink != 1
            or journal_stat.st_size <= 0
            or journal_stat.st_size > MAX_JOURNAL_BYTES
            or journal_stat.st_mode & 0o077
        ):
            raise CleanupError("unsafe recovery journal metadata")
        chunks: list[bytes] = []
        remaining = journal_stat.st_size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise CleanupError("truncated recovery journal")
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError("corrupt recovery journal") from exc
    if not isinstance(data, dict) or data.get("version") != JOURNAL_VERSION:
        raise CleanupError("unsupported recovery journal")
    original = data.get("original")
    quarantine = data.get("quarantine")
    expected = data.get("expected")
    token = match.group(1)
    expected_quarantine, _expected_journal = _names_for_token(token)
    if (
        not _is_safe_basename(original)
        or _is_reserved(original)
        or quarantine != expected_quarantine
        or not isinstance(expected, dict)
        or set(expected) != set(_IDENTITY_KEYS)
        or not all(isinstance(expected[key], int) and expected[key] >= 0 for key in _IDENTITY_KEYS)
        or not stat.S_ISREG(expected["mode"])
    ):
        raise CleanupError("invalid recovery journal fields")
    return data


def _recover_journals(
    root_fd: int,
    summary: CleanupSummary,
    *,
    apply: bool,
    max_recovery: int,
) -> set[str]:
    protected_names: set[str] = set()
    journals: list[str] = []
    with os.scandir(root_fd) as entries:
        for entry in entries:
            if entry.name.startswith(JOURNAL_PREFIX):
                journals.append(entry.name)
    journals.sort()
    for journal in journals:
        if summary.journals_seen >= max_recovery:
            summary.recovery_limit_reached = True
            break
        summary.journals_seen += 1
        try:
            data = _load_journal(root_fd, journal)
        except (CleanupError, OSError):
            summary.journal_retained += 1
            summary.errors += 1
            continue
        original = data["original"]
        quarantine = data["quarantine"]
        expected = data["expected"]
        # Do not reconsider a name whose interrupted state was recovered during
        # this same scan.  In particular, a restored replacement is user data,
        # not the stale inode described by the journal.
        protected_names.add(original)
        if not apply:
            summary.recovery_pending += 1
            continue
        try:
            captured = os.stat(quarantine, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                _remove_journal(root_fd, journal)
            except OSError:
                summary.journal_retained += 1
                summary.errors += 1
            else:
                summary.recovery_cleared += 1
            continue
        except OSError:
            summary.journal_retained += 1
            summary.errors += 1
            continue

        if _matches_quarantined_identity(captured, expected):
            try:
                os.unlink(quarantine, dir_fd=root_fd)
                _fsync_directory(root_fd)
            except OSError:
                summary.journal_retained += 1
                summary.errors += 1
                continue
            summary.recovery_removed += 1
            summary.recovery_removed_bytes += captured.st_size
            try:
                _remove_journal(root_fd, journal)
            except OSError:
                summary.journal_retained += 1
                summary.errors += 1
            continue

        try:
            _rename_noreplace(quarantine, original, dir_fd=root_fd)
            _fsync_directory(root_fd)
        except OSError:
            # Destination occupied or state ambiguous: retain both the captured
            # entry and its journal for manual inspection, never overwrite/delete.
            summary.journal_retained += 1
            summary.errors += 1
            continue
        summary.recovery_restored += 1
        try:
            _remove_journal(root_fd, journal)
        except OSError:
            summary.journal_retained += 1
            summary.errors += 1
    return protected_names


def cleanup(
    root: Path,
    *,
    older_than_days: int = 30,
    apply: bool = False,
    max_files: int = 1_000,
    max_scan: int = 100_000,
    max_recovery: int = 1_000,
    now_ns: int | None = None,
    _before_quarantine: Callable[[int, str], None] | None = None,
    _before_restore: Callable[[int, str, str], None] | None = None,
    _after_journal: Callable[[int, str, str, str], None] | None = None,
    _after_rename: Callable[[int, str, str, str], None] | None = None,
    _after_delete: Callable[[int, str, str], None] | None = None,
) -> CleanupSummary:
    """Find or remove stale top-level regular files inside ``root``."""
    if older_than_days < 1:
        raise ValueError("older_than_days must be at least 1")
    if min(max_files, max_scan, max_recovery) < 1:
        raise ValueError("cleanup limits must be at least 1")
    now_ns = time.time_ns() if now_ns is None else now_ns
    cutoff_ns = now_ns - older_than_days * NANOSECONDS_PER_DAY
    summary = CleanupSummary()
    root_fd = _open_checked_root(root)
    try:
        protected_names = _recover_journals(
            root_fd, summary, apply=apply, max_recovery=max_recovery
        )
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if summary.scanned >= max_scan:
                    summary.scan_limit_reached = True
                    break
                summary.scanned += 1
                if _is_reserved(entry.name):
                    summary.skipped_reserved += 1
                    continue
                if entry.name in protected_names:
                    summary.skipped_recovery_protected += 1
                    continue
                try:
                    initial = entry.stat(follow_symlinks=False)
                except OSError:
                    summary.errors += 1
                    continue
                if not stat.S_ISREG(initial.st_mode):
                    summary.skipped_non_regular += 1
                    continue
                if max(initial.st_mtime_ns, initial.st_ctime_ns) >= cutoff_ns:
                    continue
                summary.candidates += 1
                summary.candidate_bytes += initial.st_size
                if not apply:
                    continue
                if summary.removed >= max_files:
                    summary.delete_limit_reached = True
                    continue
                try:
                    current = os.stat(entry.name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    summary.skipped_changed += 1
                    continue
                except OSError:
                    summary.errors += 1
                    continue
                if not stat.S_ISREG(current.st_mode) or not _same_file(initial, current):
                    summary.skipped_changed += 1
                    continue

                quarantine = journal = ""
                try:
                    quarantine, journal = _write_journal(
                        root_fd, entry.name, _identity(initial)
                    )
                    protected_names.add(entry.name)
                    if _after_journal is not None:
                        _after_journal(root_fd, entry.name, quarantine, journal)
                    if _before_quarantine is not None:
                        _before_quarantine(root_fd, entry.name)
                    _rename_noreplace(entry.name, quarantine, dir_fd=root_fd)
                    _fsync_directory(root_fd)
                    if _after_rename is not None:
                        _after_rename(root_fd, entry.name, quarantine, journal)
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)) or not isinstance(exc, Exception):
                        raise
                    # renameat2 either happened completely or not at all.  If the
                    # quarantine name is absent, the durable marker is safe to clear.
                    if isinstance(exc, FileNotFoundError):
                        summary.skipped_changed += 1
                    else:
                        summary.errors += 1
                    if quarantine and journal:
                        try:
                            os.stat(quarantine, dir_fd=root_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            try:
                                _remove_journal(root_fd, journal)
                            except OSError:
                                summary.journal_retained += 1
                                summary.errors += 1
                        except OSError:
                            summary.journal_retained += 1
                            summary.errors += 1
                        else:
                            summary.journal_retained += 1
                    continue

                try:
                    captured = os.stat(quarantine, dir_fd=root_fd, follow_symlinks=False)
                except OSError:
                    summary.skipped_changed += 1
                    summary.journal_retained += 1
                    summary.errors += 1
                    continue
                if not _matches_quarantined_identity(captured, _identity(initial)):
                    summary.skipped_changed += 1
                    if _before_restore is not None:
                        _before_restore(root_fd, entry.name, quarantine)
                    try:
                        _rename_noreplace(quarantine, entry.name, dir_fd=root_fd)
                        _fsync_directory(root_fd)
                        _remove_journal(root_fd, journal)
                    except OSError:
                        summary.journal_retained += 1
                        summary.errors += 1
                    continue

                try:
                    final = os.stat(quarantine, dir_fd=root_fd, follow_symlinks=False)
                    if not _same_file(captured, final):
                        summary.skipped_changed += 1
                        summary.journal_retained += 1
                        summary.errors += 1
                        continue
                    os.unlink(quarantine, dir_fd=root_fd)
                    _fsync_directory(root_fd)
                    if _after_delete is not None:
                        _after_delete(root_fd, entry.name, journal)
                    _remove_journal(root_fd, journal)
                except OSError:
                    summary.journal_retained += 1
                    summary.errors += 1
                else:
                    summary.removed += 1
                    summary.removed_bytes += current.st_size
    finally:
        os.close(root_fd)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean old CcCompanion chat attachments.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--max-files", type=int, default=1_000)
    parser.add_argument("--max-scan", type=int, default=100_000)
    parser.add_argument("--max-recovery", type=int, default=1_000)
    parser.add_argument("--apply", action="store_true", help="delete; default is dry-run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = cleanup(
            args.root,
            older_than_days=args.older_than_days,
            apply=args.apply,
            max_files=args.max_files,
            max_scan=args.max_scan,
            max_recovery=args.max_recovery,
        )
    except (CleanupError, ValueError) as exc:
        print(f"chat_attachment_cleanup status=error reason={exc}", file=sys.stderr)
        return 2
    # Aggregate-only: never emit attachment names, paths, journal data, or content.
    fields = {
        "mode": "apply" if args.apply else "dry-run",
        **summary.__dict__,
    }
    print("chat_attachment_cleanup " + " ".join(
        f"{key}={str(value).lower() if isinstance(value, bool) else value}"
        for key, value in fields.items()
    ))
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
