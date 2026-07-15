#!/usr/bin/env python3
"""Durable, Xia-home-only native session state helpers for launcher.sh."""
from __future__ import annotations
import json, os, re, secrets, shutil, socket, stat, subprocess, sys, time, uuid
from pathlib import Path
from typing import Callable

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
LEGACY_RUNTIME_RE = re.compile(r"runtime-[0-9]+\Z")
TMUX = "/usr/bin/tmux"


class _TmuxQueryTransient(RuntimeError):
    """The captured tmux server/socket is disappearing after a successful kill."""


_TRANSIENT_TMUX_ERRORS = (
    "no server running on",
    "connection refused",
    "connection reset",
    "broken pipe",
    "lost server",
    "server exited unexpectedly",
)


def _tmux_failure_is_transient(result: subprocess.CompletedProcess) -> bool:
    detail = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    if "permission denied" in detail or "operation not permitted" in detail:
        return False
    return any(message in detail for message in _TRANSIENT_TMUX_ERRORS)


def _tmux_query_error(*results: subprocess.CompletedProcess) -> type[RuntimeError]:
    # Successful probes are not relevant. Every failed command must
    # independently identify a known connection-loss transition; one unknown,
    # empty, malformed, or permission result makes the whole query hard-fail.
    failed = [result for result in results if result.returncode != 0]
    if failed and all(_tmux_failure_is_transient(result) for result in failed):
        return _TmuxQueryTransient
    return RuntimeError

def has_transcript(home: str | Path, session_id: str) -> bool:
    root = Path(home) / ".claude" / "projects"
    if not root.is_dir() or not session_id:
        return False
    for candidate in root.rglob(f"{session_id}.jsonl"):
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
            return True
    return False

def write_marker(path: str | Path, generation: int, session_id: str, model: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump({"generation": int(generation), "session_id": session_id, "model": model}, handle)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.chmod(temp, 0o600); os.replace(temp, target); os.chmod(target, 0o600)
    fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(fd)
    finally: os.close(fd)

def _private_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"unsafe runtime directory: {path}")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"runtime directory owner/mode mismatch: {path}")

def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)

def _atomic_bytes(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, 0o600); os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass

def _atomic_symlink(path: Path, target: Path) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    try:
        os.symlink(target, temp)
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        try: temp.unlink()
        except FileNotFoundError: pass

def _cleanup_legacy_runtimes(state_dir: Path) -> list[str]:
    removed: list[str] = []
    for candidate in state_dir.iterdir():
        if not LEGACY_RUNTIME_RE.fullmatch(candidate.name):
            continue
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError(f"unsafe legacy runtime entry: {candidate}")
        shutil.rmtree(candidate)
        removed.append(candidate.name)
    if removed: _fsync_dir(state_dir)
    return removed

def prepare_runtime_workspace(install_dir: str | Path, state_dir: str | Path, workspace: str | Path,
                              generation: int, session_id: str, model: str, bootstrap_token: str) -> Path:
    """Publish one generation into a stable project path after the old TUI exits."""
    install = Path(install_dir)
    state = Path(state_dir)
    _private_dir(state)
    _cleanup_legacy_runtimes(state)
    runtime = state / "runtime"
    _private_dir(runtime)
    claude_dir = runtime / ".claude"
    _private_dir(claude_dir)

    replacements = {
        "@INSTALL_DIR@": str(install),
        "@STATE_DIR@": str(state),
        "@TOKEN_FILE@": str(state / "channel.token"),
        "@GENERATION@": str(int(generation)),
        "@SESSION_ID@": str(session_id),
        "@MODEL@": str(model),
        "@BOOTSTRAP_TOKEN@": str(bootstrap_token),
    }
    mcp_text = (install / ".mcp.json.in").read_text(encoding="utf-8")
    for needle, value in replacements.items():
        mcp_text = mcp_text.replace(needle, value)
    if re.search(r"@[A-Z_]+@", mcp_text):
        raise RuntimeError("unresolved MCP runtime placeholder")
    json.loads(mcp_text)
    settings = (install / "settings.json").read_bytes()
    json.loads(settings.decode("utf-8"))

    # There is no concurrent reader: launcher confirms the dedicated tmux
    # session is gone before invoking this helper. Each individual artifact is
    # still atomically published and fsynced for crash recovery.
    _atomic_bytes(runtime / ".mcp.json", mcp_text.encode("utf-8"))
    _atomic_bytes(claude_dir / "settings.json", settings)
    _atomic_symlink(runtime / "CLAUDE.md", Path(workspace) / "CLAUDE.md")
    return runtime

def _socket_present(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError("unsafe or inaccessible dedicated tmux socket")
    return True

def _validate_socket_parent(path: Path) -> None:
    try:
        info = path.parent.lstat()
    except OSError as error:
        raise RuntimeError("dedicated tmux socket directory is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("unsafe dedicated tmux socket directory")

def _run_tmux(socket_path: Path, *args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner([TMUX, "-S", str(socket_path), *args], capture_output=True, text=True, timeout=2)

def _query_session_pids(socket_path: Path, session: str, *, runner=subprocess.run) -> list[int] | None:
    if not _socket_present(socket_path):
        return None
    probe = _run_tmux(socket_path, "has-session", "-t", session, runner=runner)
    if probe.returncode == 0:
        panes = _run_tmux(socket_path, "list-panes", "-t", session, "-F", "#{pane_pid}", runner=runner)
        if panes.returncode != 0:
            error = _tmux_query_error(panes)
            raise error("dedicated tmux pane query failed")
        try:
            pids = [int(line) for line in panes.stdout.splitlines() if line.strip()]
        except ValueError as error:
            raise RuntimeError("dedicated tmux returned an invalid pane pid") from error
        if not pids or any(pid <= 1 for pid in pids):
            raise RuntimeError("dedicated tmux returned no safe pane pid")
        return pids

    # has-session uses the same nonzero status for "not found" and connection
    # failures. Only a vanished socket or a successful authoritative session
    # listing may prove that the dedicated session is absent.
    if not _socket_present(socket_path):
        return None
    listing = _run_tmux(socket_path, "list-sessions", "-F", "#{session_name}", runner=runner)
    if listing.returncode != 0:
        if not _socket_present(socket_path):
            return None
        error = _tmux_query_error(probe, listing)
        raise error("dedicated tmux session query failed")
    names = {line.strip() for line in listing.stdout.splitlines() if line.strip()}
    if session in names:
        raise RuntimeError("dedicated tmux session probe was inconsistent")
    if names:
        raise RuntimeError("unexpected session exists on dedicated tmux socket")
    return None

def _proc_stat(pid: int) -> tuple[int, int, int] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = text.rsplit(")", 1)[1].split()
    if len(fields) < 20:
        raise RuntimeError("invalid /proc process identity")
    if fields[0] in {"Z", "X"}:
        return None
    return int(fields[1]), int(fields[2]), int(fields[19])  # ppid, pgrp, starttime

def _capture_process_guard(pane_pids: list[int], *, proc_reader=_proc_stat) -> dict:
    identities: dict[int, tuple[int, int]] = {}
    pgrps: dict[int, int | None] = {}
    pending = list(pane_pids)
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen: continue
        seen.add(pid)
        value = proc_reader(pid)
        if value is None:
            raise RuntimeError("dedicated tmux pane process disappeared before kill")
        _ppid, pgrp, starttime = value
        identities[pid] = (pgrp, starttime)
        leader = proc_reader(pgrp)
        pgrps[pgrp] = leader[2] if leader is not None else None
        for candidate in Path("/proc").iterdir():
            if not candidate.name.isdigit(): continue
            child = int(candidate.name)
            child_value = proc_reader(child)
            if child_value is not None and child_value[0] == pid and child not in seen:
                pending.append(child)
    return {"identities": identities, "pgrps": pgrps}

def _process_guard_alive(guard: dict, *, proc_reader=_proc_stat) -> bool:
    for pid, (_pgrp, starttime) in guard["identities"].items():
        current = proc_reader(int(pid))
        if current is not None and current[2] == starttime:
            return True
    for pgrp, leader_start in guard["pgrps"].items():
        leader = proc_reader(int(pgrp))
        if leader is not None and leader[2] != leader_start:
            # The numeric process-group id was reused after the old group died.
            continue
        for candidate in Path("/proc").iterdir():
            if not candidate.name.isdigit(): continue
            value = proc_reader(int(candidate.name))
            if value is not None and value[1] == int(pgrp):
                return True
    return False

def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.15):
            return True
    except OSError:
        return False

def _capture_tmux_server_guard(path: Path, *, runner=subprocess.run, proc_reader=_proc_stat) -> dict:
    response = _run_tmux(path, "display-message", "-p", "#{pid}", runner=runner)
    try:
        pid = int(response.stdout.strip()) if response.returncode == 0 else 0
    except ValueError as error:
        raise RuntimeError("dedicated tmux returned an invalid server pid") from error
    identity = proc_reader(pid) if pid > 1 else None
    if identity is None:
        raise RuntimeError("dedicated tmux server identity is unavailable")
    socket_info = path.lstat()
    return {"pid": pid, "starttime": identity[2], "socket_dev": socket_info.st_dev, "socket_ino": socket_info.st_ino}

def _retire_stale_tmux_socket(path: Path, guard: dict, *, proc_reader=_proc_stat) -> bool:
    server = proc_reader(int(guard["pid"]))
    if server is not None and server[2] == guard["starttime"]:
        return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or
            (info.st_dev, info.st_ino) != (guard["socket_dev"], guard["socket_ino"])):
        raise RuntimeError("dedicated tmux socket changed after server exit")
    path.unlink()
    _fsync_dir(path.parent)
    return not path.exists()

def stop_dedicated_tui(socket_path: str | Path, session: str, *, host: str = "127.0.0.1", port: int = 8821,
                       timeout: float = 5.0, runner=subprocess.run, proc_reader=_proc_stat,
                       port_probe=_port_open, monotonic=time.monotonic, sleeper=time.sleep,
                       server_guard_capture=_capture_tmux_server_guard,
                       stale_socket_retire=_retire_stale_tmux_socket) -> None:
    path = Path(socket_path)
    _validate_socket_parent(path)
    pane_pids = _query_session_pids(path, session, runner=runner)
    if pane_pids is None:
        if port_probe(host, port):
            raise RuntimeError("Claude channel port remains reachable without a verified tmux session")
        return
    guard = _capture_process_guard(pane_pids, proc_reader=proc_reader)
    server_guard = server_guard_capture(path, runner=runner, proc_reader=proc_reader)
    killed = _run_tmux(path, "kill-session", "-t", session, runner=runner)
    if killed.returncode != 0:
        raise RuntimeError("dedicated tmux kill-session failed")

    deadline = monotonic() + timeout
    while True:
        try:
            remaining = _query_session_pids(path, session, runner=runner)
            query_authoritative = True
        except _TmuxQueryTransient:
            # After a successful kill, tmux may briefly leave a socket whose
            # server is already disappearing. This is not success and not a
            # reason to publish. Catch only known connection-loss outcomes;
            # permission, malformed-response, and consistency errors remain
            # hard failures. Keep the gate closed until an authoritative
            # absence or the captured server's socket is safely retired.
            stale_socket_retire(path, server_guard, proc_reader=proc_reader)
            # Even a safely retired stale socket is observed as absent on the
            # next pass. Never publish from the same pass whose tmux query
            # failed.
            if monotonic() >= deadline:
                raise RuntimeError("old Claude/TUI/MCP did not fully exit before runtime replacement")
            sleeper(0.1)
            continue
        processes_alive = _process_guard_alive(guard, proc_reader=proc_reader)
        channel_open = port_probe(host, port)
        if query_authoritative and remaining is None and not processes_alive and not channel_open:
            return
        if monotonic() >= deadline:
            raise RuntimeError("old Claude/TUI/MCP did not fully exit before runtime replacement")
        sleeper(0.1)

def prepare_after_stop(stop_action: Callable[[], None], prepare_action: Callable[[], Path]) -> Path:
    stop_action()
    return prepare_action()

def _load_or_create_control(state_dir: str | Path) -> dict:
    state = Path(state_dir)
    path = state / "control.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {"version": 1, "generation": 1, "session_id": str(uuid.uuid4()), "model": "",
                 "requires_fresh": False, "draining": False, "bootstrap_token": ""}
        _atomic_bytes(path, (json.dumps(value) + "\n").encode("utf-8"))
    if (not isinstance(value, dict) or value.get("version") != 1 or
            not isinstance(value.get("generation"), int) or value["generation"] < 1 or
            not isinstance(value.get("session_id"), str) or
            not isinstance(value.get("model", ""), str) or
            not isinstance(value.get("bootstrap_token", ""), str)):
        raise RuntimeError("invalid durable channel control snapshot")
    try: uuid.UUID(value["session_id"])
    except (ValueError, AttributeError) as error: raise RuntimeError("invalid durable channel session id") from error
    return value

def _control_fingerprint(value: dict) -> tuple:
    return (value["generation"], value["session_id"], value.get("model", ""), value.get("bootstrap_token", ""))

def prepare_controlled_runtime_after_stop(socket_path: str | Path, tmux_session: str,
                                          install_dir: str | Path, state_dir: str | Path,
                                          workspace: str | Path, *, stop_action=None,
                                          prepare_action=prepare_runtime_workspace) -> dict:
    (stop_action or (lambda: stop_dedicated_tui(socket_path, tmux_session)))()
    # No Claude process exists now. Re-read after the stop gate, then verify the
    # durable snapshot did not change while its files were being published.
    for _attempt in range(3):
        snapshot = _load_or_create_control(state_dir)
        runtime = prepare_action(
            install_dir, state_dir, workspace, snapshot["generation"], snapshot["session_id"],
            snapshot.get("model", ""), snapshot.get("bootstrap_token", ""),
        )
        confirmed = _load_or_create_control(state_dir)
        if _control_fingerprint(confirmed) == _control_fingerprint(snapshot):
            return {"runtime": str(runtime), "generation": snapshot["generation"],
                    "session_id": snapshot["session_id"], "model": snapshot.get("model", ""),
                    "bootstrap_token": snapshot.get("bootstrap_token", "")}
    raise RuntimeError("channel control changed repeatedly during runtime publication")

def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "mode":
        print("resume" if has_transcript(argv[2], argv[3]) else "fresh")
        return 0
    if len(argv) == 6 and argv[1] == "write-marker":
        write_marker(argv[2], int(argv[3]), argv[4], argv[5])
        return 0
    if len(argv) == 4 and argv[1] == "stop-tui":
        stop_dedicated_tui(argv[2], argv[3])
        return 0
    if len(argv) == 7 and argv[1] == "prepare-after-stop":
        snapshot = prepare_controlled_runtime_after_stop(argv[2], argv[3], argv[4], argv[5], argv[6])
        print(json.dumps(snapshot, separators=(",", ":")))
        return 0
    return 64

if __name__ == "__main__": raise SystemExit(main(sys.argv))
