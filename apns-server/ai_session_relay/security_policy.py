"""Fail-closed execution policy for the pinned Xia Yizhou AI session relay.

This module is copied beside the pinned upstream relay at deployment time.  It
must be imported before either engine module so path/config isolation is in
force before their module-level state is initialized.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class SecurityPolicyError(RuntimeError):
    """The relay cannot prove that the requested execution boundary is safe."""


class ToolUseBlocked(SecurityPolicyError):
    """An engine attempted to use a tool in chat-only mode."""


_MODE = "chat_only"
_CODEX_SAFE_ITEM_TYPES = frozenset({"agentMessage", "reasoning", "plan", "userMessage"})
_CODEX_SAFE_ITEM_METHODS = frozenset({
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
})
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_LEDGER_TERMINAL = frozenset({"completed", "uncertain"})


@dataclass(frozen=True)
class RelayPolicy:
    execution_mode: str
    instance_root: Path
    workspace: Path
    state_dir: Path
    claude_config_dir: Path
    codex_home: Path
    runtime_home: Path
    claude_bin: Path
    codex_bin: Path
    host: str


def _required_path(env: Mapping[str, str], name: str) -> Path:
    raw = env.get(name, "").strip()
    if not raw:
        raise SecurityPolicyError(f"{name} must be set to an absolute path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SecurityPolicyError(f"{name} must be absolute")
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SecurityPolicyError(f"{name} does not exist") from exc


def _required_root_executable(env: Mapping[str, str], name: str) -> Path:
    path = _required_path(env, name)
    raw_path = Path(env[name]).expanduser()
    if (name == "AI_RELAY_CODEX_BIN" and raw_path.is_symlink()) \
            or not path.is_file() or str(path).startswith("/root/"):
        raise SecurityPolicyError(f"{name} must be a real globally accessible file")
    st = path.stat()
    if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o755 or not os.access(path, os.X_OK):
        raise SecurityPolicyError(f"{name} must be root-owned 0755 and executable")
    return path


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return child != parent
    except ValueError:
        return False


def _assert_owned_mode(path: Path, expected_mode: int, expected_uid: int) -> None:
    st = path.stat()
    if st.st_uid != expected_uid:
        raise SecurityPolicyError(f"unexpected owner for {path}")
    if stat.S_IMODE(st.st_mode) != expected_mode:
        raise SecurityPolicyError(
            f"{path} must have mode {expected_mode:04o}"
        )


def _mount_is_read_only(path: Path) -> bool:
    """Return whether the most-specific Linux mount containing path is read-only."""
    target = str(path.resolve())
    best_mount = ""
    best_options: set[str] = set()
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                fields = line.rstrip("\n").split(" ")
                if len(fields) < 7:
                    continue
                mountpoint = fields[4].replace("\\040", " ")
                if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
                    if len(mountpoint) > len(best_mount):
                        best_mount = mountpoint
                        best_options = set(fields[5].split(","))
    except OSError as exc:
        raise SecurityPolicyError("cannot inspect workspace mount boundary") from exc
    return bool(best_mount and "ro" in best_options)


def load_policy(env: Mapping[str, str] | None = None) -> RelayPolicy:
    """Validate explicit isolation roots and return the immutable policy.

    There is intentionally no permissive default.  An unset or future unknown
    mode stops process startup instead of silently restoring upstream's
    autonomous behavior.
    """
    env = os.environ if env is None else env
    mode = env.get("AI_RELAY_EXECUTION_MODE", "").strip()
    if mode != _MODE:
        raise SecurityPolicyError(
            "AI_RELAY_EXECUTION_MODE must be exactly 'chat_only'"
        )

    root = _required_path(env, "AI_RELAY_INSTANCE_ROOT")
    workspace = _required_path(env, "AI_RELAY_WORKSPACE")
    state = _required_path(env, "AI_RELAY_STATE_DIR")
    if not _inside(workspace, root) or not _inside(state, root):
        raise SecurityPolicyError("workspace and state must be children of instance root")
    if workspace == state or _inside(workspace, state) or _inside(state, workspace):
        raise SecurityPolicyError("workspace and state must be disjoint")

    service_uid = os.geteuid()
    # root/workspace are deliberately read-only in the service mount namespace.
    # Validate their deployment contract without trying to mutate the bind.
    _assert_owned_mode(root, 0o700, service_uid)
    _assert_owned_mode(workspace, 0o700, service_uid)
    if not _mount_is_read_only(workspace):
        raise SecurityPolicyError("workspace must be mounted read-only")

    # State is the only writable subtree. Tightening it is safe and required.
    try:
        if state.stat().st_uid != service_uid:
            raise SecurityPolicyError("unexpected owner for relay state")
        os.chmod(state, 0o700)
    except OSError as exc:
        raise SecurityPolicyError("cannot enforce private relay state") from exc

    host = env.get("AI_RELAY_HOST", "127.0.0.1").strip()
    if host not in {"127.0.0.1", "::1"}:
        raise SecurityPolicyError("AI_RELAY_HOST must be a numeric loopback address")

    expected_claude = state / "claude-home"
    expected_codex = state / "codex-home"
    expected_home = state / "runtime-home"
    for path in (expected_claude, expected_codex, expected_home):
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SecurityPolicyError(f"isolated runtime directory missing: {path.name}") from exc
        if not _inside(resolved, state):
            raise SecurityPolicyError(f"isolated runtime directory escaped state: {path.name}")
        try:
            if resolved.stat().st_uid != service_uid:
                raise SecurityPolicyError(f"unexpected owner for {path.name}")
            os.chmod(resolved, 0o700)
        except OSError as exc:
            raise SecurityPolicyError(f"cannot enforce mode 0700 on {path.name}") from exc

    configured_claude = Path(env.get("CLAUDE_CONFIG_DIR", str(expected_claude))).resolve()
    configured_codex = Path(env.get("CODEX_HOME", str(expected_codex))).resolve()
    configured_home = Path(env.get("HOME", str(expected_home))).resolve()
    if configured_claude != expected_claude.resolve():
        raise SecurityPolicyError("CLAUDE_CONFIG_DIR must use the isolated state directory")
    if configured_codex != expected_codex.resolve():
        raise SecurityPolicyError("CODEX_HOME must use the isolated state directory")
    if configured_home != expected_home.resolve():
        raise SecurityPolicyError("HOME must use the isolated runtime directory")
    claude_bin = _required_root_executable(env, "AI_RELAY_CLAUDE_BIN")
    codex_bin = _required_root_executable(env, "AI_RELAY_CODEX_BIN")

    for persona_name in ("CLAUDE.md", "AGENTS.md"):
        persona_path = workspace / persona_name
        if not persona_path.is_file():
            raise SecurityPolicyError(f"missing persona file: {persona_name}")
        _assert_owned_mode(persona_path, 0o600, service_uid)

    return RelayPolicy(
        execution_mode=mode,
        instance_root=root,
        workspace=workspace,
        state_dir=state,
        claude_config_dir=expected_claude.resolve(),
        codex_home=expected_codex.resolve(),
        runtime_home=expected_home.resolve(),
        claude_bin=claude_bin,
        codex_bin=codex_bin,
        host=host,
    )


def child_env(policy: RelayPolicy, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a small non-secret environment for engine subprocesses."""
    source = os.environ if source is None else source
    result: dict[str, str] = {
        "PATH": source.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(policy.runtime_home),
        "CLAUDE_CONFIG_DIR": str(policy.claude_config_dir),
        "CLAUDE_CODE_SAFE_MODE": "1",
        "CODEX_HOME": str(policy.codex_home),
        "AI_RELAY_EXECUTION_MODE": policy.execution_mode,
        "AI_RELAY_INSTANCE_ROOT": str(policy.instance_root),
        "AI_RELAY_WORKSPACE": str(policy.workspace),
        "AI_RELAY_STATE_DIR": str(policy.state_dir),
        "AI_RELAY_CLAUDE_BIN": str(policy.claude_bin),
        "AI_RELAY_CODEX_BIN": str(policy.codex_bin),
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = source.get(name)
        if value:
            result[name] = value
    return result


def claude_args(
    policy: RelayPolicy,
    text: str,
    sid: str | None,
    model: str | None,
    effort: str | None,
    valid_efforts: set[str] | frozenset[str],
) -> list[str]:
    persona = policy.workspace / "CLAUDE.md"
    codex_persona = policy.workspace / "AGENTS.md"
    empty_mcp = policy.state_dir / "empty-mcp.json"
    if not persona.is_file() or not codex_persona.is_file() or not empty_mcp.is_file():
        raise SecurityPolicyError("chat-only persona or empty MCP policy file is missing")
    try:
        if json.loads(empty_mcp.read_text(encoding="utf-8")) != {"mcpServers": {}}:
            raise SecurityPolicyError("empty-mcp.json must declare no MCP servers")
        _assert_owned_mode(persona, 0o600, os.geteuid())
        _assert_owned_mode(codex_persona, 0o600, os.geteuid())
        os.chmod(empty_mcp, 0o600)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SecurityPolicyError("cannot validate private Claude policy files") from exc
    args = [
        str(policy.claude_bin), "-p", text,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--safe-mode", "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", str(empty_mcp),
        "--system-prompt-file", str(persona),
    ]
    if sid:
        args += ["--resume", sid]
    if model:
        args += ["--model", model]
    if effort in valid_efforts:
        args += ["--effort", effort]
    return args


def codex_args(policy: RelayPolicy) -> list[str]:
    """Build an app-server command with no write, MCP, web, app, or approval path."""
    return [
        str(policy.codex_bin),
        "--strict-config",
        "-c", 'approval_policy="never"',
        "-c", 'sandbox_mode="read-only"',
        "-c", 'web_search="disabled"',
        "-c", "mcp_servers={}",
        "-c", "apps={}",
        "--disable", "browser_use",
        "--disable", "browser_use_external",
        "--disable", "computer_use",
        "--disable", "image_generation",
        "--disable", "multi_agent",
        "app-server", "--stdio",
    ]


def codex_thread_security() -> dict:
    return {"approvalPolicy": "never", "sandbox": "read-only"}


def assert_request_mode(value: object) -> None:
    if value != _MODE:
        raise SecurityPolicyError("request execution_mode must be exactly 'chat_only'")


def validate_request_id(value: object) -> str:
    """Accept only the backend's stable, log-safe idempotency identifier."""
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise SecurityPolicyError(
            "request_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,199}"
        )
    return value


def is_codex_tool_item(item: Mapping[str, object] | None) -> bool:
    # App-server may add new tool item variants.  Unknown non-empty item types
    # are unsafe until explicitly reviewed, so this is an allowlist.
    return bool(item and item.get("type") not in _CODEX_SAFE_ITEM_TYPES)


def assert_no_claude_tool(block_type: object) -> None:
    if block_type == "tool_use":
        raise ToolUseBlocked("Claude emitted tool_use in chat_only mode")


def assert_no_codex_tool(item: Mapping[str, object] | None) -> None:
    if is_codex_tool_item(item):
        raise ToolUseBlocked("Codex emitted a tool item in chat_only mode")


def assert_safe_codex_notification(method: str, params: Mapping[str, object] | None) -> None:
    """Fail closed before consuming any Codex item lifecycle notification."""
    params = params or {}
    item = params.get("item")
    if isinstance(item, Mapping):
        assert_no_codex_tool(item)
    if not method.startswith("item/"):
        return
    if method in {"item/started", "item/completed"}:
        if not isinstance(item, Mapping):
            raise ToolUseBlocked(f"Codex {method} omitted its item payload")
        return
    if method not in _CODEX_SAFE_ITEM_METHODS:
        raise ToolUseBlocked(f"unrecognized Codex item lifecycle: {method}")


async def guard_codex_notification(
    proc: asyncio.subprocess.Process | None,
    method: str,
    params: Mapping[str, object] | None,
) -> None:
    """Kill the whole engine group before surfacing an unsafe lifecycle."""
    try:
        assert_safe_codex_notification(method, params)
    except ToolUseBlocked:
        await terminate_process_group(proc)
        raise


def denial_for_server_request(method: str) -> dict:
    if "mcpserver/elicitation" in method.lower():
        return {"action": "cancel"}
    return {"decision": "cancel"}


async def terminate_process_group(proc: asyncio.subprocess.Process | None) -> None:
    """Kill the engine process group and wait briefly; never signal this relay."""
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


def command_contains(args: Sequence[str], forbidden: str) -> bool:
    """Small public helper used only by policy contract tests."""
    return forbidden in args


def durable_json_replace(path: Path, value: object) -> None:
    """Crash-safe 0600 JSON replace: fsync data, rename, then fsync parent."""
    parent = path.parent
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)


class RequestLedger:
    """Small durable idempotency ledger for chat engine submissions.

    Callers serialize methods with their process-local state lock. Corrupt or
    unknown ledger data fails closed: losing deduplication evidence must never
    turn into an automatic engine replay.
    """

    def __init__(self, path: Path, *, max_entries: int = 512,
                 max_bytes: int = 64 * 1024 * 1024) -> None:
        self.path = path
        self.max_entries = max(64, min(int(max_entries), 10_000))
        self.max_bytes = max(1024 * 1024, min(int(max_bytes), 256 * 1024 * 1024))

    @staticmethod
    def _empty() -> dict:
        return {"version": 1, "next_seq": 1, "records": []}

    def load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SecurityPolicyError("request ledger is unreadable; refusing replay") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise SecurityPolicyError("request ledger schema is invalid; refusing replay")
        records = raw.get("records")
        next_seq = raw.get("next_seq")
        if not isinstance(records, list) or not isinstance(next_seq, int) or next_seq < 1:
            raise SecurityPolicyError("request ledger structure is invalid; refusing replay")
        seen_ids: set[str] = set()
        seen_keys: set[tuple[str, int, str]] = set()
        for rec in records:
            if not isinstance(rec, dict):
                raise SecurityPolicyError("request ledger record is invalid; refusing replay")
            try:
                validate_request_id(rec.get("request_id"))
                provider = rec["provider"]
                epoch = rec["epoch"]
                status = rec["status"]
                seq = rec["seq"]
            except (KeyError, SecurityPolicyError) as exc:
                raise SecurityPolicyError("request ledger record is invalid; refusing replay") from exc
            if provider not in {"claude", "codex"} or not isinstance(epoch, int) or epoch < 0:
                raise SecurityPolicyError("request ledger identity is invalid; refusing replay")
            if status not in {"accepted", "running", "completed", "uncertain"}:
                raise SecurityPolicyError("request ledger status is invalid; refusing replay")
            if not isinstance(seq, int) or seq < 1 or not isinstance(rec.get("owner"), str):
                raise SecurityPolicyError("request ledger sequence is invalid; refusing replay")
            identity = (provider, epoch, rec["request_id"])
            if rec["request_id"] in seen_ids or identity in seen_keys:
                raise SecurityPolicyError("request ledger contains duplicate identities; refusing replay")
            seen_ids.add(rec["request_id"])
            seen_keys.add(identity)
            if status == "completed":
                done = rec.get("done")
                if not isinstance(done, dict) or done.get("done") is not True:
                    raise SecurityPolicyError("completed ledger record lacks authoritative done")
        return raw

    @staticmethod
    def find(data: dict, provider: str, epoch: int, request_id: str) -> dict | None:
        return next((rec for rec in data["records"]
                     if rec["provider"] == provider and rec["epoch"] == epoch
                     and rec["request_id"] == request_id), None)

    @staticmethod
    def find_request(data: dict, request_id: str) -> dict | None:
        return next((rec for rec in data["records"]
                     if rec["request_id"] == request_id), None)

    @staticmethod
    def active(data: dict) -> list[dict]:
        return [rec for rec in data["records"]
                if rec["status"] in {"accepted", "running"}]

    def _next_seq(self, data: dict) -> int:
        seq = data["next_seq"]
        data["next_seq"] = seq + 1
        return seq

    def _prune(self, data: dict, *, preserve: dict | None = None) -> bool:
        def encoded_size() -> int:
            return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))

        while len(data["records"]) > self.max_entries or encoded_size() > self.max_bytes:
            candidates = [rec for rec in data["records"]
                          if rec is not preserve and rec["status"] in _LEDGER_TERMINAL]
            if not candidates:
                return False
            data["records"].remove(min(candidates, key=lambda rec: rec["seq"]))
        return True

    def accept(self, data: dict, provider: str, epoch: int,
               request_id: str, owner: str) -> dict:
        validate_request_id(request_id)
        if self.find_request(data, request_id) is not None:
            raise SecurityPolicyError("request_id already exists in durable ledger")
        rec = {
            "provider": provider, "epoch": int(epoch), "request_id": request_id,
            "status": "accepted", "owner": owner, "seq": self._next_seq(data),
        }
        data["records"].append(rec)
        if not self._prune(data, preserve=rec):
            data["records"].remove(rec)
            raise SecurityPolicyError("request ledger capacity is exhausted")
        durable_json_replace(self.path, data)
        return rec

    def transition(self, data: dict, rec: dict, status: str, *,
                   done: Mapping[str, object] | None = None) -> bool:
        if status not in {"running", "completed", "uncertain"}:
            raise SecurityPolicyError("invalid request ledger transition")
        current = self.find(data, rec["provider"], rec["epoch"], rec["request_id"])
        if current is None or current["status"] not in {"accepted", "running"}:
            raise SecurityPolicyError("request ledger transition lost its active record")
        if status == "completed":
            if not isinstance(done, Mapping) or done.get("done") is not True:
                raise SecurityPolicyError("completion requires an authoritative done event")
            current["done"] = dict(done)
        else:
            current.pop("done", None)
        current["status"] = status
        current["seq"] = self._next_seq(data)
        if not self._prune(data, preserve=current):
            # A terminal result too large to retain cannot safely be advertised
            # as replayable. Preserve the id as terminal-uncertain instead.
            current["status"] = "uncertain"
            current.pop("done", None)
            current["seq"] = self._next_seq(data)
            if not self._prune(data, preserve=current):
                raise SecurityPolicyError("request ledger capacity is exhausted")
            durable_json_replace(self.path, data)
            return False
        durable_json_replace(self.path, data)
        return status == "completed"

    def crashed_active(self, data: dict, owner: str) -> list[dict]:
        return [rec for rec in self.active(data) if rec.get("owner") != owner]

    def mark_crashed_uncertain(self, data: dict, records: Sequence[dict]) -> None:
        identities = {(r["provider"], r["epoch"], r["request_id"]) for r in records}
        for rec in data["records"]:
            ident = (rec["provider"], rec["epoch"], rec["request_id"])
            if ident in identities and rec["status"] in {"accepted", "running"}:
                rec["status"] = "uncertain"
                rec.pop("done", None)
                rec["seq"] = self._next_seq(data)
        if not self._prune(data):
            raise SecurityPolicyError("request ledger capacity is exhausted")
        durable_json_replace(self.path, data)


def mark_refresh_pending(state: dict) -> dict:
    """Idempotently arm one fresh-session handoff before the next turn."""
    if not state.get("pending_switch"):
        state["epoch"] = int(state.get("epoch", 0)) + 1
    state["pending_switch"] = True
    return state


def invalidate_provider_resume(policy: RelayPolicy, provider: str) -> None:
    """Durably remove only the isolated provider's resume pointers."""
    names = {
        "claude": ("headless_last_session",),
        "codex": ("codex_last_thread", "codex_thread_epoch"),
    }.get(provider)
    if names is None:
        raise SecurityPolicyError("unknown provider resume state")
    changed = False
    for name in names:
        path = policy.state_dir / name
        try:
            path.unlink()
            changed = True
        except FileNotFoundError:
            pass
    if changed:
        fd = os.open(policy.state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
