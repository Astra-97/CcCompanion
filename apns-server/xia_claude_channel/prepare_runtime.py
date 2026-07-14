#!/usr/bin/env python3
"""Fail-closed preparation of the isolated Xia Claude channel runtime."""
from __future__ import annotations

import ctypes
import errno
import json
import os
import pwd
import re
import secrets
import stat
import sys
from pathlib import Path

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
RENAME_NOREPLACE = 1
TOKEN_RE = re.compile(rb"[0-9a-f]{64}\n?\Z")


class PrepareError(RuntimeError):
    pass


def _check_dir(info: os.stat_result, *, uid: int, gid: int, mode: int, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise PrepareError(f"{label} is not a directory")
    if (info.st_uid, info.st_gid) != (uid, gid):
        raise PrepareError(f"{label} has unexpected owner")
    if stat.S_IMODE(info.st_mode) != mode:
        raise PrepareError(f"{label} has unexpected permissions")


def _same_entry(parent_fd: int, name: str, opened: os.stat_result, label: str) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise PrepareError(f"{label} changed while preparing")


def _ensure_dir(parent_fd: int, name: str, *, uid: int, gid: int, mode: int, label: str) -> int:
    created = False
    try:
        fd = os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, mode, dir_fd=parent_fd)
        created = True
        fd = os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        raise PrepareError(f"refusing unsafe {label}: {error.strerror}") from error
    try:
        if created:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
        info = os.fstat(fd)
        _check_dir(info, uid=uid, gid=gid, mode=mode, label=label)
        _same_entry(parent_fd, name, info, label)
        if created:
            os.fsync(parent_fd)
        return fd
    except Exception:
        os.close(fd)
        raise


def _rename_noreplace(dir_fd: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PrepareError("renameat2(RENAME_NOREPLACE) is required")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(dir_fd, os.fsencode(source), dir_fd, os.fsencode(target), RENAME_NOREPLACE) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise PrepareError(f"refusing to replace concurrently created {target}")
        raise OSError(error, os.strerror(error), target)


def _read_fd(fd: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise PrepareError("managed file exceeds size limit")


def _validate_file_fd(fd: int, *, uid: int, gid: int, mode: int, label: str, maximum: int) -> tuple[os.stat_result, bytes]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise PrepareError(f"{label} is not a regular file")
    if (info.st_uid, info.st_gid) != (uid, gid):
        raise PrepareError(f"{label} has unexpected owner")
    if stat.S_IMODE(info.st_mode) != mode:
        raise PrepareError(f"{label} has unexpected permissions")
    if info.st_size > maximum:
        raise PrepareError(f"{label} exceeds size limit")
    os.lseek(fd, 0, os.SEEK_SET)
    return info, _read_fd(fd, maximum)


def _open_existing_file(parent_fd: int, name: str, *, uid: int, gid: int, mode: int, label: str,
                        maximum: int) -> tuple[os.stat_result, bytes] | None:
    try:
        fd = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PrepareError(f"refusing unsafe {label}: {error.strerror}") from error
    try:
        info, data = _validate_file_fd(fd, uid=uid, gid=gid, mode=mode, label=label, maximum=maximum)
        _same_entry(parent_fd, name, info, label)
        return info, data
    finally:
        os.close(fd)


def _create_file(parent_fd: int, name: str, data: bytes, *, uid: int, gid: int, mode: int, label: str) -> None:
    temp = f".{name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode, dir_fd=parent_fd)
    temp_info: os.stat_result | None = None
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        temp_info = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        _rename_noreplace(parent_fd, temp, name)
        os.fsync(parent_fd)
        existing = _open_existing_file(parent_fd, name, uid=uid, gid=gid, mode=mode, label=label,
                                       maximum=max(len(data), 1))
        if existing is None or temp_info is None or (existing[0].st_dev, existing[0].st_ino) != (temp_info.st_dev, temp_info.st_ino):
            raise PrepareError(f"{label} changed while publishing")
        if existing[1] != data:
            raise PrepareError(f"{label} changed while publishing")
    finally:
        try:
            os.unlink(temp, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _validate_token(data: bytes) -> None:
    if not TOKEN_RE.fullmatch(data):
        raise PrepareError("channel token has invalid contents")


def _validate_onboarding(data: bytes) -> None:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrepareError("Claude onboarding config is invalid JSON") from error
    if not isinstance(value, dict) or value.get("hasCompletedOnboarding") is not True:
        raise PrepareError("Claude onboarding config is missing the completed marker")


def _read_template(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    except OSError as error:
        raise PrepareError("onboarding template is unavailable") from error
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise PrepareError("onboarding template changed while reading")
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 4096:
            raise PrepareError("onboarding template is unsafe")
        data = _read_fd(fd, 4096)
    finally:
        os.close(fd)
    _validate_onboarding(data)
    return data


def prepare_runtime(*, root: Path, relay_uid: int, relay_gid: int, root_uid: int, root_gid: int,
                    onboarding_template: Path) -> None:
    root_fd = os.open(root, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    opened: list[int] = [root_fd]
    try:
        var_fd = _ensure_dir(root_fd, "var", uid=root_uid, gid=root_gid, mode=0o755, label="runtime /var")
        opened.append(var_fd)
        lib_fd = _ensure_dir(var_fd, "lib", uid=root_uid, gid=root_gid, mode=0o755, label="runtime /var/lib")
        opened.append(lib_fd)
        relay_fd = _ensure_dir(lib_fd, "cc-xia-relay", uid=relay_uid, gid=relay_gid, mode=0o700,
                               label="relay runtime root")
        opened.append(relay_fd)
        state_fd = _ensure_dir(relay_fd, "channel-state", uid=relay_uid, gid=relay_gid, mode=0o700,
                               label="channel state")
        opened.append(state_fd)
        tmux_fd = _ensure_dir(state_fd, "tmux", uid=relay_uid, gid=relay_gid, mode=0o700,
                              label="channel tmux state")
        opened.append(tmux_fd)
        tmux_user_fd = _ensure_dir(tmux_fd, f"tmux-{relay_uid}", uid=relay_uid, gid=relay_gid, mode=0o700,
                                   label="channel tmux user socket directory")
        opened.append(tmux_user_fd)
        home_fd = _ensure_dir(relay_fd, "claude-channel-home", uid=relay_uid, gid=relay_gid, mode=0o700,
                              label="Claude channel HOME")
        opened.append(home_fd)
        config_fd = _ensure_dir(home_fd, ".claude", uid=relay_uid, gid=relay_gid, mode=0o700,
                                label="Claude config directory")
        opened.append(config_fd)
        workspace_fd = _ensure_dir(relay_fd, "workspace", uid=root_uid, gid=root_gid, mode=0o755,
                                   label="read-only workspace mountpoint")
        opened.append(workspace_fd)

        token = _open_existing_file(state_fd, "channel.token", uid=relay_uid, gid=relay_gid, mode=0o600,
                                    label="channel token", maximum=4096)
        if token is None:
            _create_file(state_fd, "channel.token", f"{secrets.token_hex(32)}\n".encode(),
                         uid=relay_uid, gid=relay_gid, mode=0o600, label="channel token")
        else:
            _validate_token(token[1])

        onboarding = _open_existing_file(config_fd, ".claude.json", uid=relay_uid, gid=relay_gid, mode=0o600,
                                         label="Claude onboarding config", maximum=2 * 1024 * 1024)
        if onboarding is None:
            _create_file(config_fd, ".claude.json", _read_template(onboarding_template), uid=relay_uid,
                         gid=relay_gid, mode=0o600, label="Claude onboarding config")
        else:
            _validate_onboarding(onboarding[1])

        # Re-check every parent/name relationship after publishing. The
        # service must be stopped, but a final inode check also prevents a
        # concurrent rename in a relay-owned parent from looking successful.
        for parent_fd, name, child_fd, label in [
            (root_fd, "var", var_fd, "runtime /var"),
            (var_fd, "lib", lib_fd, "runtime /var/lib"),
            (lib_fd, "cc-xia-relay", relay_fd, "relay runtime root"),
            (relay_fd, "channel-state", state_fd, "channel state"),
            (state_fd, "tmux", tmux_fd, "channel tmux state"),
            (tmux_fd, f"tmux-{relay_uid}", tmux_user_fd, "channel tmux user socket directory"),
            (relay_fd, "claude-channel-home", home_fd, "Claude channel HOME"),
            (home_fd, ".claude", config_fd, "Claude config directory"),
            (relay_fd, "workspace", workspace_fd, "read-only workspace mountpoint"),
        ]:
            _same_entry(parent_fd, name, os.fstat(child_fd), label)
    finally:
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError:
                pass


def main(argv: list[str]) -> int:
    if os.geteuid() != 0 or len(argv) != 2:
        return 64
    account = pwd.getpwnam("cc-xia-relay")
    prepare_runtime(root=Path("/"), relay_uid=account.pw_uid, relay_gid=account.pw_gid,
                    root_uid=0, root_gid=0, onboarding_template=Path(argv[1]))
    print("Runtime prepared with non-secret onboarding state. Provision the isolated credential snapshot manually.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (OSError, PrepareError, KeyError) as error:
        print(f"prepare-runtime: {error}", file=sys.stderr)
        raise SystemExit(78)
