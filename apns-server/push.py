"""
Cc APNs server - Live Activity push 主入口

听三个 endpoint
  POST /register-token    iPhone app 启动 Live Activity 后上报 push token
  POST /unregister-token  iPhone app 结束 Live Activity 上报
  POST /push              本机其他脚本 (bus_stop_hook 等) 触发 push 给所有 active iPhone
  GET  /health            健康检查

POST /push 触发 SPOKE / 状态切换 等
请求 body
{
  "event": "update" | "end",
  "state": "listening" | "thinking" | "spoken",
  "preview": "想你了",
  "color": "orange",
  "message_count": 5,
  "alert_title": "Cc" (optional),
  "alert_body": "想你了" (optional)
}

成功返回 200 + 每个 token 的 push 结果
失败 token 自动从 store 移除 (Apple 410 = 失效)

启动
  python3 push.py [--config config.toml] [--sandbox]

部署
  launchd plist 在 deploy/com.cccompanion.apns-server.plist
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
import hmac
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
import sys
import threading
import time
import unicodedata
try:
    import fcntl
except ImportError:  # pragma: no cover - Linux service path uses fcntl.
    fcntl = None  # type: ignore[assignment]
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jwt_helper import APNsJWT
from apns_client import APNsClient, APNsResponse
from token_store import TokenStore
from device_token_store import DeviceTokenStore
from task_queue import TaskQueue
from chat_history import ChatHistory, ChatStreamBus, EphemeralTaskBuffer
from diary_stream import DiaryStream
from group_chat import GroupChatStore
from calendar_store import CalendarStore, CATEGORIES, CATEGORY_LABELS
from rp_history import RPHistory, validate_sid as validate_rp_sid
from diary import Diary
from favorites import Favorites
from worklog import Worklog
from reminders import ReminderStore
from tool_dispatcher import ScheduleStore, ToolDispatcher, DEFAULT_SCHEDULE
import rollback_driver
from codex_app_bridge import (
    CodexAppBridge,
    CodexAppBridgeError,
    CodexPromptLockBusy,
    CodexThreadTokenUsage,
    prompt_lock_is_busy,
)
from timeline import Timeline
from tts import TTS
from settings import Settings
from usage import UsageReader
import todos as todos_mod
from studyroom import StudyroomDB
from ai_chat import AIChatManager
import subprocess
import threading

try:
    import rp_session_manager
except ImportError:
    rp_session_manager = None


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.toml"
CLIENT_LOG_PATH = HERE / "client_logs.jsonl"
CLIENT_LOG_MAX_FIELD = 20_000


@dataclass(frozen=True)
class TmuxInjectionResult:
    """Outcome of one direct tmux prompt injection.

    ``injection_uncertain`` is deliberately separate from ``success``.  It
    means tmux may have pasted or submitted the prompt and the caller must keep
    the exact turn active until a real terminal interrupt or completion hook
    resolves it.
    """

    success: bool
    error: str = ""
    phase: str = ""
    injection_uncertain: bool = False
    cleanup_confirmed: bool = False

    def __iter__(self):
        # Preserve the long-standing ``ok, err = _inject_to_session(...)`` API.
        yield self.success
        yield self.error


@contextmanager
def _locked_json_state(path: Path, *, exclusive: bool):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("cc-apns-server")


XIAOKE_STALE_COMPLETION_GRACE_SECONDS = 120.0


def _should_expire_chat_typing(contact_id: str, state: dict[str, Any], age_seconds: float) -> bool:
    exact_xiaoke_turn = (
        contact_id == "xiaoke"
        and str(state.get("transport") or "") == "tmux"
        and bool(str(state.get("session") or ""))
        and bool(re.fullmatch(r"[0-9a-f]{32}", str(state.get("turn_token") or "")))
    )
    if exact_xiaoke_turn:
        # Exact turns end through their correlated Stop hook, never a cosmetic
        # TTL.  One bounded exception: a completion callback for the same tmux
        # session that named another turn proves the CLI already finished a
        # turn there.  If nothing resolves the active claim within the grace
        # window afterwards, treat the turn as ended instead of pinning the
        # App on Stop and the ••• bubble forever.
        try:
            stale_at = float(state.get("stale_completion_at") or 0.0)
        except (TypeError, ValueError):
            stale_at = 0.0
        return bool(stale_at) and time.time() - stale_at > XIAOKE_STALE_COMPLETION_GRACE_SECONDS
    return age_seconds > 120


AUTO_FORGE_CLAIM_HISTORY_LIMIT = 256
AUTO_FORGE_SEEN_BITS = 131_072
AUTO_FORGE_SEEN_HASHES = 5


def _auto_forge_usage_percent(token_usage: CodexThreadTokenUsage | None) -> float | None:
    if token_usage is None:
        return None
    window = token_usage.model_context_window
    context_tokens = token_usage.last.total_tokens
    if window is None or window <= 0 or context_tokens < 0:
        return None
    return context_tokens / window * 100.0


def _should_auto_forge(
    token_usage: CodexThreadTokenUsage | None,
    *,
    threshold_percent: float,
    context_compacted: bool,
) -> bool:
    if context_compacted:
        return True
    usage_percent = _auto_forge_usage_percent(token_usage)
    return usage_percent is not None and usage_percent >= threshold_percent


class AutoForgeClaimStore:
    """Cross-process once-only claims without persisting session IDs or prompts."""

    def __init__(self, path: str | Path, *, history_limit: int = AUTO_FORGE_CLAIM_HISTORY_LIMIT):
        self.path = Path(path).expanduser()
        self.history_limit = max(16, int(history_limit))

    @staticmethod
    def _session_key(session_id: str) -> str:
        return hashlib.sha256(f"cc-auto-forge-v1\0{session_id}".encode("utf-8")).hexdigest()

    @staticmethod
    def _seen_indices(key: str) -> list[int]:
        digest = bytes.fromhex(key)
        first = int.from_bytes(digest[:8], "big")
        step = int.from_bytes(digest[8:16], "big") | 1
        return [
            (first + index * step) % AUTO_FORGE_SEEN_BITS
            for index in range(AUTO_FORGE_SEEN_HASHES)
        ]

    @classmethod
    def _seen_contains(cls, seen: bytearray, key: str) -> bool:
        return all(seen[index // 8] & (1 << (index % 8)) for index in cls._seen_indices(key))

    @classmethod
    def _seen_add(cls, seen: bytearray, key: str) -> None:
        for index in cls._seen_indices(key):
            seen[index // 8] |= 1 << (index % 8)

    @staticmethod
    def _owner_is_alive(owner_pid: Any) -> bool:
        try:
            pid = int(owner_pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _load_unlocked(self) -> tuple[list[dict[str, Any]], bytearray]:
        seen = bytearray(AUTO_FORGE_SEEN_BITS // 8)
        if not self.path.exists():
            return [], seen
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return [], seen
        claims = payload.get("claims") if isinstance(payload, dict) else None
        if not isinstance(claims, list):
            claims = []
        clean_claims = [
            item for item in claims
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        ][-self.history_limit:]
        version = payload.get("version") if isinstance(payload, dict) else None
        seen_hex = payload.get("seen") if isinstance(payload, dict) else None
        if version == 3 and isinstance(seen_hex, str):
            try:
                decoded = bytearray.fromhex(seen_hex)
                if len(decoded) == len(seen):
                    seen = decoded
            except ValueError:
                pass
        # Rebuild pre-v3 files because v2 also marked in-progress claims seen.
        # In-progress claims must stay recoverable when their owner disappears.
        for item in clean_claims:
            if item.get("status") == "claimed":
                continue
            try:
                self._seen_add(seen, item["key"])
            except (ValueError, TypeError):
                continue
        return clean_claims, seen

    def _write_unlocked(self, claims: list[dict[str, Any]], seen: bytearray) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 3,
            "seen": seen.hex(),
            "claims": claims[-self.history_limit:],
        }
        tmp = self.path.with_name(
            f".{self.path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    def claim(self, session_id: str) -> bool:
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        key = self._session_key(session_id)
        with _locked_json_state(self.path, exclusive=True):
            claims, seen = self._load_unlocked()
            if self._seen_contains(seen, key):
                return False
            now = int(time.time())
            existing = next(
                (item for item in reversed(claims) if item.get("key") == key),
                None,
            )
            if existing is not None:
                if existing.get("status") != "claimed":
                    return False
                if self._owner_is_alive(existing.get("owner_pid")):
                    return False
                existing.update({
                    "owner_pid": os.getpid(),
                    "claimed_at": now,
                    "updated_at": now,
                    "recovered": True,
                })
            else:
                claims.append({
                    "key": key,
                    "status": "claimed",
                    "owner_pid": os.getpid(),
                    "claimed_at": now,
                    "updated_at": now,
                })
            self._write_unlocked(claims, seen)
            return True

    def finish(self, session_id: str, status: str) -> None:
        key = self._session_key(str(session_id or "").strip())
        clean_status = status if status in {"completed", "failed", "cancelled", "cas_failed"} else "failed"
        with _locked_json_state(self.path, exclusive=True):
            claims, seen = self._load_unlocked()
            for item in reversed(claims):
                if item.get("key") == key:
                    item["status"] = clean_status
                    item["updated_at"] = int(time.time())
                    self._seen_add(seen, key)
                    self._write_unlocked(claims, seen)
                    return


def _compare_and_swap_codex_target_state(
    state_path: Path,
    *,
    shared_session_name: str,
    user_id: str,
    expected_session_id: str,
    new_session_id: str,
    cwd: Path,
    source: str,
) -> tuple[bool, str | None]:
    """Atomically switch the shared/user pointer only if it still names the old thread."""
    state_path = Path(state_path).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_json_state(state_path, exclusive=True):
        data: dict[str, Any] = {}
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        shared_sessions = data.get("shared_sessions")
        shared = (
            shared_sessions.get(shared_session_name)
            if isinstance(shared_sessions, dict)
            else None
        )
        shared_current = str((shared or {}).get("active_session_id") or "").strip() or None
        users = data.get("users")
        user = users.get(user_id) if isinstance(users, dict) else None
        user_current = str((user or {}).get("active_session_id") or "").strip() or None
        observed = [value for value in (shared_current, user_current) if value]
        mismatched = next(
            (value for value in observed if value != expected_session_id),
            None,
        )
        if not observed or mismatched is not None:
            return False, mismatched or shared_current or user_current

        if not isinstance(shared_sessions, dict):
            shared_sessions = {}
            data["shared_sessions"] = shared_sessions
        shared = shared_sessions.get(shared_session_name)
        if not isinstance(shared, dict):
            shared = {}
            shared_sessions[shared_session_name] = shared
        shared["active_session_id"] = new_session_id
        shared["active_cwd"] = str(cwd)
        shared["updated_at"] = int(time.time())
        shared["updated_by"] = source

        if not isinstance(users, dict):
            users = {}
            data["users"] = users
        user = users.get(user_id)
        if not isinstance(user, dict):
            user = {}
            users[user_id] = user
        user["active_session_id"] = new_session_id
        user["active_cwd"] = str(cwd)

        tmp = state_path.with_name(
            f".{state_path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(state_path))
        return True, new_session_id


class _CodexRunRegistry:
    """Process-local registry for Kairos Codex runs started by CcCompanion."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def start(
        self,
        *,
        source: str,
        session_id: str | None,
        cwd: Path,
        cancel_event: threading.Event | None = None,
        contact_id: str | None = None,
        user_ts: str | None = None,
    ) -> tuple[str, threading.Event] | None:
        with self._lock:
            if self._runs:
                return None
            run_id = f"{int(time.time() * 1000)}-{os.getpid()}"
            resolved_cancel_event = cancel_event or threading.Event()
            self._runs[run_id] = {
                "run_id": run_id,
                "source": source,
                "session_id": session_id,
                "cwd": str(cwd),
                "started_at": time.time(),
                "cancel_event": resolved_cancel_event,
                "contact_id": str(contact_id or ""),
                "user_ts": str(user_ts or ""),
            }
            return run_id, resolved_cancel_event

    def finish(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._runs:
                return None
            item = max(self._runs.values(), key=lambda run: float(run.get("started_at") or 0))
            return dict(item)

    def cancel_latest(
        self,
        *,
        source: str | None = None,
        contact_id: str | None = None,
        user_ts: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            candidates = [
                run for run in self._runs.values()
                if (source is None or str(run.get("source") or "") == source)
                and (contact_id is None or str(run.get("contact_id") or "") == contact_id)
                and (user_ts is None or str(run.get("user_ts") or "") == user_ts)
            ]
            if not candidates:
                return None
            item = max(candidates, key=lambda run: float(run.get("started_at") or 0))
            event = item.get("cancel_event")
            if isinstance(event, threading.Event):
                event.set()
            return dict(item)


CODEX_RUNS = _CodexRunRegistry()


# P0-3: auto-generate and persist shared_secret if not configured
def _load_or_create_secret() -> str:
    """Load existing auto-generated secret or create one. Stored at ~/.ots/secret (mode 0600)."""
    secret_dir = Path.home() / ".ots"
    secret_file = secret_dir / "secret"
    try:
        secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if secret_file.exists():
            s = secret_file.read_text().strip()
            if s:
                return s
        import secrets as _secrets
        new_secret = _secrets.token_hex(32)
        secret_file.write_text(new_secret)
        secret_file.chmod(0o600)
        logger.info("P0-3: auto-generated shared_secret written to %s", secret_file)
        logger.info("P0-3: SHARED SECRET: %s  ← copy to your OTS app onboarding", new_secret)
        return new_secret
    except Exception as e:
        logger.warning("P0-3: could not auto-generate secret: %s", e)
        return ""


VPS_SERVICE_UNITS: list[tuple[str, str]] = [
    ("ccbot-lite", "ccbot-lite.service"),
    ("claude-tg", "claude-tg.service"),
    ("cc-companion", "cc-companion.service"),
    ("healthcheck timer", "cc-companion-healthcheck.timer"),
    ("cloudflared", "cloudflared.service"),
    ("hysteria-server", "hysteria-server.service"),
    ("terminal-mcp", "terminal-mcp.service"),
    ("windows-codex-tg", "windows-codex-tg.service"),
]

VPS_STATUS_CACHE_TTL = 5.0
VPS_STATUS_CACHE_LOCK = threading.Lock()
VPS_STATUS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
CODEX_QUOTA_CACHE_TTL = 60.0
CODEX_QUOTA_CACHE_LOCK = threading.Lock()
CODEX_QUOTA_CACHE: dict[str, Any] = {"ts": 0.0, "lines": None}


def _run_status_cmd(args: list[str], timeout: float = 1.5) -> str:
    try:
        return subprocess.run(
            args,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).stdout.strip()
    except Exception:
        return ""


def _read_proc_stat_cpu() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        if not parts or parts[0] != "cpu":
            return None
        nums = [int(x) for x in parts[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return sum(nums), idle
    except Exception:
        return None


def _cpu_percent() -> float:
    first = _read_proc_stat_cpu()
    if first is None:
        return 0.0
    time.sleep(0.12)
    second = _read_proc_stat_cpu()
    if second is None:
        return 0.0
    total_delta = second[0] - first[0]
    idle_delta = second[1] - first[1]
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def _read_meminfo() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0])
    except Exception:
        values = {}
    total = values.get("MemTotal", 0) * 1024
    available = values.get("MemAvailable", 0) * 1024
    used = max(0, total - available)
    percent = round((used / total * 100.0), 1) if total else 0.0
    return {
        "used_mb": round(used / 1024 / 1024, 1),
        "total_mb": round(total / 1024 / 1024, 1),
        "available_mb": round(available / 1024 / 1024, 1),
        "percent": percent,
    }


def _disk_usage(path: str = "/") -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        total = usage.total
        used = usage.used
        available = usage.free
    except Exception:
        total = used = available = 0
    percent = round((used / total * 100.0), 1) if total else 0.0
    return {
        "used_mb": round(used / 1024 / 1024, 1),
        "total_mb": round(total / 1024 / 1024, 1),
        "available_mb": round(available / 1024 / 1024, 1),
        "percent": percent,
    }


def _uptime_info() -> dict[str, Any]:
    seconds = 0.0
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            seconds = float(f.read().split()[0])
    except Exception:
        seconds = 0.0
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {minutes}m"
    else:
        text = f"{minutes}m"
    return {"seconds": int(seconds), "text": text}


def _parse_systemctl_show(output: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def _service_status(label: str, unit: str) -> dict[str, Any]:
    output = _run_status_cmd([
        "systemctl",
        "show",
        unit,
        "--property=LoadState,ActiveState,SubState,MainPID,MemoryCurrent,ActiveEnterTimestamp",
        "--no-pager",
    ])
    data = _parse_systemctl_show(output)
    active_state = data.get("ActiveState") or "unknown"
    sub_state = data.get("SubState") or ""
    load_state = data.get("LoadState") or "unknown"
    if active_state == "active":
        status = "up"
    elif active_state in {"activating", "reloading"}:
        status = "wait"
    elif load_state == "not-found":
        status = "missing"
    else:
        status = "down" if active_state != "unknown" else "unknown"

    mem_current = data.get("MemoryCurrent") or ""
    try:
        mem_mb = None if mem_current in {"", "[not set]", "18446744073709551615"} else round(int(mem_current) / 1024 / 1024, 1)
    except Exception:
        mem_mb = None

    return {
        "label": label,
        "unit": unit,
        "status": status,
        "active_state": active_state,
        "sub_state": sub_state,
        "uptime": data.get("ActiveEnterTimestamp") or "",
        "mem_mb": mem_mb,
    }


def _process_label(command: str, args: str) -> str:
    line = f"{command} {args}"
    if "claude --resume" in line:
        return "Claude Code 主进程"
    if "tg_codex_bot.py" in line:
        return "Windows-Codex-TG bot"
    if "codex exec" in line:
        return "当前 Codex 会话"
    if "bun server.ts" in line:
        return "Telegram 插件 bun"
    if "terminal-mcp/server.js" in line:
        return "terminal-mcp node"
    if "CcCompanion" in line and "push.py" in line:
        return "CcCompanion 服务"
    if "ccbot_lite.main" in line:
        return "ccbot-lite"
    if "cloudflared" in line:
        return "cloudflared"
    if "hysteria server" in line:
        return "hysteria2"
    return command


def _top_memory_processes(limit: int = 12) -> list[dict[str, Any]]:
    output = _run_status_cmd(["ps", "-eo", "rss=,comm=,args=", "--sort=-rss"], timeout=1.5)
    processes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if len(processes) >= limit:
            break
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        rss_kb = parts[0]
        command = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        label = _process_label(command, args)
        label = label.strip()[:48] or "process"
        if label in seen:
            continue
        seen.add(label)
        try:
            mem_mb = round(int(rss_kb) / 1024, 1)
        except Exception:
            continue
        processes.append({"label": label, "mem_mb": mem_mb})
    return processes


def _collect_vps_status_uncached() -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    services = [_service_status(label, unit) for label, unit in VPS_SERVICE_UNITS]
    top_memory = _top_memory_processes()
    return {
        "ok": True,
        "host": _run_status_cmd(["hostname"], timeout=1.0) or os.uname().nodename,
        "time": datetime.now(timezone.utc).isoformat(),
        "uptime": _uptime_info(),
        "load": {
            "one": round(load1, 2),
            "five": round(load5, 2),
            "fifteen": round(load15, 2),
        },
        "cpu": {"percent": _cpu_percent()},
        "memory": _read_meminfo(),
        "disk": _disk_usage("/"),
        "services": services,
        "processes": {"top_memory": top_memory},
        "top_memory": top_memory,
        "health": {"ok": True},
    }


def collect_vps_status() -> dict[str, Any]:
    now = time.time()
    with VPS_STATUS_CACHE_LOCK:
        cached = VPS_STATUS_CACHE.get("data")
        cached_ts = float(VPS_STATUS_CACHE.get("ts") or 0.0)
        if isinstance(cached, dict) and now - cached_ts < VPS_STATUS_CACHE_TTL:
            return dict(cached)

    data = _collect_vps_status_uncached()
    with VPS_STATUS_CACHE_LOCK:
        VPS_STATUS_CACHE["ts"] = now
        VPS_STATUS_CACHE["data"] = data
    return data


WEB_CHAT_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cc Chat</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body { background: #1E1E1E; color: #fff; font: 14px -apple-system, "PingFang SC", "Segoe UI", system-ui, sans-serif; display: flex; flex-direction: column; }
  header { padding: 10px 16px; background: #111; border-bottom: 1px solid #333; display: flex; align-items: center; gap: 8px; }
  header .dot { width: 8px; height: 8px; border-radius: 50%; background: #5cff7e; }
  header .title { font-weight: 600; }
  header .meta { color: #888; font-size: 12px; margin-left: auto; }
  #log { flex: 1; overflow-y: auto; padding: 16px; }
  .row { margin: 8px 0; max-width: 80%; line-height: 1.5; }
  .row.user { margin-left: auto; }
  .row .who { font-size: 11px; color: #888; margin-bottom: 2px; }
  .row.user .who { text-align: right; }
  .bubble { padding: 8px 12px; border-radius: 10px; word-wrap: break-word; white-space: pre-wrap; }
  .row.assistant .bubble { background: #2a2a2a; color: #fff; }
  .row.user .bubble { background: #d96d36; color: #fff; }
  .row .ts { font-size: 10px; color: #666; margin-top: 2px; }
  .row.user .ts { text-align: right; }
  footer { padding: 10px; background: #111; border-top: 1px solid #333; display: flex; gap: 8px; }
  textarea { flex: 1; background: #222; color: #fff; border: 1px solid #333; border-radius: 6px; padding: 8px; font: inherit; resize: none; min-height: 38px; max-height: 120px; }
  button { background: #d96d36; color: #fff; border: 0; border-radius: 6px; padding: 0 18px; font: inherit; cursor: pointer; }
  button:disabled { opacity: .4; cursor: default; }
  .empty { text-align: center; color: #666; padding: 40px; }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <span class="title">Cc · Web Chat</span>
  <span class="meta" id="meta">加载中...</span>
</header>
<main id="log"><div class="empty">连接中...</div></main>
<footer>
  <textarea id="input" placeholder="发消息给 Cc (Cmd/Ctrl + Enter 发送)" rows="1"></textarea>
  <button id="send">发送</button>
</footer>
<script>
  const log = document.getElementById('log');
  const meta = document.getElementById('meta');
  const dot = document.getElementById('dot');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  let lastTs = null;
  let seenKeys = new Set();
  let firstLoad = true;

  function fmtTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      const pad = n => String(n).padStart(2, '0');
      return pad(d.getHours()) + ':' + pad(d.getMinutes());
    } catch (e) { return ts.slice(11, 16); }
  }

  function renderRecord(r) {
    const key = (r.ts || '') + '|' + (r.role || '') + '|' + (r.text || '').slice(0, 64);
    if (seenKeys.has(key)) return;
    seenKeys.add(key);
    const row = document.createElement('div');
    row.className = 'row ' + (r.role === 'user' ? 'user' : 'assistant');
    const who = document.createElement('div');
    who.className = 'who';
    who.textContent = r.role === 'user' ? '你' : 'Cc';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = r.text || '';
    const ts = document.createElement('div');
    ts.className = 'ts';
    ts.textContent = fmtTime(r.ts);
    row.appendChild(who); row.appendChild(bubble); row.appendChild(ts);
    log.appendChild(row);
  }

  async function poll() {
    try {
      const url = lastTs ? '/chat/history?since=' + encodeURIComponent(lastTs) : '/chat/history?limit=200';
      const res = await fetch(url, { cache: 'no-store' });
      const data = await res.json();
      if (data.ok && Array.isArray(data.records)) {
        if (firstLoad) {
          log.innerHTML = '';
          firstLoad = false;
        }
        for (const r of data.records) {
          renderRecord(r);
          if (r.ts && (!lastTs || r.ts > lastTs)) lastTs = r.ts;
        }
        log.scrollTop = log.scrollHeight;
        meta.textContent = '在线 · ' + (lastTs ? fmtTime(lastTs) : '--');
        dot.style.background = '#5cff7e';
      }
    } catch (e) {
      meta.textContent = '断线 重试中';
      dot.style.background = '#ff5c5c';
    }
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    try {
      const res = await fetch('/chat/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      });
      if (res.ok) {
        input.value = '';
        await poll();
      } else {
        alert('发送失败 ' + res.status);
      }
    } catch (e) {
      alert('网络出错 ' + e.message);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  });

  poll();
  setInterval(poll, 2000);
  input.focus();
</script>
</body>
</html>
"""


def _channel_transport_post(
    state: "ServerState",
    *,
    message_id: str,
    contact_id: str,
    text: str,
    metadata: dict[str, Any],
) -> tuple[bool, str]:
    """POST a message to the channel transport (same endpoint as /chat/send).

    Standalone (no PushHandler) so the tool dispatcher can reuse the exact
    transport the iOS app uses. Returns (ok, error). Errors are token-redacted.
    """
    import urllib.error
    import urllib.request

    def _redact(value: Any) -> str:
        out = str(value or "").strip()
        if state.channel_transport_token:
            out = out.replace(state.channel_transport_token, "[redacted]")
        return out[:500]

    url = f"{state.channel_transport_url}/messages"
    payload = {
        "message_id": message_id,
        "contact_id": contact_id,
        "text": text,
        "quoted_ts": None,
        "metadata": metadata,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if state.channel_transport_token:
        headers["X-Auth-Token"] = state.channel_transport_token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
            req, timeout=max(0.1, state.channel_transport_timeout_seconds)
        ) as resp:
            status = int(resp.status)
            raw = resp.read(64 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read(4096).decode("utf-8", errors="replace")
        return False, _redact(f"http {e.code}: {raw}")
    except urllib.error.URLError as e:
        return False, _redact(f"request failed: {e.reason}")
    except Exception as e:
        return False, _redact(f"request failed: {e}")

    if not (200 <= status < 300):
        return False, _redact(f"http {status}: {raw[:500]}")
    try:
        response_json = json.loads(raw) if raw.strip() else {}
    except Exception:
        return False, _redact(f"http {status}: invalid json response")
    if isinstance(response_json, dict) and response_json.get("ok") is False:
        err = response_json.get("error") or response_json.get("message") or "ok false"
        return False, _redact(err)
    return True, ""


def _kill_and_reap_tmux_loader(process: subprocess.Popen, *, timeout: float = 1.0) -> None:
    """Bounded cleanup for a stuck ``tmux load-buffer`` child."""

    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        # SIGKILL should make this exceptional path extremely short.  Keep the
        # second wait bounded too; never trade a leaked child for a hung HTTP
        # worker holding the XiaoKe exact-turn lock forever.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=timeout)


def _tmux_interrupt_uncertain_injection(
    session: str,
    *,
    phase: str,
    error: str,
) -> TmuxInjectionResult:
    """Try to clear a prompt whose paste/submission result is uncertain."""

    try:
        cleanup = subprocess.run(
            ["tmux", "send-keys", "-t", session, "C-c"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if cleanup.returncode == 0:
            return TmuxInjectionResult(
                False,
                f"{error}; terminal input cleared",
                phase,
                injection_uncertain=False,
                cleanup_confirmed=True,
            )
        detail = cleanup.stderr.strip() or f"tmux exit {cleanup.returncode}"
    except Exception as exc:
        detail = str(exc)
    return TmuxInjectionResult(
        False,
        f"{error}; terminal cleanup failed: {detail}",
        phase,
        injection_uncertain=True,
        cleanup_confirmed=False,
    )


def _direct_tmux_injection(session: str, text: str) -> TmuxInjectionResult:
    """Inject one prompt through a private, one-use tmux buffer.

    Buffer names are generated by the server and passed explicitly to both
    load and paste.  A concurrent direct injector can therefore never replace
    this prompt between those two commands.  The buffer is deleted on every
    exit path.
    """

    try:
        has = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, text=True, timeout=2,
        )
        if has.returncode != 0:
            return TmuxInjectionResult(False, f"tmux session not found: {session}", "has_session")
    except FileNotFoundError:
        return TmuxInjectionResult(False, "tmux not installed", "has_session")
    except Exception as e:
        return TmuxInjectionResult(False, f"tmux has-session check failed: {e}", "has_session")

    buffer_name = f"ccc-direct-{secrets.token_hex(16)}"
    result: TmuxInjectionResult | None = None
    try:
        loader = subprocess.Popen(
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _stdout, load_stderr = loader.communicate(input=text.encode("utf-8"), timeout=3)
        except subprocess.TimeoutExpired:
            _kill_and_reap_tmux_loader(loader)
            result = TmuxInjectionResult(False, "tmux load-buffer timed out", "load")
        else:
            if loader.returncode != 0:
                detail = (load_stderr or b"").decode("utf-8", errors="replace").strip()
                result = TmuxInjectionResult(
                    False,
                    f"tmux load-buffer failed: {detail or f'exit {loader.returncode}'}",
                    "load",
                )

        if result is None:
            try:
                paste = subprocess.run(
                    ["tmux", "paste-buffer", "-b", buffer_name, "-t", session, "-p"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except subprocess.TimeoutExpired:
                result = _tmux_interrupt_uncertain_injection(
                    session,
                    phase="paste",
                    error="tmux paste-buffer timed out",
                )
            except Exception as exc:
                result = _tmux_interrupt_uncertain_injection(
                    session,
                    phase="paste",
                    error=f"tmux paste-buffer failed: {exc}",
                )
            else:
                if paste.returncode != 0:
                    result = TmuxInjectionResult(
                        False,
                        f"tmux paste-buffer failed: {paste.stderr.strip() or f'exit {paste.returncode}'}",
                        "paste",
                    )

        if result is None:
            try:
                send = subprocess.run(
                    ["tmux", "send-keys", "-t", session, "Enter"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            except subprocess.TimeoutExpired:
                result = _tmux_interrupt_uncertain_injection(
                    session,
                    phase="enter",
                    error="tmux send-keys Enter timed out",
                )
            except Exception as exc:
                result = _tmux_interrupt_uncertain_injection(
                    session,
                    phase="enter",
                    error=f"tmux send-keys Enter failed: {exc}",
                )
            else:
                if send.returncode != 0:
                    result = _tmux_interrupt_uncertain_injection(
                        session,
                        phase="enter",
                        error=(
                            "tmux send-keys Enter failed: "
                            f"{send.stderr.strip() or f'exit {send.returncode}'}"
                        ),
                    )
                else:
                    result = TmuxInjectionResult(True, "", "submitted")
    except FileNotFoundError:
        result = TmuxInjectionResult(False, "tmux not installed", "load")
    except Exception as exc:
        result = TmuxInjectionResult(False, f"tmux inject failed: {exc}", "load")
    finally:
        try:
            deleted = subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if deleted.returncode != 0:
                logger.warning(
                    "tmux delete-buffer failed buffer=%s: %s",
                    buffer_name,
                    deleted.stderr.strip(),
                )
        except Exception as exc:
            logger.warning("tmux delete-buffer failed buffer=%s: %s", buffer_name, exc)
    return result or TmuxInjectionResult(False, "tmux injection failed", "unknown")


def _inject_to_tmux_session(state: "ServerState", session: str, text: str) -> tuple[bool, str]:
    """Direct tmux fallback used by scheduled dispatcher delivery."""

    return tuple(_direct_tmux_injection(session, text))  # type: ignore[return-value]


# Toolbot command-menu model allowlist (mirrors ccbot-lite AVAILABLE_MODELS).
# Only these concrete ids may be passed to `/model` via /toolbot/command;
# aliases are resolved to a member of TOOLBOT_MODEL_ALLOWLIST. Anything else is
# rejected — no free-form model string ever reaches the tmux injection.
TOOLBOT_MODEL_ALLOWLIST: frozenset = frozenset({
    "claude-fable-5",
    "claude-opus-4-6",
    "claude-opus-4-6[1m]",
    "claude-opus-4-7",
    "claude-opus-4-7[1m]",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
})
TOOLBOT_MODEL_ALIASES: dict[str, str] = {
    "fable": "claude-fable-5",
    "fable5": "claude-fable-5",
    "fable-5": "claude-fable-5",
    "opus": "claude-opus-4-6",
    "opus-1m": "claude-opus-4-6[1m]",
    "opus4.7": "claude-opus-4-7",
    "opus4.7-1m": "claude-opus-4-7[1m]",
    "opus4.8": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class ServerState:
    def __init__(self, config: dict[str, Any], sandbox_override: bool | None = None):
        apns_cfg = config.get("apns", {})
        _apns_required = ("p8_path", "team_id", "key_id", "bundle_id")
        self.apns_enabled: bool = all(apns_cfg.get(k) for k in _apns_required)
        if self.apns_enabled:
            self.bundle_id: str = apns_cfg["bundle_id"]
            self.team_id: str = apns_cfg["team_id"]
            self.key_id: str = apns_cfg["key_id"]
            self.p8_path: str = apns_cfg["p8_path"]
            self.sandbox: bool = (
                sandbox_override
                if sandbox_override is not None
                else apns_cfg.get("sandbox", True)
            )
        else:
            self.bundle_id = ""
            self.team_id = ""
            self.key_id = ""
            self.p8_path = ""
            self.sandbox = False

        server_cfg = config.get("server", {})
        self.host: str = server_cfg.get("host", "127.0.0.1")
        self.port: int = int(server_cfg.get("port", 8291))
        self.token_store_path: str = server_cfg.get(
            "token_store_path", str(HERE / "tokens" / "active.json")
        )
        # P0-3: auto-generate secret if not set
        raw_secret = server_cfg.get("shared_secret") or ""
        if not raw_secret:
            raw_secret = _load_or_create_secret()
        self.shared_secret: str | None = raw_secret or None
        # P0-1: strict_auth defaults to True (secure-by-default for CcCompanion community release)
        self.strict_auth: bool = bool(server_cfg.get("strict_auth", True))
        self.allow_public_bind: bool = bool(server_cfg.get("allow_public_bind", False))
        self.allow_remote_control: bool = bool(server_cfg.get("allow_remote_control", False))
        self.allowed_ips: list[str] = list(server_cfg.get("allowed_ips", []) or [])
        self.default_session: str = server_cfg.get("default_session", "cc")
        self.channel_transport_enabled: bool = bool(server_cfg.get("channel_transport_enabled", False))
        self.channel_transport_url: str = str(
            server_cfg.get("channel_transport_url", "http://127.0.0.1:8810")
        ).rstrip("/")
        # Warn if channel transport URL points to a non-localhost address
        try:
            from urllib.parse import urlparse
            _ct_host = urlparse(self.channel_transport_url).hostname or ""
            if _ct_host not in ("127.0.0.1", "localhost", "::1"):
                logger.warning(
                    "channel_transport_url points to non-localhost host %r — "
                    "ensure this is intentional and the endpoint is trusted",
                    _ct_host,
                )
        except Exception:
            pass
        self.channel_transport_token: str = str(server_cfg.get("channel_transport_token", "") or "")
        self.channel_transport_contacts: list[str] = [
            str(item).strip().lower()
            for item in (server_cfg.get("channel_transport_contacts", ["xiaoke"]) or [])
            if str(item).strip()
        ]
        self.channel_transport_fallback_to_tmux: bool = bool(
            server_cfg.get("channel_transport_fallback_to_tmux", True)
        )
        self.channel_transport_timeout_seconds: float = float(
            server_cfg.get("channel_transport_timeout_seconds", 5)
        )

        auth_cfg = config.get("auth", {})
        self.login_username: str = str(auth_cfg.get("username", "") or "")
        self.login_password: str = str(auth_cfg.get("password", "") or "")
        self.login_ghp_token: str = str(auth_cfg.get("ghp_token", "") or "")
        public_server_url = str(auth_cfg.get("server_url", "") or "").strip()
        self.public_server_url: str = (
            public_server_url or "https://companion-vps2.xiaonancaleb.xyz"
        ).rstrip("/")

        if self.apns_enabled:
            self.jwt = APNsJWT(
                p8_path=self.p8_path,
                key_id=self.key_id,
                team_id=self.team_id,
            )
            # primary client 跟 self.sandbox 配合 (默认是 config 里设的)
            self.client = APNsClient(
                bundle_id=self.bundle_id,
                jwt_provider=self.jwt,
                sandbox=self.sandbox,
            )
            # alt client 跟 primary 相反 当 BadDeviceToken 时 fallback 试这个
            # 解 5-1 BadDeviceToken 反复问题 — token 的 endpoint 不一定跟 server 配置一致
            # (例 TestFlight 通常 prod 但开发 build 是 sandbox 一台 device 在两种 build 间切会改 endpoint)
            self.client_alt = APNsClient(
                bundle_id=self.bundle_id,
                jwt_provider=self.jwt,
                sandbox=not self.sandbox,
            )
            self._primary_endpoint = "sandbox" if self.sandbox else "prod"
            self._alt_endpoint = "prod" if self.sandbox else "sandbox"
            self.notification_client = APNsClient(
                bundle_id=self.bundle_id,
                jwt_provider=self.jwt,
                sandbox=False,
            )
        else:
            self.jwt = None
            self.client = None
            self.client_alt = None
            self._primary_endpoint = None
            self._alt_endpoint = None
            self.notification_client = None

        self.tokens = TokenStore(self.token_store_path)

        # standard remote notification device tokens (非 Live Activity)
        device_tokens_path = Path(self.token_store_path).parent / "device_tokens.jsonl"
        self.device_tokens = DeviceTokenStore(device_tokens_path)

        # task queue 持久化跟 token 同目录
        task_queue_path = Path(self.token_store_path).parent / "task_queue.json"
        self.tasks = TaskQueue(task_queue_path)

        # chat history 持久化跟 token 同目录
        chat_history_path = Path(self.token_store_path).parent / "chat_history.jsonl"
        self.chat = ChatHistory(chat_history_path)
        contact_history_dir = Path(self.token_store_path).parent
        self.contact_chats: dict[str, ChatHistory] = {
            "xiaoke": self.chat,
            "kairos": ChatHistory(contact_history_dir / "chat_history_kairos.jsonl"),
            "hajiki": ChatHistory(contact_history_dir / "chat_history_hajiki.jsonl"),
            "apples": ChatHistory(contact_history_dir / "chat_history_apples.jsonl"),
            # 小克·工具版 (toolbot) — 只读派活存档窗口。scheduler 往这里写派活记录，
            # app 端围观；用户不能往这个 contact 发消息 (chat/send 会 501)。
            "toolbot": ChatHistory(contact_history_dir / "chat_history_toolbot.jsonl"),
        }
        self.group_reply_lock = threading.Lock()
        self.group_reply_pending: list[dict[str, Any]] = []
        self.codex_bot_state_path: str = server_cfg.get(
            "codex_bot_state_path",
            "/root/Windows-Codex-TG/.runtime/bot_state.json",
        )
        self.codex_user_id: str = str(server_cfg.get("codex_user_id", "8715009653"))
        self.codex_shared_session_name: str = str(server_cfg.get("codex_shared_session_name", "kairos") or "kairos")
        self.codex_bin: str = server_cfg.get("codex_bin", "/usr/bin/codex")
        self.codex_home: str = server_cfg.get("codex_home", "/root/.codex")
        self.codex_model: str = server_cfg.get("codex_model", "gpt-5.5")
        self.codex_reasoning_effort: str = server_cfg.get("codex_reasoning_effort", "high")
        self.codex_kairos_backend: str = str(
            server_cfg.get("codex_kairos_backend", "app-server") or "app-server"
        ).strip().lower()
        if self.codex_kairos_backend not in {"app-server", "legacy-exec"}:
            logger.warning("invalid codex_kairos_backend; using app-server")
            self.codex_kairos_backend = "app-server"
        self.codex_app_server_fallback_to_exec: bool = bool(
            server_cfg.get("codex_app_server_fallback_to_exec", False)
        )
        self.codex_auto_forge_enabled: bool = bool(
            server_cfg.get("codex_auto_forge_enabled", True)
        )
        try:
            self.codex_auto_forge_threshold_percent = float(
                server_cfg.get("codex_auto_forge_threshold_percent", 80.0)
            )
        except (TypeError, ValueError):
            self.codex_auto_forge_threshold_percent = 80.0
        if not 1.0 <= self.codex_auto_forge_threshold_percent <= 100.0:
            logger.warning("invalid codex_auto_forge_threshold_percent; using 80")
            self.codex_auto_forge_threshold_percent = 80.0
        try:
            retain_messages = int(server_cfg.get("codex_auto_forge_retain_messages", 80))
        except (TypeError, ValueError):
            retain_messages = 80
        self.codex_auto_forge_retain_messages: int = max(20, min(160, retain_messages))
        raw_compact_limit = server_cfg.get("codex_app_server_model_auto_compact_token_limit")
        try:
            self.codex_app_server_model_auto_compact_token_limit: int | None = (
                int(raw_compact_limit) if raw_compact_limit not in {None, ""} else None
            )
        except (TypeError, ValueError):
            logger.warning("invalid codex_app_server_model_auto_compact_token_limit; not overriding")
            self.codex_app_server_model_auto_compact_token_limit = None
        if (
            self.codex_app_server_model_auto_compact_token_limit is not None
            and self.codex_app_server_model_auto_compact_token_limit <= 0
        ):
            logger.warning("non-positive codex app-server compact limit; not overriding")
            self.codex_app_server_model_auto_compact_token_limit = None
        self.codex_app_bridge = CodexAppBridge(
            codex_bin=self.codex_bin,
            codex_home=self.codex_home,
            logger=logger,
            model_auto_compact_token_limit=(
                self.codex_app_server_model_auto_compact_token_limit
            ),
        )
        self.codex_auto_forge_claims = AutoForgeClaimStore(
            Path(self.token_store_path).expanduser().parent / "codex_auto_forge.json"
        )
        group_chat_path = Path(self.token_store_path).parent / "group_chat.jsonl"
        group_state_path = Path(self.token_store_path).parent / "group_state.json"
        self.group_chat = GroupChatStore(group_chat_path, group_state_path)
        calendar_path = Path(self.token_store_path).parent / "calendar_events.jsonl"
        self.calendar = CalendarStore(calendar_path)
        self.rp_history = RPHistory("/tmp")
        self.ai_chat = AIChatManager(HERE / "state")
        self.task_buffer = EphemeralTaskBuffer(capacity=100)
        # 流式回复广播 (2026-07-12): channel reply_chunk → /chat/stream_chunk → SSE
        self.chat_stream_bus = ChatStreamBus()
        # Handy-Clawd pet state (2026-05-08 用户 push)
        from pet_state import PetState, PetStateBus, PetBubbleBus, PetActivityBus
        pet_state_path = Path(self.token_store_path).parent / "pet_state.json"
        self.pet = PetState(pet_state_path)
        self.pet_bus = PetStateBus()
        self.pet_bubble_bus = PetBubbleBus()
        self.pet_activity_bus = PetActivityBus()
        # typing indicator 状态 (内存 不持久化)
        self.typing_state: dict[str, Any] = {"is_typing": False, "since": None}
        self.contact_typing_states: dict[str, dict[str, Any]] = {
            "xiaoke": self.typing_state,
            "kairos": {"is_typing": False, "since": None},
            "hajiki": {"is_typing": False, "since": None},
            "apples": {"is_typing": False, "since": None},
            "toolbot": {"is_typing": False, "since": None},
        }
        # XiaoKe's Stop action is fenced by the exact App turn and dedicated
        # tmux session.  This process-local state intentionally fails closed
        # after a backend restart: a pre-restart Stop must never send keys to a
        # newer Claude turn merely because the tmux session name is unchanged.
        self.xiaoke_stop_lock = threading.RLock()
        self.xiaoke_stop_tombstone: dict[str, Any] = {}
        self.xiaoke_stopping_claim: dict[str, Any] = {}
        self.xiaoke_send_reservation: dict[str, Any] = {}
        # In-memory assistant drafts for polling clients. Drafts are transient UI
        # state only; final assistant replies remain in chat_history jsonl.
        self.chat_draft_lock = threading.Lock()
        self.chat_drafts: dict[str, dict[str, Any]] = {}
        self.chat_reply_states: dict[str, dict[str, Any]] = {}
        self.kairos_queue_lock = threading.Lock()
        self.kairos_queue_path = contact_history_dir / "kairos_queue.json"
        self.kairos_queue: deque[dict[str, Any]] = self._load_kairos_queue()
        self.kairos_queue_worker_running = False
        self.kairos_active_task: dict[str, Any] | None = None
        self.kairos_active_task_cancel: threading.Event | None = None
        self.kairos_pending_run_path = contact_history_dir / "kairos_pending_run.json"
        self._recover_pending_kairos_run()
        # 书房 v1 (2026-05-09) — vault-aware project dashboard. read-only db (indexer 写)
        studyroom_db_path = HERE / "state" / "studyroom.db"
        self.studyroom = StudyroomDB(studyroom_db_path)
        self.bus_send_path = server_cfg.get(
            "bus_send_path", str(Path.home() / "scripts" / "bus_send.py")
        )
        # 附件 (图片 / 文件) 存储目录
        attachments_dir = Path(self.token_store_path).expanduser().parent / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir = attachments_dir
        # 用户偏好 settings (TTS toggle 等)
        settings_path = Path(self.token_store_path).expanduser().parent / "settings.json"
        self.settings = Settings(settings_path)
        # 当前活跃 chain session (slash /switch 持久化)
        active_session_path = Path(self.token_store_path).expanduser().parent / "active_session.json"
        self.active_session_path = active_session_path
        self.active_session: str = self.default_session  # default
        if active_session_path.exists():
            try:
                _as = json.loads(active_session_path.read_text())
                self.active_session = _as.get("active_sid", self.default_session)
            except Exception:
                pass
        self.diary = Diary(Path("~/Documents/星原/眠的小家/日记/").expanduser())
        # 2026-05-11 OTS Diary tab — chain↔用户 chat-style journaling stream.
        # Distinct from `self.diary` (vault markdown CRUD) and `self.chat`
        # (open-ended Cc chat). Per-day JSONL under apns-server/diary_chat/.
        diary_stream_dir = Path(self.token_store_path).expanduser().parent / "diary_chat"
        self.diary_stream = DiaryStream(diary_stream_dir)
        self.favorites = Favorites(
            jsonl_path=Path(self.token_store_path).expanduser().parent / "favorites.jsonl",
            vault_path=Path("~/Documents/星原/眠的小家/收藏夹/").expanduser(),
        )
        self.usage = UsageReader()
        self.worklog = Worklog()
        self.timeline = Timeline(self.diary, self.chat, self.tasks, self.worklog)
        # 五子棋 client_msg_id 去重缓存 (内存 LRU 100 条)
        self.gomoku_msg_cache: OrderedDict[str, dict] = OrderedDict()
        # 定时 reminder 队列
        reminders_path = Path(self.token_store_path).parent / "reminders.jsonl"
        self.reminders = ReminderStore(reminders_path)
        # 小克·工具版 (tool-version dispatcher) — rule-driven, no-AI scheduler.
        # 在排定时间把 trigger 文本走和 /chat/send 一样的通道注入主 session，
        # 同时写进 chat history 让用户可见。
        self.tool_schedule_enabled: bool = bool(
            server_cfg.get("tool_dispatcher_enabled", True)
        )
        tool_schedule_path = server_cfg.get("tool_dispatcher_schedule_path") or str(
            Path(self.token_store_path).parent / "tool_dispatcher.json"
        )
        self.tool_schedule = ScheduleStore(tool_schedule_path)
        if bool(server_cfg.get("tool_dispatcher_seed_default", True)):
            self.tool_schedule.ensure_seed(DEFAULT_SCHEDULE)
        self.tool_dispatcher = ToolDispatcher(
            self.tool_schedule,
            self.deliver_trigger,
            tick_seconds=float(server_cfg.get("tool_dispatcher_tick_seconds", 20)),
        )
        # 服务器启动时间 (unix timestamp) — 用于 uptime 计算
        self.started_at: float = time.time()
        # 完整 config 引用 (anthropic dashboard url 等)
        self.config: dict[str, Any] = config

        logger.info(
            "loaded apns_enabled=%s bundle_id=%s sandbox=%s store=%s tokens=%d tasks_active=%s",
            self.apns_enabled,
            self.bundle_id or "(none)",
            self.sandbox,
            self.token_store_path,
            len(self.tokens.all_active()),
            self.tasks.snapshot()["active"]["title"] if self.tasks.snapshot()["active"] else None,
        )

    def _load_kairos_queue(self) -> deque[dict[str, Any]]:
        try:
            if not self.kairos_queue_path.exists():
                return deque()
            payload = json.loads(self.kairos_queue_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                return deque()
            tasks = deque()
            for item in payload:
                if isinstance(item, dict) and str(item.get("user_ts") or "").strip():
                    tasks.append(item)
            return tasks
        except Exception:
            logger.warning("load kairos queue failed", exc_info=True)
            return deque()

    def persist_kairos_queue_locked(self) -> None:
        try:
            items = list(self.kairos_queue)
            if items:
                self.kairos_queue_path.write_text(
                    json.dumps(items, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                self.kairos_queue_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("persist kairos queue failed", exc_info=True)

    def mark_kairos_pending_run(self, contact_id: str, user_ts: str, text: str) -> None:
        contact_id = (contact_id or "kairos").strip().lower() or "kairos"
        preview = " ".join(str(text or "").split())[:300]
        payload = {
            "contact_id": contact_id,
            "user_ts": str(user_ts or ""),
            "text_preview": preview,
            "draft_text": "",
            "draft_updated_at": "",
            "started_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
        }
        try:
            self.kairos_pending_run_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("mark kairos pending run failed", exc_info=True)

    def update_kairos_pending_draft(self, contact_id: str, user_ts: str, text: str) -> None:
        contact_id = (contact_id or "kairos").strip().lower() or "kairos"
        user_ts = str(user_ts or "")
        draft_text = str(text or "")
        if not user_ts or not draft_text.strip():
            return
        try:
            if not self.kairos_pending_run_path.exists():
                return
            try:
                payload = json.loads(self.kairos_pending_run_path.read_text(encoding="utf-8"))
            except Exception:
                return
            if str(payload.get("contact_id") or "kairos").strip().lower() != contact_id:
                return
            if str(payload.get("user_ts") or "") != user_ts:
                return
            payload["draft_text"] = draft_text[-20000:]
            payload["draft_updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
            self.kairos_pending_run_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("update kairos pending draft failed", exc_info=True)

    def clear_kairos_pending_run(self, user_ts: str | None = None) -> None:
        try:
            if user_ts and self.kairos_pending_run_path.exists():
                try:
                    payload = json.loads(self.kairos_pending_run_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
                if str(payload.get("user_ts") or "") not in {"", str(user_ts)}:
                    return
            self.kairos_pending_run_path.unlink(missing_ok=True)
        except Exception:
            logger.warning("clear kairos pending run failed", exc_info=True)

    def _recover_pending_kairos_run(self) -> None:
        path = getattr(self, "kairos_pending_run_path", None)
        if not path or not Path(path).exists():
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            logger.warning("recover kairos pending run: unreadable pending file", exc_info=True)
            self.clear_kairos_pending_run()
            return
        contact_id = str(payload.get("contact_id") or "kairos").strip().lower() or "kairos"
        user_ts = str(payload.get("user_ts") or "")
        chat = self.contact_chats.get(contact_id)
        if chat is None or not user_ts:
            self.clear_kairos_pending_run()
            return
        try:
            later = chat.read_since(since_ts=user_ts, limit=200, include_hidden=True)
            if not any(rec.get("role") == "assistant" for rec in later):
                draft_text = str(payload.get("draft_text") or "").strip()
                if draft_text:
                    recovery_text = (
                        draft_text
                        + "\n\n[这条回复在后端重启或进程退出时中断，以上是已经生成的草稿。]"
                    )
                else:
                    recovery_text = (
                        "上一条 Kairos 回复在后端重启或进程退出时中断了，没有完成落库。"
                        "你可以直接重发；我不会再让它静默消失。"
                    )
                chat.append(
                    role="assistant",
                    text=recovery_text,
                    source="codex:kairos:recovered",
                )
                logger.warning(
                    "recovered interrupted kairos run contact_id=%s user_ts=%s",
                    contact_id,
                    user_ts,
                )
        finally:
            self.clear_kairos_pending_run(user_ts)

    def deliver_trigger(self, contact_id: str, text: str, rule_id: str) -> tuple[bool, str]:
        """Deliver a scheduled tool-dispatcher trigger into the live session.

        Inject into the session via channel transport (preferred), falling
        back to direct tmux injection when transport is disabled/unavailable.
        On success, archive to toolbot window (chat_history_toolbot.jsonl).
        The trigger text is intentionally NOT written to the contact's own
        chat history — it must not appear as a user message in their chat view.
        Returns (ok, error). On failure nothing is marked served by the caller,
        so it retries on the next tick within the rule's grace window.
        """
        contact_id = (contact_id or "xiaoke").strip().lower() or "xiaoke"

        from datetime import datetime as _dt
        ts_prefix = "[" + _dt.now().strftime("%Y-%m-%d %H:%M:%S") + "]"
        injected = f"{ts_prefix} {text}"

        # 1) inject — scheduled triggers remain channel-transport first.
        if self.channel_transport_enabled and contact_id in self.channel_transport_contacts:
            message_id = f"tool:{rule_id}:{int(time.time())}"
            metadata = {
                "source": "tool-dispatcher",
                "transport": "channel",
                "tool_dispatcher_rule": rule_id,
            }
            ok, err = _channel_transport_post(
                self,
                message_id=message_id,
                contact_id=contact_id,
                text=injected,
                metadata=metadata,
            )
            if ok:
                # 2) archive after successful injection — prevents duplicate
                #    "已派活" entries when injection fails and retries occur.
                if contact_id != "toolbot":
                    self.toolbot_archive(
                        f"已派活 → @{contact_id}（{rule_id}）\n{text}",
                        source="tool-dispatcher",
                        metadata={
                            "tool_dispatcher_rule": rule_id,
                            "dispatched_to": contact_id,
                            "trigger": True,
                        },
                    )
                return True, ""
            logger.warning("deliver_trigger: channel transport failed rule=%s: %s", rule_id, err)
            if not self.channel_transport_fallback_to_tmux:
                return False, err or "channel transport failed"

        # fallback: direct tmux injection into the active session.
        target = (self.active_session or self.default_session).strip()
        ok, err = _inject_to_tmux_session(self, target, injected)
        if not ok:
            return False, f"tmux inject to '{target}' failed: {err}"
        # 2) archive after successful tmux injection.
        if contact_id != "toolbot":
            self.toolbot_archive(
                f"已派活 → @{contact_id}（{rule_id}）\n{text}",
                source="tool-dispatcher",
                metadata={
                    "tool_dispatcher_rule": rule_id,
                    "dispatched_to": contact_id,
                    "trigger": True,
                },
            )
        return True, ""

    def toolbot_archive(
        self,
        text: str,
        *,
        title: str | None = None,
        role: str = "assistant",
        source: str = "toolbot-broadcast",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Append a record to the 小克·工具版 (toolbot) window.

        Single source of truth for writing into the toolbot public-archive
        window. Used by deliver_trigger's archive write, by /toolbot/broadcast
        (system sentinels / daily reports), and by /toolbot/command (command
        execution results). Failures are swallowed (returns None) so callers
        never break their primary flow on an archive write.
        """
        toolbot_chat = self.contact_chats.get("toolbot")
        if toolbot_chat is None:
            return None
        body = text or ""
        if title:
            body = f"【{title}】\n{body}" if body else f"【{title}】"
        meta: dict[str, Any] = {"toolbot_archive": True}
        if metadata and isinstance(metadata, dict):
            meta.update(metadata)
        try:
            return toolbot_chat.append(
                role=role,
                text=body,
                source=source,
                metadata=meta,
            )
        except Exception as e:
            logger.warning("toolbot_archive append failed: %s", e)
            return None

    def shutdown(self):
        self.codex_app_bridge.close()
        if self.client:
            self.client.close()


# ---------- helpers ----------


def _state_to_payload(body: dict[str, Any]) -> dict[str, Any]:
    """body -> APNs content-state 字段名跟 swift 端 ActivityAttributes.ContentState 对齐

    必须填 ContentState 所有 non-optional 字段否则 Swift Codable decode 失败
    ActivityKit 静默丢弃 update widget 不刷新.

    ContentState non-optional: status / unreadCount
    ContentState optional: lastMessagePreview / sourceChannel / lastUpdate
    """
    cs: dict[str, Any] = {
        # non-optional 默认值
        "status": "idle",
        "unreadCount": 0,
    }

    state = body.get("state")
    if state:
        # client 兼容: "spoken" -> "spoke" (旧 script alias)
        cs["status"] = "spoke" if state == "spoken" else state
    if "preview" in body:
        cs["lastMessagePreview"] = str(body["preview"])[:200]
    if "channel" in body:
        cs["sourceChannel"] = str(body["channel"])
    if "unread" in body:
        cs["unreadCount"] = int(body["unread"])
    elif "message_count" in body:
        cs["unreadCount"] = int(body["message_count"])

    # 任务进度字段 (A+C 模式)
    if "task_label" in body:
        cs["taskLabel"] = str(body["task_label"])[:12]
    if "task_title" in body:
        cs["taskTitle"] = str(body["task_title"])[:50]
    if "task_progress" in body:
        cs["taskProgress"] = float(body["task_progress"])
    if "task_current" in body:
        cs["taskCurrent"] = int(body["task_current"])
    if "task_total" in body:
        cs["taskTotal"] = int(body["task_total"])
    if "task_step" in body:
        cs["taskStep"] = str(body["task_step"])[:80]

    if "completed_titles" in body:
        cs["completedTitles"] = [str(t)[:30] for t in body["completed_titles"]][:5]

    return cs


# ---------- HTTP handler ----------


class PushHandler(BaseHTTPRequestHandler):
    state: ServerState  # set by run_server before serving

    server_version = "CcAPNsServer/0.1"

    def log_message(self, format: str, *args):
        logger.info("%s %s", self.address_string(), format % args)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _check_auth(self) -> bool:
        if self._auth_matches():
            return True
        return not self.state.strict_auth

    def _auth_matches(self) -> bool:
        if not self.state.shared_secret:
            return True
        token = self.headers.get("X-Auth-Token", "") or self.headers.get("X-Auth", "")
        return token == self.state.shared_secret

    def _require_auth(self) -> bool:
        if self._auth_matches():
            return True
        if not self.state.strict_auth:
            ip = self.client_address[0] if self.client_address else "unknown"
            logger.warning(
                "unauthenticated request allowed strict_auth=false ip=%s method=%s path=%s",
                ip,
                self.command,
                self.path,
            )
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _require_write_auth(self) -> bool:
        return self._require_auth()

    def _is_public_get(self) -> bool:
        if self.path == "/health" and not self.headers.get("X-Forwarded-For"):
            return True
        return self.path in {"/version"}

    def _clean_contact_id(self, value: Any) -> str:
        contact_id = str(value or "xiaoke").strip().lower()
        if contact_id in self.state.contact_chats:
            return contact_id
        return "xiaoke"

    def _query_params(self) -> dict[str, list[str]]:
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(self.path).query)

    def _contact_id_from_query(self) -> str:
        qs = self._query_params()
        return self._clean_contact_id(qs.get("contact_id", qs.get("contactId", ["xiaoke"]))[0])

    def _contact_id_from_body(self, body: dict[str, Any]) -> str:
        return self._clean_contact_id(body.get("contact_id") or body.get("contactId") or "xiaoke")

    def _source_for_request(self, contact_suffix: str = "") -> str:
        """Detect client platform from User-Agent and produce a source tag.

        Returns ``android-app`` for the Android companion, ``ios-app`` for the
        iOS one, ``mobile-app`` when we know it's mobile but not which OS, and
        finally falls back to ``ios-app`` for backwards compatibility when
        nothing identifying is sent (legacy callers / scripts).

        Pass ``contact_suffix`` to get ``"<source>:<suffix>"`` (e.g. ``android-app:apples``).
        """
        try:
            ua = (self.headers.get("User-Agent", "") or "").lower()
        except Exception:
            ua = ""
        xp = ""
        try:
            xp = (self.headers.get("X-Client-Platform", "") or "").lower()
        except Exception:
            pass
        if xp in ("android", "ios"):
            base = "android-app" if xp == "android" else "ios-app"
        elif "android" in ua or "okhttp" in ua or "cccompanion" in ua:
            base = "android-app"
        elif "iphone" in ua or "ipad" in ua or "ios" in ua or "cfnetwork" in ua or "darwin" in ua:
            base = "ios-app"
        elif "mozilla" in ua or "chrome" in ua or "safari" in ua:
            base = "ios-app"  # web inspector; preserve legacy default
        else:
            base = "ios-app"
        if contact_suffix:
            return f"{base}:{contact_suffix}"
        return base

    @staticmethod
    def _client_log_value(value: Any, *, limit: int = CLIENT_LOG_MAX_FIELD) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value[:limit]
        if isinstance(value, list):
            return [PushHandler._client_log_value(item, limit=limit) for item in value[:20]]
        if isinstance(value, dict):
            return {
                str(key)[:120]: PushHandler._client_log_value(val, limit=limit)
                for key, val in list(value.items())[:80]
            }
        return str(value)[:limit]

    def _handle_client_log(self, body: dict[str, Any]) -> None:
        try:
            entry = {
                "server_ts": datetime.now(timezone.utc).isoformat(),
                "source": self._source_for_request(),
                "ip": self.client_address[0] if self.client_address else "",
                "ua": (self.headers.get("User-Agent", "") or "")[:300],
                "body": self._client_log_value(body),
            }
            CLIENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with CLIENT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            event = str(body.get("event") or body.get("type") or "client_log")[:80]
            logger.warning("client_log event=%s source=%s", event, entry["source"])
            self._send_json(200, {"ok": True})
        except Exception as e:
            logger.exception("client_log write failed")
            self._send_json(500, {"ok": False, "error": str(e)})

    def _chat_for_contact(self, contact_id: str) -> ChatHistory:
        return self.state.contact_chats.get(contact_id) or self.state.chat

    def _typing_for_contact(self, contact_id: str) -> dict[str, Any]:
        if contact_id == "xiaoke":
            lock = getattr(self.state, "xiaoke_stop_lock", None)
            if lock is None:
                return self.state.typing_state
            with lock:
                return dict(self.state.typing_state)
        return self.state.contact_typing_states.setdefault(contact_id, {"is_typing": False, "since": None})

    def _set_typing_for_contact(self, contact_id: str, value: dict[str, Any]) -> None:
        if contact_id == "xiaoke":
            lock = getattr(self.state, "xiaoke_stop_lock", None)
            if lock is None:
                self.state.typing_state = value
                self.state.contact_typing_states["xiaoke"] = value
            else:
                with lock:
                    resolved = dict(value)
                    self.state.typing_state = resolved
                    self.state.contact_typing_states["xiaoke"] = resolved
        else:
            self.state.contact_typing_states[contact_id] = value

    def _clear_xiaoke_typing_if_match(self, user_ts: str, session: str | None = None) -> bool:
        """Clear only the XiaoKe turn named by the caller.

        Send failures and late completion callbacks must not hide a newer turn's
        Stop button.  All comparisons happen under the same lock used by
        ``_handle_chat_stop``.
        """
        lock = getattr(self.state, "xiaoke_stop_lock", None) or threading.RLock()
        with lock:
            current = dict(getattr(self.state, "typing_state", {}) or {})
            if str(current.get("since") or "") != str(user_ts or ""):
                return False
            current_session = str(current.get("session") or "")
            if session and current_session and current_session != str(session):
                return False
            value = {"is_typing": False, "since": None}
            self.state.typing_state = value
            self.state.contact_typing_states["xiaoke"] = value
            return True

    def _stamp_xiaoke_stale_completion_locked(self, target_session: str) -> None:
        """Record proof that the CLI finished a turn in the claim's session.

        Caller must hold ``xiaoke_stop_lock``.  Idempotent: a claim that is
        already stamped keeps its earlier timestamp, so repeated beacons can
        never push the grace deadline further out.
        """
        active = dict(self.state.typing_state or {})
        if (
            active.get("is_typing")
            and str(active.get("session") or "") == target_session
            and str(active.get("transport") or "") == "tmux"
            and not active.get("stale_completion_at")
        ):
            active["stale_completion_at"] = time.time()
            self.state.typing_state = active
            self.state.contact_typing_states["xiaoke"] = active

    def _complete_xiaoke_turn_if_match(self, turn_token: str, session: str) -> bool:
        """Apply a Stop-hook completion only to its exact injected App turn.

        A completion that arrives while Ctrl-C is in flight marks that claim as
        completed but deliberately keeps the stopping barrier.  This prevents a
        failed subprocess from resurrecting a turn whose final hook already won.
        """
        token = str(turn_token or "").strip().lower()
        target_session = str(session or "").strip()
        if not target_session:
            return False
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            if token:
                # Malformed identity can never name a turn; drop it.
                return False
            # Empty token is a sessionful end-of-turn beacon from a Stop hook
            # that could not correlate a marker (message merged into a running
            # turn, terminal-typed prompt, ambiguous transcript).  It proves
            # the CLI finished *a* turn in that session, so stamp the active
            # claim for the bounded grace expiry instead of completing it
            # outright — a late beacon must never kill a freshly injected turn.
            with self.state.xiaoke_stop_lock:
                self._stamp_xiaoke_stale_completion_locked(target_session)
            return False
        lock = self.state.xiaoke_stop_lock
        with lock:
            active = dict(self.state.typing_state or {})
            if (
                str(active.get("turn_token") or "").lower() != token
                or str(active.get("session") or "") != target_session
            ):
                # Never complete the active turn from another turn's callback.
                # But a callback naming the same tmux session proves the CLI
                # finished a turn there (it may be idle at a survey/interactive
                # prompt now).  Record the event so the typing poll can expire
                # the unresolved claim after a bounded grace window.
                self._stamp_xiaoke_stale_completion_locked(target_session)
                return False
            stopping = dict(self.state.xiaoke_stopping_claim or {})
            if (
                str(stopping.get("turn_token") or "").lower() == token
                and str(stopping.get("session") or "") == target_session
            ):
                stopping["completed"] = True
                stopping["completed_at"] = time.time()
                self.state.xiaoke_stopping_claim = stopping
                return True
            # Retain the completed turn's identity so clients can dismiss Stop
            # only when this exact terminal payload matches their tracked turn.
            value = {
                "is_typing": False,
                "since": str(active.get("since") or ""),
                "session": str(active.get("session") or ""),
                "transport": "tmux",
                "turn_token": token,
                "completed": True,
            }
            self.state.typing_state = value
            self.state.contact_typing_states["xiaoke"] = value
            return True

    def _release_xiaoke_send_reservation(self, turn_token: str) -> bool:
        token = str(turn_token or "").strip().lower()
        with self.state.xiaoke_stop_lock:
            reservation = dict(self.state.xiaoke_send_reservation or {})
            if str(reservation.get("turn_token") or "").lower() != token:
                return False
            self.state.xiaoke_send_reservation = {}
            return True

    def _activate_xiaoke_send_reservation(
        self,
        *,
        turn_token: str,
        user_ts: str,
        session: str,
        transport: str,
    ) -> bool:
        token = str(turn_token or "").strip().lower()
        with self.state.xiaoke_stop_lock:
            reservation = dict(self.state.xiaoke_send_reservation or {})
            if str(reservation.get("turn_token") or "").lower() != token:
                return False
            value = {
                "is_typing": True,
                "since": str(user_ts or ""),
                "session": str(session or ""),
                "transport": str(transport or ""),
                "turn_token": token,
            }
            self.state.typing_state = value
            self.state.contact_typing_states["xiaoke"] = value
            self.state.xiaoke_send_reservation = {}
            return True

    def _set_chat_draft(
        self,
        contact_id: str,
        text: str,
        *,
        source: str | None = None,
        session_id: str | None = None,
        user_ts: str | None = None,
        queued_at: str | None = None,
        started_at: str | None = None,
        activity_text: str | None = None,
        activity_count: int = 0,
        activity_items: list[str] | None = None,
    ) -> None:
        draft_text = str(text or "")
        if not draft_text.strip():
            return
        now = datetime.now(timezone.utc).isoformat()
        items = [str(item).strip() for item in (activity_items or []) if str(item).strip()]
        with self.state.chat_draft_lock:
            self.state.chat_drafts[contact_id] = {
                "contact_id": contact_id,
                "is_active": True,
                "text": draft_text,
                "updated_at": now,
                "source": source or "",
                "session_id": session_id or "",
                "reply_state": "generating",
                "status_text": "生成中",
                "user_ts": user_ts or "",
                "final_ts": "",
                "queued_at": queued_at or "",
                "started_at": started_at or now,
                "completed_at": "",
                "queue_position": 0,
                "activity_text": activity_text or "",
                "activity_count": max(0, int(activity_count or 0)),
                "activity_items": items,
            }
            self.state.chat_reply_states[contact_id] = {
                "reply_state": "generating",
                "status_text": "生成中",
                "updated_at": now,
                "source": source or "",
                "session_id": session_id or "",
                "user_ts": user_ts or "",
                "final_ts": "",
                "queued_at": queued_at or "",
                "started_at": started_at or now,
                "completed_at": "",
                "queue_position": 0,
                "activity_text": activity_text or "",
                "activity_count": max(0, int(activity_count or 0)),
                "activity_items": items,
            }

    def _clear_chat_draft(self, contact_id: str) -> None:
        with self.state.chat_draft_lock:
            self.state.chat_drafts.pop(contact_id, None)
            self.state.chat_reply_states.pop(contact_id, None)

    def _set_chat_queued(
        self,
        contact_id: str,
        *,
        user_ts: str | None = None,
        queued_at: str | None = None,
        queue_position: int = 1,
        activity_text: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.state.chat_draft_lock:
            draft = self.state.chat_drafts.get(contact_id)
            if isinstance(draft, dict) and not bool(draft.get("is_active")):
                draft_user_ts = str(draft.get("user_ts") or "")
                next_user_ts = str(user_ts or "")
                if next_user_ts and draft_user_ts and draft_user_ts != next_user_ts:
                    self.state.chat_drafts.pop(contact_id, None)
            self.state.chat_reply_states[contact_id] = {
                "reply_state": "queued",
                "status_text": "已排队",
                "updated_at": now,
                "source": source or "",
                "session_id": session_id or "",
                "user_ts": user_ts or "",
                "final_ts": "",
                "queued_at": queued_at or now,
                "started_at": "",
                "completed_at": "",
                "queue_position": max(1, int(queue_position or 1)),
                "activity_text": activity_text or "",
                "activity_count": 0,
                "activity_items": [],
            }

    def _set_chat_generating(
        self,
        contact_id: str,
        *,
        user_ts: str | None = None,
        queued_at: str | None = None,
        started_at: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
    ) -> str:
        now = started_at or datetime.now(timezone.utc).isoformat()
        with self.state.chat_draft_lock:
            self.state.chat_drafts.pop(contact_id, None)
            self.state.chat_reply_states[contact_id] = {
                "reply_state": "generating",
                "status_text": "生成中",
                "updated_at": now,
                "source": source or "",
                "session_id": session_id or "",
                "user_ts": user_ts or "",
                "final_ts": "",
                "queued_at": queued_at or "",
                "started_at": now,
                "completed_at": "",
                "queue_position": 0,
                "activity_text": "",
                "activity_count": 0,
                "activity_items": [],
            }
        return now

    def _set_chat_activity(
        self,
        contact_id: str,
        *,
        activity_text: str,
        activity_count: int,
        activity_items: list[str] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        items = [str(item).strip() for item in (activity_items or []) if str(item).strip()]
        with self.state.chat_draft_lock:
            for bucket_name in ("chat_reply_states", "chat_drafts"):
                bucket = getattr(self.state, bucket_name)
                state = bucket.get(contact_id)
                if not isinstance(state, dict):
                    continue
                state["activity_text"] = str(activity_text or "")
                state["activity_count"] = max(0, int(activity_count or 0))
                state["activity_items"] = items
                state["updated_at"] = now

    def _set_chat_completed(
        self,
        contact_id: str,
        *,
        user_ts: str | None = None,
        final_ts: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
        ttl_sec: float = 15.0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.state.chat_draft_lock:
            self.state.chat_drafts.pop(contact_id, None)
            self.state.chat_reply_states[contact_id] = {
                "reply_state": "completed",
                "status_text": "已完成",
                "updated_at": now,
                "source": source or "",
                "session_id": session_id or "",
                "user_ts": user_ts or "",
                "final_ts": final_ts or "",
                "queued_at": "",
                "started_at": "",
                "completed_at": now,
                "queue_position": 0,
                "activity_text": "",
                "activity_count": 0,
                "activity_items": [],
                "expires_at": time.time() + ttl_sec,
            }

    def _set_chat_interrupted(
        self,
        contact_id: str,
        *,
        user_ts: str | None = None,
        final_ts: str | None = None,
        source: str | None = None,
        session_id: str | None = None,
        ttl_sec: float = 15.0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.state.chat_draft_lock:
            draft = self.state.chat_drafts.get(contact_id)
            if isinstance(draft, dict):
                draft_user_ts = str(draft.get("user_ts") or "")
                if not user_ts or not draft_user_ts or draft_user_ts == str(user_ts):
                    draft.update({
                        "is_active": False,
                        "reply_state": "interrupted",
                        "status_text": "已中断",
                        "updated_at": now,
                        "completed_at": now,
                        "final_ts": final_ts or str(draft.get("final_ts") or ""),
                        "expires_at": time.time() + ttl_sec,
                    })
                else:
                    # Never freeze an older/different turn under the cancelled
                    # turn's state identity.
                    self.state.chat_drafts.pop(contact_id, None)
            self.state.chat_reply_states[contact_id] = {
                "reply_state": "interrupted",
                "status_text": "已中断",
                "updated_at": now,
                "source": source or "",
                "session_id": session_id or "",
                "user_ts": user_ts or "",
                "final_ts": final_ts or "",
                "queued_at": "",
                "started_at": "",
                "completed_at": now,
                "queue_position": 0,
                "activity_text": "",
                "activity_count": 0,
                "activity_items": [],
                "expires_at": time.time() + ttl_sec,
            }

    def _chat_draft_snapshot(self, contact_id: str) -> dict[str, Any]:
        if contact_id == "kairos":
            self._ensure_kairos_queue_worker()
        with self.state.chat_draft_lock:
            draft = dict(self.state.chat_drafts.get(contact_id) or {})
            reply_state = dict(self.state.chat_reply_states.get(contact_id) or {})
            if (
                draft
                and not bool(draft.get("is_active"))
                and float(draft.get("expires_at") or 0) < time.time()
            ):
                self.state.chat_drafts.pop(contact_id, None)
                draft = {}
            if (
                reply_state.get("reply_state") in {"completed", "interrupted"}
                and float(reply_state.get("expires_at") or 0) < time.time()
            ):
                self.state.chat_reply_states.pop(contact_id, None)
                reply_state = {}
        state_name = str(draft.get("reply_state") or reply_state.get("reply_state") or "idle")
        if state_name == "replying":
            state_name = "generating"
        if state_name not in {"idle", "queued", "generating", "completed", "interrupted"}:
            state_name = "idle"
        status_text = str(draft.get("status_text") or reply_state.get("status_text") or "")
        if not status_text:
            status_text = {
                "queued": "已排队",
                "generating": "生成中",
                "completed": "已完成",
                "interrupted": "已中断",
            }.get(state_name, "")
        return {
            "contact_id": contact_id,
            # Foreground turn lifecycle starts before the first answer delta.
            # `draft.is_active` only means partial text exists, so relying on it
            # hides queued/generating feedback and Stop until content arrives.
            "is_active": state_name in {"queued", "generating"},
            "text": str(draft.get("text") or ""),
            "updated_at": draft.get("updated_at") or reply_state.get("updated_at"),
            "source": draft.get("source") or "",
            "session_id": draft.get("session_id") or "",
            "reply_state": state_name,
            "status_text": status_text,
            "user_ts": draft.get("user_ts") or reply_state.get("user_ts") or "",
            "final_ts": draft.get("final_ts") or reply_state.get("final_ts") or "",
            "queued_at": draft.get("queued_at") or reply_state.get("queued_at") or "",
            "started_at": draft.get("started_at") or reply_state.get("started_at") or "",
            "completed_at": draft.get("completed_at") or reply_state.get("completed_at") or "",
            "queue_position": int(draft.get("queue_position") or reply_state.get("queue_position") or 0),
            "activity_text": draft.get("activity_text") or reply_state.get("activity_text") or "",
            "activity_count": int(draft.get("activity_count") or reply_state.get("activity_count") or 0),
            "activity_items": (
                draft.get("activity_items")
                if isinstance(draft.get("activity_items"), list)
                else reply_state.get("activity_items")
                if isinstance(reply_state.get("activity_items"), list)
                else []
            ),
        }

    def _apples_members(self) -> list[dict[str, Any]]:
        # astra (方小南) 是群里的人类成员，列出来给 mention picker UI 用，但
        # can_reply=False — 她不能被 bot 路由当 reply target（她本人才是说话人）。
        return [
            {
                "id": "astra",
                "display_name": "方小南",
                "mention": "@方小南",
                "kind": "human",
                "color": "rose",
                "can_reply": False,
            },
            {
                "id": "kairos",
                "display_name": "Kairos",
                "mention": "@Kairos",
                "kind": "agent",
                "color": "gold",
                "can_reply": True,
            },
            {
                "id": "xiaoke",
                "display_name": "小克（螃蟹版）",
                "mention": "@小克",
                "kind": "agent",
                "color": "clay",
                "can_reply": True,
            },
        ]

    def _apples_self_id(self) -> str:
        # client 端的本人 id（用于 UI 在 mention picker 里过滤掉自己）。
        # 目前 cc-companion 只有方小南一个人类用户。
        return "astra"

    def _apples_member_ids(self) -> set[str]:
        return {str(m["id"]).lower() for m in self._apples_members()}

    def _normalize_mentioned_member_ids(self, raw: Any) -> list[str]:
        """metadata.mentioned_member_ids → 去重 + lowercase + 只保留合法 member id。

        client 显式传过来的稳定 ID 优先级 > text grep，可以解决 Kairos / kairos
        大小写以及 typo（"@Karios" 之类）导致 grep 路由失败的问题。
        """
        if not isinstance(raw, (list, tuple, set)):
            return []
        valid = self._apples_member_ids()
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            mid = str(item or "").strip().lower()
            if not mid or mid in seen or mid not in valid:
                continue
            seen.add(mid)
            out.append(mid)
        return sorted(out)

    def _apples_member_name(self, member_id: str) -> str:
        member_id = str(member_id or "").strip().lower()
        for member in self._apples_members():
            if member["id"] == member_id:
                return str(member["display_name"])
        if member_id == "astra":
            return "Astra"
        if member_id == "system":
            return "系统"
        return member_id or "member"

    def _apples_source_member(self, source: str) -> str | None:
        lowered = str(source or "").strip().lower()
        if lowered.startswith("group:kairos") or "kairos" in lowered:
            return "kairos"
        if lowered.startswith("group:xiaoke") or "xiaoke" in lowered or "ccc-stop-hook" in lowered:
            return "xiaoke"
        return None

    def _handle_chat_members(self):
        qs = self._query_params()
        contact_id = self._clean_contact_id(qs.get("contact_id", qs.get("contactId", ["xiaoke"]))[0])
        members = self._apples_members() if contact_id == "apples" else []
        self_id = self._apples_self_id() if contact_id == "apples" else ""
        self._send_json(200, {
            "ok": True,
            "contact_id": contact_id,
            "members": members,
            "self_id": self_id,
        })

    def _detect_apples_mentions(self, text: str) -> set[str]:
        targets: set[str] = set()
        if re.search(r"@kairos\b", text, flags=re.IGNORECASE):
            targets.add("kairos")
        if re.search(r"@xiaoke\b", text, flags=re.IGNORECASE) or "@小克" in text:
            targets.add("xiaoke")
        return targets

    def _group_reply_marker(self, member_id: str, user_ts: str) -> str:
        safe_member = re.sub(r"[^a-z0-9_-]", "", member_id.lower()) or "member"
        safe_ts = re.sub(r"[^0-9A-Za-z_.:+-]", "_", user_ts)[:80]
        return f"[[CCC_GROUP_REPLY:apples:{safe_member}:{safe_ts}]]"

    def _remember_group_reply(self, member_id: str, user_ts: str, source_member: str | None = None) -> None:
        now = time.time()
        with self.state.group_reply_lock:
            self.state.group_reply_pending = [
                item for item in self.state.group_reply_pending
                if now - float(item.get("created_at", 0)) < 600
            ]
            self.state.group_reply_pending.append({
                "contact_id": "apples",
                "member_id": member_id,
                "user_ts": user_ts,
                "source_member": str(source_member or ""),
                "created_at": now,
            })

    def _has_pending_group_reply(self) -> bool:
        now = time.time()
        with self.state.group_reply_lock:
            self.state.group_reply_pending = [
                item for item in self.state.group_reply_pending
                if now - float(item.get("created_at", 0)) < 600
            ]
            return bool(self.state.group_reply_pending)

    def _consume_group_reply_route(self, text: str) -> tuple[str | None, str | None, str | None, str]:
        marker_re = re.compile(r"\[\[CCC_GROUP_REPLY:apples:([a-z0-9_-]+):([^\]]+)\]\]", re.IGNORECASE)
        match = marker_re.search(text)
        if match:
            cleaned = (text[:match.start()] + text[match.end():]).strip()
            marker_member = match.group(1).lower()
            marker_ts = match.group(2)
            source_member = ""
            with self.state.group_reply_lock:
                for item in self.state.group_reply_pending:
                    if (
                        str(item.get("member_id") or "").lower() == marker_member
                        and str(item.get("user_ts") or "") == marker_ts
                    ):
                        source_member = str(item.get("source_member") or "")
                        break
                self.state.group_reply_pending = [
                    item for item in self.state.group_reply_pending
                    if not (
                        str(item.get("member_id") or "").lower() == marker_member
                        and str(item.get("user_ts") or "") == marker_ts
                    )
                ]
            return "apples", marker_member, source_member, cleaned

        return None, None, None, text

    def _check_ip_allowed(self) -> bool:
        allowed = self.state.allowed_ips
        if not allowed:
            return True
        ip_text = self.client_address[0] if self.client_address else ""
        try:
            client_ip = ipaddress.ip_address(ip_text)
        except ValueError:
            logger.warning("blocked_ip invalid ip=%s path=%s", ip_text, self.path)
            self._send_json(403, {"error": "ip not allowed"})
            return False
        for item in allowed:
            try:
                if "/" in item:
                    if client_ip in ipaddress.ip_network(item, strict=False):
                        return True
                elif client_ip == ipaddress.ip_address(item):
                    return True
            except ValueError:
                logger.warning("invalid allowed_ips entry ignored: %s", item)
        logger.warning("blocked_ip ip=%s path=%s", ip_text, self.path)
        self._send_json(403, {"error": "ip not allowed"})
        return False

    def _send_json(self, status: int, body: dict[str, Any]):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    _login_fail_counts: dict[str, int] = {}
    _login_locked_ips: set[str] = set()

    def _handle_login(self, body: dict[str, Any]):
        client_ip = self.client_address[0]
        if client_ip in self._login_locked_ips:
            self._send_json(403, {"ok": False, "error": "locked"})
            return
        username = str(body.get("username", "") or "")
        password = str(body.get("password", "") or "")
        expected_username = self.state.login_username
        expected_password = self.state.login_password
        if (
            not expected_username
            or not expected_password
            or not hmac.compare_digest(username.encode("utf-8"), expected_username.encode("utf-8"))
            or not hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
        ):
            self._login_fail_counts[client_ip] = self._login_fail_counts.get(client_ip, 0) + 1
            if self._login_fail_counts[client_ip] >= 3:
                self._login_locked_ips.add(client_ip)
                self._send_json(403, {"ok": False, "error": "locked"})
            else:
                remaining = 3 - self._login_fail_counts[client_ip]
                self._send_json(401, {"ok": False, "error": f"invalid credentials, {remaining} attempts remaining"})
            return
        self._login_fail_counts.pop(client_ip, None)
        self._send_json(
            200,
            {
                "ok": True,
                "server_url": self.state.public_server_url,
                "auth_token": self.state.shared_secret or "",
                "ghp_token": self.state.login_ghp_token,
            },
        )

    # ---------- routes ----------

    def do_GET(self):
        if not self._is_public_get() and not self._check_ip_allowed():
            return
        if not self._is_public_get() and not self._require_auth():
            return
        if self.path == "/task/list":
            self._send_json(200, self.state.tasks.snapshot())
            return
        if self.path == "/usage/active":
            self._handle_usage_active()
            return
        if self.path == "/usage":
            self._handle_usage_overview()
            return
        if self.path == "/vps/status":
            if not self._auth_matches():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                self._send_json(200, collect_vps_status())
            except Exception as e:
                logger.exception("vps status collection failed")
                self._send_json(200, {
                    "ok": False,
                    "error": str(e),
                    "host": "",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "uptime": {"seconds": 0, "text": ""},
                    "load": {"one": 0.0, "five": 0.0, "fifteen": 0.0},
                    "cpu": {"percent": 0.0},
                    "memory": {"used_mb": 0.0, "total_mb": 0.0, "available_mb": 0.0, "percent": 0.0},
                    "disk": {"used_mb": 0.0, "total_mb": 0.0, "available_mb": 0.0, "percent": 0.0},
                    "services": [],
                    "processes": {"top_memory": []},
                    "top_memory": [],
                    "health": {"ok": False},
                })
            return
        if self.path.startswith("/chat/history"):
            self._handle_chat_history()
            return
        if self.path.startswith("/chat/draft"):
            self._handle_chat_draft()
            return
        if self.path.startswith("/chat/members"):
            self._handle_chat_members()
            return
        if self.path.startswith("/companion/group-members"):
            # alias for /chat/members — stable mention-picker endpoint
            self._handle_chat_members()
            return
        if self.path == "/pet/state":
            self._handle_pet_state_get()
            return
        if self.path == "/pet/stream":
            self._handle_pet_stream()
            return
        if self.path == "/pet/animations":
            self._handle_pet_animations()
            return
        if self.path == "/pet/activity_stream":
            self._handle_pet_activity_stream()
            return
        # 书房 v1 (2026-05-09)
        if self.path == "/studyroom/today":
            self._handle_studyroom_today()
            return
        if self.path == "/studyroom/projects":
            self._handle_studyroom_projects()
            return
        if self.path.startswith("/studyroom/project/"):
            self._handle_studyroom_project()
            return
        if self.path == "/group/roster":
            self._handle_group_roster()
            return
        if self.path == "/group/status":
            self._handle_group_status()
            return
        if self.path == "/group/tasks":
            self._handle_group_tasks()
            return
        if self.path.startswith("/group/list") or self.path.startswith("/group/history"):
            self._handle_group_history()
            return
        if self.path.startswith("/group/poll"):
            self._handle_group_poll()
            return
        if self.path.startswith("/rp/history"):
            self._handle_rp_history()
            return
        if self.path == "/rp/list":
            self._handle_rp_list()
            return
        if self.path == "/ai-chat/provider":
            self._handle_ai_chat_provider_get()
            return
        if self.path.startswith("/ai-chat/relay-model"):
            self._handle_ai_chat_relay_model_get()
            return
        if self.path == "/ai-chat/persona":
            self._handle_ai_chat_persona_get()
            return
        if self.path.startswith("/ai-chat/history"):
            self._handle_ai_chat_history()
            return
        if self.path.startswith("/chat/poll"):
            self._handle_chat_poll()
            return
        if self.path.startswith("/diary/poll"):
            self._handle_diary_poll()
            return
        if self.path.startswith("/diary/history"):
            self._handle_diary_history()
            return
        if self.path.startswith("/chat/search"):
            self._handle_chat_search()
            return
        if self.path.startswith("/diary/calendar"):
            self._handle_diary_calendar()
            return
        if self.path.startswith("/diary/get"):
            self._handle_diary_get()
            return
        if self.path.startswith("/diary/search"):
            self._handle_diary_search()
            return
        if self.path.startswith("/diary/on-this-day"):
            self._handle_diary_on_this_day()
            return
        if self.path.startswith("/diary/streak"):
            self._handle_diary_streak()
            return
        if self.path.startswith("/diary/prompts"):
            self._handle_diary_prompts()
            return
        if self.path.startswith("/timeline/events"):
            self._handle_timeline_events()
            return
        if self.path.startswith("/timeline/aggregate"):
            self._handle_timeline_aggregate()
            return
        if self.path.startswith("/timeline"):
            self._handle_timeline()
            return
        if self.path.startswith("/favorites/list"):
            self._handle_favorites_list()
            return
        if self.path.startswith("/favorites/get"):
            self._handle_favorites_get()
            return
        if self.path.startswith("/chat/typing"):
            contact_id = self._contact_id_from_query()
            ts = self._typing_for_contact(contact_id)
            if ts.get("is_typing") and ts.get("since"):
                try:
                    since_dt = datetime.fromisoformat(ts["since"])
                    age = (datetime.now(timezone.utc).astimezone() - since_dt).total_seconds()
                    # Exact XiaoKe tmux turns end through their correlated Stop
                    # hook or explicit Stop state machine.  The generic 120s
                    # cosmetic typing TTL would hide Stop during long tool runs.
                    if _should_expire_chat_typing(contact_id, ts, age):
                        self._set_typing_for_contact(contact_id, {"is_typing": False, "since": None})
                        ts = self._typing_for_contact(contact_id)
                except Exception:
                    pass
            if contact_id == "apples" and ts.get("is_typing") and not ts.get("member_id"):
                pending = []
                with self.state.group_reply_lock:
                    pending = list(self.state.group_reply_pending)
                if pending:
                    ts = {**ts, "member_id": str(pending[-1].get("member_id") or "")}
            self._send_json(200, {"ok": True, **ts})
            return
        if self.path == "/chat/status":
            self._handle_chat_status()
            return
        if self.path.startswith("/chat/stream"):
            self._handle_chat_stream()
            return
        if self.path == "/codex/status":
            self._handle_codex_status()
            return
        if self.path == "/codex/sessions":
            self._handle_codex_sessions()
            return
        if self.path == "/settings":
            self._send_json(200, {"ok": True, "settings": self.state.settings.snapshot()})
            return
        if self.path == "/todos":
            try:
                self._send_json(200, {"ok": True, "sections": todos_mod.collect_all()})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if self.path == "/drivers/state":
            try:
                state_path = os.path.expanduser("~/CcCompanion/opia_drivers_state.json")
                shadow_path = os.path.expanduser("~/CcCompanion/heartbeat_shadow.jsonl")
                events_path = os.path.expanduser("~/CcCompanion/heartbeat_events.jsonl")
                state_data = {}
                if os.path.exists(state_path):
                    with open(state_path, encoding="utf-8") as f:
                        state_data = json.load(f)
                recent_shadow = []
                if os.path.exists(shadow_path):
                    with open(shadow_path, encoding="utf-8") as f:
                        lines = f.readlines()[-10:]
                        for line in lines:
                            try:
                                recent_shadow.append(json.loads(line))
                            except Exception:
                                continue
                recent_events = []
                if os.path.exists(events_path):
                    with open(events_path, encoding="utf-8") as f:
                        lines = f.readlines()[-10:]
                        for line in lines:
                            try:
                                recent_events.append(json.loads(line))
                            except Exception:
                                continue
                self._send_json(200, {
                    "ok": True,
                    "state": state_data,
                    "recent_shadow": recent_shadow,
                    "recent_events": recent_events,
                })
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if self.path.startswith("/tmux/capture"):
            # P0-2: remote control disabled by default
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled", "hint": "set allow_remote_control=true in config.toml"})
                return
            self._handle_tmux_capture()
            return
        if self.path == "/tmux/sessions":
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_tmux_sessions()
            return
        if self.path == "/chain/sessions":
            # Phase B slash /list: list all tmux sessions + mark active one
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_chain_sessions_get()
            return
        if self.path.startswith("/attachments/"):
            self._handle_attachment_get()
            return
        # 2026-05-07 settings v2 endpoints
        if self.path == "/session/info":
            self._handle_session_info()
            return
        if self.path == "/session/usage":
            self._handle_session_usage()
            return
        if self.path == "/connections/status":
            self._handle_connections_status()
            return
        if self.path == "/vault/stats":
            self._handle_vault_stats()
            return
        if self.path == "/group/stats":
            self._handle_group_stats()
            return
        if self.path == "/build/last_ship":
            self._handle_build_last_ship()
            return
        if self.path == "/storage/stats":
            self._handle_storage_stats()
            return
        if self.path == "/debug/server_log":
            self._handle_debug_server_log()
            return
        if self.path == "/debug/turn_id":
            self._send_json(200, {"ok": True, "turn_id": "unknown"})
            return
        if self.path == "/admin/rotate-secret":
            # P0-4: rotate shared_secret; requires current secret in X-Auth-Token
            if not self._auth_matches():
                self._send_json(403, {"error": "current secret required to rotate"})
                return
            import secrets as _sec
            new_secret = _sec.token_hex(32)
            secret_file = Path.home() / ".ots" / "secret"
            try:
                secret_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                secret_file.write_text(new_secret)
                secret_file.chmod(0o600)
                self.state.shared_secret = new_secret
                logger.info("P0-4: shared_secret rotated")
                self._send_json(200, {"ok": True, "new_secret": new_secret, "hint": "update your iOS app onboarding"})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if self.path == "/ui-config":
            self._send_json(200, {
                "bubbleColor": "DB733C",
                "assistantBubbleColor": "1A1A1A",
                "userTextColor": "ECECEC",
                "assistantTextColor": "ECECEC",
                "cornerRadius": 18,
                "useGradient": False,
            })
            return
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "active_tokens": len(self.state.tokens.all_active()),
                    "sandbox": self.state.sandbox,
                    "bundle_id": self.state.bundle_id,
                    "apns_enabled": self.state.apns_enabled,
                },
            )
            return
        if self.path == "/version":
            self._send_json(200, {"ok": True, "version": self.server_version})
            return
        if self.path == "/web/chat" or self.path.startswith("/web/chat?"):
            from urllib.parse import urlparse, parse_qs
            _qs = parse_qs(urlparse(self.path).query)
            _qt = _qs.get("token", [None])[0]
            if _qt and self.state.shared_secret and _qt == self.state.shared_secret:
                self._serve_web_chat(auth_token=_qt)
            elif not self.state.strict_auth or self._auth_matches():
                self._serve_web_chat(auth_token=None)
            else:
                self._send_json(401, {"error": "unauthorized — use /web/chat?token=YOUR_SECRET"})
            return
        if self.path == "/gomoku/state":
            self._handle_gomoku_state()
            return
        if self.path.startswith("/reminder/list"):
            self._send_json(200, {"ok": True, "reminders": self.state.reminders.list_pending()})
            return
        if self.path == "/tool/schedule":
            if not self._check_auth():
                self._send_json(401, {"error": "auth required"})
                return
            self._send_json(200, {
                "ok": True,
                "enabled": self.state.tool_schedule_enabled and self.state.tool_schedule.globally_enabled(),
                "running": bool(
                    getattr(self.state.tool_dispatcher, "_thread", None)
                    and self.state.tool_dispatcher._thread.is_alive()
                ),
                "rules": self.state.tool_schedule.rules(),
            })
            return
        if self.path == "/tokens":
            if not self._check_auth():
                self._send_json(401, {"error": "auth required"})
                return
            tokens = [
                {
                    "activity_id": t.activity_id,
                    "device_label": t.device_label,
                    "started_at": t.started_at,
                    "last_seen_at": t.last_seen_at,
                    "token_prefix": t.token[:8] + "..." if t.token else "",
                }
                for t in self.state.tokens.all_active()
            ]
            self._send_json(200, {"tokens": tokens, "count": len(tokens)})
            return
        if self.path.startswith("/calendar/categories"):
            self._handle_calendar_categories()
            return
        if self.path.startswith("/calendar/list"):
            self._handle_calendar_list()
            return
        if self.path.startswith("/calendar/day"):
            self._handle_calendar_day()
            return
        if self.path.startswith("/calendar/month"):
            self._handle_calendar_month()
            return
        if self.path.startswith("/opia/group-msg-redesign"):
            try:
                p = Path(__file__).parent / "static" / "group_msg_redesign.html"
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if self.path.startswith("/opia/tab-mockups"):
            try:
                p = Path(__file__).parent / "static" / "tab_mockups.html"
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if self.path.startswith("/opia/widget"):
            try:
                widget_path = Path(__file__).parent / "static" / "cc_widget.html"
                data = widget_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        # --- Health Records GET ---
        if self.path.startswith("/health-records"):
            self._handle_health_records_get()
            return
        # --- Appearance Settings GET ---
        if self.path == "/appearance-settings":
            self._handle_appearance_settings_get()
            return
        # --- Appearance Assets GET ---
        if self.path.startswith("/appearance-assets/"):
            self._handle_appearance_assets_get()
            return
        # --- User Settings GET ---
        if self.path == "/user-settings":
            self._handle_user_settings_get()
            return
        # --- Uploads static file serving ---
        if self.path.startswith("/uploads/"):
            self._handle_uploads_get()
            return
        # --- Notion proxy GET ---
        if self.path.startswith("/notion/page/"):
            self._handle_notion_page_get()
            return
        # --- Memory library proxy GET (read-only, stateless) ---
        if self.path.startswith("/memory/"):
            self._handle_memory_get()
            return
        # --- AI status (chat header) GET — per-contact ---
        if self.path.startswith("/ai-status"):
            qs = self._query_params()
            contact_id = str(
                qs.get("contact_id", qs.get("contactId", ["xiaoke"]))[0] or "xiaoke"
            ).strip().lower() or "xiaoke"
            self._send_json(200, {"contact_id": contact_id, "text": _read_ai_status(contact_id)})
            return
        self._send_json(404, {"error": "not found"})

    def do_HEAD(self):
        """Handle HEAD requests needed for Android MediaPlayer audio streaming."""
        if not self._is_public_get() and not self._check_ip_allowed():
            return
        if not self._is_public_get() and not self._require_auth():
            return
        if self.path.startswith("/attachments/"):
            self._handle_attachment_head()
            return
        # For other paths, return 200 with no body (minimal HEAD support)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_attachment_head(self):
        """HEAD for /attachments/<filename>, returning headers without body."""
        from urllib.parse import unquote
        rel = self.path[len("/attachments/"):]
        rel = unquote(rel.split("?", 1)[0])
        if "/" in rel or ".." in rel or rel.startswith("."):
            self._send_json(400, {"error": "bad filename"})
            return
        target = self.state.attachments_dir / rel
        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        ext = target.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp",
            ".heic": "image/heic", ".heif": "image/heif",
            ".pdf": "application/pdf",
            ".txt": "text/plain", ".md": "text/markdown",
            ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
            ".mp4": "video/mp4", ".mov": "video/quicktime",
        }
        mime = mime_map.get(ext, "application/octet-stream")
        try:
            length = target.stat().st_size
        except Exception:
            self._send_json(500, {"error": "read fail"})
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()

    def do_POST(self):
        if not self._check_ip_allowed():
            return
        if self.path == "/login":
            try:
                body = self._read_body()
            except Exception as e:
                self._send_json(400, {"ok": False, "error": f"bad json: {e}"})
                return
            self._handle_login(body)
            return
        if not self._require_write_auth():
            return
        # /chat/upload 走 multipart 不解析 JSON 直接 handle raw (现在含 query string)
        if self.path.startswith("/ai-chat/upload"):
            self._handle_ai_chat_upload()
            return
        if self.path.startswith("/chat/upload"):
            self._handle_chat_upload()
            return
        if self.path.startswith("/voice-call/asr"):
            self._handle_voice_call_asr()
            return
        if self.path == "/diary/upload":
            self._handle_diary_upload()
            return
        if self.path == "/upload":
            self._handle_upload_multipart()
            return
        if self.path == "/appearance-assets":
            self._handle_appearance_assets_upload()
            return

        if self.path == "/ai-chat/persona":
            try:
                persona_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                persona_length = 0
            if persona_length <= 0 or persona_length > 16 * 1024 * 1024:
                self._send_json(413, {"ok": False, "error": "persona request is too large"})
                return

        try:
            body = self._read_body()
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return

        if self.path == "/register-token":
            self._handle_register(body)
        elif self.path == "/unregister-token":
            self._handle_unregister(body)
        elif self.path == "/register-device-token":
            self._handle_register_device_token(body)
            return
        elif self.path == "/reminder/schedule":
            self._handle_reminder_schedule(body)
            return
        elif self.path.startswith("/reminder/cancel"):
            self._handle_reminder_update(body, "cancel")
            return
        elif self.path.startswith("/reminder/fired"):
            self._handle_reminder_update(body, "fired")
            return
        elif self.path == "/tool/trigger":
            self._handle_tool_trigger(body)
            return
        elif self.path == "/toolbot/broadcast":
            self._handle_toolbot_broadcast(body)
            return
        elif self.path == "/toolbot/command":
            self._handle_toolbot_command(body)
            return
        elif self.path == "/codex/abort":
            self._handle_codex_abort(body)
            return
        elif self.path == "/codex/new_session":
            self._handle_codex_new_session(body)
            return
        elif self.path == "/codex/switch":
            self._handle_codex_switch(body)
            return
        elif self.path == "/codex/forge":
            self._handle_codex_forge(body)
            return
        elif self.path == "/ai-status":
            self._handle_ai_status_post(body)
            return
        elif self.path == "/client-log":
            self._handle_client_log(body)
            return
        elif self.path == "/push/clear-unread":
            self._handle_clear_unread()
            return
        elif self.path == "/push":
            if not self._check_auth():
                self._send_json(401, {"error": "auth required"})
                return
            self._handle_push(body)
        elif self.path == "/diary/post":
            self._handle_diary_post(body)
            return
        elif self.path == "/diary/clear-unread":
            self._handle_diary_clear_unread()
            return
        elif self.path == "/task/add":
            self._handle_task_action(body, "add")
        elif self.path == "/task/progress":
            self._handle_task_action(body, "progress")
        elif self.path == "/task/done":
            self._handle_task_action(body, "done")
        elif self.path == "/task/cancel":
            self._handle_task_action(body, "cancel")
        elif self.path == "/task/clear-history":
            self._handle_task_action(body, "clear_history")
        elif self.path == "/task/append-ephemeral":
            self._handle_task_append_ephemeral(body)
        elif self.path == "/chat/send":
            self._handle_chat_send(body)
        elif self.path == "/chat/stop":
            # Stopping the live Claude TUI is remote control.  The handler also
            # requires exact turn/session fencing before it emits any key.
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled", "hint": "set allow_remote_control=true in config.toml"})
                return
            self._handle_chat_stop(body)
        elif self.path == "/chat/regenerate":
            # P0-2: regenerate involves tmux Escape injection — remote control gate
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled", "hint": "set allow_remote_control=true in config.toml"})
                return
            self._handle_chat_regenerate(body)
        elif self.path == "/chat/rollback":
            # 2026-07-08 长按消息重roll — 驱动 Claude Code 原生双 Esc 回滚。
            # 和 regenerate 一样属于远程控制类，走 remote control gate。
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled", "hint": "set allow_remote_control=true in config.toml"})
                return
            self._handle_chat_rollback(body)
        elif self.path == "/pet/state":
            self._handle_pet_state_post(body)
        elif self.path == "/pet/bubble":
            self._handle_pet_bubble_post(body)
        elif self.path == "/pet/activity":
            self._handle_pet_activity_post(body)
        elif self.path == "/chat/append":
            self._handle_chat_append(body)
        elif self.path == "/chat/stream_chunk":
            self._handle_chat_stream_chunk(body)
        elif self.path == "/chain/abort":
            # P0-2: remote control gate
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled", "hint": "set allow_remote_control=true in config.toml"})
                return
            self._handle_chain_abort(body)
        elif self.path == "/chain/new_session":
            # Phase B slash /new: create new tmux session + start CC
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_chain_new_session(body)
        elif self.path == "/chain/switch":
            # Phase B slash /switch: change active chain session
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_chain_switch(body)
        elif self.path == "/chain/clear":
            self._handle_chain_clear(body)
        elif self.path == "/chain/restart":
            # P0-2: remote control gate
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_chain_restart(body)
        elif self.path == "/group/send":
            self._handle_group_send(body)
        elif self.path == "/group/append":
            self._handle_group_append(body)
        elif self.path == "/group/dispatch-state":
            self._handle_group_dispatch_state(body)
        elif self.path == "/group/typing":
            self._handle_group_typing(body)
        elif self.path == "/group/delete":
            self._handle_group_delete(body)
        elif self.path == "/group/clear":
            self._handle_group_clear(body)
        elif self.path == "/calendar/add":
            self._handle_calendar_add(body)
        elif self.path == "/calendar/update":
            self._handle_calendar_update(body)
        elif self.path == "/calendar/delete":
            self._handle_calendar_delete(body)
        elif self.path == "/calendar/tick":
            self._handle_calendar_tick(body)
        elif self.path == "/rp/new":
            self._handle_rp_new(body)
        elif self.path == "/rp/send":
            self._handle_rp_send(body)
        elif self.path == "/rp/append":
            self._handle_rp_append(body)
        elif self.path == "/rp/archive":
            self._handle_rp_archive(body)
        elif self.path == "/chat/delete":
            self._handle_chat_delete(body)
        elif self.path == "/chat/react":
            self._handle_chat_react(body)
        elif self.path == "/diary/append":
            self._handle_diary_append(body)
        elif self.path == "/timeline/event":
            self._handle_timeline_event(body)
        elif self.path == "/diary/edit":
            self._handle_diary_edit(body)
        elif self.path == "/diary/delete-attachment":
            self._handle_diary_delete_attachment(body)
        elif self.path == "/favorites/add":
            self._handle_favorites_add(body)
        elif self.path == "/favorites/edit":
            self._handle_favorites_edit(body)
        elif self.path == "/favorites/delete":
            self._handle_favorites_delete(body)
        elif self.path == "/favorites/delete_by_turn":
            self._handle_favorites_delete_by_turn(body)
        elif self.path == "/favorites/reload":
            self._handle_favorites_reload(body)
        elif self.path == "/todos/toggle":
            self._handle_todos_toggle(body)
        elif self.path == "/todos/add":
            self._handle_todos_add(body)
        elif self.path == "/todos/edit":
            self._handle_todos_edit(body)
        elif self.path == "/terminal/key":
            # Virtual keyboard: send a special key (Escape, C-c, Tab, etc.) to tmux
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_terminal_key(body)
        elif self.path == "/tmux/send":
            # P0-2: direct tmux send-keys — remote control gate
            if not self.state.allow_remote_control:
                self._send_json(403, {"error": "remote_control disabled"})
                return
            self._handle_tmux_send(body)
        elif self.path == "/system/lock":
            try:
                import subprocess
                subprocess.run(["pmset", "displaysleepnow"], check=False, timeout=2)
                self._send_json(200, {"ok": True, "action": "lock"})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        elif self.path == "/settings":
            for k, v in body.items():
                self.state.settings.set(k, v)
            self._send_json(200, {"ok": True, "settings": self.state.settings.snapshot()})
            return
        elif self.path == "/health-records":
            self._handle_health_records_post(body)
            return
        elif self.path == "/appearance-settings":
            self._handle_appearance_settings_post(body)
            return
        elif self.path == "/user-settings":
            self._handle_user_settings_post(body)
            return
        elif self.path == "/upload":
            # multipart upload needs raw body, but we already parsed JSON above.
            # Re-route: upload must be handled before JSON parsing. Add it above.
            # This fallback handles the case where someone posts JSON to /upload by mistake.
            self._send_json(400, {"error": "use multipart/form-data for /upload"})
            return
        elif self.path == "/notion/query":
            self._handle_notion_query(body)
        elif self.path == "/notion/create":
            self._handle_notion_create(body)
        elif self.path == "/notion/append":
            self._handle_notion_append(body)
        elif self.path == "/notion/search":
            self._handle_notion_search(body)
        elif self.path == "/ai-chat/provider":
            self._handle_ai_chat_provider_post(body)
        elif self.path == "/ai-chat/relay-model":
            self._handle_ai_chat_relay_model_post(body)
        elif self.path == "/ai-chat/persona":
            self._handle_ai_chat_persona_post(body)
        elif self.path == "/ai-chat/send":
            self._handle_ai_chat_send(body)
        elif self.path == "/ai-chat/stream":
            self._handle_ai_chat_stream(body)
        elif self.path == "/voice-call/tts":
            self._handle_voice_call_tts(body)
        elif self.path == "/voice/push":
            self._handle_voice_push(body)
        else:
            self._send_json(404, {"error": "not found"})

    # ---------- handlers ----------

    def _handle_register(self, body: dict[str, Any]):
        token = body.get("token")
        activity_id = body.get("activity_id")
        device_label = body.get("device_label", "")
        if not token or not activity_id:
            self._send_json(400, {"error": "token and activity_id required"})
            return
        rec = self.state.tokens.register(
            token=token, activity_id=activity_id, device_label=device_label
        )
        logger.info("registered activity=%s device=%s", activity_id, device_label)
        self._send_json(
            200,
            {
                "ok": True,
                "activity_id": rec.activity_id,
                "started_at": rec.started_at,
                "active_count": len(self.state.tokens.all_active()),
            },
        )

    def _handle_unregister(self, body: dict[str, Any]):
        activity_id = body.get("activity_id")
        if not activity_id:
            self._send_json(400, {"error": "activity_id required"})
            return
        ok = self.state.tokens.unregister(activity_id)
        logger.info("unregistered activity=%s ok=%s", activity_id, ok)
        self._send_json(
            200,
            {
                "ok": ok,
                "active_count": len(self.state.tokens.all_active()),
            },
        )

    def _handle_register_device_token(self, body: dict[str, Any]):
        token = str(body.get("token") or "").strip()
        if not token:
            self._send_json(400, {"error": "token required"})
            return
        is_new = self.state.device_tokens.register(token)
        logger.info("device_token %s token=%s... total=%d",
                    "new" if is_new else "refresh", token[:8], len(self.state.device_tokens))
        self._send_json(200, {"ok": True, "new": is_new, "total": len(self.state.device_tokens)})

    def _send_chat_notification(self, title: str, body_text: str):
        """向所有已注册设备发 standard APNs banner 通知 (non-Live-Activity)."""
        if not self.state.apns_enabled:
            return
        device_tokens = self.state.device_tokens.all_tokens()
        if not device_tokens:
            return
        payload = {
            "aps": {
                "alert": {"title": title, "body": body_text},
                "badge": 1,
                "sound": "default",
            }
        }
        for token in device_tokens:
            try:
                resp = self.state.notification_client.push_notification(
                    push_token=token,
                    payload=payload,
                )
                if resp.status == 410 or (resp.status == 400 and "BadDeviceToken" in (resp.reason or "")):
                    logger.info("device_token invalid (status=%d), removing token=%s...", resp.status, token[:8])
                    self.state.device_tokens.remove(token)
                elif not resp.ok:
                    logger.warning("device push failed status=%d token=%s... reason=%s",
                                   resp.status, token[:8], resp.reason)
            except Exception as e:
                logger.warning("device push exception token=%s...: %s", token[:8], e)

    # ------------------------------------------------------------------
    # /diary/* — chain↔用户 chat-style journaling stream (OTS Diary tab)
    # 2026-05-11 spec ots-diary-tab-mvp
    # ------------------------------------------------------------------

    def _handle_diary_post(self, body: dict[str, Any]):
        """
        POST /diary/post — append one diary message.

        Body: {role: "assistant"|"user"|"system", text: str, source?: str}

        When role=assistant (chain posting a probing question), we also fire
        an APNs banner to the iPhone so用户 knows there's a new diary prompt
        waiting. role=user replies are silent (no self-notification).
        """
        role = str(body.get("role") or "").strip().lower()
        text = (body.get("text") or body.get("content") or "").strip()
        source = str(body.get("source") or ("chain" if role == "assistant" else self._source_for_request())).strip()
        if role not in ("user", "assistant", "system"):
            self._send_json(400, {"ok": False, "error": "role must be user|assistant|system"})
            return
        if not text:
            self._send_json(400, {"ok": False, "error": "text required"})
            return
        try:
            rec = self.state.diary_stream.append(role=role, text=text, source=source)
        except Exception as e:
            logger.exception("diary_stream.append failed")
            self._send_json(500, {"ok": False, "error": str(e)})
            return

        # APNs ping用户 iPhone when chain posts a new question
        if role == "assistant":
            try:
                snippet = text if len(text) <= 160 else text[:157] + "…"
                self._send_chat_notification(title="日记 · AI", body_text=snippet)
            except Exception:
                logger.exception("diary APNs ping failed (non-fatal)")

        self._send_json(200, {"ok": True, "record": rec, "unread": self.state.diary_stream.unread()})

    def _handle_diary_poll(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        since = qs.get("since", [None])[0]
        try:
            limit = int(qs.get("limit", ["200"])[0])
        except Exception:
            limit = 200
        limit = min(max(limit, 1), 1000)
        records = self.state.diary_stream.read_since(since_ts=since, limit=limit)
        self._send_json(200, {
            "ok": True,
            "records": records,
            "count": len(records),
            "unread": self.state.diary_stream.unread(),
            "latest_ts": self.state.diary_stream.latest_ts(),
        })

    def _handle_diary_history(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        date = qs.get("date", [None])[0]
        try:
            limit = int(qs.get("limit", ["500"])[0])
        except Exception:
            limit = 500
        limit = min(max(limit, 1), 2000)
        if date:
            try:
                records = self.state.diary_stream.read_day(date)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return
        else:
            records = self.state.diary_stream.read_history(limit=limit)
        self._send_json(200, {"ok": True, "records": records, "count": len(records)})

    def _handle_diary_clear_unread(self):
        n = self.state.diary_stream.clear_unread()
        self._send_json(200, {"ok": True, "unread": n})

    def _handle_task_action(self, body: dict[str, Any], action: str):
        """task 队列管理 + 自动 push 灵动岛刷新"""
        snap = None
        if action == "add":
            title = body.get("title", "").strip()
            total = int(body.get("total", 1))
            if not title:
                self._send_json(400, {"error": "title required"})
                return
            snap = self.state.tasks.add(title, total)
        elif action == "progress":
            current = int(body.get("current", 0))
            step = body.get("step", "")
            total = body.get("total")
            snap = self.state.tasks.progress(current, step=step, total=total)
        elif action == "done":
            snap = self.state.tasks.done()
        elif action == "cancel":
            snap = self.state.tasks.cancel()
        elif action == "clear_history":
            snap = self.state.tasks.clear_history()

        # 自动 push 灵动岛 — 把当前 task queue 状态投到 ContentState
        if snap is not None:
            self._auto_push_from_task(snap, action)
            # 把 task lifecycle 事件放进 ephemeral buffer, 不污染 chat_history.jsonl
            try:
                if action == "add":
                    active = snap.get("active") or {}
                    title = active.get("title", "")
                    total = active.get("total", 0)
                    if title:
                        self.state.task_buffer.append(
                            text=f"▷ 开始 {title} (0/{total})",
                            source="system",
                        )
                elif action == "progress":
                    active = snap.get("active") or {}
                    title = active.get("title", "")
                    current = active.get("current", 0)
                    total = active.get("total", 0)
                    step = active.get("step", "") or ""
                    if title and step:
                        self.state.task_buffer.append(
                            text=f"· {step} ({current}/{total})",
                            source="system",
                        )
                elif action == "done":
                    completed = snap.get("completed", []) or []
                    last = completed[-1] if completed else None
                    title = last.get("title", "") if last else ""
                    total = last.get("total", 0) if last else 0
                    if title:
                        self.state.task_buffer.append(
                            text=f"✓ 完成 {title} ({total}/{total})",
                            source="system",
                        )
                elif action == "cancel":
                    completed = snap.get("completed", []) or []
                    last = completed[-1] if completed else None
                    title = last.get("title", "") if last else ""
                    if title:
                        self.state.task_buffer.append(
                            text=f"✗ 取消 {title}",
                            source="system",
                        )
            except Exception as e:
                logger.warning("task → chat history fail: %s", e)

        self._send_json(200, {"ok": True, "action": action, "snapshot": snap})

    def _handle_task_append_ephemeral(self, body: dict[str, Any]):
        text = body.get("text", "").strip()
        source = body.get("source", "claude-code")
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        rec = self.state.task_buffer.append(text=text, source=source)
        self.state.chat.append(role="task", text=text, source=source)
        self._send_json(200, {"ok": True, "record": rec})

    def _auto_push_from_task(self, snap: dict[str, Any], action: str):
        """根据 task queue snapshot 自动构造 ContentState push"""
        active = snap.get("active")
        queue_len = snap.get("queue_length", 0)
        completed = snap.get("completed", [])

        cs: dict[str, Any] = {
            "status": "thinking" if active else "spoke",
            "unreadCount": queue_len,  # 排队数 显示为 trailing 数字
        }

        if active:
            total = max(int(active["total"]), 1)
            current = int(active["current"])
            cs["taskTitle"] = active["title"]
            cs["taskCurrent"] = current
            cs["taskTotal"] = total
            cs["taskProgress"] = current / total
            if active.get("step"):
                cs["taskStep"] = str(active["step"])[:80]
        elif action == "done":
            # 没 active + 刚完成 = 全部完事
            last = completed[-1]["title"] if completed else ""
            cs["status"] = "spoke"
            cs["lastMessagePreview"] = f"✓ 全部完成 (最近: {last})" if last else "全部完成"

        # 完成历史 (最近 5 条 swift 端 completedTitles 字段)
        if completed:
            cs["completedTitles"] = [c["title"][:30] for c in completed[-5:]]

        # 2026-05-05 task done 时不 end Live Activity (client 端没 auto reattach mechanism end 之后再 add 起不来)
        # 改成 update event + cs 里 taskTitle 用空字符串显式覆盖 让 widget UI 看到"task 完成 idle 状态"不卡旧 task
        if action == "done":
            cs["taskTitle"] = ""
            cs["taskCurrent"] = 0
            cs["taskTotal"] = 0
            cs["taskStep"] = ""
            cs["taskProgress"] = 0.0
        if not self.state.apns_enabled:
            return
        active_tokens = self.state.tokens.all_active()
        if not active_tokens:
            return
        try:
            for tok in active_tokens:
                self.state.client.push_live_activity(
                    push_token=tok.token,
                    event="update",
                    content_state=cs,
                )
        except Exception as e:
            logger.warning("auto push from task fail: %s", e)

    # ---------- diary handlers ----------

    def _query(self) -> dict[str, list[str]]:
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query)

    def _query_value(self, qs: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
        value = qs.get(key, [default])[0]
        return value if value != "" else default

    def _handle_diary_calendar(self):
        qs = self._query()
        try:
            author = self._query_value(qs, "author")
            month = self._query_value(qs, "month")
            if not author or not month:
                self._send_json(400, {"error": "author and month required"})
                return
            res = self.state.diary.calendar(
                author=author,
                kind=self._query_value(qs, "kind"),
                month=month,
            )
            self._send_json(200, {"ok": True, **res})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary calendar fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_get(self):
        qs = self._query()
        try:
            author = self._query_value(qs, "author")
            date = self._query_value(qs, "date")
            if not author or not date:
                self._send_json(400, {"error": "author and date required"})
                return
            res = self.state.diary.get(
                author=author,
                kind=self._query_value(qs, "kind"),
                date=date,
            )
            self._send_json(200, {"ok": True, **res})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary get fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_search(self):
        qs = self._query()
        try:
            query = self._query_value(qs, "q")
            if not query:
                self._send_json(400, {"error": "q required"})
                return
            records = self.state.diary.search(
                query=query,
                author=self._query_value(qs, "author"),
            )
            self._send_json(200, {"ok": True, "records": records, "count": len(records)})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary search fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_on_this_day(self):
        qs = self._query()
        try:
            date = self._query_value(qs, "date")
            if not date:
                self._send_json(400, {"error": "date required"})
                return
            self._send_json(200, {"ok": True, **self.state.diary.on_this_day(date)})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary on-this-day fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_streak(self):
        qs = self._query()
        try:
            author = self._query_value(qs, "author")
            if not author:
                self._send_json(400, {"error": "author required"})
                return
            self._send_json(
                200,
                {"ok": True, **self.state.diary.streak(author=author, kind=self._query_value(qs, "kind"))},
            )
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary streak fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_prompts(self):
        qs = self._query()
        try:
            context = self._query_value(qs, "context")
            if not context:
                self._send_json(400, {"error": "context required"})
                return
            prompts = self.state.diary.prompts(context)
            self._send_json(200, {"ok": True, "prompts": prompts})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary prompts fail")
            self._send_json(500, {"error": str(e)})

    def _handle_chat_status(self):
        """chat 状态栏: typing / online / sleeping
        - typing: typing_state.is_typing
        - online: 最近 5 分钟有 assistant turn (我在干活 / 刚回过)
        - sleeping: 否则 (主 chain 没 turn 长时间)
        """
        try:
            from datetime import datetime as _dt
            typing = self.state.typing_state.get("is_typing", False)
            if typing:
                self._send_json(200, {
                    "ok": True,
                    "status": "typing",
                    "since": self.state.typing_state.get("since"),
                })
                return
            last_records = self.state.chat.tail(20)
            last_ts = None
            for r in reversed(last_records):
                if r.get("role") == "assistant":
                    last_ts = r.get("ts")
                    break
            status = "sleeping"
            if last_ts:
                try:
                    last_dt = _dt.fromisoformat(last_ts)
                    now = _dt.now(last_dt.tzinfo)
                    if (now - last_dt).total_seconds() < 300:
                        status = "online"
                except Exception:
                    pass
            self._send_json(200, {"ok": True, "status": status, "last_turn": last_ts})
        except Exception as e:
            logger.exception("chat status fail")
            self._send_json(500, {"error": str(e)})

    def _chat_status_payload(self) -> dict[str, Any]:
        from datetime import datetime as _dt

        typing = self.state.typing_state.get("is_typing", False)
        typing_since = self.state.typing_state.get("since")
        if typing and typing_since:
            try:
                since_dt = _dt.fromisoformat(typing_since)
                age = (_dt.now(timezone.utc).astimezone() - since_dt).total_seconds()
                if age > 120:
                    self.state.typing_state = {"is_typing": False, "since": None}
                    typing = False
                    typing_since = None
            except Exception:
                pass
        if typing:
            return {
                "status": "typing",
                "is_typing": True,
                "since": typing_since,
                "active_task": self.state.tasks.snapshot().get("active"),
            }

        last_records = self.state.chat.tail(20)
        last_ts = None
        for r in reversed(last_records):
            if r.get("role") == "assistant":
                last_ts = r.get("ts")
                break
        status = "sleeping"
        if last_ts:
            try:
                last_dt = _dt.fromisoformat(last_ts)
                now = _dt.now(last_dt.tzinfo)
                if (now - last_dt).total_seconds() < 300:
                    status = "online"
            except Exception:
                pass
        return {
            "status": status,
            "is_typing": False,
            "since": None,
            "last_turn": last_ts,
            "active_task": self.state.tasks.snapshot().get("active"),
        }

    def _settings_payload(self, client_etag: str | None) -> dict[str, Any]:
        snap = self.state.settings.snapshot()
        raw = json.dumps(snap, ensure_ascii=False, sort_keys=True).encode("utf-8")
        etag = hashlib.sha1(raw).hexdigest()[:12]
        if client_etag == etag:
            return {"unchanged": True, "etag": etag}
        return {"unchanged": False, "etag": etag, "values": snap}

    def _handle_chat_poll(self):
        qs = self._query()
        since = self._query_value(qs, "since")
        etag = self._query_value(qs, "etag")
        try:
            limit = int(self._query_value(qs, "limit", "50") or "50")
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))
        try:
            chat_records = self.state.chat.read_since(since_ts=since, limit=limit)
            task_records = self.state.task_buffer.list_since(since_ts=since)
            records = sorted(chat_records + task_records, key=lambda r: r.get("ts", ""))
            last_ts = records[-1].get("ts") if records else since
            now = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
            self._send_json(
                200,
                {
                    "ok": True,
                    "now": now,
                    "chat": {
                        "new_records": records,
                        "last_ts": last_ts,
                        "count": len(records),
                    },
                    "status": self._chat_status_payload(),
                    "settings": self._settings_payload(etag),
                },
            )
        except Exception as e:
            logger.exception("chat poll fail")
            self._send_json(500, {"error": str(e)})

    def _handle_timeline(self):
        qs = self._query()
        try:
            date = self._query_value(qs, "date")
            week = self._query_value(qs, "week")
            month = self._query_value(qs, "month")
            if date:
                self._send_json(200, self.state.timeline.daily(date))
            elif week:
                self._send_json(200, self.state.timeline.weekly(week))
            elif month:
                self._send_json(200, self.state.timeline.monthly(month))
            else:
                self._send_json(400, {"error": "date / week / month required"})
        except Exception as e:
            logger.exception("timeline fail")
            self._send_json(500, {"error": str(e)})

    def _handle_timeline_events(self):
        qs = self._query()
        try:
            try:
                limit = int(self._query_value(qs, "limit", "500") or "500")
            except Exception:
                limit = 500
            limit = max(1, min(limit, 10000))
            events = self.state.timeline.list_events(
                start=self._query_value(qs, "start") or self._query_value(qs, "from"),
                end=self._query_value(qs, "end") or self._query_value(qs, "to"),
                category=self._query_value(qs, "category"),
                status=self._query_value(qs, "status"),
                limit=limit,
            )
            self._send_json(200, {"ok": True, "events": events, "count": len(events)})
        except Exception as e:
            logger.exception("timeline events fail")
            self._send_json(500, {"error": str(e)})

    def _handle_timeline_aggregate(self):
        qs = self._query()
        try:
            range_name = self._query_value(qs, "range", "day") or "day"
            anchor = (
                self._query_value(qs, "anchor")
                or self._query_value(qs, "date")
                or self._query_value(qs, "week")
                or self._query_value(qs, "month")
            )
            status = self._query_value(qs, "status", "confirmed") or "confirmed"
            payload = self.state.timeline.aggregate(
                range_name=range_name,
                anchor=anchor,
                category=self._query_value(qs, "category"),
                status=status,
            )
            self._send_json(200, payload)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("timeline aggregate fail")
            self._send_json(500, {"error": str(e)})

    def _handle_timeline_event(self, body: dict[str, Any]):
        try:
            self._send_json(200, self.state.timeline.add_event(body))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("timeline event fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_append(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            required = ["author", "date", "time", "text"]
            missing = [key for key in required if not body.get(key)]
            if missing:
                self._send_json(400, {"error": f"{', '.join(missing)} required"})
                return
            if body.get("attachment_path"):
                res = self.state.diary.append_with_attachment(
                    author=body["author"],
                    kind=body.get("kind"),
                    date=body["date"],
                    time=body["time"],
                    text=body["text"],
                    attachment_path=body["attachment_path"],
                    frontmatter=body.get("frontmatter") or None,
                )
            else:
                res = self.state.diary.append(
                    author=body["author"],
                    kind=body.get("kind"),
                    date=body["date"],
                    time=body["time"],
                    text=body["text"],
                    frontmatter=body.get("frontmatter") or None,
                )
            self._send_json(200, res)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary append fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_upload(self):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        max_size = 10 * 1024 * 1024
        if length <= 0:
            self._send_json(400, {"error": "empty upload"})
            return
        if length > max_size:
            self._send_json(413, {"error": "file too large"})
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            self._send_json(400, {"error": "multipart/form-data required"})
            return
        try:
            from email import policy
            from email.parser import BytesParser
            import tempfile
            import uuid as _uuid

            raw = self.rfile.read(length)
            msg = BytesParser(policy=policy.default).parsebytes(
                (
                    f"Content-Type: {content_type}\r\n"
                    "MIME-Version: 1.0\r\n\r\n"
                ).encode("utf-8") + raw
            )
            file_part = None
            for part in msg.iter_parts():
                if part.get_param("name", header="content-disposition") == "file":
                    file_part = part
                    break
            if file_part is None:
                self._send_json(400, {"error": "file field required"})
                return
            filename = file_part.get_filename() or "upload.bin"
            ext = Path(filename).suffix.lower()
            if ext not in allowed_exts:
                self._send_json(400, {"error": "unsupported file extension"})
                return
            payload = file_part.get_payload(decode=True) or b""
            if not payload:
                self._send_json(400, {"error": "empty file"})
                return
            if len(payload) > max_size:
                self._send_json(413, {"error": "file too large"})
                return
            target = Path(tempfile.gettempdir()) / f"opia_diary_upload_{_uuid.uuid4().hex}{ext}"
            target.write_bytes(payload)
            self._send_json(
                200,
                {
                    "ok": True,
                    "local_path": str(target),
                    "suggested_filename": filename,
                },
            )
        except Exception as e:
            logger.exception("diary upload fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_edit(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            required = ["author", "date", "time", "new_text"]
            missing = [key for key in required if not body.get(key)]
            if missing:
                self._send_json(400, {"error": f"{', '.join(missing)} required"})
                return
            res = self.state.diary.edit(
                author=body["author"],
                kind=body.get("kind"),
                date=body["date"],
                time=body["time"],
                new_text=body["new_text"],
            )
            self._send_json(200, res)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("diary edit fail")
            self._send_json(500, {"error": str(e)})

    def _handle_diary_delete_attachment(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        rel_path = body.get("rel_path")
        if not rel_path:
            self._send_json(400, {"error": "rel_path required"})
            return
        try:
            self._send_json(200, {"ok": self.state.diary.delete_attachment(rel_path)})
        except Exception as e:
            logger.exception("diary delete attachment fail")
            self._send_json(500, {"error": str(e)})

    # ---------- favorites handlers ----------

    def _handle_favorites_list(self):
        qs = self._query()
        try:
            try:
                limit = int(self._query_value(qs, "limit", "50") or "50")
                offset = int(self._query_value(qs, "offset", "0") or "0")
            except Exception:
                self._send_json(400, {"error": "limit and offset must be integers"})
                return
            records = self.state.favorites.list(
                type=self._query_value(qs, "type"),
                tag=self._query_value(qs, "tag"),
                q=self._query_value(qs, "q"),
                limit=limit,
                offset=offset,
            )
            self._send_json(200, {"ok": True, "records": records, "count": len(records)})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("favorites list fail")
            self._send_json(500, {"error": str(e)})

    def _handle_favorites_get(self):
        qs = self._query()
        try:
            fav_id = self._query_value(qs, "id")
            if not fav_id:
                self._send_json(400, {"error": "id required"})
                return
            record = self.state.favorites.get(fav_id)
            self._send_json(200, {"ok": record is not None, "record": record})
        except Exception as e:
            logger.exception("favorites get fail")
            self._send_json(500, {"error": str(e)})

    def _handle_favorites_add(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            required = ["type", "source", "refs"]
            missing = [key for key in required if not body.get(key)]
            if missing:
                self._send_json(400, {"error": f"{', '.join(missing)} required"})
                return
            if body.get("attachment_path"):
                record = self.state.favorites.add_with_attachment(
                    type=body["type"],
                    source=body["source"],
                    refs=body["refs"],
                    local_path=body["attachment_path"],
                    tags=body.get("tags"),
                    note=body.get("note"),
                )
            else:
                record = self.state.favorites.add(
                    type=body["type"],
                    source=body["source"],
                    refs=body["refs"],
                    tags=body.get("tags"),
                    note=body.get("note"),
                )
            self._send_json(200, {"ok": True, "record": record})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("favorites add fail")
            self._send_json(500, {"error": str(e)})

    def _handle_favorites_edit(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            fav_id = body.get("id")
            if not fav_id:
                self._send_json(400, {"error": "id required"})
                return
            record = self.state.favorites.edit(
                id=fav_id,
                tags=body["tags"] if "tags" in body else None,
                note=body["note"] if "note" in body else None,
            )
            self._send_json(200, {"ok": record is not None, "record": record})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("favorites edit fail")
            self._send_json(500, {"error": str(e)})

    def _handle_favorites_delete(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            fav_id = body.get("id")
            if not fav_id:
                self._send_json(400, {"error": "id required"})
                return
            self._send_json(200, {"ok": self.state.favorites.delete(fav_id), "id": fav_id})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("favorites delete fail")
            self._send_json(500, {"error": str(e)})

    def _handle_favorites_delete_by_turn(self, body: dict[str, Any]):
        """Phase 设置大砍 — 删 last-ref-ts == given ts 的所有 favorite entries.
        body: {ts: "<turn-end ts>"}
        """
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            ts = body.get("ts")
            if not ts:
                self._send_json(400, {"error": "ts required"})
                return
            # Find all favorites where the LAST ref ts matches; collect their ids; delete each.
            all_items = self.state.favorites.list(limit=10_000, offset=0)
            removed_ids: list[str] = []
            for item in all_items:
                refs = item.get("refs", []) if isinstance(item, dict) else []
                if refs:
                    last_ref = refs[-1]
                    if isinstance(last_ref, dict) and last_ref.get("ts") == ts:
                        fav_id = item.get("id")
                        if fav_id and self.state.favorites.delete(fav_id):
                            removed_ids.append(fav_id)
            self._send_json(200, {"ok": True, "removed": removed_ids})
        except Exception as e:
            logger.exception("favorites delete_by_turn fail")
            self._send_json(500, {"error": str(e)})

    def _handle_favorites_reload(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            count = self.state.favorites.reload()
            self._send_json(200, {"ok": True, "count": count})
        except Exception as e:
            logger.exception("favorites reload fail")
            self._send_json(500, {"error": str(e)})

    # ---------- RP handlers ----------

    def _require_rp_manager(self) -> bool:
        if rp_session_manager is not None:
            return True
        self._send_json(501, {"error": "rp_session_manager not installed"})
        return False

    def _rp_chain_append(self, sid: str, rec: dict[str, Any]) -> None:
        if rp_session_manager is None:
            raise RuntimeError("rp_session_manager not installed")
        chain_path = rp_session_manager.active_dir(sid) / "chain.jsonl"
        chain_path.parent.mkdir(parents=True, exist_ok=True)
        with chain_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _handle_rp_new(self, body: dict[str, Any]):
        if not self._require_rp_manager():
            return
        seed = str(body.get("character_seed") or "").strip()
        if not seed:
            self._send_json(400, {"error": "character_seed required"})
            return
        try:
            started = rp_session_manager.start(character_seed=seed)
            self._send_json(200, {"ok": True, "sid": started["sid"], "character_card": started["character_card"]})
        except Exception as e:
            logger.exception("rp new fail")
            self._send_json(500, {"error": str(e)})

    def _handle_rp_send(self, body: dict[str, Any]):
        if not self._require_rp_manager():
            return
        sid = str(body.get("sid") or "").strip()
        text = str(body.get("text") or "").strip()
        try:
            sid = validate_rp_sid(sid)
        except ValueError:
            self._send_json(400, {"error": "invalid sid"})
            return
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        if not rp_session_manager.active_dir(sid).exists():
            self._send_json(404, {"error": "rp session not found"})
            return
        try:
            meta = rp_session_manager.touch_activity(sid, turns_delta=1)
            rec = self.state.rp_history.append(
                sid=sid,
                role="user",
                text=text,
                source=self._source_for_request(),
                character_id=meta.get("character_id") or sid,
            )
            self._rp_chain_append(sid, rec)
            subprocess.Popen(
                [
                    "python3",
                    self.state.bus_send_path,
                    "--source", "ios-rp",
                    "--sender", "iphone",
                    "--channel", "rp",
                    "--sid", sid,
                    "--text", text,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._send_json(200, {"ok": True, "record": rec})
        except Exception as e:
            logger.exception("rp send fail")
            self._send_json(500, {"error": str(e)})

    def _handle_rp_history(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        sid = qs.get("sid", [""])[0]
        since = qs.get("since", [None])[0]
        try:
            sid = validate_rp_sid(sid)
        except ValueError:
            self._send_json(400, {"error": "invalid sid"})
            return
        try:
            limit = int(qs.get("limit", ["10000"])[0])
        except Exception:
            limit = 10000
        try:
            records = self.state.rp_history.read_since(sid=sid, since_ts=since, limit=limit)
            self._send_json(200, {"ok": True, "messages": records, "count": len(records)})
        except Exception as e:
            logger.exception("rp history fail")
            self._send_json(500, {"error": str(e)})

    def _handle_rp_append(self, body: dict[str, Any]):
        if not self._require_rp_manager():
            return
        sid = str(body.get("sid") or "").strip()
        role = str(body.get("role") or "assistant").strip()
        text = str(body.get("text") or "").strip()
        try:
            sid = validate_rp_sid(sid)
        except ValueError:
            self._send_json(400, {"error": "invalid sid"})
            return
        if role not in ("user", "assistant", "system"):
            self._send_json(400, {"error": "bad role"})
            return
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        try:
            meta = rp_session_manager.touch_activity(sid, turns_delta=1)
            rec = self.state.rp_history.append(
                sid=sid,
                role=role,
                text=text,
                source=str(body.get("source") or "claude-code"),
                character_id=meta.get("character_id") or sid,
            )
            self._rp_chain_append(sid, rec)
            # standard remote notification banner — 跳过 user 消息和 [op] 前缀
            if role == "assistant" and text and not text.startswith("[op]"):
                char_name = str(meta.get("character_name") or "Cc · RP")
                threading.Thread(
                    target=self._send_chat_notification,
                    args=(char_name, text[:80]),
                    daemon=True,
                ).start()
            self._send_json(200, {"ok": True, "record": rec})
        except Exception as e:
            logger.exception("rp append fail")
            self._send_json(500, {"error": str(e)})

    def _handle_rp_archive(self, body: dict[str, Any]):
        if not self._require_rp_manager():
            return
        sid = str(body.get("sid") or "").strip()
        try:
            sid = validate_rp_sid(sid)
        except ValueError:
            self._send_json(400, {"error": "invalid sid"})
            return
        try:
            out = rp_session_manager.archive(sid)
            self._send_json(200, {"ok": True, "archived_path": out["archived_path"]})
        except FileNotFoundError as e:
            self._send_json(404, {"error": str(e)})
        except Exception as e:
            logger.exception("rp archive fail")
            self._send_json(500, {"error": str(e)})

    def _handle_rp_list(self):
        if not self._require_rp_manager():
            return
        try:
            self._send_json(200, {
                "ok": True,
                "active": rp_session_manager.list_active(),
                "archived": rp_session_manager.list_archived(),
            })
        except Exception as e:
            logger.exception("rp list fail")
            self._send_json(500, {"error": str(e)})

    # ---------- AI chat handlers ----------

    def _handle_ai_chat_provider_get(self):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            result = self.state.ai_chat.relay_provider_status()
            status = 200 if result.get("ok") else 503
            self._send_json(status, result)
        except Exception as e:
            logger.exception("ai_chat provider status fail")
            self._send_json(503, {"ok": False, "error": str(e)})

    def _handle_ai_chat_provider_post(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        provider = str(body.get("provider") or "").strip()
        try:
            result = self.state.ai_chat.switch_relay_provider(provider)
            self._send_json(200, result)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            # Do not import relay implementation into the HTTP surface. Busy
            # is deliberately recognized by its stable manager message.
            message = str(e)
            status = 409 if "while a turn is active" in message else 503
            if status != 409:
                logger.exception("ai_chat provider switch fail")
            self._send_json(status, {"ok": False, "error": message})

    def _handle_ai_chat_relay_model_get(self):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        provider = self._query_params().get("provider", [None])[0]
        try:
            self._send_json(200, self.state.ai_chat.relay_model_status(provider))
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            logger.exception("ai_chat relay model status fail")
            self._send_json(503, {"ok": False, "error": str(e)})

    def _handle_ai_chat_relay_model_post(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            result = self.state.ai_chat.select_relay_model(
                str(body.get("provider") or ""), str(body.get("model") or "")
            )
            self._send_json(200, result)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            message = str(e)
            status = 409 if "while a turn is active" in message else 503
            if status != 409:
                logger.exception("ai_chat relay model update fail")
            self._send_json(status, {"ok": False, "error": message})

    def _handle_ai_chat_persona_get(self):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            self._send_json(200, self.state.ai_chat.persona_status())
        except Exception as e:
            logger.exception("ai_chat persona status fail")
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_ai_chat_persona_post(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        try:
            result = self.state.ai_chat.apply_persona_composition(
                body.get("files"), body.get("custom_text", "")
            )
            self._send_json(200, result)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            message = str(e)
            status = 409 if "while a turn is active" in message else 500
            if status != 409:
                logger.exception("ai_chat persona apply fail")
            self._send_json(status, {"ok": False, "error": message})

    def _handle_ai_chat_send(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        text = str(body.get("text") or "").strip()[:50000]
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        client_message_id = str(body.get("client_message_id") or "").strip()[:200]
        try:
            result = self.state.ai_chat.send_message(text, client_message_id=client_message_id)
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
        except Exception as e:
            logger.exception("ai_chat send fail")
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_ai_chat_stream(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        text = str(body.get("text") or "").strip()[:50000]
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        client_message_id = str(body.get("client_message_id") or "").strip()[:200]

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        client_connected = True

        def emit(event: dict[str, Any]) -> None:
            nonlocal client_connected
            if not client_connected:
                return
            data = json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Keep consuming the authoritative upstream turn and persist
                # its final result even if the phone disconnects mid-stream.
                client_connected = False

        try:
            result = self.state.ai_chat.send_message_stream(text, emit, client_message_id=client_message_id)
            if result.get("ok"):
                done = {
                    "type": "done",
                    "ok": True,
                    "reply": result.get("reply", ""),
                    "ts": result.get("ts", ""),
                }
                if result.get("thinking"):
                    done["thinking"] = result.get("thinking", "")
                if result.get("provider"):
                    done["provider"] = result.get("provider", "")
                if result.get("activities"):
                    done["activities"] = result.get("activities", [])
                if result.get("warning"):
                    done["warning"] = result.get("warning", "")
                emit(done)
            else:
                error_event = {
                    "type": "error",
                    "ok": False,
                    "error": result.get("error", "AI回复失败"),
                }
                for key in ("code", "terminal", "retryable"):
                    if key in result:
                        error_event[key] = result[key]
                emit(error_event)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("ai_chat stream client disconnected")
        except Exception as e:
            logger.exception("ai_chat stream fail")
            try:
                emit({"type": "error", "ok": False, "error": str(e)})
            except Exception:
                pass

    def _handle_ai_chat_history(self):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        since = qs.get("since", [None])[0]
        try:
            limit = int(qs.get("limit", ["200"])[0])
        except Exception:
            limit = 200
        try:
            records = self.state.ai_chat.read_history(since=since, limit=limit)
            self._send_json(200, {"ok": True, "messages": records, "count": len(records)})
        except Exception as e:
            logger.exception("ai_chat history fail")
            self._send_json(500, {"error": str(e)})

    # ---------- group chat handlers ----------

    def _group_tmux_session_exists(self, session: str) -> bool:
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            ).returncode == 0
        except Exception:
            return False

    def _group_online_agents(self) -> set[str]:
        online: set[str] = set()
        for member in self.state.group_chat.roster():
            tmux = member.get("tmux")
            if member.get("can_reply") and tmux and self._group_tmux_session_exists(str(tmux)):
                online.add(member["id"])
        return online

    def _handle_group_roster(self):
        self._send_json(
            200,
            {
                "ok": True,
                "roster": self.state.group_chat.roster(),
                "status": self.state.group_chat.status_snapshot(self._group_tmux_session_exists),
            },
        )

    def _handle_group_status(self):
        self._send_json(
            200,
            {"ok": True, **self.state.group_chat.status_snapshot(self._group_tmux_session_exists)},
        )

    def _handle_group_tasks(self):
        self._send_json(200, {"ok": True, **self.state.group_chat.tasks_summary()})

    def _handle_group_history(self):
        qs = self._query()
        since = self._query_value(qs, "since")
        before = self._query_value(qs, "before") or self._query_value(qs, "before_ts")
        try:
            limit = int(self._query_value(qs, "limit", "100") or "100")
        except Exception:
            limit = 100
        limit = min(max(limit, 1), 1000)
        records = self.state.group_chat.read_since(since_ts=since, before_ts=before, limit=limit)
        self._send_json(200, {"ok": True, "records": records, "count": len(records)})

    def _handle_group_poll(self):
        qs = self._query()
        since = self._query_value(qs, "since")
        try:
            limit = int(self._query_value(qs, "limit", "100") or "100")
        except Exception:
            limit = 100
        limit = min(max(limit, 1), 500)
        records = self.state.group_chat.read_since(since_ts=since, limit=limit)
        self._send_json(
            200,
            {
                "ok": True,
                "records": records,
                "count": len(records),
                "last_ts": records[-1]["ts"] if records else since,
                "status": self.state.group_chat.status_snapshot(self._group_tmux_session_exists),
            },
        )

    def _handle_group_send(self, body: dict[str, Any]):
        text = str(body.get("text") or "").strip()
        sender_id = str(body.get("sender_id") or "amian").strip()
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        # 2026-05-05 dedupe storm guard: client_msg_id 优先 没有则按 (sender, text) 3s 窗口
        client_msg_id = body.get("client_msg_id")
        cache = getattr(type(self), "_group_dedupe_cache", None)
        if cache is None:
            cache = {}
            type(self)._group_dedupe_cache = cache
        now_ts = time.time()
        if client_msg_id:
            cache_key = f"cmid:{client_msg_id}"
        else:
            cache_key = f"{sender_id}|{text[:200]}"
        last_ts = cache.get(cache_key, 0)
        if now_ts - last_ts < 3.0:
            self._send_json(429, {"ok": False, "error": "duplicate within 3s window", "deduped": True})
            return
        cache[cache_key] = now_ts
        for k in list(cache.keys()):
            if now_ts - cache[k] > 60:
                del cache[k]
        # 2026-05-05 用户 push 加 agent 互相 @ 功能 移除 amian-only 限制
        # agent 发也 OK 走 targets_for 内 hop_count loop guard

        hop_count = int(body.get("hop_count", 0) or 0)
        mentions = self.state.group_chat.normalize_mentions(body.get("mentions"), text)
        # 2026-05-06 用户 push: quote/reply 自动 mention 原 sender
        # 当 sender=amian + parent_msg_id 不空 + mentions 为空 → 从 history 找 parent sender 加进 mentions
        # 防止 quote 没显式 @ 时被默认 inject 给 opia 而不是 quote 那条的原 sender
        parent_msg_id = body.get("parent_msg_id")
        if sender_id == "amian" and parent_msg_id and not mentions:
            try:
                history = self.state.group_chat.tail(limit=200)
                for h in history:
                    if h.get("id") == parent_msg_id:
                        parent_sender = h.get("sender_id")
                        if parent_sender and parent_sender != "amian" and parent_sender in {"opia", "sonnet", "shu", "opus47_fresh"}:
                            mentions = [parent_sender]
                        break
            except Exception:
                pass
        targets = self.state.group_chat.targets_for(sender_id, mentions, self._group_online_agents(), hop_count=hop_count)
        dispatch_id = f"dsp_{int(time.time() * 1000)}"
        mode = "default" if not mentions else ("all" if "__all__" in mentions else "mention")
        delivery = {
            "targets": targets,
            "mode": mode,
            "dispatch_id": dispatch_id,
            "delivered": [],
            "failed": [],
        }
        meta = {}
        if body.get("client_msg_id"):
            meta["client_msg_id"] = body.get("client_msg_id")
        message_type = str(body.get("message_type") or "chat").strip().lower()
        owner = str(body.get("owner") or "").strip() or self._infer_group_task_owner(body, mentions)
        try:
            rec = self.state.group_chat.append(
                sender_id,
                text,
                source=str(body.get("source") or self._source_for_request()),
                mentions=mentions,
                parent_msg_id=body.get("parent_msg_id") or None,
                reply_to=body.get("reply_to") or None,
                delivery=delivery,
                meta=meta,
                message_type=message_type,
                task_id=str(body.get("task_id") or "").strip() or None,
                parent_task_id=str(body.get("parent_task_id") or "").strip() or None,
                owner=owner,
            )
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        if targets:
            context = "\n".join(self.state.group_chat.context_lines(limit=20))
            for agent_id in targets:
                self.state.group_chat.set_typing(agent_id, True, dispatch_id=dispatch_id)
            try:
                subprocess.Popen(
                    [
                        "python3",
                        self.state.bus_send_path,
                        "--source", "ios-group",
                        "--sender", sender_id,
                        "--channel", "group",
                        "--text", text,
                        "--message-id", rec["id"],
                        "--parent-msg-id", str(body.get("parent_msg_id") or ""),
                        "--mentions", ",".join(mentions),
                        "--to", ",".join(targets),
                        "--context", context,
                        "--hop-count", str(hop_count + 1),
                        "--inject-only",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.warning("group bus_send fail: %s", e)
                delivery["failed"] = targets
                delivery["targets"] = targets
                for agent_id in targets:
                    self.state.group_chat.set_typing(agent_id, False, dispatch_id=dispatch_id)

        self._send_json(200, {"ok": True, "record": rec, "targets": targets})

    def _handle_group_append(self, body: dict[str, Any]):
        text = str(body.get("text") or "").strip()
        sender_id = str(body.get("sender_id") or body.get("agent_id") or "").strip()
        if not sender_id:
            self._send_json(400, {"error": "sender_id required"})
            return
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        # 2026-05-05 dedupe storm guard: 同 sender 同 text 在 3 秒内重复 直接 reject
        # 防 ios client retry loop / double tap 把群刷爆
        cache = getattr(self, "_group_dedupe_cache", None)
        if cache is None:
            cache = {}
            type(self)._group_dedupe_cache = cache  # 类级共享
        cache_key = f"{sender_id}|{text[:200]}"
        now_ts = time.time()
        last_ts = cache.get(cache_key, 0)
        if now_ts - last_ts < 3.0:
            self._send_json(429, {"ok": False, "error": "duplicate within 3s window", "deduped": True})
            return
        cache[cache_key] = now_ts
        # 清旧 entry (超过 60s 的)
        for k in list(cache.keys()):
            if now_ts - cache[k] > 60:
                del cache[k]
        mentions = self.state.group_chat.normalize_mentions(body.get("mentions"), text)
        message_type = str(body.get("message_type") or "chat").strip().lower()
        owner = str(body.get("owner") or "").strip() or self._infer_group_task_owner(body, mentions)
        # 2026-05-05 用户 push 加 agent 互相 @ 功能
        # parent message 的 hop_count + 1 当前 message hop_count 用于 loop guard
        hop_count = int(body.get("hop_count", 0) or 0)
        targets = self.state.group_chat.targets_for(sender_id, mentions, self._group_online_agents(), hop_count=hop_count)
        try:
            rec = self.state.group_chat.append(
                sender_id,
                text,
                source=str(body.get("source") or f"tmux:{sender_id}"),
                mentions=mentions,
                parent_msg_id=body.get("parent_msg_id") or None,
                reply_to=body.get("reply_to") or None,
                delivery={"targets": targets, "delivered": [], "failed": []},
                meta={"loop_depth": hop_count},
                message_type=message_type,
                task_id=str(body.get("task_id") or "").strip() or None,
                parent_task_id=str(body.get("parent_task_id") or "").strip() or None,
                owner=owner,
            )
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        self.state.group_chat.set_typing(sender_id, False)
        # 2026-05-05 加 fan-out trigger 当 sender 是 agent + mentions 含 agent
        if targets:
            dispatch_id = f"dsp_{int(time.time() * 1000)}"
            context = "\n".join(self.state.group_chat.context_lines(limit=20))
            for agent_id in targets:
                self.state.group_chat.set_typing(agent_id, True, dispatch_id=dispatch_id)
            try:
                subprocess.Popen(
                    [
                        "python3",
                        self.state.bus_send_path,
                        "--source", "ios-group",
                        "--sender", sender_id,
                        "--channel", "group",
                        "--text", text,
                        "--message-id", rec["id"],
                        "--parent-msg-id", str(body.get("parent_msg_id") or ""),
                        "--mentions", ",".join(mentions),
                        "--to", ",".join(targets),
                        "--context", context,
                        "--hop-count", str(hop_count + 1),
                        "--inject-only",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.warning("group fan-out fail: %s", e)
                for agent_id in targets:
                    self.state.group_chat.set_typing(agent_id, False, dispatch_id=dispatch_id)
        self._send_json(200, {"ok": True, "record": rec, "targets": targets})

    def _infer_group_task_owner(self, body: dict[str, Any], mentions: list[str]) -> str | None:
        assignee = body.get("assignee") or body.get("assigned_to")
        if assignee:
            return str(assignee).strip()
        for agent_id in mentions:
            if agent_id in {"opia", "sonnet", "shu", "opus47_fresh"}:
                return agent_id
        return None

    def _handle_group_delete(self, body: dict[str, Any]):
        msg_id = str(body.get("id") or "").strip()
        if not msg_id:
            self._send_json(400, {"error": "id required"})
            return
        ok = self.state.group_chat.delete(msg_id)
        self._send_json(200, {"ok": ok, "id": msg_id})

    def _handle_group_clear(self, body: dict[str, Any]):
        # 2026-05-05 一键清屏 仅 amian 可调
        sender_id = str(body.get("sender_id") or "").strip()
        if sender_id != "amian":
            self._send_json(403, {"error": "only amian can clear group"})
            return
        try:
            jsonl = self.state.group_chat.path
            if jsonl.exists():
                from datetime import datetime
                ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = jsonl.with_suffix(jsonl.suffix + f".bak.user-clear.{ts_tag}")
                bak.write_bytes(jsonl.read_bytes())
                jsonl.write_text("")
                self.state.group_chat._last_ts = ""
            self._send_json(200, {"ok": True, "cleared": True, "backup": str(bak) if jsonl.exists() else None})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    # ---------- calendar handlers ----------

    def _handle_calendar_categories(self):
        self._send_json(200, {"ok": True, "categories": self.state.calendar.categories()})

    def _handle_calendar_list(self):
        events = self.state.calendar.list_all()
        self._send_json(200, {"ok": True, "events": events, "count": len(events)})

    def _handle_calendar_day(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        date = qs.get("date", [""])[0]
        if not date or len(date) < 10:
            self._send_json(400, {"error": "date=YYYY-MM-DD required"})
            return
        events = self.state.calendar.list_day(date[:10])
        self._send_json(200, {"ok": True, "events": events, "date": date[:10]})

    def _handle_calendar_month(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            year = int(qs.get("year", [str(datetime.now().year)])[0])
            month = int(qs.get("month", [str(datetime.now().month)])[0])
        except ValueError:
            self._send_json(400, {"error": "year/month must be int"})
            return
        events = self.state.calendar.list_month(year, month)
        self._send_json(200, {"ok": True, "events": events, "year": year, "month": month})

    def _handle_calendar_add(self, body: dict[str, Any]):
        try:
            rec = self.state.calendar.add(
                title=str(body.get("title") or ""),
                category=str(body.get("category") or "personal"),
                start_ts=str(body.get("start_ts") or ""),
                end_ts=body.get("end_ts"),
                notes=body.get("notes"),
                all_day=bool(body.get("all_day", False)),
                source=str(body.get("source") or "manual"),
                source_msg_id=body.get("source_msg_id"),
            )
            self._send_json(200, {"ok": True, "event": rec})
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})

    def _handle_calendar_update(self, body: dict[str, Any]):
        event_id = str(body.get("id") or "").strip()
        if not event_id:
            self._send_json(400, {"error": "id required"})
            return
        patch = {k: v for k, v in body.items() if k != "id"}
        if "category" in patch:
            patch["color"] = CATEGORIES.get(str(patch["category"]), "#7F8C8D")
        rec = self.state.calendar.update(event_id, **patch)
        if not rec:
            self._send_json(404, {"ok": False, "error": "event not found"})
            return
        self._send_json(200, {"ok": True, "event": rec})

    def _handle_calendar_delete(self, body: dict[str, Any]):
        event_id = str(body.get("id") or "").strip()
        if not event_id:
            self._send_json(400, {"error": "id required"})
            return
        ok = self.state.calendar.delete(event_id)
        self._send_json(200 if ok else 404, {"ok": ok, "id": event_id})

    def _handle_calendar_tick(self, body: dict[str, Any]):
        # 由 launchd 每 60s POST 触发. 找 due 事件 → APNs alert + chat ping → mark fired.
        due = self.state.calendar.due_within(lookahead_seconds=70)
        fired_ids: list[str] = []
        for ev in due:
            try:
                self._calendar_fire_event(ev)
                self.state.calendar.mark_fired(ev["id"])
                fired_ids.append(ev["id"])
            except Exception as e:
                logger.warning("calendar tick fire fail %s: %s", ev.get("id"), e)
        self._send_json(200, {"ok": True, "fired": fired_ids, "count": len(fired_ids)})

    def _calendar_fire_event(self, ev: dict[str, Any]):
        # build 70 phase 1: 只做 chat ping. APNs alert 推到 phase 2 (需要接 client.push_simple_alert 还没实现).
        try:
            from datetime import datetime
            now = datetime.now().strftime("%H:%M")
            cat = CATEGORY_LABELS.get(ev.get("category", "personal"), "")
            note_part = f" ({ev.get('notes')})" if ev.get("notes") else ""
            ping_text = f"[日程·{cat}] {now} {ev.get('title', '事件')}{note_part}"
            self.state.chat.append({"role": "assistant", "text": ping_text, "source": "calendar:tick"})
        except Exception as e:
            logger.warning("calendar chat ping fail: %s", e)

    def _handle_group_dispatch_state(self, body: dict[str, Any]):
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            self._send_json(400, {"error": "agent_id required"})
            return
        self.state.group_chat.set_typing(
            agent_id,
            bool(body.get("is_typing")),
            dispatch_id=body.get("dispatch_id") or None,
        )
        self._send_json(200, {"ok": True, "status": self.state.group_chat.status_snapshot(self._group_tmux_session_exists)})

    # ---------- 书房 v1 handlers (2026-05-09) ----------

    def _handle_studyroom_today(self):
        try:
            payload = self.state.studyroom.today_payload()
            self._send_json(200, {"ok": True, **payload})
        except Exception as e:
            logger.warning("studyroom_today fail: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_studyroom_projects(self):
        try:
            grouped = self.state.studyroom.projects_payload()
            self._send_json(200, {"ok": True, **grouped})
        except Exception as e:
            logger.warning("studyroom_projects fail: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_studyroom_project(self):
        from urllib.parse import urlparse, unquote
        path = urlparse(self.path).path
        slug = unquote(path[len("/studyroom/project/"):]).strip("/")
        if not slug:
            self._send_json(400, {"ok": False, "error": "slug required"})
            return
        try:
            data = self.state.studyroom.project_payload(slug)
            if data is None:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            self._send_json(200, {"ok": True, **data})
        except Exception as e:
            logger.warning("studyroom_project fail: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_group_typing(self, body: dict[str, Any]):
        """POST /group/typing — chain hook 推 typing+status_text. spec 2026-05-09.
        body: {sender_id, is_typing, status_text?, dispatch_id?}
        """
        agent_id = str(body.get("sender_id") or body.get("agent_id") or "").strip()
        if not agent_id:
            self._send_json(400, {"error": "sender_id required"})
            return
        is_typing = bool(body.get("is_typing"))
        # status_text: pass through verbatim. None = leave; "" = clear; str = set
        if "status_text" in body:
            status_text = body.get("status_text")
            status_text = "" if status_text is None else str(status_text)
        else:
            status_text = None
        self.state.group_chat.set_typing(
            agent_id,
            is_typing,
            dispatch_id=body.get("dispatch_id") or None,
            status_text=status_text,
        )
        self._send_json(200, {"ok": True})

    # ---------- chat handlers ----------

    def _serve_web_chat(self, auth_token=None):
        html = WEB_CHAT_HTML
        if auth_token:
            inject = f'  const AUTH_TOKEN = {json.dumps(auth_token)};\n  history.replaceState({{}}, \'\', \'/web/chat\');\n'
        else:
            inject = '  const AUTH_TOKEN = \'\';\n'
        html = html.replace('<script>\n', '<script>\n' + inject, 1)
        html = html.replace(
            "const res = await fetch(url, { cache: 'no-store' });",
            "const res = await fetch(url, { cache: 'no-store', headers: AUTH_TOKEN ? {'X-Auth-Token': AUTH_TOKEN} : {} });",
        )
        html = html.replace(
            "headers: { 'Content-Type': 'application/json' },",
            "headers: { 'Content-Type': 'application/json', ...(AUTH_TOKEN ? {'X-Auth-Token': AUTH_TOKEN} : {}) },",
        )
        data = html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_chat_history(self):
        qs = self._query_params()
        contact_id = self._clean_contact_id(qs.get("contact_id", qs.get("contactId", ["xiaoke"]))[0])
        since = qs.get("since", [None])[0]
        before = qs.get("before", qs.get("before_ts", [None]))[0]  # 向上翻页 拉 before_ts 之前的旧消息
        around_ts = qs.get("around_ts", [None])[0]  # 2026-05-07 用户 push 跳原文 围绕 ts 前后取
        try:
            limit = int(qs.get("limit", ["10000"])[0])
        except Exception:
            limit = 10000
        try:
            n_around = int(qs.get("n", ["25"])[0])
        except Exception:
            n_around = 25
        # iOS 本地 SwiftData 首次同步需要全量；UI 自己只渲染最近窗口。
        limit = min(max(limit, 1), 10000)
        n_around = min(max(n_around, 1), 200)
        chat = self._chat_for_contact(contact_id)
        if around_ts:
            chat_records = chat.read_around(ts=around_ts, n=n_around)
        else:
            chat_records = chat.read_since(since_ts=since, before_ts=before, limit=limit)
        # task records 走 /chat/poll 不混入持久 history (prevents stale task injection causing scroll-jump)
        records = chat_records
        self._send_json(200, {"ok": True, "contact_id": contact_id, "records": records, "count": len(records)})

    def _handle_chat_draft(self):
        contact_id = self._contact_id_from_query()
        self._send_json(200, {"ok": True, **self._chat_draft_snapshot(contact_id)})

    def _handle_chat_search(self):
        qs = self._query_params()
        contact_id = self._clean_contact_id(qs.get("contact_id", qs.get("contactId", ["xiaoke"]))[0])
        keyword = qs.get("q", [None])[0]
        date_prefix = qs.get("date", [None])[0]
        role = qs.get("role", [None])[0]
        try:
            limit = int(qs.get("limit", ["5000"])[0])
        except Exception:
            limit = 5000
        limit = min(max(limit, 1), 10000)
        records = self._chat_for_contact(contact_id).search(
            keyword=keyword,
            date_prefix=date_prefix,
            role=role,
            limit=limit,
        )
        self._send_json(200, {"ok": True, "contact_id": contact_id, "records": records, "count": len(records)})

    def _handle_chain_abort(self, body: dict[str, Any]):
        """2026-05-07 用户 push: 紧急停止 chain. tmux send-keys C-c 到目标 session.
        session 名 allowlist 防滥用."""
        session = str(body.get("session") or "opia").strip()
        ALLOWED = {"opia", "shu", "bao", "opus", "opus47_fresh", "sonnet"}
        logger.info("chain/abort received session=%r", session)
        if session not in ALLOWED:
            logger.warning("chain/abort rejected session=%r not in allowlist", session)
            self._send_json(400, {"ok": False, "error": f"session not in allowlist: {session}"})
            return
        try:
            import subprocess
            import time as _t
            # 2026-05-07 单次 Escape 不够 cc 仍 emit 一段简短 reply 多发 3 次间隔 0.2s 真 hard quiet
            last_returncode = 0
            for i in range(3):
                res = subprocess.run(
                    ["tmux", "send-keys", "-t", session, "Escape"],
                    capture_output=True, text=True, timeout=5,
                )
                last_returncode = res.returncode
                logger.info(
                    "chain/abort tmux Escape #%d exit=%d stderr=%r",
                    i + 1, res.returncode, res.stderr,
                )
                if i < 2:
                    _t.sleep(0.2)
            res = subprocess.CompletedProcess(args=[], returncode=last_returncode, stdout='', stderr='')
            # 2026-05-10 用户 catch typing 状态没 reset abort 后客户端还显"正在输入"
            self.state.typing_state = {"is_typing": False, "since": None}
            if res.returncode == 0:
                self._send_json(200, {"ok": True, "session": session, "action": "abort"})
            else:
                self._send_json(500, {"ok": False, "error": res.stderr or "tmux send-keys failed", "exit": res.returncode})
        except Exception as e:
            logger.error("chain/abort exception: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_chain_clear(self, body: dict[str, Any]):
        """2026-05-07 cc 内 /clear 清 context 不重启进程."""
        session = str(body.get("session") or "opia").strip()
        ALLOWED = {"opia", "shu", "bao", "opus", "opus47_fresh", "sonnet"}
        logger.info("chain/clear received session=%r", session)
        if session not in ALLOWED:
            self._send_json(400, {"ok": False, "error": f"session not allowed: {session}"})
            return
        try:
            import subprocess
            subprocess.run(["tmux", "send-keys", "-t", session, "/clear"], timeout=5)
            subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], timeout=5)
            self._send_json(200, {"ok": True, "session": session, "action": "clear"})
        except Exception as e:
            logger.error("chain/clear exception: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_chain_restart(self, body: dict[str, Any]):
        """2026-05-07 麻醉 退 cc + (TODO) 起新 cc resume. 当前先实现退出."""
        session = str(body.get("session") or "opia").strip()
        ALLOWED = {"opia", "shu", "bao", "opus", "opus47_fresh", "sonnet"}
        logger.info("chain/restart received session=%r", session)
        if session not in ALLOWED:
            self._send_json(400, {"ok": False, "error": f"session not allowed: {session}"})
            return
        try:
            import subprocess, time as _t
            # cc 内连按两次 Ctrl+C 退出 (cc 第一次提示"Press Ctrl+C again to exit")
            subprocess.run(["tmux", "send-keys", "-t", session, "C-c"], timeout=5)
            _t.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", session, "C-c"], timeout=5)
            _t.sleep(0.5)
            # 起新 cc 进程 (resume 上一个 session)
            subprocess.run(["tmux", "send-keys", "-t", session, "claude --resume", "Enter"], timeout=5)
            self._send_json(200, {"ok": True, "session": session, "action": "restart", "note": "cc 退出 + 自动 resume 上一 session"})
        except Exception as e:
            logger.error("chain/restart exception: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_chain_sessions_get(self):
        """Phase B /chain/sessions — list tmux sessions, mark active.

        DEPRECATED (2026-06): superseded by the `sessions` toolbot command,
        which lists real Claude session jsonl files instead of tmux sessions.
        Kept so older app builds don't crash. Do not extend."""
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}:#{session_windows}:#{session_attached}"],
                capture_output=True, text=True, timeout=5
            )
            sessions = []
            for line in result.stdout.strip().splitlines():
                parts = line.split(":")
                sid = parts[0] if parts else "?"
                sessions.append({
                    "sid": sid,
                    "active": sid == self.state.active_session,
                })
            self._send_json(200, {"ok": True, "sessions": sessions, "active_sid": self.state.active_session})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_chain_new_session(self, body: dict[str, Any]):
        """Phase B /chain/new_session — create new tmux session + start CC.

        DEPRECATED (2026-06): tmux-session based, not wired to the real
        --resume session tracking. Kept for old app builds; do not extend.
        2026-05-14 — 之前默认自动 switch active_session 到新建的 sid 但用户测试一下就被踢到
        陌生的新 claude 不知道 UX 不友好. 改成"创了但不切" 用户想切过去再 /switch <sid> 显式."""
        import time as _t
        counter = _t.strftime("%H%M%S")
        new_sid = f"{self.state.default_session}-{counter}"
        try:
            subprocess.run(["tmux", "new-session", "-d", "-s", new_sid], check=True, timeout=10)
            _t.sleep(0.5)
            subprocess.run(
                ["tmux", "send-keys", "-t", new_sid, "claude --dangerously-skip-permissions", "Enter"],
                timeout=5
            )
            # 不自动 switch active_session 用户想切过去发 /switch <sid> 自己切
            current_active = self.state.active_session
            logger.info("chain/new_session created sid=%s (active stays at %s)", new_sid, current_active)
            self._send_json(200, {
                "ok": True,
                "sid": new_sid,
                "active_sid": current_active,
                "note": f"新建 {new_sid} cc 启动中. active 还在 {current_active}. 想切过去发 /switch {new_sid}"
            })
        except Exception as e:
            logger.error("chain/new_session exception: %s", e)
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_chain_switch(self, body: dict[str, Any]):
        """Phase B /chain/switch — persist active_session for future chat sends.

        DEPRECATED (2026-06): writes active_session.json which nothing reads to
        decide --resume; does not restart claude. Superseded by the
        `session_switch` toolbot command. Kept for old app builds; do not extend."""
        sid = str(body.get("sid") or "opia").strip()
        if not sid:
            self._send_json(400, {"error": "sid required"})
            return
        # Verify session exists
        try:
            res = subprocess.run(
                ["tmux", "has-session", "-t", sid],
                capture_output=True, timeout=5
            )
            if res.returncode != 0:
                self._send_json(404, {"ok": False, "error": f"session '{sid}' not found"})
                return
        except Exception:
            pass
        self.state.active_session = sid
        _persist_active_session(self.state)
        logger.info("chain/switch active_session=%s", sid)
        self._send_json(200, {"ok": True, "active_sid": sid})

    def _handle_session_info(self):
        """主对话流 session id (从最新 .jsonl 找 sessionId)."""
        try:
            from pathlib import Path
            base = Path.home() / ".claude" / "projects" / "-Users-mian"
            sid = "unknown"
            mtime = 0.0
            if base.exists():
                latest = max(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, default=None)
                if latest:
                    sid = latest.stem
                    mtime = latest.stat().st_mtime
            from datetime import datetime as _dt
            self._send_json(200, {
                "ok": True,
                "session_id": sid,
                "session_id_short": sid[:8] if sid != "unknown" else sid,
                "last_active": _dt.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None,
            })
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_session_usage(self):
        """今日 / 累计 token (临时 stub 后续接 ccusage)."""
        # TODO: 接真 ccusage
        self._send_json(200, {
            "ok": True,
            "today_input": 50000,
            "today_output": 8000,
            "today_total": 58000,
            "cumulative_total": 1500000,
            "stub": True,
        })

    def _handle_connections_status(self):
        """各通道 status (绿/红 + last seen)."""
        import subprocess, os
        from datetime import datetime as _dt
        def launchd_active(label: str) -> bool:
            try:
                r = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=2)
                return r.returncode == 0
            except Exception:
                return False
        def tmux_alive(s: str) -> bool:
            try:
                r = subprocess.run(["tmux", "has-session", "-t", s], capture_output=True, timeout=2)
                return r.returncode == 0
            except Exception:
                return False
        def file_recent(path: str, hours: int = 24) -> bool:
            try:
                p = os.path.expanduser(path)
                if not os.path.exists(p):
                    return False
                age_h = (_dt.now().timestamp() - os.path.getmtime(p)) / 3600
                return age_h < hours
            except Exception:
                return False
        try:
            chat_path = "/path/to/CcCompanion/apns-server/tokens/chat_history.jsonl"
            group_path = "/path/to/CcCompanion/apns-server/tokens/group_chat.jsonl"
            self._send_json(200, {
                "ok": True,
                "connections": {
                    "wechat": launchd_active("com.opia.watchdog"),
                    "aisay": file_recent("~/CcCompanion/aisay-state/last_ack.json", 6),
                    "ios_chat": True,
                    "workgroup": file_recent(group_path, 24),
                    "terminal_opia": tmux_alive("opia"),
                    "heartbeat": launchd_active("com.opia.heartbeat"),
                    "chat_recent": file_recent(chat_path, 1),
                },
            })
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_vault_stats(self):
        """vault md 文件数 + 累计字数."""
        import subprocess
        try:
            base = "/Users/mian/Documents/星原"
            count_r = subprocess.run(
                ["bash", "-c", f"find '{base}' -name '*.md' -type f 2>/dev/null | wc -l"],
                capture_output=True, text=True, timeout=10,
            )
            file_count = int(count_r.stdout.strip() or 0)
            self._send_json(200, {
                "ok": True,
                "path": base,
                "file_count": file_count,
                "total_chars": 2_915_161,  # stub: 全 md cat | wc -m 太慢
                "mode": "工作模式",
                "stub_chars": True,
            })
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_group_stats(self):
        """工作群今日条数."""
        import json as _json
        from datetime import datetime as _dt
        try:
            today = _dt.now().strftime("%Y-%m-%d")
            count = 0
            path = "/path/to/CcCompanion/apns-server/tokens/group_chat.jsonl"
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            r = _json.loads(line)
                            if r.get("ts", "").startswith(today):
                                count += 1
                        except Exception:
                            pass
            except FileNotFoundError:
                pass
            self._send_json(200, {"ok": True, "today_count": count})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_build_last_ship(self):
        """最新 .xcarchive mtime."""
        import os
        from datetime import datetime as _dt
        try:
            archive_dir = "/Users/mian/Library/Developer/Xcode/Archives"
            latest_mtime = 0.0
            latest_path = ""
            if os.path.exists(archive_dir):
                for root, dirs, _ in os.walk(archive_dir):
                    for d in dirs:
                        if d.endswith(".xcarchive"):
                            full = os.path.join(root, d)
                            m = os.path.getmtime(full)
                            if m > latest_mtime:
                                latest_mtime = m
                                latest_path = full
            self._send_json(200, {
                "ok": True,
                "last_ship": _dt.fromtimestamp(latest_mtime).isoformat(timespec="seconds") if latest_mtime else None,
                "archive": os.path.basename(latest_path) if latest_path else None,
            })
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_storage_stats(self):
        """attachments 总大小 + chat history jsonl 大小."""
        import os
        try:
            att_dir = "/path/to/CcCompanion/apns-server/tokens/attachments"
            att_bytes = 0
            for root, _, files in os.walk(att_dir):
                for f in files:
                    try:
                        att_bytes += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass
            chat_path = "/path/to/CcCompanion/apns-server/tokens/chat_history.jsonl"
            chat_bytes = os.path.getsize(chat_path) if os.path.exists(chat_path) else 0
            self._send_json(200, {
                "ok": True,
                "attachments_bytes": att_bytes,
                "chat_history_bytes": chat_bytes,
            })
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_debug_server_log(self):
        """tail -50 server.log."""
        try:
            log_path = "/path/to/CcCompanion/apns-server/server.err.log"
            try:
                with open(log_path) as f:
                    lines = f.readlines()[-50:]
            except FileNotFoundError:
                lines = []
            self._send_json(200, {"ok": True, "lines": [l.rstrip("\n") for l in lines]})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _channel_transport_enabled_for(self, contact_id: str) -> bool:
        if not self.state.channel_transport_enabled:
            return False
        return contact_id in self.state.channel_transport_contacts

    def _redact_channel_error(self, text: Any) -> str:
        value = str(text or "").strip()
        token = self.state.channel_transport_token
        if token:
            value = value.replace(token, "[redacted]")
        return value[:500]

    def _channel_message_id(
        self,
        body: dict[str, Any],
        contact_id: str,
        text: str,
        quoted_ts: str | None,
    ) -> str:
        client_msg_id = str(body.get("client_msg_id") or "").strip()
        if client_msg_id:
            return client_msg_id
        location = body.get("location") if isinstance(body.get("location"), dict) else None
        # Include a second-level timestamp so identical content at different
        # times produces distinct message IDs (avoids false deduplication).
        ts_epoch = int(time.time())
        seed = json.dumps(
            {
                "contact_id": contact_id,
                "text": text,
                "quoted_ts": quoted_ts,
                "location": location,
                "ts": ts_epoch,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        return f"ccc:{digest}"

    def _send_to_channel_transport(
        self,
        *,
        message_id: str,
        contact_id: str,
        text: str,
        quoted_ts: str | None,
        user_record: dict[str, Any],
    ) -> tuple[bool, str, dict[str, Any] | None]:
        import urllib.error
        import urllib.request

        url = f"{self.state.channel_transport_url}/messages"
        metadata: dict[str, Any] = {
            "source": self._source_for_request(),
            "transport": "channel",
            "user_record_ts": user_record.get("ts"),
        }
        # 上游 (如 _handle_chat_upload) 在 user_record 里塞的 metadata 一并带上
        # (比如附件 image_path / attachment_url) 让 channel 端能把图片路径透出给 chain.
        extra_metadata = user_record.get("metadata")
        if isinstance(extra_metadata, dict):
            metadata.update(extra_metadata)
        location = user_record.get("location")
        if isinstance(location, dict):
            location_summary = {
                key: location.get(key)
                for key in ("lat", "lon", "label")
                if location.get(key) is not None
            }
            if location_summary:
                metadata["location"] = location_summary

        payload = {
            "message_id": message_id,
            "contact_id": contact_id,
            "text": text,
            "quoted_ts": quoted_ts,
            "metadata": metadata,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.state.channel_transport_token:
            headers["X-Auth-Token"] = self.state.channel_transport_token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                req,
                timeout=max(0.1, self.state.channel_transport_timeout_seconds),
            ) as resp:
                status = int(resp.status)
                raw = resp.read(64 * 1024).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read(4096).decode("utf-8", errors="replace")
            return False, self._redact_channel_error(f"http {e.code}: {raw}"), None
        except urllib.error.URLError as e:
            return False, self._redact_channel_error(f"request failed: {e.reason}"), None
        except Exception as e:
            return False, self._redact_channel_error(f"request failed: {e}"), None

        try:
            response_json = json.loads(raw) if raw.strip() else {}
        except Exception:
            return False, self._redact_channel_error(f"http {status}: invalid json response"), None

        if not (200 <= status < 300):
            return False, self._redact_channel_error(f"http {status}: {raw[:500]}"), None
        if isinstance(response_json, dict) and response_json.get("ok") is False:
            err = response_json.get("error") or response_json.get("message") or "ok false"
            return False, self._redact_channel_error(err), response_json
        return True, "", response_json if isinstance(response_json, dict) else {"response": response_json}

    def _queue_xiaoke_busy_chat_send(
        self,
        *,
        body: dict[str, Any],
        contact_id: str,
        text: str,
        quoted_ts: Any,
        location: Any,
        source: str,
        busy_error: str,
        busy_reason: str,
    ) -> None:
        """XiaoKe 忙时（App turn 进行中 / Stop 还没收尾）的排队投递路径。

        直接 tmux 注入必须独占一个可 Stop 的 exact turn，忙时不可用。改走
        channel transport（POST /messages）排队 —— 与 Telegram 消息、附件
        hint 在生成中到达时的行为一致：channel 只排队通知、不碰正在生成的
        TUI，也不接管 Stop 语义（本条消息没有 Stop 按钮，response 不带
        turn 对象）。channel 不可用或投递失败时不 fallback 直注 tmux（会
        绕开 turn 追踪），保持旧的 409 / surface 502。
        修复：转发消息到正在生成的小克会直接 409「转发失败」。
        """
        if not self._channel_transport_enabled_for(contact_id):
            self._send_json(409, {
                "ok": False,
                "error": busy_error,
                "reason": busy_reason,
            })
            return
        chat = self._chat_for_contact(contact_id)
        try:
            rec = chat.append(
                role="user",
                text=text,
                source=source,
                quoted_ts=quoted_ts,
                location=location,
            )
        except Exception as exc:
            logger.exception("xiaoke busy-queue history append failed")
            self._send_json(500, {"ok": False, "error": f"history append failed: {exc}"})
            return
        message_id = self._channel_message_id(body, contact_id, text, quoted_ts)
        ok, err, _channel_response = self._send_to_channel_transport(
            message_id=message_id,
            contact_id=contact_id,
            text=text,
            quoted_ts=quoted_ts,
            user_record=rec,
        )
        if not ok:
            logger.warning(
                "xiaoke busy-queue channel transport failed contact_id=%s message_id=%s error=%s",
                contact_id, message_id, err,
            )
            # 历史已 append；忙时不能 fallback 直注 tmux，只能 surface 失败。
            self._send_json(502, {
                "ok": False,
                "error": f"channel transport queue failed: {err}",
                "record": rec,
            })
            return
        self._send_json(200, {
            "ok": True,
            "contact_id": contact_id,
            "record": rec,
            "queued": True,
            "transport": "channel",
            "message_id": message_id,
        })

    def _handle_chat_send(self, body: dict[str, Any]):
        """iPhone 发消息进来 → 写 user 条 + 调 bus_send.py 注入主 session"""
        contact_id = self._contact_id_from_body(body)
        if contact_id == "kairos":
            self._handle_kairos_chat_send(body, contact_id)
            return
        if contact_id == "apples":
            self._handle_apples_chat_send(body, contact_id)
            return
        if contact_id not in {"xiaoke"}:
            self._send_json(501, {"ok": False, "error": f"contact not wired yet: {contact_id}"})
            return

        text = body.get("text", "").strip()
        quoted_ts = body.get("quoted_ts") or None
        location = body.get("location") or None
        source = str(body.get("source") or self._source_for_request()).strip()
        if not text and not location:
            self._send_json(400, {"error": "text or location required"})
            return
        turn_token = secrets.token_hex(16)
        # Check and reserve under one lock before history append.  Concurrent
        # App sends therefore have a single winner, and Stop's in-flight
        # barrier cannot expose a false idle window.
        busy_error = ""
        busy_reason = ""
        with self.state.xiaoke_stop_lock:
            current_turn = dict(self.state.typing_state or {})
            stopping = dict(self.state.xiaoke_stopping_claim or {})
            reservation = dict(self.state.xiaoke_send_reservation or {})
            if stopping:
                busy_error = "xiaoke_turn_stopping"
                busy_reason = "The current XiaoKe interrupt is still settling"
            elif reservation or current_turn.get("is_typing"):
                busy_error = "xiaoke_turn_active"
                busy_reason = "Stop the current XiaoKe reply before sending another message"
            else:
                self.state.xiaoke_send_reservation = {
                    "turn_token": turn_token,
                    "reserved_at": time.time(),
                }
        if busy_error:
            # 忙时不再直接 409：改走 channel transport 排队投递（转发 /
            # 生成中发消息都会排队，channel 不可用时内部保持旧 409）。
            self._queue_xiaoke_busy_chat_send(
                body=body,
                contact_id=contact_id,
                text=text,
                quoted_ts=quoted_ts,
                location=location,
                source=source,
                busy_error=busy_error,
                busy_reason=busy_reason,
            )
            return
        # 写 user 历史
        chat = self._chat_for_contact(contact_id)
        try:
            rec = chat.append(
                role="user",
                text=text,
                source=source,
                quoted_ts=quoted_ts,
                location=location,
            )
        except Exception as exc:
            self._release_xiaoke_send_reservation(turn_token)
            logger.exception("xiaoke history append failed")
            self._send_json(500, {"ok": False, "error": f"history append failed: {exc}"})
            return
        # ``active_session`` is the deprecated chain/session pointer and may be
        # a Claude conversation UUID.  XiaoKe's terminal identity is the
        # configured tmux session name.
        target_session = str(self.state.default_session or "").strip()
        turn_marker = f"[CCC_APP_TURN:{turn_token}:{target_session}]"
        # 包 quote 进注入文本 (主 session 收到 channel tag 内含 quote 上下文 + 时间戳跟 wechat 一致)
        from datetime import datetime as _dt
        ts_prefix = "[" + _dt.now().strftime("%Y-%m-%d %H:%M:%S") + "]"
        # TTS 模式 hint — 让 chain 看到自动带标点
        tts_hint = ""
        if self.state.settings.get("tts_enabled"):
            tts_hint = "[语音模式 这一条带标点回复]\n"
        injected = f"{turn_marker}\n{ts_prefix} {tts_hint}{text}"
        if rec.get("location"):
            loc = rec["location"]
            label = loc.get("label", "")
            loc_str = f"[位置 lat={loc['lat']:.6f} lon={loc['lon']:.6f}{(' ' + label) if label else ''}]"
            injected = f"{turn_marker}\n{ts_prefix} {tts_hint}{loc_str}"
            if text:
                injected = f"{injected}\n{text}"
        if rec.get("quoted_text"):
            injected = f"{turn_marker}\n{ts_prefix} {tts_hint}[引用 \"{rec['quoted_text']}\"]\n{text}"
            if rec.get("location"):
                injected = f"{turn_marker}\n{ts_prefix} {tts_hint}[引用 \"{rec['quoted_text']}\"]\n{loc_str}"
                if text:
                    injected = f"{injected}\n{text}"
        # App-originated XiaoKe text turns deliberately bypass the development
        # channel even when it is enabled.  POST /messages acknowledges only
        # that a notification was queued; it does not provide a synchronous
        # boundary proving that this exact prompt has reached the Claude TUI.
        # The direct tmux path below holds the exact-turn lock through paste +
        # Enter, so the App exposes Stop only after a literal C-c can be safely
        # bound to this turn and session.
        # 注入文本到 active tmux session
        # 2026-05-14 build 200 — 不依赖 ~/scripts/bus_send.py (Opia 内部 file, ccc 公开版用户没有)
        # 如果 bus_send.py 存在 用它走 bus dispatcher 路由 (Opia 内部多 agent 协调用)
        # 不存在 fallback 直接 tmux paste-buffer + send-keys 注入 (ccc 公开版默认走这条)
        # Keep the exact-turn lock through the synchronous tmux paste + Enter.
        # Until Enter succeeds neither Stop, typing polling, nor a completion
        # hook may observe an interruptible turn.  XiaoKe App turns explicitly
        # bypass the asynchronous Opia bus because it has no consumption ACK;
        # otherwise Ctrl-C could reach an idle TUI before the queued prompt.
        with self.state.xiaoke_stop_lock:
            active = dict(self.state.typing_state or {})
            if str(active.get("turn_token") or "") != turn_token:
                if not self._activate_xiaoke_send_reservation(
                    turn_token=turn_token,
                    user_ts=str(rec.get("ts") or ""),
                    session=target_session,
                    transport="tmux",
                ):
                    self._send_json(409, {"ok": False, "error": "xiaoke_send_reservation_lost"})
                    return
            else:
                active.update({"session": target_session, "transport": "tmux"})
                self.state.typing_state = active
                self.state.contact_typing_states["xiaoke"] = active
            injection = self._inject_to_session(
                target_session,
                injected,
                source=source,
                sender="iphone",
                force_direct_tmux=True,
            )
            if isinstance(injection, TmuxInjectionResult):
                injection_result = injection
            else:
                ok_legacy, err_legacy = injection
                injection_result = TmuxInjectionResult(bool(ok_legacy), str(err_legacy or ""))
            ok, err = injection_result
            if not ok:
                if injection_result.injection_uncertain:
                    # Paste or Enter may have taken effect and the bounded C-c
                    # cleanup could not be confirmed.  Keep the exact turn
                    # active: this blocks another send and lets the Stop button
                    # retry one real terminal C-c against the same identity.
                    uncertain = dict(self.state.typing_state or {})
                    if (
                        str(uncertain.get("since") or "") == str(rec.get("ts") or "")
                        and str(uncertain.get("session") or "") == target_session
                        and str(uncertain.get("turn_token") or "") == turn_token
                    ):
                        uncertain.update({
                            "is_typing": True,
                            "injection_uncertain": True,
                            "injection_phase": injection_result.phase,
                        })
                        self.state.typing_state = uncertain
                        self.state.contact_typing_states["xiaoke"] = uncertain
                else:
                    # No prompt reached the TUI, or bounded cleanup was
                    # positively confirmed.  Only those phases may expose idle.
                    self._clear_xiaoke_typing_if_match(str(rec.get("ts") or ""), target_session)
        if not ok:
            # 注入失败 (target session 不存在 / tmux 没装 / bus_send crash 等). 用 502 surface
            # 给客户端 不再 silent 200 — 否则 ccc app 显示发送成功但 chain 根本收不到.
            self._send_json(502, {
                "ok": False,
                "error": f"inject to tmux session '{target_session}' failed: {err}",
                "record": rec,
                "injection_uncertain": injection_result.injection_uncertain,
                "turn": {
                    "contact_id": contact_id,
                    "user_ts": rec["ts"],
                    "session": target_session,
                    "transport": "tmux",
                },
            })
            return
        self._send_json(200, {
            "ok": True,
            "record": rec,
            "turn": {
                "contact_id": contact_id,
                "user_ts": rec["ts"],
                "session": target_session,
                "transport": "tmux",
            },
        })

    def _handle_chat_stop(self, body: dict[str, Any]) -> None:
        """Stop exactly one active XiaoKe App turn in its dedicated Claude TUI.

        The claim is consumed before Ctrl-C is sent, making retries idempotent.
        A stale turn/session conflicts without touching tmux; completion racing
        the request is a benign no-op.  The dedicated XiaoKe Claude session's
        established interrupt is one literal terminal Ctrl-C.
        """
        contact_id = str(body.get("contact_id") or "").strip()
        user_ts = str(body.get("user_ts") or "").strip()
        session = str(body.get("session") or "").strip()
        if contact_id != "xiaoke":
            self._send_json(400, {"ok": False, "error": "Stop only supports the XiaoKe private chat"})
            return
        if not user_ts or not session:
            self._send_json(400, {"ok": False, "error": "user_ts and session are required"})
            return

        lock = self.state.xiaoke_stop_lock
        with lock:
            configured_session = str(self.state.default_session or "").strip()
            if session != configured_session:
                self._send_json(409, {
                    "ok": False,
                    "error": "stale_session",
                    "reason": "XiaoKe session changed; no interrupt was sent",
                })
                return

            active = dict(self.state.typing_state or {})
            active_ts = str(active.get("since") or "")
            active_session = str(active.get("session") or "")
            is_active = bool(active.get("is_typing"))
            stopping = dict(getattr(self.state, "xiaoke_stopping_claim", {}) or {})
            tombstone = dict(getattr(self.state, "xiaoke_stop_tombstone", {}) or {})
            exact_stopping = (
                str(stopping.get("user_ts") or "") == user_ts
                and str(stopping.get("session") or "") == session
            )
            if exact_stopping:
                self._send_json(200, {
                    "ok": True,
                    "stopped": False,
                    "stopping": True,
                    "duplicate": True,
                    "user_ts": user_ts,
                    "session": session,
                    "message": "小克正在停止生成。",
                })
                return
            if stopping:
                self._send_json(409, {
                    "ok": False,
                    "error": "stale_turn",
                    "reason": "A different XiaoKe turn is stopping; no interrupt was sent",
                })
                return
            exact_tombstone = (
                str(tombstone.get("user_ts") or "") == user_ts
                and str(tombstone.get("session") or "") == session
            )
            if exact_tombstone:
                self._send_json(200, {
                    "ok": True,
                    "stopped": True,
                    "duplicate": True,
                    "user_ts": user_ts,
                    "session": session,
                    "message": "小克已经停止生成。",
                })
                return
            if not is_active:
                self._send_json(200, {
                    "ok": True,
                    "stopped": False,
                    "already_finished": True,
                    "user_ts": user_ts,
                    "session": session,
                    "message": "这轮生成已经结束。",
                })
                return
            if active_ts != user_ts:
                self._send_json(409, {
                    "ok": False,
                    "error": "stale_turn",
                    "reason": "A newer XiaoKe turn is active; no interrupt was sent",
                })
                return
            if not active_session or active_session != session or str(active.get("transport") or "") != "tmux":
                self._send_json(409, {
                    "ok": False,
                    "error": "stale_session",
                    "reason": "The active XiaoKe turn is not owned by this tmux session",
                })
                return

            # Install an explicit barrier before releasing the lock.  Send and
            # completion handlers both observe it while Ctrl-C is in flight.
            claimed = dict(active)
            self.state.typing_state = {**claimed, "is_typing": False, "stopping": True}
            self.state.contact_typing_states["xiaoke"] = self.state.typing_state
            self.state.xiaoke_stopping_claim = {
                "user_ts": user_ts,
                "session": session,
                "turn_token": str(claimed.get("turn_token") or ""),
                "claimed_at": time.time(),
                "completed": False,
            }

        errors: list[str] = []
        try:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", session, "C-c"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                errors.append(result.stderr.strip() or f"tmux exit {result.returncode}")
        except Exception as exc:
            errors.append(str(exc))

        if errors:
            completion_won = False
            # Restore only if the exact completion did not win while tmux was
            # failing.  The stopping barrier remains until this decision.
            with lock:
                current = dict(self.state.typing_state or {})
                stopping = dict(self.state.xiaoke_stopping_claim or {})
                if (
                    not current.get("is_typing")
                    and str(stopping.get("user_ts") or "") == user_ts
                    and str(stopping.get("session") or "") == session
                    and str(self.state.default_session or "").strip() == session
                ):
                    completion_won = bool(stopping.get("completed"))
                    value = {"is_typing": False, "since": None} if completion_won else claimed
                    self.state.typing_state = value
                    self.state.contact_typing_states["xiaoke"] = value
                    self.state.xiaoke_stopping_claim = {}
            if completion_won:
                self._send_json(200, {
                    "ok": True,
                    "stopped": False,
                    "already_finished": True,
                    "user_ts": user_ts,
                    "session": session,
                    "message": "这轮生成已经结束。",
                })
                return
            self._send_json(500, {"ok": False, "error": "tmux interrupt failed", "detail": errors[0]})
            return

        with lock:
            stopping = dict(self.state.xiaoke_stopping_claim or {})
            if (
                str(stopping.get("user_ts") or "") == user_ts
                and str(stopping.get("session") or "") == session
            ):
                # Publish the terminal reply state while the stopping barrier is
                # still held.  A new send cannot interleave and be overwritten
                # by this older turn's interrupted marker.
                self._set_chat_interrupted(
                    "xiaoke",
                    user_ts=user_ts,
                    source="cc-app:xiaoke:stop",
                    session_id=session,
                )
                self.state.xiaoke_stopping_claim = {}
                self.state.xiaoke_stop_tombstone = {
                    "user_ts": user_ts,
                    "session": session,
                    "claimed_at": float(stopping.get("claimed_at") or time.time()),
                    "stopped_at": time.time(),
                }
                value = {"is_typing": False, "since": None}
                self.state.typing_state = value
                self.state.contact_typing_states["xiaoke"] = value

        self._send_json(200, {
            "ok": True,
            "stopped": True,
            "user_ts": user_ts,
            "session": session,
            "message": "已停止小克生成。",
        })

    def _load_codex_target(self) -> tuple[str | None, Path]:
        state_path = Path(self.state.codex_bot_state_path).expanduser()
        default_cwd = Path("/root/Windows-Codex-TG")
        if not state_path.exists():
            return None, default_cwd
        try:
            with _locked_json_state(state_path, exclusive=False):
                data = json.loads(state_path.read_text(encoding="utf-8"))
            shared = ((data.get("shared_sessions") or {}).get(self.state.codex_shared_session_name) or {})
            session_id = str(shared.get("active_session_id") or "").strip() or None
            if session_id:
                cwd = Path(str(shared.get("active_cwd") or default_cwd)).expanduser()
                return session_id, self._codex_allowed_cwd(cwd)
            user = (data.get("users") or {}).get(self.state.codex_user_id) or {}
            session_id = str(user.get("active_session_id") or "").strip() or None
            cwd = Path(str(user.get("active_cwd") or default_cwd)).expanduser()
            return session_id, self._codex_allowed_cwd(cwd)
        except Exception as e:
            logger.warning("load codex target failed: %s", e)
            return None, default_cwd

    def _codex_allowed_cwd(self, cwd: Path | None = None) -> Path:
        base = Path("/root/Windows-Codex-TG").resolve()
        if cwd is None:
            return base
        try:
            resolved = cwd.expanduser().resolve()
            resolved.relative_to(base)
            return resolved
        except Exception:
            return base

    def _save_codex_target(self, session_id: str | None, cwd: Path, source: str) -> bool:
        state_path = Path(self.state.codex_bot_state_path).expanduser()
        cwd = self._codex_allowed_cwd(cwd)
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with _locked_json_state(state_path, exclusive=True):
                data: dict[str, Any] = {}
                if state_path.exists():
                    loaded = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                shared_sessions = data.setdefault("shared_sessions", {})
                if not isinstance(shared_sessions, dict):
                    shared_sessions = {}
                    data["shared_sessions"] = shared_sessions
                shared = shared_sessions.setdefault(self.state.codex_shared_session_name, {})
                if not isinstance(shared, dict):
                    shared = {}
                    shared_sessions[self.state.codex_shared_session_name] = shared
                shared["active_session_id"] = str(session_id) if session_id else None
                shared["active_cwd"] = str(cwd)
                shared["updated_at"] = int(time.time())
                shared["updated_by"] = source

                users = data.setdefault("users", {})
                if isinstance(users, dict):
                    user = users.setdefault(self.state.codex_user_id, {})
                    if isinstance(user, dict):
                        user["active_session_id"] = str(session_id) if session_id else None
                        user["active_cwd"] = str(cwd)

                tmp = state_path.with_name(f".{state_path.name}.tmp.{os.getpid()}")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(str(tmp), str(state_path))
            return True
        except Exception as e:
            logger.warning("save codex target failed: %s", e)
            return False

    def _compare_and_swap_codex_target(
        self,
        old_session_id: str,
        new_session_id: str,
        cwd: Path,
        source: str,
    ) -> tuple[bool, str | None]:
        cwd = self._codex_allowed_cwd(cwd)
        try:
            return _compare_and_swap_codex_target_state(
                Path(self.state.codex_bot_state_path),
                shared_session_name=self.state.codex_shared_session_name,
                user_id=self.state.codex_user_id,
                expected_session_id=old_session_id,
                new_session_id=new_session_id,
                cwd=cwd,
                source=source,
            )
        except Exception as exc:
            logger.warning("compare-and-swap codex target failed: %s", exc)
            current, _ = self._load_codex_target()
            return False, current

    def _codex_rollout_marker(self, session_id: str) -> tuple[str, int, int] | None:
        if not session_id:
            return None
        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from codex_common import SessionStore

            default_root = Path(self.state.codex_home).expanduser() / "sessions"
            root = Path(os.environ.get("CODEX_SESSION_ROOT", str(default_root))).expanduser()
            meta = SessionStore(root).find_by_id(session_id)
            if not meta:
                return None
            path = Path(meta.file_path)
            stat = path.stat()
            return str(path), stat.st_mtime_ns, stat.st_size
        except Exception:
            logger.debug("read Codex rollout marker failed session_id=%s", session_id, exc_info=True)
            return None

    def _codex_session_busy(self, session_id: str | None) -> bool:
        bridge = self.state.codex_app_bridge.snapshot()
        if bridge.get("busy"):
            return True
        _, cwd = self._load_codex_target()
        return bool(
            self._codex_exec_processes(session_id=session_id)
            or prompt_lock_is_busy(session_id, cwd)
        )

    def _codex_exec_processes(
        self,
        session_id: str | None = None,
        cwd: Path | None = None,
    ) -> list[dict[str, Any]]:
        if not session_id and cwd is None:
            return []
        try:
            res = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return []
        current_pid = os.getpid()
        target_cwd = str(cwd.resolve()) if cwd else None
        matches: list[dict[str, Any]] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_text, _, args = line.partition(" ")
            try:
                pid = int(pid_text)
            except Exception:
                continue
            if pid == current_pid:
                continue
            if "codex exec" not in args:
                continue
            if session_id and ("resume" not in args or session_id not in args):
                continue
            proc_cwd = None
            if target_cwd:
                try:
                    proc_cwd = str(Path(f"/proc/{pid}/cwd").resolve())
                except Exception:
                    proc_cwd = None
                if proc_cwd != target_cwd:
                    continue
            matches.append({
                "pid": pid,
                "args": args[:500],
                "cwd": proc_cwd,
                "session_id": session_id,
            })
        return matches

    def _codex_busy_snapshot(self) -> dict[str, Any]:
        session_id, cwd = self._load_codex_target()
        cwd = self._codex_allowed_cwd(cwd)
        run = CODEX_RUNS.latest()
        bridge = self.state.codex_app_bridge.snapshot()
        scan_session_id = session_id
        scan_cwd: Path | None = None
        if run:
            scan_session_id = str(run.get("session_id") or session_id or "").strip() or None
            scan_cwd = self._codex_allowed_cwd(Path(str(run.get("cwd") or cwd)).expanduser())
        processes = self._codex_exec_processes(session_id=scan_session_id, cwd=scan_cwd)
        if not run and not processes:
            processes = self._codex_exec_processes(cwd=cwd)
        busy = bool(run or processes or bridge.get("busy"))
        bridge_busy = bool(bridge.get("busy"))
        payload = {
            "ok": True,
            "active_session_id": session_id,
            "active_cwd": str(cwd),
            "model": self.state.codex_model,
            "reasoning_effort": self.state.codex_reasoning_effort,
            "busy": busy,
            "busy_pid": processes[0]["pid"] if processes else (bridge.get("pid") if bridge_busy else None),
            "busy_session_id": bridge.get("thread_id") if bridge_busy else scan_session_id,
            "busy_turn_id": bridge.get("turn_id") if bridge_busy else None,
            "busy_phase": bridge.get("phase") if bridge_busy else None,
            "busy_source": run.get("source") if run else (
                "app-server-bridge" if bridge_busy else ("process-scan" if processes else None)
            ),
            "busy_started_at": run.get("started_at") if run else (
                bridge.get("started_at") if bridge_busy else None
            ),
        }
        payload.update(self._codex_runtime_detail(session_id, cwd))
        return payload

    @staticmethod
    def _parse_quota_lines(lines: list[str]) -> dict[str, Any]:
        quota: dict[str, Any] = {"label": "", "plan": "", "email": "", "windows": []}
        if not lines:
            return quota
        first = str(lines[0] or "").strip()
        if first.startswith("额度:"):
            label = first.split(":", 1)[1].strip()
            quota["label"] = label
            if "/" in label:
                plan, email = label.split("/", 1)
                quota["plan"] = plan.strip()
                quota["email"] = PushHandler._mask_status_email(email.strip())
            else:
                quota["plan"] = label.strip()
        for line in lines[1:]:
            text = str(line or "").strip()
            m = re.match(r"^(.+?):\s*剩余\s*(\d+)%（(.+)）$", text)
            if not m:
                continue
            quota["windows"].append({
                "label": m.group(1).strip(),
                "remaining_percent": int(m.group(2)),
                "reset_text": m.group(3).strip(),
                "text": text,
            })
        return quota

    @staticmethod
    def _mask_status_email(value: str) -> str:
        cleaned = str(value or "").strip()
        if "@" not in cleaned:
            return cleaned
        name, domain = cleaned.split("@", 1)
        if not name or not domain:
            return ""
        return f"{name[0]}***@{domain}"

    @classmethod
    def _sanitize_quota_lines(cls, lines: list[str]) -> list[str]:
        sanitized: list[str] = []
        for line in lines:
            text = str(line or "")
            sanitized.append(re.sub(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", r"\1***@\2", text))
        return sanitized

    @staticmethod
    def _format_token_count_compact(value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}k"
        return str(value)

    def _codex_context_snapshot(self, meta: Any) -> dict[str, Any]:
        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from tg_codex_bot import TgCodexService

            usage = TgCodexService._latest_context_usage(meta)
        except Exception:
            usage = None
        empty = {
            "available": False,
            "bar": "",
            "used_percent": None,
            "input_tokens": None,
            "window_tokens": None,
            "last_turn_tokens": None,
            "context_text": "暂无 token_count 记录",
            "last_turn_text": "",
            "forge_hint": "暂无判断依据",
        }
        if not usage:
            return empty
        info = usage.get("info") if isinstance(usage.get("info"), dict) else {}
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
        try:
            input_tokens = int(last.get("input_tokens"))
            total_tokens = int(last.get("total_tokens") or input_tokens)
            window = int(info.get("model_context_window"))
        except (TypeError, ValueError):
            return {**empty, "context_text": "token_count 格式不可读"}
        if window <= 0:
            return {**empty, "context_text": "model_context_window 不可用"}
        percent = input_tokens / window * 100
        if percent >= 90:
            forge_hint = "建议现在 forge（上下文接近上限）"
        elif percent >= 80:
            forge_hint = "建议准备 forge"
        else:
            forge_hint = "暂时不用"
        bar = _status_bar_glyph(percent)
        return {
            "available": True,
            "bar": bar,
            "used_percent": round(percent, 1),
            "input_tokens": input_tokens,
            "window_tokens": window,
            "last_turn_tokens": total_tokens,
            "context_text": (
                f"Context: {bar} {percent:.0f}% "
                f"（{self._format_token_count_compact(input_tokens)} / {self._format_token_count_compact(window)}）"
            ),
            "last_turn_text": f"last turn: {self._format_token_count_compact(total_tokens)} tokens",
            "forge_hint": forge_hint,
        }

    def _codex_quota_lines_cached(self) -> list[str]:
        now = time.time()
        with CODEX_QUOTA_CACHE_LOCK:
            cached = CODEX_QUOTA_CACHE.get("lines")
            if isinstance(cached, list) and now - float(CODEX_QUOTA_CACHE.get("ts") or 0) < CODEX_QUOTA_CACHE_TTL:
                return [str(line) for line in cached]
        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from tg_codex_bot import TgCodexService

            lines = TgCodexService._quota_status_lines(Path(self.state.codex_home).expanduser())
        except Exception as e:
            lines = [f"额度: unavailable（{e.__class__.__name__}）"]
        lines = self._sanitize_quota_lines(lines)
        with CODEX_QUOTA_CACHE_LOCK:
            CODEX_QUOTA_CACHE["ts"] = now
            CODEX_QUOTA_CACHE["lines"] = list(lines)
        return list(lines)

    def _codex_runtime_detail(self, session_id: str | None, cwd: Path) -> dict[str, Any]:
        account_label = self._mask_status_email(os.environ.get("CODEX_STATUS_ACCOUNT_LABEL", "main") or "main")
        quota_lines = self._codex_quota_lines_cached()
        quota = self._parse_quota_lines(quota_lines)

        meta = None
        session_title = f"session {str(session_id or '')[:8]}".strip()
        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from codex_common import SessionStore

            root = Path(os.environ.get("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
            if session_id:
                meta = SessionStore(root).find_by_id(session_id)
                if meta and getattr(meta, "title", ""):
                    session_title = str(meta.title)
        except Exception:
            meta = None
        context = self._codex_context_snapshot(meta)
        percent_value = context.get("used_percent")
        percent_text = ""
        try:
            if percent_value is not None:
                percent_text = f"{float(percent_value):.0f}%"
        except (TypeError, ValueError):
            percent_text = ""
        summary_parts = [self.state.codex_model]
        if percent_text:
            summary_parts.append(percent_text)
        if quota.get("plan"):
            summary_parts.append(str(quota.get("plan")))
        return {
            "account": {
                "label": account_label,
                "email": quota.get("email") or "",
            },
            "permissions": {
                "bypass": 2,
                "text": "bypass=2",
            },
            "quota": quota,
            "current_session": {
                "title": session_title if session_id else "当前没有绑定会话",
                "session_id": session_id or "",
                "cwd": str(cwd),
                "shared_pointer": self.state.codex_shared_session_name,
                "supports_local_client": True,
            },
            "context": context,
            "summary": {
                "default": " · ".join(part for part in summary_parts if str(part).strip()),
                "model": self.state.codex_model,
                "context_percent": percent_text,
                "plan": quota.get("plan") or "",
            },
            "runtime_lines": [
                "运行状态:",
                f"account: {account_label}",
                f"model: {self.state.codex_model}",
                f"cwd: {cwd}",
                "权限: bypass=2",
                *quota_lines,
                "",
                "当前会话:",
                session_title if session_id else "当前没有绑定会话。",
                f"session: {session_id or ''}",
                f"cwd: {cwd}",
                "支持与本地 Codex 客户端交替续聊。",
                f"共享指针: {self.state.codex_shared_session_name}",
                "",
                "上下文:",
                str(context.get("context_text") or ""),
                str(context.get("last_turn_text") or ""),
                f"forge: {context.get('forge_hint') or '暂无判断依据'}",
            ],
        }

    def _terminate_codex_processes(self, processes: list[dict[str, Any]]) -> int:
        terminated = 0
        for proc in processes:
            try:
                pid = int(proc.get("pid"))
            except Exception:
                continue
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    continue
            terminated += 1
        if terminated:
            time.sleep(1.0)
        for proc in processes:
            try:
                pid = int(proc.get("pid"))
            except Exception:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except Exception:
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        return terminated

    def _handle_codex_status(self):
        self._send_json(200, self._codex_busy_snapshot())

    def _handle_codex_sessions(self):
        session_id, _ = self._load_codex_target()
        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from codex_common import SessionStore

            root = Path(os.environ.get("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
            sessions = []
            for meta in SessionStore(root).list_recent(limit=24):
                sessions.append({
                    "sid": meta.session_id,
                    "name": meta.title,
                    "mtime_iso": meta.timestamp,
                    "cwd": meta.cwd,
                    "preview": meta.title,
                    "active": bool(session_id and meta.session_id == session_id),
                })
            self._send_json(200, {"ok": True, "sessions": sessions, "active_session_id": session_id})
        except Exception as e:
            logger.exception("codex/sessions failed")
            self._send_json(500, {"ok": False, "error": str(e)})

    def _cancel_kairos_pending_task(
        self,
        contact_id: str,
        user_ts: str | None = None,
    ) -> dict[str, Any] | None:
        """Cancel one exact/current Kairos task without touching another Codex run."""
        target_contact = str(contact_id or "kairos").strip().lower() or "kairos"
        target_user_ts = str(user_ts or "").strip()
        cancelled: dict[str, Any] | None = None
        remaining_for_contact: list[dict[str, Any]] = []
        active_for_contact = False
        with self.state.kairos_queue_lock:
            active = self.state.kairos_active_task
            active_for_contact = bool(
                isinstance(active, dict)
                and str(active.get("contact_id") or "kairos").strip().lower() == target_contact
            )
            active_matches = bool(
                isinstance(active, dict)
                and str(active.get("contact_id") or "kairos").strip().lower() == target_contact
                and (not target_user_ts or str(active.get("user_ts") or "") == target_user_ts)
            )
            if active_matches:
                event = self.state.kairos_active_task_cancel
                if isinstance(event, threading.Event):
                    event.set()
                cancelled = dict(active)
                cancelled["cancel_kind"] = "active_task"
            else:
                items = list(self.state.kairos_queue)
                matching_indexes = [
                    idx for idx, item in enumerate(items)
                    if str(item.get("contact_id") or "kairos").strip().lower() == target_contact
                    and (not target_user_ts or str(item.get("user_ts") or "") == target_user_ts)
                ]
                if matching_indexes:
                    idx = matching_indexes[-1]
                    cancelled = dict(items.pop(idx))
                    cancelled["cancel_kind"] = "queued_task"
                    self.state.kairos_queue = deque(items)
                    self.state.persist_kairos_queue_locked()
            remaining_for_contact = [
                dict(item) for item in self.state.kairos_queue
                if str(item.get("contact_id") or "kairos").strip().lower() == target_contact
            ]

        if cancelled and cancelled.get("cancel_kind") == "queued_task":
            cancelled_ts = str(cancelled.get("user_ts") or "")
            mark_interrupted = not active_for_contact and not remaining_for_contact
            cancelled["mark_interrupted"] = mark_interrupted
            if remaining_for_contact and not active_for_contact:
                next_task = remaining_for_contact[0]
                self._set_chat_queued(
                    target_contact,
                    user_ts=str(next_task.get("user_ts") or ""),
                    queued_at=str(next_task.get("queued_at") or ""),
                    queue_position=1,
                    source="cc-app:kairos",
                )
            elif mark_interrupted:
                self._set_chat_interrupted(
                    target_contact,
                    user_ts=cancelled_ts,
                    source="cc-app:kairos:cancelled-before-run",
                )
                self._set_typing_for_contact(target_contact, {"is_typing": False, "since": None})
        return cancelled

    def _handle_codex_abort(self, body: dict[str, Any]):
        contact_id = str(body.get("contact_id") or "").strip().lower()
        cancel_pending = bool(body.get("cancel_pending"))
        target_user_ts = str(body.get("user_ts") or "").strip() or None
        if contact_id == "kairos" and cancel_pending:
            pending = self._cancel_kairos_pending_task(contact_id, target_user_ts)
            run = CODEX_RUNS.cancel_latest(
                source="cc-app:kairos",
                contact_id=contact_id,
                user_ts=target_user_ts,
            )
            bridge_interrupted = self.state.codex_app_bridge.interrupt_active() if run else False
            ok = bool(pending or run or bridge_interrupted)
            resolved_user_ts = str(
                (pending or {}).get("user_ts")
                or (run or {}).get("user_ts")
                or target_user_ts
                or ""
            )
            cancel_kind = str((pending or {}).get("cancel_kind") or "")
            # An active worker owns the live draft and will persist that exact
            # partial text before transitioning to interrupted.  Clearing the
            # draft here races the worker and loses everything the App already
            # displayed.  Only a queued task with no worker can be finalized
            # eagerly in the request handler.
            should_mark_interrupted = bool((pending or {}).get("mark_interrupted"))
            if ok and should_mark_interrupted and not run and cancel_kind != "active_task":
                self._set_chat_interrupted(
                    contact_id,
                    user_ts=resolved_user_ts,
                    source="cc-app:kairos:cancelled",
                    session_id=str((run or {}).get("session_id") or "") or None,
                )
                self._set_typing_for_contact(contact_id, {"is_typing": False, "since": None})
            action = "active_run" if run else (
                (cancel_kind or "pending_task") if pending else "none"
            )
            self._send_json(200, {
                "ok": ok,
                "action": action,
                "contact_id": contact_id,
                "user_ts": resolved_user_ts or None,
                "bridge_interrupted": bridge_interrupted,
                "message": (
                    "已请求中断 Kairos 当前生成。"
                    if ok else "当前没有可中断的 Kairos 生成或排队消息。"
                ),
            })
            return

        run = CODEX_RUNS.cancel_latest()
        bridge_interrupted = self.state.codex_app_bridge.interrupt_active()
        session_id, cwd = self._load_codex_target()
        cwd = self._codex_allowed_cwd(cwd)
        target_session_id = str((run or {}).get("session_id") or session_id or "").strip() or None
        target_cwd = self._codex_allowed_cwd(Path(str((run or {}).get("cwd") or cwd)).expanduser())
        processes = self._codex_exec_processes(session_id=target_session_id, cwd=target_cwd)
        if not run and not processes:
            processes = self._codex_exec_processes(cwd=target_cwd)
        killed = 0
        if not run and processes:
            killed = self._terminate_codex_processes(processes)
        self._set_typing_for_contact("kairos", {"is_typing": False, "since": None})
        self._set_typing_for_contact("apples", {"is_typing": False, "since": None})
        action = "turn_interrupt" if bridge_interrupted else (
            "cancel_event" if run else ("terminated_process" if killed else "none")
        )
        self._send_json(200, {
            "ok": bool(run or bridge_interrupted or killed),
            "action": action,
            "session_id": target_session_id,
            "pid": processes[0]["pid"] if processes else None,
            "message": "已请求中断 Kairos Codex 生成。" if (run or bridge_interrupted or killed) else "当前没有 CcCompanion 可中断的 Kairos Codex 生成。",
        })

    def _handle_codex_new_session(self, body: dict[str, Any]):
        _, current_cwd = self._load_codex_target()
        cwd_text = str(body.get("cwd") or "").strip()
        cwd = Path(cwd_text).expanduser() if cwd_text else current_cwd
        cwd = self._codex_allowed_cwd(cwd)
        self._save_codex_target(None, cwd, "cc-app:codex:new_session")
        self._send_json(200, {"ok": True, "active_session_id": None, "active_cwd": str(cwd)})

    def _handle_codex_switch(self, body: dict[str, Any]):
        session_id = str(body.get("session_id") or body.get("sessionId") or "").strip()
        if not session_id:
            self._send_json(400, {"ok": False, "error": "session_id required"})
            return
        if len(session_id) > 200 or any(ch.isspace() for ch in session_id):
            self._send_json(400, {"ok": False, "error": "invalid session_id"})
            return
        _, cwd = self._load_codex_target()
        cwd_text = str(body.get("cwd") or "").strip()
        if cwd_text:
            cwd = Path(cwd_text).expanduser()
        cwd = self._codex_allowed_cwd(cwd)
        self._save_codex_target(session_id, cwd, "cc-app:codex:switch")
        self._send_json(200, {"ok": True, "active_session_id": session_id, "active_cwd": str(cwd)})

    def _handle_codex_forge(self, body: dict[str, Any]):
        old_session_id, cwd = self._load_codex_target()
        cwd = self._codex_allowed_cwd(cwd)
        if not old_session_id:
            self._send_json(400, {"ok": False, "error": "当前没有 active Kairos session。"})
            return
        if CODEX_RUNS.latest() or self.state.codex_app_bridge.snapshot().get("busy") or self._codex_exec_processes(cwd=cwd):
            self._send_json(409, {"ok": False, "error": "Kairos 正在生成中，请稍后再 forge。"})
            return
        history_limit = self._parse_codex_forge_limit(body.get("retain"))
        model = str(body.get("model") or "").strip() or None
        run = CODEX_RUNS.start(source="cc-app:codex:forge", session_id=old_session_id, cwd=cwd)
        if run is None:
            self._send_json(409, {"ok": False, "error": "Kairos 正在生成中，请稍后再 forge。"})
            return
        run_id, cancel_event = run
        worker = threading.Thread(
            target=self._run_codex_forge_worker,
            args=(run_id, cancel_event, old_session_id, cwd, history_limit, model),
            daemon=True,
        )
        worker.start()
        self._send_json(202, {
            "ok": True,
            "message": f"Kairos forge 已开始，保留最近 {history_limit} 条上下文；完成后会自动切到新 session。",
            "active_session_id": old_session_id,
            "active_cwd": str(cwd),
        })

    def _parse_codex_forge_limit(self, raw: Any) -> int:
        text = str(raw or "").strip().lower()
        if text in {"all", "max", "full"}:
            return 160
        try:
            value = int(text) if text else 80
        except ValueError:
            value = 80
        return max(20, min(160, value))

    def _build_codex_forge_prompt(
        self,
        old_session_id: str,
        cwd: Path,
        messages: list[tuple[str, str]],
    ) -> str:
        lines = [
            "这是一次 Codex App 控制台触发的平滑 forge 交接。",
            "",
            "你将作为新的 active session 继续服务 Astra / 方小南。",
            "请遵守当前仓库的 AGENTS.md，以及本会话中的系统和开发者规则。",
            "不要复述整段交接内容，不要暴露任何密钥或本地敏感内容。",
            "你的任务只是吸收上下文，后续用户继续说话时自然续聊。",
            "",
            f"旧 session: {old_session_id}",
            f"工作目录: {cwd}",
            "",
            "近期对话摘要如下：",
        ]
        if not messages:
            lines.append("- 没有读到可用的近期对话。")
        for role, message in messages:
            compact = " ".join(str(message or "").split())
            if len(compact) > 1200:
                compact = compact[:1199] + "..."
            role_label = "Astra" if role == "user" else "Kairos"
            lines.append(f"- {role_label}: {compact}")
        lines.extend([
            "",
            "请只回复一行：forge-ready，然后用很短一句中文说明已经接住上下文。",
        ])
        return "\n".join(lines)

    @staticmethod
    def _short_codex_session_id(session_id: str | None) -> str:
        clean = str(session_id or "").strip()
        return clean[:8] if clean else "无"

    def _auto_forge_pointer_notice(self, old_session_id: str) -> str:
        current_session_id, _ = self._load_codex_target()
        old_short = self._short_codex_session_id(old_session_id)
        current_short = self._short_codex_session_id(current_session_id)
        if current_session_id == old_session_id:
            return f"旧 session {old_short} 仍保持 active"
        return f"当前 pointer 为 {current_short}，未被覆盖"

    def _run_kairos_auto_forge(
        self,
        *,
        old_session_id: str,
        cwd: Path,
        token_usage: CodexThreadTokenUsage | None,
        context_compacted: bool,
        cancel_event: threading.Event,
    ) -> bool:
        """Run a new app-server thread synchronously while the Kairos queue lock is held."""
        chat = self.state.contact_chats["kairos"]
        claims = self.state.codex_auto_forge_claims
        usage_percent = _auto_forge_usage_percent(token_usage)
        usage_label = f"{usage_percent:.1f}%" if usage_percent is not None else "比例未知"
        reason = (
            f"系统压缩已先发生，最新上下文用量 {usage_label}"
            if context_compacted
            else f"上下文用量 {usage_label}"
        )
        old_short = self._short_codex_session_id(old_session_id)

        try:
            claimed = claims.claim(old_session_id)
        except Exception:
            logger.exception("auto forge claim failed old_session=%s", old_session_id)
            chat.append(
                role="assistant",
                text=(
                    f"Kairos 自动 forge 未启动（{reason}）：去重状态写入失败；"
                    f"{self._auto_forge_pointer_notice(old_session_id)}。"
                ),
                source="codex:kairos:auto-forge",
            )
            return False
        if not claimed:
            return False

        def _finish(status: str) -> None:
            try:
                claims.finish(old_session_id, status)
            except Exception:
                logger.exception("auto forge result persistence failed old_session=%s", old_session_id)

        def _append_failure(message: str, status: str = "failed") -> bool:
            _finish(status)
            chat.append(
                role="assistant",
                text=(
                    f"Kairos 自动 forge {message}（{reason}，旧 {old_short}）；"
                    f"{self._auto_forge_pointer_notice(old_session_id)}。"
                ),
                source="codex:kairos:auto-forge",
            )
            return False

        if cancel_event.is_set():
            return _append_failure("已取消", "cancelled")

        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from codex_common import SessionStore

            default_root = Path(self.state.codex_home).expanduser() / "sessions"
            root = Path(os.environ.get("CODEX_SESSION_ROOT", str(default_root))).expanduser()
            session_store = SessionStore(root)
            meta, messages = session_store.get_history(
                old_session_id,
                limit=self.state.codex_auto_forge_retain_messages,
            )
            if not meta:
                return _append_failure("失败：读不到旧 session 历史")
            prompt = self._build_codex_forge_prompt(old_session_id, cwd, messages)
            result = self.state.codex_app_bridge.run_turn(
                thread_id=None,
                cwd=cwd,
                prompt=prompt,
                model=self.state.codex_model,
                effort=self.state.codex_reasoning_effort,
                cancel_event=cancel_event,
                max_runtime_sec=900,
            )
            if cancel_event.is_set() or result.status == "interrupted":
                return _append_failure("已取消", "cancelled")
            new_session_id = str(result.thread_id or "").strip()
            if result.status != "completed" or not new_session_id or new_session_id == old_session_id:
                logger.warning(
                    "auto forge handoff failed old_session=%s status=%s error=%s",
                    old_session_id,
                    result.status,
                    str(result.error or "")[:500],
                )
                return _append_failure("失败：新 thread 未完成交接")

            switched, current_session_id = self._compare_and_swap_codex_target(
                old_session_id,
                new_session_id,
                cwd,
                "cc-app:kairos:auto-forge",
            )
            new_short = self._short_codex_session_id(new_session_id)
            if not switched:
                _finish("cas_failed")
                current_short = self._short_codex_session_id(current_session_id)
                chat.append(
                    role="assistant",
                    text=(
                        f"Kairos 自动 forge 已生成新 session，但 CAS 切换取消（{reason}）："
                        f"旧 {old_short}，新 {new_short}；当前 pointer 为 {current_short}，未覆盖。"
                    ),
                    source="codex:kairos:auto-forge",
                )
                return False
            try:
                session_store.mark_as_desktop_session(new_session_id)
            except Exception:
                logger.debug("mark auto-forged Codex session failed", exc_info=True)
            _finish("completed")
            chat.append(
                role="assistant",
                text=(
                    f"Kairos 自动 forge 完成（{reason}）：旧 {old_short} → 新 {new_short}，"
                    "active pointer 已原子切换。"
                ),
                source="codex:kairos:auto-forge",
            )
            logger.info(
                "auto forge completed old_session=%s new_session=%s usage_percent=%s compacted=%s",
                old_session_id,
                new_session_id,
                usage_percent,
                context_compacted,
            )
            return True
        except Exception:
            logger.exception("kairos auto forge failed old_session=%s", old_session_id)
            return _append_failure("异常失败")

    def _run_codex_forge_worker(
        self,
        run_id: str,
        cancel_event: threading.Event,
        old_session_id: str,
        cwd: Path,
        history_limit: int,
        model: str | None,
    ) -> None:
        try:
            sys.path.insert(0, "/root/Windows-Codex-TG")
            from codex_common import CodexRunner, SessionStore

            root = Path(os.environ.get("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
            meta, messages = SessionStore(root).get_history(old_session_id, limit=history_limit)
            if not meta:
                logger.warning("codex forge failed: missing old session %s", old_session_id)
                return
            prompt = self._build_codex_forge_prompt(old_session_id, cwd, messages)
            runner = CodexRunner(
                codex_bin=self.state.codex_bin,
                sandbox_mode=None,
                approval_policy=None,
                dangerous_bypass_level=2,
                idle_timeout_sec=240,
            )
            env_overrides = {
                "CODEX_HOME": self.state.codex_home,
                "CODEX_MODEL": model or self.state.codex_model,
                "CODEX_REASONING_EFFORT": self.state.codex_reasoning_effort,
            }
            thread_id, answer, stderr_text, return_code = runner.run_prompt(
                prompt=prompt,
                cwd=cwd,
                session_id=None,
                env_overrides=env_overrides,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                logger.info("codex forge cancelled old_session=%s", old_session_id)
                return
            if return_code != 0 or not thread_id:
                detail = " ".join((answer or stderr_text or "").split())
                logger.warning("codex forge failed old_session=%s detail=%s", old_session_id, detail[:800])
                return
            self._save_codex_target(thread_id, cwd, "cc-app:codex:forge")
            try:
                SessionStore(root).mark_as_desktop_session(thread_id)
            except Exception:
                logger.debug("mark forged codex session failed", exc_info=True)
            logger.info("codex forge completed old_session=%s new_session=%s", old_session_id, thread_id)
        except Exception:
            logger.exception("codex forge worker failed")
        finally:
            CODEX_RUNS.finish(run_id)

    def _kairos_queue_position_locked(self, contact_id: str, user_ts: str) -> int:
        position = 0
        for task in self.state.kairos_queue:
            if task.get("contact_id") != contact_id:
                continue
            position += 1
            if task.get("user_ts") == user_ts:
                return max(1, position)
        return 1

    def _ensure_kairos_queue_worker(self) -> None:
        should_start = False
        with self.state.kairos_queue_lock:
            if self.state.kairos_queue and not self.state.kairos_queue_worker_running:
                self.state.kairos_queue_worker_running = True
                should_start = True
        if should_start:
            threading.Thread(target=self._kairos_queue_worker, daemon=True).start()

    def _enqueue_kairos_task(self, task: dict[str, Any]) -> None:
        contact_id = str(task.get("contact_id") or "kairos")
        user_ts = str(task.get("user_ts") or "")
        queued_at = str(task.get("queued_at") or user_ts or datetime.now(timezone.utc).isoformat())
        task["queued_at"] = queued_at
        with self.state.kairos_queue_lock:
            self.state.kairos_queue.append(task)
            position = self._kairos_queue_position_locked(contact_id, user_ts)
            self.state.persist_kairos_queue_locked()
            self._set_chat_queued(
                contact_id,
                user_ts=user_ts,
                queued_at=queued_at,
                queue_position=position,
                source="cc-app:kairos",
            )
            should_start = not self.state.kairos_queue_worker_running
            if should_start:
                self.state.kairos_queue_worker_running = True
        if should_start:
            threading.Thread(target=self._kairos_queue_worker, daemon=True).start()

    def _kairos_queue_worker(self) -> None:
        while True:
            with self.state.kairos_queue_lock:
                if not self.state.kairos_queue:
                    self.state.kairos_queue_worker_running = False
                    self.state.persist_kairos_queue_locked()
                    return
                task = self.state.kairos_queue.popleft()
                task_cancel_event = threading.Event()
                self.state.kairos_active_task = task
                self.state.kairos_active_task_cancel = task_cancel_event
                self.state.persist_kairos_queue_locked()
                for idx, queued in enumerate(self.state.kairos_queue, start=1):
                    self._set_chat_queued(
                        str(queued.get("contact_id") or "kairos"),
                        user_ts=str(queued.get("user_ts") or ""),
                        queued_at=str(queued.get("queued_at") or ""),
                        queue_position=idx,
                        source="cc-app:kairos",
                    )
            try:
                self._process_kairos_task(task, task_cancel_event=task_cancel_event)
            finally:
                with self.state.kairos_queue_lock:
                    active = self.state.kairos_active_task or {}
                    if str(active.get("user_ts") or "") == str(task.get("user_ts") or ""):
                        self.state.kairos_active_task = None
                        self.state.kairos_active_task_cancel = None

    def _kairos_prompt_for_task(self, task: dict[str, Any]) -> str:
        text = str(task.get("text") or "").strip()
        attachment_lines = []
        for path in task.get("image_paths") or []:
            attachment_lines.append(f"- 图片本地路径：{path}")
        attachment_text = ""
        if attachment_lines:
            attachment_text = "\n\n随附图片：\n" + "\n".join(attachment_lines)
        return (
            "当前时间：" + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M") + "\n"
            "[消息来源]\n入口: cc_companion_kairos_private\ncontact_id: kairos\n\n"
            "Astra 正在通过 CcCompanion app 和 Kairos 对话。请直接回复她，不要提到后台路由。\n"
            f"对方说：{text or '[发来了一张图片]'}"
            f"{attachment_text}"
        )

    @staticmethod
    def _is_codex_prompt_busy_answer(answer: str) -> bool:
        return str(answer or "").startswith("同一个 Kairos session 正在另一边生成中")

    def _process_kairos_task(
        self,
        task: dict[str, Any],
        *,
        task_cancel_event: threading.Event | None = None,
    ) -> None:
        contact_id = str(task.get("contact_id") or "kairos")
        text = str(task.get("text") or "")
        user_ts = str(task.get("user_ts") or "")
        queued_at = str(task.get("queued_at") or user_ts)
        chat = self._chat_for_contact(contact_id)
        run_id = None
        cancel_event = task_cancel_event or threading.Event()
        session_id: str | None = None
        assistant_appended = False
        source = "codex:kairos"
        activity_count = 0
        activity_items: list[str] = []
        wait_started_at = time.monotonic()
        max_queue_wait_sec = 900.0
        if user_ts:
            self.state.mark_kairos_pending_run(contact_id, user_ts, text)

        def _append_activity_card() -> None:
            if not activity_items or task.get("activity_appended"):
                return
            for activity in activity_items:
                chat.append(role="task", text=activity, source="codex:kairos:activity")
            task["activity_appended"] = True

        def _append_assistant(message: str, append_source: str = source) -> None:
            nonlocal assistant_appended
            _append_activity_card()
            assistant_rec = chat.append(
                role="assistant",
                text=message,
                source=append_source,
                metadata={"kairos_user_ts": user_ts} if user_ts else None,
            )
            assistant_appended = True
            self._set_chat_completed(
                contact_id,
                user_ts=user_ts,
                final_ts=str(assistant_rec.get("ts") or ""),
                source=append_source,
            )
            self.state.clear_kairos_pending_run(user_ts)

        def _current_draft_text() -> str:
            with self.state.chat_draft_lock:
                draft = self.state.chat_drafts.get(contact_id)
                if isinstance(draft, dict):
                    return str(draft.get("text") or "").strip()
            return ""

        def _append_interrupted_draft() -> None:
            nonlocal assistant_appended
            _append_activity_card()
            draft_text = _current_draft_text()
            if draft_text:
                message = (
                    draft_text
                    + "\n\n[已停止]"
                )
            else:
                message = "已中断当前生成。"
            interrupted_rec = chat.append(
                role="assistant",
                text=message,
                source=f"{source}:interrupted",
                metadata={"kairos_user_ts": user_ts} if user_ts else None,
            )
            assistant_appended = True
            self._set_chat_interrupted(
                contact_id,
                user_ts=user_ts,
                final_ts=str(interrupted_rec.get("ts") or ""),
                source=source,
                session_id=session_id,
            )
            self.state.clear_kairos_pending_run(user_ts)

        def _append_uncertain_draft(detail: str) -> None:
            nonlocal assistant_appended
            _append_activity_card()
            draft_text = _current_draft_text()
            notice = (
                "[app-server 连接中断，当前 turn 的最终状态无法确认。"
                "为避免重复执行工具，这条消息不会自动重放。]"
            )
            message = f"{draft_text}\n\n{notice}" if draft_text else notice
            chat.append(
                role="assistant",
                text=message,
                source=f"{source}:uncertain",
                metadata={"kairos_user_ts": user_ts} if user_ts else None,
            )
            assistant_appended = True
            self._set_chat_interrupted(
                contact_id,
                user_ts=user_ts,
                source=f"{source}:uncertain",
                session_id=session_id,
            )
            self.state.clear_kairos_pending_run(user_ts)

        lock = getattr(type(self), "_kairos_codex_lock", None)
        if lock is None:
            lock = threading.Lock()
            type(self)._kairos_codex_lock = lock

        with lock:
            try:
                if cancel_event.is_set():
                    _append_interrupted_draft()
                    return
                session_id, cwd = self._load_codex_target()
                while True:
                    if cancel_event.is_set():
                        _append_interrupted_draft()
                        return
                    if time.monotonic() - wait_started_at >= max_queue_wait_sec:
                        _append_assistant("这条消息排队超过 15 分钟还没轮到，我先标记失败。你可以直接重发。")
                        return
                    if CODEX_RUNS.latest() or self._codex_session_busy(session_id):
                        self._set_chat_queued(
                            contact_id,
                            user_ts=user_ts,
                            queued_at=queued_at,
                            queue_position=1,
                            activity_text="等上一条结束",
                            source="cc-app:kairos",
                        )
                        cancel_event.wait(1.0)
                        continue
                    run = CODEX_RUNS.start(
                        source="cc-app:kairos",
                        session_id=session_id,
                        cwd=cwd,
                        cancel_event=cancel_event,
                        contact_id=contact_id,
                        user_ts=user_ts,
                    )
                    if run is not None:
                        run_id, cancel_event = run
                        break
                    cancel_event.wait(1.0)

                if cancel_event.is_set():
                    _append_interrupted_draft()
                    return

                started_at = self._set_chat_generating(
                    contact_id,
                    user_ts=user_ts,
                    queued_at=queued_at,
                    source="cc-app:kairos",
                    session_id=session_id,
                )
                self._set_typing_for_contact(contact_id, {"is_typing": True, "since": user_ts or started_at})
                prompt = self._kairos_prompt_for_task(task)
                sys.path.insert(0, "/root/Windows-Codex-TG")
                from codex_common import CodexRunner, SessionStore

                runner = CodexRunner(
                    codex_bin=self.state.codex_bin,
                    sandbox_mode=None,
                    approval_policy=None,
                    dangerous_bypass_level=2,
                    idle_timeout_sec=240,
                )
                env_overrides = {
                    "CODEX_HOME": self.state.codex_home,
                    "CODEX_MODEL": self.state.codex_model,
                    "CODEX_REASONING_EFFORT": self.state.codex_reasoning_effort,
                }

                def _on_update(live_text: str) -> None:
                    self._set_chat_draft(
                        contact_id,
                        live_text,
                        source=source,
                        session_id=session_id,
                        user_ts=user_ts,
                        queued_at=queued_at,
                        started_at=started_at,
                        activity_text=str(task.get("activity_text") or ""),
                        activity_count=activity_count,
                        activity_items=activity_items,
                    )
                    self.state.update_kairos_pending_draft(contact_id, user_ts, live_text)

                def _on_thread(new_thread_id: str) -> None:
                    nonlocal session_id
                    if not self._save_codex_target(new_thread_id, cwd, "cc-app:kairos:app-server"):
                        raise CodexAppBridgeError("failed to persist app-server thread pointer")
                    session_id = new_thread_id

                def _on_activity(activity_text: str) -> None:
                    nonlocal activity_count
                    activity = str(activity_text or "").strip()
                    if not activity:
                        return
                    activity_count += 1
                    activity_items.append(activity)
                    task["activity_text"] = activity
                    self._set_chat_activity(
                        contact_id,
                        activity_text=activity,
                        activity_count=activity_count,
                        activity_items=activity_items,
                    )

                session_root = Path(os.environ.get("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser()
                session_store = SessionStore(session_root)

                def _codex_session_marker(target_session_id: str | None) -> tuple[Path, int] | None:
                    if not target_session_id:
                        return None
                    try:
                        meta = session_store.find_by_id(target_session_id)
                        if not meta:
                            return None
                        path = Path(meta.file_path)
                        return path, path.stat().st_size
                    except Exception:
                        return None

                def _mcp_activity_label(evt: dict[str, Any], seen_call_ids: set[str]) -> str:
                    payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else {}
                    payload_type = str(payload.get("type") or "").strip()
                    call_id = str(payload.get("call_id") or "").strip()
                    if payload_type in {"function_call", "custom_tool_call"}:
                        namespace = str(payload.get("namespace") or "").strip()
                        if not namespace.startswith("mcp__"):
                            return ""
                        if call_id:
                            seen_call_ids.add(call_id)
                        name = str(payload.get("name") or "").strip()
                        return f"调用 {name}" if name else ""
                    if payload_type == "mcp_tool_call_end":
                        if call_id and call_id in seen_call_ids:
                            return ""
                        invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
                        name = str(
                            invocation.get("tool")
                            or invocation.get("name")
                            or invocation.get("tool_name")
                            or ""
                        ).strip()
                        return f"调用 {name}" if name else ""
                    return ""

                def _harvest_mcp_session_activities(
                    marker: tuple[Path, int] | None,
                    target_session_id: str | None,
                ) -> None:
                    resolved_marker = marker or _codex_session_marker(target_session_id)
                    if not resolved_marker:
                        return
                    path, offset = resolved_marker
                    if not path.exists():
                        return
                    seen_call_ids: set[str] = set()
                    try:
                        with path.open("rb") as f:
                            f.seek(max(0, offset))
                            for raw in f:
                                try:
                                    evt = json.loads(raw.decode("utf-8", "replace"))
                                except Exception:
                                    continue
                                activity = _mcp_activity_label(evt, seen_call_ids)
                                if activity:
                                    _on_activity(activity)
                    except Exception:
                        logger.debug("harvest MCP session activities failed", exc_info=True)

                image_paths = [Path(p) for p in task.get("image_paths") or [] if str(p).strip()]
                bridge_status: str | None = None
                bridge_token_usage: CodexThreadTokenUsage | None = None
                bridge_context_compacted = False
                while True:
                    if time.monotonic() - wait_started_at >= max_queue_wait_sec:
                        _append_assistant("这条消息排队超过 15 分钟还没轮到，我先标记失败。你可以直接重发。")
                        return
                    backend = self.state.codex_kairos_backend
                    if backend == "app-server":
                        try:
                            bridge_result = self.state.codex_app_bridge.run_turn(
                                thread_id=session_id,
                                cwd=cwd,
                                prompt=prompt,
                                model=self.state.codex_model,
                                effort=self.state.codex_reasoning_effort,
                                image_paths=image_paths,
                                cancel_event=cancel_event,
                                on_update=_on_update,
                                on_activity=_on_activity,
                                on_thread=_on_thread,
                                marker_provider=self._codex_rollout_marker,
                                # Interactive Kairos turns can legitimately run while
                                # agents, builds, or reviews are still making progress.
                                # A zero wall-clock limit leaves explicit Stop/cancel as
                                # the only interruption path; the separate auto-forge
                                # handoff remains bounded above.
                                max_runtime_sec=0,
                            )
                            thread_id = bridge_result.thread_id
                            answer = bridge_result.text
                            stderr_text = bridge_result.error or ""
                            bridge_status = bridge_result.status
                            bridge_token_usage = bridge_result.token_usage
                            bridge_context_compacted = bridge_result.context_compacted
                            return_code = 0 if bridge_status == "completed" else 1
                        except CodexPromptLockBusy:
                            thread_id = session_id
                            answer = "同一个 Kairos session 正在另一边生成中。"
                            stderr_text = ""
                            return_code = 0
                            bridge_status = "busy"
                        except CodexAppBridgeError as exc:
                            logger.warning(
                                "kairos app-server failure fallback_safe=%s uncertain=%s error=%s",
                                exc.fallback_safe,
                                exc.uncertain,
                                str(exc)[:500],
                            )
                            if exc.uncertain:
                                _append_uncertain_draft(str(exc))
                                return
                            if not (self.state.codex_app_server_fallback_to_exec and exc.fallback_safe):
                                _append_assistant(
                                    "Kairos 的 app-server 接入失败了，这条消息没有进入模型。"
                                    "我没有自动切回旧链路，避免在状态不明时重复执行。",
                                    append_source=f"{source}:app-server-error",
                                )
                                return
                            logger.warning("kairos app-server pre-start failure; using legacy exec fallback")
                            session_marker = _codex_session_marker(session_id)
                            thread_id, answer, stderr_text, return_code = runner.run_prompt(
                                prompt=prompt,
                                cwd=cwd,
                                session_id=session_id,
                                env_overrides=env_overrides,
                                cancel_event=cancel_event,
                                on_update=_on_update,
                                on_activity=_on_activity,
                                image_paths=image_paths,
                                max_runtime_sec=0,
                            )
                            _harvest_mcp_session_activities(session_marker, thread_id or session_id)
                            bridge_status = None
                    else:
                        session_marker = _codex_session_marker(session_id)
                        thread_id, answer, stderr_text, return_code = runner.run_prompt(
                            prompt=prompt,
                            cwd=cwd,
                            session_id=session_id,
                            env_overrides=env_overrides,
                            cancel_event=cancel_event,
                            on_update=_on_update,
                            on_activity=_on_activity,
                            image_paths=image_paths,
                            max_runtime_sec=0,
                        )
                        _harvest_mcp_session_activities(session_marker, thread_id or session_id)
                        bridge_status = None
                    if not self._is_codex_prompt_busy_answer(answer):
                        break
                    if run_id:
                        CODEX_RUNS.finish(run_id)
                        run_id = None
                    self._set_chat_queued(
                        contact_id,
                        user_ts=user_ts,
                        queued_at=queued_at,
                        queue_position=1,
                        activity_text="等上一条结束",
                        source="cc-app:kairos",
                    )
                    cancel_event.wait(1.0)
                    if cancel_event.is_set():
                        _append_interrupted_draft()
                        return
                    run = CODEX_RUNS.start(
                        source="cc-app:kairos",
                        session_id=session_id,
                        cwd=cwd,
                        cancel_event=cancel_event,
                        contact_id=contact_id,
                        user_ts=user_ts,
                    )
                    if run is not None:
                        run_id, cancel_event = run
                        self._set_chat_generating(
                            contact_id,
                            user_ts=user_ts,
                            queued_at=queued_at,
                            started_at=started_at,
                            source="cc-app:kairos",
                            session_id=session_id,
                        )
                        continue
                    cancel_event.wait(1.0)

                if cancel_event and cancel_event.is_set():
                    _append_interrupted_draft()
                    return
                if bridge_status == "interrupted":
                    _append_interrupted_draft()
                    return
                if bridge_status == "uncertain":
                    _append_uncertain_draft(stderr_text)
                    return
                if bridge_status == "failed":
                    logger.warning("kairos app-server turn failed error=%s", stderr_text[:500])
                    _append_assistant(
                        "Kairos 这次生成失败了，app-server 已保留原 thread；你可以重发这条消息。",
                        append_source=f"{source}:app-server-failed",
                    )
                    return
                if thread_id:
                    self._save_codex_target(thread_id, cwd, "cc-app:kairos")
                if return_code != 0 and stderr_text:
                    logger.warning("kairos codex return_code=%s stderr=%s", return_code, stderr_text[-800:])
                answer = (answer or "").strip() or _current_draft_text() or "Kairos 没有返回可展示内容。"
                _append_assistant(answer)
                if (
                    contact_id == "kairos"
                    and self.state.codex_auto_forge_enabled
                    and bridge_status == "completed"
                    and thread_id
                    and _should_auto_forge(
                        bridge_token_usage,
                        threshold_percent=self.state.codex_auto_forge_threshold_percent,
                        context_compacted=bridge_context_compacted,
                    )
                ):
                    self._run_kairos_auto_forge(
                        old_session_id=thread_id,
                        cwd=cwd,
                        token_usage=bridge_token_usage,
                        context_compacted=bridge_context_compacted,
                        cancel_event=cancel_event,
                    )
            except Exception:
                logger.exception("kairos codex queue worker failed")
                _append_assistant("Kairos 接入出错：后端生成进程异常退出。你可以重发这条，我不会让它静默消失。")
            finally:
                if run_id:
                    CODEX_RUNS.finish(run_id)
                if not assistant_appended:
                    _append_interrupted_draft()
                self._set_typing_for_contact(contact_id, {"is_typing": False, "since": None})

    def _handle_kairos_chat_send(self, body: dict[str, Any], contact_id: str):
        text = body.get("text", "").strip()
        quoted_ts = body.get("quoted_ts") or None
        if not text:
            self._send_json(400, {"error": "text required"})
            return

        chat = self._chat_for_contact(contact_id)
        rec = chat.append(
            role="user",
            text=text,
            source="cc-app:kairos",
            quoted_ts=quoted_ts,
        )
        self._set_typing_for_contact(contact_id, {"is_typing": True, "since": rec["ts"]})
        self._clear_chat_draft(contact_id)
        self._enqueue_kairos_task({
            "contact_id": contact_id,
            "text": text,
            "quoted_ts": quoted_ts,
            "user_ts": rec["ts"],
            "queued_at": rec["ts"],
            "image_paths": [],
        })
        self._send_json(200, {"ok": True, "contact_id": contact_id, "record": rec, "queued": True})

    def _start_group_kairos_reply(self, chat: ChatHistory, text: str, sender_name: str = "Astra", hop_count: int = 0) -> None:
        self._clear_chat_draft("apples")

        def _worker():
            if CODEX_RUNS.latest():
                chat.append(
                    role="assistant",
                    text="我正在处理另一边的消息，稍等一下再叫我。",
                    source="group:kairos",
                    sender_id="kairos",
                    sender_name=self._apples_member_name("kairos"),
                )
                self._set_typing_for_contact("apples", {"is_typing": False, "since": None})
                return
            lock = getattr(type(self), "_kairos_codex_lock", None)
            if lock is None:
                lock = threading.Lock()
                type(self)._kairos_codex_lock = lock
            with lock:
                run_id = None
                cancel_event = None
                routed_after_answer: list[str] = []
                try:
                    session_id, cwd = self._load_codex_target()
                    run = CODEX_RUNS.start(source="cc-app:apples:kairos", session_id=session_id, cwd=cwd)
                    if run is None:
                        chat.append(
                            role="assistant",
                            text="我正在处理另一边的消息，稍等一下再叫我。",
                            source="group:kairos",
                            sender_id="kairos",
                            sender_name=self._apples_member_name("kairos"),
                        )
                        return
                    run_id, cancel_event = run
                    if self._codex_session_busy(session_id):
                        chat.append(
                            role="assistant",
                            text="我正在处理另一边的消息，稍等一下再叫我。",
                            source="group:kairos",
                            sender_id="kairos",
                            sender_name=self._apples_member_name("kairos"),
                        )
                        return
                    hop_hint = ""
                    if hop_count and hop_count > 0:
                        hop_hint = (
                            f"\n[hop {hop_count}] 这是 agent-to-agent 的第 {hop_count} 跳对话，"
                            f"请简洁回复并避免再次主动 @ 其他 agent，让循环自然结束。"
                        )
                    prompt = (
                        "当前时间：" + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M") + "\n"
                        "[消息来源]\n入口: cc_companion_apples_group\ncontact_id: apples\n\n"
                        f"{sender_name} 正在 CcCompanion 的“苹果幼稚园”群聊里 @Kairos。"
                        "请以 Kairos 身份直接回复群聊，不要提到后台路由，也不要触发或代替其他成员。"
                        f"{hop_hint}\n"
                        f"群聊消息：{text}"
                    )
                    sys.path.insert(0, "/root/Windows-Codex-TG")
                    from codex_common import CodexRunner

                    runner = CodexRunner(
                        codex_bin=self.state.codex_bin,
                        sandbox_mode=None,
                        approval_policy=None,
                        dangerous_bypass_level=2,
                        idle_timeout_sec=240,
                    )
                    env_overrides = {
                        "CODEX_HOME": self.state.codex_home,
                        "CODEX_MODEL": self.state.codex_model,
                        "CODEX_REASONING_EFFORT": self.state.codex_reasoning_effort,
                    }

                    def _on_update(live_text: str) -> None:
                        self._set_chat_draft(
                            "apples",
                            live_text,
                            source="group:kairos",
                            session_id=session_id,
                        )

                    thread_id, answer, stderr_text, return_code = runner.run_prompt(
                        prompt=prompt,
                        cwd=cwd,
                        session_id=session_id,
                        env_overrides=env_overrides,
                        cancel_event=cancel_event,
                        on_update=_on_update,
                    )
                    if cancel_event.is_set():
                        chat.append(
                            role="assistant",
                            text="已中断当前生成。",
                            source="group:kairos",
                            sender_id="kairos",
                            sender_name=self._apples_member_name("kairos"),
                        )
                        return
                    if thread_id:
                        self._save_codex_target(thread_id, cwd, "cc-app:apples:kairos")
                    if return_code != 0 and stderr_text:
                        logger.warning("group kairos codex return_code=%s stderr=%s", return_code, stderr_text[-800:])
                    answer = (answer or "").strip() or "Kairos 没有返回可展示内容。"
                    rec = chat.append(
                        role="assistant",
                        text=answer,
                        source="group:kairos",
                        sender_id="kairos",
                        sender_name=self._apples_member_name("kairos"),
                        mentions=sorted(self._detect_apples_mentions(answer)),
                    )
                    routed_after_answer = self._maybe_route_apples_assistant_mention(
                        chat, "assistant", "group:kairos", answer, rec
                    )
                except Exception as e:
                    logger.exception("group kairos codex worker failed")
                    chat.append(
                        role="assistant",
                        text=f"Kairos 接入出错：{e}",
                        source="group:kairos",
                        sender_id="kairos",
                        sender_name=self._apples_member_name("kairos"),
                    )
                finally:
                    self._clear_chat_draft("apples")
                    if run_id:
                        CODEX_RUNS.finish(run_id)
                    if not routed_after_answer and not self._has_pending_group_reply():
                        self._set_typing_for_contact("apples", {"is_typing": False, "since": None})

        threading.Thread(target=_worker, daemon=True).start()

    # apples 群 agent-to-agent loop guard 常量
    APPLES_HOP_LIMIT = 2  # 人发起后, agent A 回 -> agent B 再接一轮就停 (kairos verdict 收紧 3->2)
    APPLES_HOP_LIMIT_DEBUG = 3  # debug-only escape hatch (未启用)
    APPLES_PAIR_RATE_LIMIT_SEC = 60.0  # 同 sender->target 60s 一次
    APPLES_SENDER_GLOBAL_LIMIT = 3  # 单 sender 60s 内最多 dispatch 次数 (任何 target)
    APPLES_ROOM_GLOBAL_LIMIT = 6  # 整个 apples 群 60s 内最多 dispatch 次数 (任何 sender)
    APPLES_GLOBAL_WINDOW_SEC = 60.0

    # 已知 agent sender (人类 sender 全部豁免限速)
    APPLES_AGENT_SENDERS = frozenset({"kairos", "xiaoke"})

    def _apples_is_human_sender(self, sender_id: str) -> bool:
        """Human / unknown sender 全部豁免限速。astra 是人类, 其他非 agent 也走人类逻辑。"""
        sid = (sender_id or "").strip().lower()
        return sid not in self.APPLES_AGENT_SENDERS

    def _apples_dispatch_allowed(self, sender_id: str, target_id: str) -> bool:
        """同一 sender→target 60s 内只允许 1 次 dispatch (super 防 loop)。
        返回 True 表示允许并已记录, False 表示限速 drop.
        人类 sender 豁免, 不记 cache."""
        if not sender_id or not target_id:
            return True
        if self._apples_is_human_sender(sender_id):
            return True
        cache = getattr(type(self), "_apples_dispatch_rate", None)
        if cache is None:
            cache = {}
            type(self)._apples_dispatch_rate = cache
        key = f"{sender_id.lower()}->{target_id.lower()}"
        now_ts = time.time()
        last = cache.get(key, 0.0)
        if now_ts - last < self.APPLES_PAIR_RATE_LIMIT_SEC:
            return False
        cache[key] = now_ts
        # GC 老条目
        for k in list(cache.keys()):
            if now_ts - cache[k] > 600:
                del cache[k]
        return True

    def _apples_sender_global_allowed(self, sender_id: str) -> bool:
        """Check (no record) — per-sender global rate limit.
        人类 sender 豁免. 返回 True 表示允许 (调用方需要 dispatch 成功后调 _apples_record_global)."""
        if self._apples_is_human_sender(sender_id):
            return True
        cache = getattr(type(self), "_apples_sender_global", None)
        if cache is None:
            return True
        now_ts = time.time()
        key = sender_id.lower()
        deque_list = cache.get(key, [])
        # 清掉窗口外的
        deque_list = [t for t in deque_list if now_ts - t < self.APPLES_GLOBAL_WINDOW_SEC]
        cache[key] = deque_list
        return len(deque_list) < self.APPLES_SENDER_GLOBAL_LIMIT

    def _apples_room_global_allowed(self, sender_id: str) -> bool:
        """Check (no record) — per-room global rate limit. 人类 sender 豁免."""
        if self._apples_is_human_sender(sender_id):
            return True
        cache = getattr(type(self), "_apples_room_global", None)
        if cache is None:
            return True
        now_ts = time.time()
        deque_list = list(cache.get("ts", []))
        deque_list = [t for t in deque_list if now_ts - t < self.APPLES_GLOBAL_WINDOW_SEC]
        cache["ts"] = deque_list
        return len(deque_list) < self.APPLES_ROOM_GLOBAL_LIMIT

    def _apples_record_global(self, sender_id: str) -> None:
        """Record a successful dispatch ts for both per-sender + per-room counters.
        人类 sender 不记 (反正豁免)."""
        if self._apples_is_human_sender(sender_id):
            return
        now_ts = time.time()
        sender_cache = getattr(type(self), "_apples_sender_global", None)
        if sender_cache is None:
            sender_cache = {}
            type(self)._apples_sender_global = sender_cache
        key = sender_id.lower()
        lst = sender_cache.get(key, [])
        lst = [t for t in lst if now_ts - t < self.APPLES_GLOBAL_WINDOW_SEC]
        lst.append(now_ts)
        sender_cache[key] = lst

        room_cache = getattr(type(self), "_apples_room_global", None)
        if room_cache is None:
            room_cache = {"ts": []}
            type(self)._apples_room_global = room_cache
        rlst = [t for t in room_cache.get("ts", []) if now_ts - t < self.APPLES_GLOBAL_WINDOW_SEC]
        rlst.append(now_ts)
        room_cache["ts"] = rlst

    def _apples_emit_drop_system_msg(
        self,
        contact_id: str,
        reason: str,
        sender_id: str,
        targets: list[str] | set[str],
        hop_count: int,
        original_text: str = "",
    ) -> None:
        """Drop 不静默 — 写一条 system 消息到 apples chat history 让所有人看到。
        不触发任何 agent。"""
        reason_label = {
            "hop_limit": "hop limit",
            "pair_rate_limit": "per-pair rate limit",
            "sender_rate_limit": "sender rate limit",
            "room_rate_limit": "room rate limit",
        }.get(reason, reason)
        text = f"已停止 agent 接力，等待人类继续。({reason_label})"
        try:
            chat = self._chat_for_contact(contact_id)
            chat.append(
                role="system",
                text=text,
                source="system:apples_dispatch_guard",
                sender_id="system",
                sender_name=self._apples_member_name("system"),
                metadata={
                    "drop_reason": reason,
                    "original_sender_id": sender_id,
                    "original_targets": sorted(list(targets)),
                    "hop_count": hop_count,
                    "original_text_preview": (original_text or "")[:120],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("apples emit drop system msg failed reason=%s err=%s", reason, exc)

    def _dispatch_apples_mentions(
        self,
        rec: dict[str, Any],
        contact_id: str,
        targets: set,
        sender_name: str,
        hop_count: int = 0,
        sender_id: str = "",
    ) -> tuple[list[str], dict[str, str]]:
        """Shared dispatch for apples group @mentions.

        Routes to kairos (codex inject) and/or xiaoke (tmux inject).
        Enforces hop_count<APPLES_HOP_LIMIT + per-pair 60s + per-sender 3/60s
        + per-room 6/60s rate limits to prevent agent-to-agent infinite loops.

        人类 sender (astra / 未知非 agent) 豁免所有限速。

        Drop 不静默 — 命中任何 limit 时写一条 system 消息到 apples chat。

        Returns (routed_member_ids, errors).
        """
        routed: list[str] = []
        errors: dict[str, str] = {}
        if not targets:
            return routed, errors

        sender_id_norm = (sender_id or "").strip().lower()
        text = str(rec.get("text") or "")

        # hop guard — 第 N 跳之后停止再 dispatch (但仍允许用户原始 send → 此函数初始 hop=0)
        if hop_count >= self.APPLES_HOP_LIMIT and not self._apples_is_human_sender(sender_id_norm):
            logger.info(
                "apples dispatch hop_limit hit hop=%s sender=%s targets=%s — skip",
                hop_count, sender_id_norm, sorted(targets),
            )
            self._apples_emit_drop_system_msg(
                contact_id, "hop_limit", sender_id_norm, targets, hop_count, text,
            )
            errors["__all__"] = "hop_limit"
            return routed, errors

        # per-sender global (60s 窗口内最多 3 次, 任何 target)
        if not self._apples_sender_global_allowed(sender_id_norm or "unknown"):
            logger.info(
                "apples dispatch sender_global limit hit sender=%s targets=%s — skip",
                sender_id_norm, sorted(targets),
            )
            self._apples_emit_drop_system_msg(
                contact_id, "sender_rate_limit", sender_id_norm, targets, hop_count, text,
            )
            errors["__all__"] = "sender_rate_limit"
            return routed, errors

        # per-room global (60s 窗口内整个群最多 6 次)
        if not self._apples_room_global_allowed(sender_id_norm or "unknown"):
            logger.info(
                "apples dispatch room_global limit hit sender=%s targets=%s — skip",
                sender_id_norm, sorted(targets),
            )
            self._apples_emit_drop_system_msg(
                contact_id, "room_rate_limit", sender_id_norm, targets, hop_count, text,
            )
            errors["__all__"] = "room_rate_limit"
            return routed, errors

        chat = self._chat_for_contact(contact_id)
        next_hop = hop_count + 1

        if "kairos" in targets and sender_id_norm != "kairos":
            if not self._apples_dispatch_allowed(sender_id_norm or "unknown", "kairos"):
                logger.info("apples dispatch rate-limited sender=%s -> kairos", sender_id_norm)
                errors["kairos"] = "rate_limited (60s per sender→target)"
                self._apples_emit_drop_system_msg(
                    contact_id, "pair_rate_limit", sender_id_norm, ["kairos"], hop_count, text,
                )
            else:
                self._set_typing_for_contact(
                    contact_id, {"is_typing": True, "since": rec["ts"], "member_id": "kairos"}
                )
                self._start_group_kairos_reply(chat, text, sender_name=sender_name, hop_count=next_hop)
                self._apples_record_global(sender_id_norm or "unknown")
                routed.append("kairos")

        if "xiaoke" in targets and sender_id_norm != "xiaoke":
            if not self._apples_dispatch_allowed(sender_id_norm or "unknown", "xiaoke"):
                logger.info("apples dispatch rate-limited sender=%s -> xiaoke", sender_id_norm)
                errors["xiaoke"] = "rate_limited (60s per sender→target)"
                self._apples_emit_drop_system_msg(
                    contact_id, "pair_rate_limit", sender_id_norm, ["xiaoke"], hop_count, text,
                )
            else:
                from datetime import datetime as _dt
                ts_prefix = "[" + _dt.now().strftime("%Y-%m-%d %H:%M:%S") + "]"
                marker = self._group_reply_marker("xiaoke", rec["ts"])
                hop_hint = ""
                if next_hop > 1:
                    hop_hint = (
                        f"[hop {next_hop}] 这是 agent-to-agent 的第 {next_hop} 跳对话，"
                        f"请简洁回复并避免再次主动 @ 其他 agent，让循环自然结束。\n"
                    )
                header = (
                    f"{ts_prefix} [苹果幼稚园群聊][回复 {sender_name} 的群聊消息，"
                    f"不要触发或代替其他 AI。]\n"
                    f"请在本轮回复开头原样输出路由标记 {marker} ，然后直接回复群聊内容。"
                )
                if sender_id_norm and sender_id_norm != "astra":
                    header += "\n不要在回复里 @{0}，避免循环触发。".format(sender_name)
                injected = f"{header}\n{hop_hint}{text}"
                if rec.get("quoted_text"):
                    injected = (
                        f"{header}\n{hop_hint}"
                        f"[引用 \"{rec['quoted_text']}\"]\n{text}"
                    )
                if rec.get("location"):
                    loc = rec["location"]
                    label = loc.get("label", "")
                    loc_str = f"[位置 lat={loc['lat']:.6f} lon={loc['lon']:.6f}{(' ' + label) if label else ''}]"
                    injected = f"{injected}\n{loc_str}"

                target_session = "cctg"
                self._set_typing_for_contact(
                    contact_id, {"is_typing": True, "since": rec["ts"], "member_id": "xiaoke"}
                )
                ok, err = self._inject_to_session(
                    target_session,
                    injected,
                    source=self._source_for_request(contact_id),
                    sender="iphone",
                )
                if ok:
                    source_member = sender_id_norm if sender_id_norm and sender_id_norm != "astra" else None
                    self._remember_group_reply("xiaoke", rec["ts"], source_member=source_member)
                    self._apples_record_global(sender_id_norm or "unknown")
                    routed.append("xiaoke")
                else:
                    errors["xiaoke"] = f"inject to tmux session '{target_session}' failed: {err}"
                    logger.warning(
                        "apples dispatch xiaoke inject failed session=%s sender=%s err=%s",
                        target_session, sender_id_norm, err,
                    )

        return routed, errors

    def _maybe_route_apples_assistant_mention(
        self,
        chat: ChatHistory,
        role: str,
        source: str,
        text: str,
        rec: dict[str, Any],
        hop_count: int = 0,
    ) -> list[str]:
        """Route an assistant's @mention to other apples agents.

        Sender 用 rec['sender_id'] 判断 (不再依赖 source string), 这样 CC 通过
        mcp__companion__reply (source='cc-companion-channel') 发的 assistant 消息
        也能正确触发路由.
        """
        if role != "assistant" or not text:
            return []
        sender_id = str((rec or {}).get("sender_id") or "").strip().lower()
        if not sender_id:
            # 老逻辑兜底 — 没有 sender_id 时从 source 推断
            source_key = str(source or "").strip().lower()
            if source_key.startswith("group:xiaoke") or "xiaoke" in source_key or "ccc-stop-hook" in source_key:
                sender_id = "xiaoke"
            elif source_key.startswith("group:kairos") or "kairos" in source_key:
                sender_id = "kairos"
        # astra 是人类用户, 不该走这条 assistant 路由 (她的消息通过 _handle_apples_chat_send)
        if sender_id == "astra":
            return []

        # 优先用 rec.mentions (client 显式标记或入库时已 detect 好)，否则 text grep
        mentions_raw = (rec or {}).get("mentions") if isinstance(rec, dict) else None
        if isinstance(mentions_raw, list) and mentions_raw:
            targets = {str(m).lower() for m in mentions_raw if str(m).strip()}
        else:
            targets = set(self._detect_apples_mentions(text))
        if sender_id:
            targets.discard(sender_id)
        targets.discard("astra")  # @方小南 不路由
        if not targets:
            return []

        # 老 route_source_member 反向触发保护 (kairos→xiaoke→kairos 单跳 bounce)
        metadata = rec.get("metadata") if isinstance(rec, dict) else None
        if isinstance(metadata, dict):
            route_source_member = str(metadata.get("route_source_member") or "").strip().lower()
            if route_source_member and route_source_member in targets:
                # 上一跳来源刚发的 → 不立即 bounce 回去
                targets.discard(route_source_member)
            if not targets:
                return []

        sender_name = self._apples_member_name(sender_id) if sender_id else "成员"
        routed, _errors = self._dispatch_apples_mentions(
            rec, "apples", targets, sender_name, hop_count=hop_count, sender_id=sender_id,
        )
        return routed

    def _handle_apples_chat_send(self, body: dict[str, Any], contact_id: str):
        text = body.get("text", "").strip()
        quoted_ts = body.get("quoted_ts") or None
        location = body.get("location") or None
        if not text and not location:
            self._send_json(400, {"error": "text or location required"})
            return

        chat = self._chat_for_contact(contact_id)
        # 优先：client UI 显式标记的 member_ids（不受大小写/typo 影响）
        # fallback：text grep（兼容老 client 和纯文本路径）
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        explicit = self._normalize_mentioned_member_ids(meta.get("mentioned_member_ids"))
        if explicit:
            # astra (self) 不应该作为路由目标 — 她是发消息的人
            targets = {mid for mid in explicit if mid != self._apples_self_id()}
        else:
            targets = self._detect_apples_mentions(text)
        rec = chat.append(
            role="user",
            text=text,
            source=self._source_for_request("apples"),
            quoted_ts=quoted_ts,
            location=location,
            sender_id="astra",
            sender_name=self._apples_member_name("astra"),
            mentions=sorted(targets),
        )
        if not targets:
            self._send_json(200, {"ok": True, "contact_id": contact_id, "record": rec, "routed": []})
            return

        typing_target = None
        if "xiaoke" in targets:
            typing_target = "xiaoke"
        elif "kairos" in targets:
            typing_target = "kairos"
        typing_state = {"is_typing": True, "since": rec["ts"]}
        if typing_target:
            typing_state["member_id"] = typing_target
        self._set_typing_for_contact(contact_id, typing_state)

        # astra 直接发的消息 → hop_count 从 0 起算 (走 shared dispatcher)
        routed, errors = self._dispatch_apples_mentions(
            rec,
            contact_id,
            set(targets),
            sender_name=self._apples_member_name("astra"),
            hop_count=0,
            sender_id="astra",
        )

        if errors and not routed:
            self._set_typing_for_contact(contact_id, {"is_typing": False, "since": None})
            self._send_json(502, {"ok": False, "contact_id": contact_id, "record": rec, "errors": errors})
            return

        self._send_json(200, {
            "ok": True,
            "contact_id": contact_id,
            "record": rec,
            "routed": routed,
            "errors": errors,
        })

    def _inject_to_session(
        self,
        session: str,
        text: str,
        source: str = "ios-app",
        sender: str = "iphone",
        *,
        force_direct_tmux: bool = False,
    ):
        """Inject text into target tmux session. Returns (success, error_msg).

        Prefer bus_send.py (Opia internal bus dispatcher routing for multi-agent coord)
        if both the script exists AND /tmp/opia_bus.sock is reachable (dispatcher running).
        Otherwise fall back to direct tmux load-buffer + paste-buffer + send-keys,
        which is what ccc public users get by default — no Opia internal daemon required.
        """
        import os
        import socket
        bus_path = self.state.bus_send_path
        bus_sock = "/tmp/opia_bus.sock"
        bus_ready = False
        if not force_direct_tmux and bus_path and os.path.exists(bus_path) and os.path.exists(bus_sock):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    s.connect(bus_sock)
                bus_ready = True
            except Exception:
                bus_ready = False
        if bus_ready:
            try:
                subprocess.Popen(
                    [
                        "python3",
                        bus_path,
                        "--source", source,
                        "--sender", sender,
                        "--text", text,
                        "--target", session,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True, ""
            except Exception as e:
                logger.warning("bus_send fail, falling back to tmux: %s", e)
        # Fallback: a one-use named buffer prevents concurrent direct injectors
        # from overwriting each other's prompt between load and paste.
        result = _direct_tmux_injection(session, text)
        if not result.success:
            logger.warning("%s (session=%s phase=%s)", result.error, session, result.phase)
        return result

    def _handle_pet_state_get(self):
        """GET /pet/state — 当前 latest 状态."""
        self._send_json(200, {"ok": True, "latest": self.state.pet.latest()})

    def _handle_pet_state_post(self, body: dict[str, Any]):
        """POST /pet/state — chain hook 上报状态. body: {state, reason?, ts?}.
        VALID_STATES: idle/thinking/typing/building/juggling/conducting/error/happy/notification/sweeping/carrying/sleeping."""
        state = str(body.get("state") or "").strip()
        reason = str(body.get("reason") or "")
        ts = body.get("ts")
        if not state:
            self._send_json(400, {"error": "state required"})
            return
        rec = self.state.pet.update(state=state, reason=reason, ts=ts)
        # 推 SSE
        self.state.pet_bus.publish(rec)
        self._send_json(200, {"ok": True, "rec": rec})

    def _handle_pet_bubble_post(self, body: dict[str, Any]):
        """POST /pet/bubble — chain hook 推 speech bubble. body: {text, ts?}.
        text 已截好 (前 30 字 + ...) chain hook 那侧负责截.
        """
        text = str(body.get("text") or "").strip()
        ts = body.get("ts") or ""
        if not text:
            self._send_json(400, {"error": "text required"})
            return
        if not ts:
            from datetime import datetime, timezone, timedelta
            tz = timezone(timedelta(hours=8))
            ts = datetime.now(tz).isoformat(timespec="milliseconds")
        rec = {"text": text, "ts": ts}
        self.state.pet_bubble_bus.publish(rec)
        self._send_json(200, {"ok": True, "rec": rec})

    def _handle_pet_stream(self):
        """GET /pet/stream — SSE 实时推送 pet 状态变化.
        client 接 EventSource (iOS URLSession streaming / Mac Electron native)."""
        import time as _t
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        # 先发当前 latest
        latest = self.state.pet.latest()
        try:
            self.wfile.write(f"data: {json.dumps(latest, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            return
        # 订阅 bus (state + bubble 共用一条 SSE; client 用 event 字段区分)
        q = self.state.pet_bus.subscribe()
        bq = self.state.pet_bubble_bus.subscribe()
        try:
            while True:
                wrote = False
                if q:
                    rec = q.popleft()
                    payload = dict(rec)
                    payload.setdefault("event", "state")
                    try:
                        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        wrote = True
                    except Exception:
                        break
                if bq:
                    brec = bq.popleft()
                    payload = dict(brec)
                    payload["event"] = "bubble"
                    try:
                        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        wrote = True
                    except Exception:
                        break
                if not wrote:
                    # heartbeat keepalive 不让 client 断
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    _t.sleep(1.0)
        finally:
            self.state.pet_bus.unsubscribe(q)
            self.state.pet_bubble_bus.unsubscribe(bq)

    def _handle_chat_stream_chunk(self, body: dict[str, Any]):
        """POST /chat/stream_chunk — cc-companion-channel 转发 reply_chunk/reply_done.
        body: {event: "chunk"|"done", stream_id, contact_id?, text, seq?, ts?}
        chunk.text 是增量片段; done.text 是完整合并稿 (client 自愈用).
        Auth: 走 do_POST 顶层 _require_write_auth.
        """
        event = str(body.get("event") or "").strip()
        if event not in ("chunk", "done"):
            self._send_json(400, {"error": "event must be chunk or done"})
            return
        stream_id = str(body.get("stream_id") or "").strip()
        if not stream_id:
            self._send_json(400, {"error": "stream_id required"})
            return
        text = body.get("text")
        if not isinstance(text, str):
            self._send_json(400, {"error": "text must be a string"})
            return
        ts = str(body.get("ts") or "")
        if not ts:
            tz = timezone(timedelta(hours=8))
            ts = datetime.now(tz).isoformat(timespec="milliseconds")
        rec: dict[str, Any] = {
            "event": event,
            "stream_id": stream_id,
            "contact_id": self._clean_contact_id(body.get("contact_id")),
            "text": text,
            "ts": ts,
        }
        seq = body.get("seq")
        if isinstance(seq, int):
            rec["seq"] = seq
        if event == "done":
            rec["persisted"] = bool(body.get("persisted", True))
        self.state.chat_stream_bus.publish(rec)
        self._send_json(200, {"ok": True})

    def _handle_chat_stream(self):
        """GET /chat/stream?contact_id=xiaoke — SSE 实时推流式回复 chunk.
        Auth: 走 do_GET 顶层 _require_auth (同 /chat/history).
        client 断线靠现有 2s history polling 兜底, 不丢最终稿.
        """
        import time as _t
        contact_filter = self._contact_id_from_query()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(f"data: {json.dumps({'event': 'connected', 'contact_id': contact_filter}, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            return
        q = self.state.chat_stream_bus.subscribe()
        try:
            while True:
                wrote = False
                if q:
                    rec = q.popleft()
                    if rec.get("contact_id") != contact_filter:
                        continue
                    try:
                        self.wfile.write(f"data: {json.dumps(rec, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        wrote = True
                    except Exception:
                        break
                if not wrote:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    _t.sleep(1.0)
        finally:
            self.state.chat_stream_bus.unsubscribe(q)

    def _handle_pet_activity_post(self, body: dict[str, Any]):
        """POST /pet/activity — chain hook 推 streaming terminal display 行.
        body: {event_type, tool_name, summary, ts?}
        event_type: pre_tool / post_tool / stop / user_prompt
        """
        event_type = str(body.get("event_type") or "").strip() or "pre_tool"
        tool_name = str(body.get("tool_name") or "").strip()
        summary = str(body.get("summary") or "").strip()
        friendly_label = str(body.get("friendly_label") or "").strip()
        ts = body.get("ts")
        if not ts:
            from datetime import datetime, timezone, timedelta
            tz = timezone(timedelta(hours=8))
            ts = datetime.now(tz).isoformat(timespec="milliseconds")
        rec = {
            "event_type": event_type,
            "tool_name": tool_name,
            "summary": summary,
            "friendly_label": friendly_label,
            "ts": ts,
        }
        self.state.pet_activity_bus.publish(rec)
        self._send_json(200, {"ok": True, "rec": rec})

    def _handle_pet_activity_stream(self):
        """GET /pet/activity_stream — SSE 推 chain 实时活动 (terminal display)."""
        import time as _t
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = self.state.pet_activity_bus.subscribe()
        try:
            while True:
                wrote = False
                if q:
                    rec = q.popleft()
                    try:
                        self.wfile.write(f"data: {json.dumps(rec, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        wrote = True
                    except Exception:
                        break
                if not wrote:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    _t.sleep(1.0)
        finally:
            self.state.pet_activity_bus.unsubscribe(q)

    def _handle_pet_animations(self):
        """GET /pet/animations — 列出本地 svg 资产路径 (供 client 拉取或直接 file:// load)."""
        from pathlib import Path as _P
        svg_dir = _P("/path/to/CcCompanion/handy-clawd-assets/svg")
        if not svg_dir.exists():
            self._send_json(404, {"error": "svg dir missing", "expected": str(svg_dir)})
            return
        files = sorted([p.name for p in svg_dir.glob("*.svg")])
        self._send_json(200, {"ok": True, "count": len(files), "svg_dir": str(svg_dir), "files": files})

    def _handle_chat_regenerate(self, body: dict[str, Any]):
        """2026-05-08 用户 push 重新发言. iOS 长按 assistant msg 选 regenerate.
        flow:
        1 mark old assistant msg hidden_in_ui (UI 不展示但 jsonl 留备查)
        2 中断 chain (tmux Escape x 3 复用 chain_abort 逻辑)
        3 user_text 包 [regenerate] 标记调 bus_send 注入主 session
        4 chain 跑出新回复 走现有 stop hook 写 chat_history
        body: {"replace_msg_id": "ts", "user_text": "...", "client_msg_id": "uuid for dedupe"}
        """
        replace_msg_id = str(body.get("replace_msg_id") or "").strip()
        extra_replace_ids = [str(x).strip() for x in (body.get("extra_replace_ids") or []) if x]
        user_text = str(body.get("user_text") or "").strip()
        client_msg_id = body.get("client_msg_id")
        if not replace_msg_id or not user_text:
            self._send_json(400, {"error": "replace_msg_id and user_text required"})
            return

        # dedupe 5s 窗口防快速点击
        cache = getattr(type(self), "_regen_dedupe_cache", None)
        if cache is None:
            cache = {}
            type(self)._regen_dedupe_cache = cache
        now_ts = time.time()
        cache_key = f"cmid:{client_msg_id}" if client_msg_id else f"replace:{replace_msg_id}"
        last_ts = cache.get(cache_key, 0)
        if now_ts - last_ts < 5.0:
            self._send_json(429, {"ok": False, "error": "duplicate within 5s window", "deduped": True})
            return
        cache[cache_key] = now_ts
        for k in list(cache.keys()):
            if now_ts - cache[k] > 60:
                del cache[k]

        # mark old assistant msg hidden (first/primary)
        marked = self.state.chat.mark_regenerated(old_ts=replace_msg_id)
        logger.info("chat/regenerate marked=%s replace_msg_id=%s", marked, replace_msg_id)
        # mark extra turn bubbles hidden
        extra_marked = 0
        for eid in extra_replace_ids:
            if self.state.chat.mark_regenerated(old_ts=eid):
                extra_marked += 1
        if extra_replace_ids:
            logger.info("chat/regenerate extra_marked=%d ids=%s", extra_marked, extra_replace_ids)

        # 中断 chain (tmux Escape x 3 复用 chain_abort 逻辑)
        _regen_session = self.state.active_session or self.state.default_session
        try:
            import subprocess
            import time as _t
            for i in range(3):
                subprocess.run(
                    ["tmux", "send-keys", "-t", _regen_session, "Escape"],
                    capture_output=True, text=True, timeout=5,
                )
                if i < 2:
                    _t.sleep(0.2)
            logger.info("chat/regenerate sent 3x Escape to %s tmux", _regen_session)
        except Exception as e:
            logger.warning("chat/regenerate tmux abort fail: %s", e)

        # 给一点时间让 chain 真停 然后注入新 user_text
        try:
            import time as _t2
            _t2.sleep(0.5)
        except Exception:
            pass

        # 包 ts_prefix + [regenerate] 标记 chain 看到知道这是重生成请求
        from datetime import datetime as _dt
        ts_prefix = "[" + _dt.now().strftime("%Y-%m-%d %H:%M:%S") + "]"
        tts_hint = ""
        if self.state.settings.get("tts_enabled"):
            tts_hint = "[语音模式 这一条带标点回复]\n"
        injected = f"{ts_prefix} {tts_hint}[regenerate 用户对上一条回复不满意 重新生成] {user_text}"

        # set typing
        self.state.typing_state = {"is_typing": True, "since": _dt.now().isoformat(timespec="milliseconds")}

        # 注入 regenerate 文本到 active session — 走 _inject_to_session helper
        # ccc 公开用户没 ~/scripts/bus_send.py 时 fallback 直接 tmux 注入
        target_session = (self.state.active_session or self.state.default_session).strip()
        ok, err = self._inject_to_session(target_session, injected, source=self._source_for_request(), sender="iphone")
        if not ok:
            self._send_json(502, {
                "ok": False,
                "error": f"inject regenerate to '{target_session}' failed: {err}",
                "marked_hidden": marked,
                "replace_msg_id": replace_msg_id,
                "extra_marked": extra_marked,
            })
            return

        self._send_json(200, {
            "ok": True,
            "marked_hidden": marked,
            "replace_msg_id": replace_msg_id,
            "extra_marked": extra_marked,
            "interrupted": True,
        })

    def _handle_chat_rollback(self, body: dict[str, Any]):
        """2026-07-08 app 长按消息「回滚到这里/重roll」。

        forge 式回滚（v2，原生双 Esc Rewind 不认 channel 注入消息已弃用）：
        截断 live 会话 jsonl 到目标用户消息之前 → 重启 claude-tg → tmux
        重注入她的原话成 typed prompt，真正从模型上下文里撤掉之后的回复
        （对比 /chat/regenerate 只是追加一条 [regenerate] 请求）。

        链路几十秒，HTTP 立即返回 200（async=true），后台线程跑完整流程；
        失败往 chat_history append 一条 ⚠️ 消息告知原因。

        body: {
          "target_msg_id": "<chat_history ts，长按的那条消息>",
          "role": "user" | "assistant",   # 长按的是谁的消息
          "text": "...",                   # 兜底匹配用
          "client_msg_id": "uuid",         # 防重复点击
          "contact_id": "xiaoke",
        }
        语义：长按用户消息 = 回滚到该消息重发；长按 assistant 消息 = 定位它
        前面最近一条用户消息原话重跑。
        细节与副作用上锁都在 rollback_driver 模块里（可单测）。
        """
        contact_id = self._contact_id_from_body(body)
        if contact_id != "xiaoke":
            self._send_json(501, {"ok": False, "reason": f"rollback 只支持小克主窗口（got {contact_id}）"})
            return
        target_msg_id = str(body.get("target_msg_id") or "").strip()
        role = str(body.get("role") or "").strip() or "user"
        text_hint = str(body.get("text") or "").strip()
        client_msg_id = body.get("client_msg_id")
        if not target_msg_id and not text_hint:
            self._send_json(400, {"ok": False, "reason": "target_msg_id or text required"})
            return

        # dedupe 10s 窗口（回滚链路本身要跑好几秒）
        cache = getattr(type(self), "_rollback_dedupe_cache", None)
        if cache is None:
            cache = {}
            type(self)._rollback_dedupe_cache = cache
        now_ts = time.time()
        cache_key = f"cmid:{client_msg_id}" if client_msg_id else f"target:{target_msg_id or text_hint[:64]}"
        if now_ts - cache.get(cache_key, 0) < 10.0:
            self._send_json(429, {"ok": False, "reason": "刚触发过一次回滚，别连点", "deduped": True})
            return
        cache[cache_key] = now_ts
        for k in list(cache.keys()):
            if now_ts - cache[k] > 120:
                del cache[k]

        # 定位目标用户消息（长按 assistant → 往前找最近的 user 条）
        chat = self._chat_for_contact(contact_id)
        records = chat.read_since(since_ts=None, limit=10000, include_hidden=True)
        target_idx = -1
        if target_msg_id:
            for i, rec in enumerate(records):
                if rec.get("ts") == target_msg_id:
                    target_idx = i
                    break
        if target_idx < 0 and text_hint:
            for i, rec in enumerate(records):
                if rec.get("role") == role and (rec.get("text") or "").strip() == text_hint:
                    target_idx = i  # 取最后一个同文本命中
        if target_idx < 0:
            self._send_json(404, {"ok": False, "reason": "在聊天记录里找不到这条消息"})
            return
        user_rec: dict[str, Any] | None = None
        if records[target_idx].get("role") == "user":
            user_rec = records[target_idx]
        else:
            for rec in reversed(records[:target_idx]):
                if rec.get("role") == "user":
                    user_rec = rec
                    break
        if not user_rec or not (user_rec.get("text") or "").strip():
            self._send_json(404, {"ok": False, "reason": "这条回复前面找不到对应的用户消息"})
            return
        user_ts = str(user_rec.get("ts") or "")
        user_text = str(user_rec.get("text") or "")

        # ── 同步预检（定位 jsonl / 截断点校验干跑 / 副作用上锁 / busy 检测）──
        # 全部通过才派后台线程；这里失败不动任何东西，直接把原因回给 app。
        # 注意：active_session 是 deprecated chain 概念（可能残留 claude 会话
        # UUID，2026-07-08 首验就栽在这），tmux 会话名以 default_session 为准。
        target_session = (self.state.default_session or rollback_driver.PRODUCTION_TMUX_SESSION).strip()
        try:
            plan = rollback_driver.prepare_rollback(
                tmux_session=target_session,
                user_record_ts=user_ts,
                raw_text=user_text,
            )
        except rollback_driver.RollbackRefused as e:
            logger.info("chat/rollback refused code=%s reason=%s", e.code, e.reason)
            self._send_json(409, {"ok": False, "code": e.code, "reason": e.reason})
            return
        except rollback_driver.RollbackError as e:
            logger.warning("chat/rollback failed code=%s reason=%s", e.code, e.reason)
            self._send_json(502, {"ok": False, "code": e.code, "reason": e.reason})
            return
        except Exception as e:
            logger.exception("chat/rollback unexpected error")
            self._send_json(502, {"ok": False, "reason": f"回滚驱动异常: {e}"})
            return

        # 单飞锁必须覆盖整个异步流程（截断→重启→注入），不是只锁 handler。
        if not rollback_driver.DRIVER_LOCK.acquire(blocking=False):
            self._send_json(409, {"ok": False, "code": "in_progress", "reason": "已经有一个回滚正在执行，等它跑完。"})
            return

        set_typing = self._set_typing_for_contact  # 只碰 self.state，线程安全使用
        from datetime import datetime as _dt

        def _after_truncate(info: dict[str, Any]) -> None:
            # 截断成功 = 目标之后的回复已从模型上下文撤掉，UI 侧标记隐藏
            # （复用 regenerate 的 hidden_in_ui 机制）。
            hidden = 0
            for rec in records:
                if (
                    rec.get("role") == "assistant"
                    and str(rec.get("ts") or "") > user_ts
                    and not rec.get("hidden_in_ui")
                ):
                    if chat.mark_regenerated(old_ts=str(rec.get("ts"))):
                        hidden += 1
            info["hidden_assistant"] = hidden
            logger.info("chat/rollback truncated new_sid=%s hidden=%d", info.get("new_sid"), hidden)

        def _rollback_worker() -> None:
            try:
                info = rollback_driver.execute_rollback(plan, after_truncate=_after_truncate)
                set_typing(contact_id, {"is_typing": True, "since": _dt.now().isoformat(timespec="milliseconds")})
                logger.info(
                    "chat/rollback ok user_ts=%s new_sid=%s hidden=%s session=%s",
                    user_ts, info.get("new_sid"), info.get("hidden_assistant"), target_session,
                )
            except (rollback_driver.RollbackRefused, rollback_driver.RollbackError) as e:
                logger.warning("chat/rollback async failed code=%s reason=%s", e.code, e.reason)
                set_typing(contact_id, {"is_typing": False, "since": None})
                try:
                    chat.append(role="assistant", text=f"⚠️ 重roll失败：{e.reason}", source="rollback-system")
                except Exception:
                    logger.exception("chat/rollback failed to append failure notice")
            except Exception as e:
                logger.exception("chat/rollback async unexpected error")
                set_typing(contact_id, {"is_typing": False, "since": None})
                try:
                    chat.append(role="assistant", text=f"⚠️ 重roll失败：{e}", source="rollback-system")
                except Exception:
                    logger.exception("chat/rollback failed to append failure notice")
            finally:
                rollback_driver.DRIVER_LOCK.release()

        threading.Thread(target=_rollback_worker, name="chat-rollback", daemon=True).start()
        set_typing(contact_id, {"is_typing": True, "since": _dt.now().isoformat(timespec="milliseconds")})
        logger.info("chat/rollback started (async) user_ts=%s session=%s", user_ts, target_session)
        self._send_json(200, {
            "ok": True,
            "async": True,
            "message": "回滚已启动，小克重启中（约1分钟）",
            "rolled_back_to": user_ts,
        })

    def _handle_chat_append(self, body: dict[str, Any]):
        """bus_stop_hook 抓到回复后调 → 写 assistant 条 + push spoke 状态
        也支持从 mac mini 这边发图/文件 给 iPhone:
          attachment_path (本地文件 server 复制进 attachments/) 或
          attachment_url (server 已存的 /attachments/<file>)
        """
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        contact_id = self._contact_id_from_body(body)
        text = body.get("text", "").strip()
        role = body.get("role", "assistant")
        source = body.get("source", self._source_for_request())
        if not isinstance(source, str) or not source:
            source = self._source_for_request()
        if role == "assistant":
            has_group_marker = "[[CCC_GROUP_REPLY:apples:" in text
            if has_group_marker:
                routed_contact_id, routed_member_id, route_source_member, cleaned_text = self._consume_group_reply_route(text)
                if routed_contact_id:
                    contact_id = routed_contact_id
                    text = cleaned_text
                    source = f"group:{routed_member_id or 'xiaoke'}"
                    route_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
                    if route_source_member:
                        route_metadata = {**route_metadata, "route_source_member": route_source_member}
                        body["metadata"] = route_metadata
        chat = self._chat_for_contact(contact_id)
        if role == "task":
            if not text:
                self._send_json(400, {"error": "text required"})
                return
            rec = self.state.task_buffer.append(text=text, source=body.get("source", "system"))
            chat.append(role="task", text=text, source=body.get("source", "system"))
            self._send_json(200, {"ok": True, "record": rec})
            return

        # attachment 处理
        attachment_url = body.get("attachment_url") or None
        attachment_type = body.get("attachment_type") or None
        attachment_filename = body.get("attachment_filename") or None
        local_path = body.get("attachment_path") or None
        if local_path:
            import uuid as _uuid, shutil
            src = Path(local_path).expanduser()
            if not src.exists() or not src.is_file():
                self._send_json(400, {"error": f"attachment_path not found: {src}"})
                return
            ext = src.suffix.lower()
            stored_name = f"{_uuid.uuid4().hex}{ext}"
            target = self.state.attachments_dir / stored_name
            shutil.copy2(src, target)
            attachment_url = f"/attachments/{stored_name}"
            if not attachment_type:
                image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
                attachment_type = "image" if ext in image_exts else "file"
            if not attachment_filename:
                attachment_filename = src.name

        thinking = body.get("thinking") or ""
        if isinstance(thinking, str) and len(thinking) > 5000:
            thinking = thinking[:5000]
        tools = body.get("tools") or ""
        if isinstance(tools, str) and len(tools) > 5000:
            tools = tools[:5000]
        sender_id = None
        sender_name = None
        mentions = None
        if contact_id == "apples":
            member_id = self._apples_source_member(source)
            if role == "user":
                member_id = "astra"
            elif not member_id and role == "assistant":
                # CC append → apples group 默认是螃蟹版小克，不是泛 ai
                member_id = "xiaoke"
            if member_id:
                sender_id = member_id
                sender_name = self._apples_member_name(member_id)
            # mention 解析：优先用 metadata.mentioned_member_ids（client 显式标记），
            # 否则 fallback 到 text grep（兼容老 client / 纯文本场景）。
            meta_in = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            explicit_mentions = self._normalize_mentioned_member_ids(meta_in.get("mentioned_member_ids"))
            if explicit_mentions:
                # 排除 self（发件人自己不应被标 mention）
                mentions = [m for m in explicit_mentions if m != sender_id]
            else:
                mentions = sorted(self._detect_apples_mentions(text)) if text else None
        if role == "assistant" and contact_id == "xiaoke":
            # Claim completion before history/TTS work.  This shares XiaoKe's
            # Stop lock, so completion-vs-Ctrl-C has one deterministic winner:
            # once a final assistant callback begins, a racing Stop is a
            # benign no-op and can never press Ctrl-C at Claude's idle prompt.
            metadata_in = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            hook_turn_token = str(
                body.get("turn_token") or metadata_in.get("xiaoke_turn_token") or ""
            ).strip()
            hook_session = str(body.get("session_id") or metadata_in.get("xiaoke_session_id") or "").strip()
            self._complete_xiaoke_turn_if_match(hook_turn_token, hook_session)
            if hook_session and not text and not attachment_url and not thinking and not tools:
                # Signal-only end-of-turn beacon from the Stop hook: nothing
                # to append, the identity was already consumed above.
                self._send_json(200, {"ok": True, "signal_only": True})
                return

        if not text and not attachment_url and not thinking and not tools:
            self._send_json(400, {"error": "text or attachment required"})
            return

        if not text and not attachment_url and (thinking or tools):
            ok = chat.merge_thinking_to_last_assistant(thinking, tools)
            if ok:
                self._send_json(200, {"ok": True, "merged": True})
            else:
                self._send_json(200, {"ok": True, "merged": False, "reason": "no assistant record"})
            return

        # 通用 chat/append dedupe (非 move) 防 ios_reply 等客户端 retry 重复入库
        # 2026-05-07 修 用户 catch "为什么发两遍". role=move 走下面坐标幂等不动.
        # 5-7 升级 5s→60s + cmid fallback 加 attachment + 命中返回原 rec (枢 review 推荐)
        _req_t0 = time.time()
        client_msg_id = body.get("client_msg_id") or None
        dedupe_cache_key = None
        if role != "move":
            cache = getattr(type(self), "_chat_append_dedupe_cache", None)
            if cache is None:
                cache = {}
                type(self)._chat_append_dedupe_cache = cache
            now_ts = time.time()
            if client_msg_id:
                cache_key = f"cmid:{client_msg_id}"
            else:
                cache_key = f"{role}|{text[:200]}|{body.get('source', '')}|{attachment_url or ''}|{attachment_filename or ''}"
            entry = cache.get(cache_key)
            last_ts = entry[0] if isinstance(entry, tuple) else (entry or 0)
            if now_ts - last_ts < 60.0:
                cached_rec = entry[1] if isinstance(entry, tuple) else None
                _ms = int((time.time() - _req_t0) * 1000)
                print(f"chat_append_ms={_ms} dedupe_hit=1 role={role}", file=sys.stderr, flush=True)
                self._send_json(200, {"ok": True, "duplicate": True, "deduped": True, "record": cached_rec})
                return
            # 占位 真 rec 入库后回填
            cache[cache_key] = (now_ts, None)
            dedupe_cache_key = cache_key
            for k in list(cache.keys()):
                v = cache[k]
                v_ts = v[0] if isinstance(v, tuple) else v
                if now_ts - v_ts > 120:
                    del cache[k]

        if role == "move":
            # 层 1: client_msg_id 缓存
            if client_msg_id:
                cached = self.state.gomoku_msg_cache.get(client_msg_id)
                if cached is not None:
                    self._send_json(200, {"ok": True, "duplicate": True, "record": cached})
                    return
            # 层 2: 坐标幂等 — 检查当前局面该格是否已有子
            text_parts = text.split()
            if len(text_parts) >= 2 and text_parts[0] in ("black", "white"):
                coord_parts = text_parts[1].split(",")
                if len(coord_parts) == 2:
                    try:
                        move_r, move_c = int(coord_parts[0]), int(coord_parts[1])
                        state_snap = self._compute_gomoku_state()
                        dup = next(
                            (m for m in state_snap["moves"]
                             if m["row"] == move_r and m["col"] == move_c),
                            None,
                        )
                        if dup is not None:
                            existing_text = f"{dup['color']} {dup['row']},{dup['col']}"
                            existing_rec = {"ts": dup["ts"], "role": "move", "text": existing_text}
                            logger.info("gomoku dedup coord %d,%d", move_r, move_c)
                            self._send_json(200, {"ok": True, "duplicate": True, "record": existing_rec})
                            return
                    except Exception:
                        pass

        metadata = body.get("metadata") or None
        if metadata and not isinstance(metadata, dict):
            metadata = None

        rec = chat.append(
            role=role,
            text=text,
            source=source,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
            attachment_filename=attachment_filename,
            metadata=metadata,
            thinking=thinking,
            tools=tools,
            sender_id=sender_id,
            sender_name=sender_name,
            mentions=mentions,
        )

        # move 成功 append 后缓存 client_msg_id (LRU 100)
        if role == "move" and client_msg_id:
            cache = self.state.gomoku_msg_cache
            cache[client_msg_id] = rec
            while len(cache) > 100:
                cache.popitem(last=False)

        # role=move (五子棋落子): notify chain 让 Cc 自动收到对方 (black 用户) 落子 → 决策回手
        # 只 trigger 当 text 以 "black" 开头 (white 是我自己 chain 落 不 notify)
        if role == "move" and text.startswith("black"):
            self._notify_chain_todo(f"[用户 落子: {text}]")

        # assistant text reply 后台异步生成 TTS mp3 — 不阻塞 hook (仅 settings.tts_enabled)
        if role == "assistant" and text and not attachment_url and self.state.settings.get("tts_enabled"):
            ts = rec["ts"]
            chat_for_tts = chat
            attachments_dir = self.state.attachments_dir
            def _tts_async():
                logger.info("tts multi thread start ts=%s len=%d", ts, len(text))
                try:
                    res = TTS.generate_multi(text, attachments_dir)
                except Exception as e:
                    logger.exception("tts multi gen fail")
                    return
                update_kwargs = {}
                for lang in ("zh", "en", "ja"):
                    item = res.get(lang)
                    if item:
                        fname, _ = item
                        update_kwargs[f"audio_{lang}"] = f"/attachments/{fname}"
                if not update_kwargs:
                    logger.warning("tts multi gen returned no audio")
                    return
                ok = chat_for_tts.update_audio(ts=ts, **update_kwargs)
                logger.info("tts multi attach %s langs=%s", "ok" if ok else "FAIL", ",".join(sorted(update_kwargs)))
            threading.Thread(target=_tts_async, daemon=True).start()
        # 我刚 reply 完 — typing = false
        if role == "assistant":
            routed: list[str] = []
            if contact_id == "apples":
                # hop_count: client/上游可在 body 顶层 或 metadata 里塞, 默认 0
                try:
                    raw_hop = body.get("hop_count")
                    if raw_hop is None and isinstance(body.get("metadata"), dict):
                        raw_hop = body["metadata"].get("hop_count")
                    hop_count = int(raw_hop or 0)
                except Exception:
                    hop_count = 0
                routed = self._maybe_route_apples_assistant_mention(
                    chat, role, source, text, rec, hop_count=hop_count,
                )
            if not routed and not self._has_pending_group_reply() and contact_id != "xiaoke":
                self._set_typing_for_contact(contact_id, {"is_typing": False, "since": None})

        # 5-7 dedupe cache 回填真 rec
        if dedupe_cache_key is not None:
            cache = getattr(type(self), "_chat_append_dedupe_cache", {})
            entry = cache.get(dedupe_cache_key)
            if isinstance(entry, tuple):
                cache[dedupe_cache_key] = (entry[0], rec)

        # 5-7 主修 (枢 review): Live Activity push 跟 standard notification 都搬到异步
        # 防 ACK 5-16s 阻塞 ios_reply 客户端 5s timeout
        # 这之前所有事必须做完 否则 ACK 后再读会拿不到 rec/text 之类局部
        active_tokens_snapshot = self.state.tokens.all_active() if role == "assistant" else []
        snap_tasks = self.state.tasks.snapshot() if active_tokens_snapshot else None
        push_text_snap = text  # 闭包捕获

        def _async_side_effects():
            try:
                if active_tokens_snapshot and self.state.apns_enabled:
                    cs: dict[str, Any] = {
                        "status": "spoke",
                        "lastMessagePreview": push_text_snap[:200],
                        "sourceChannel": "iPhone",
                        "unreadCount": 0,
                    }
                    active_task = (snap_tasks or {}).get("active")
                    if active_task:
                        total = max(int(active_task["total"]), 1)
                        current = int(active_task["current"])
                        cs["taskTitle"] = active_task["title"]
                        cs["taskCurrent"] = current
                        cs["taskTotal"] = total
                        cs["taskProgress"] = current / total
                        if active_task.get("step"):
                            cs["taskStep"] = str(active_task["step"])[:80]
                    push_kwargs: dict[str, Any] = {"event": "update", "content_state": cs}
                    if role == "assistant" and push_text_snap:
                        push_kwargs["alert_title"] = "Cc"
                        push_kwargs["alert_body"] = push_text_snap[:120]
                    apns_t0 = time.time()
                    for tok in active_tokens_snapshot:
                        try:
                            self.state.client.push_live_activity(
                                push_token=tok.token,
                                **push_kwargs,
                            )
                        except Exception as e:
                            logger.warning("push spoke fail: %s", e)
                    apns_ms = int((time.time() - apns_t0) * 1000)
                    print(f"apns_live_ms={apns_ms} tokens={len(active_tokens_snapshot)}", file=sys.stderr, flush=True)
                # standard remote notification banner (非灵动岛) — 跳过 [op] 前缀和非 assistant
                if role == "assistant" and push_text_snap and not push_text_snap.startswith("[op]"):
                    notif_t0 = time.time()
                    self._send_chat_notification("Cc", push_text_snap[:80])
                    notif_ms = int((time.time() - notif_t0) * 1000)
                    print(f"notification_ms={notif_ms}", file=sys.stderr, flush=True)
            except Exception as e:
                logger.exception("async side effects error: %s", e)

        # 立刻 ACK
        _ack_ms = int((time.time() - _req_t0) * 1000)
        print(f"chat_append_ms={_ack_ms} dedupe_hit=0 role={role}", file=sys.stderr, flush=True)
        self._send_json(200, {"ok": True, "record": rec})

        # ACK 之后再起异步线程做 APNs / notification 不影响 client 5s timeout
        threading.Thread(target=_async_side_effects, daemon=True).start()

    def _handle_chat_upload(self):
        """raw POST + query string (header 不支持非 ASCII char 中文 caption 会丢字)
        ?filename=foo.jpg&role=user&text=caption&quoted_ts=...
        body: raw bytes (image / file)

        老 client 兼容 — 也读 X-Filename / X-Text header
        """
        import uuid as _uuid
        from urllib.parse import urlparse, parse_qs, unquote

        qs = parse_qs(urlparse(self.path).query)
        contact_id = self._clean_contact_id(qs.get("contact_id", qs.get("contactId", ["xiaoke"]))[0])
        filename = (qs.get("filename", [None])[0]
                    or self.headers.get("X-Filename")
                    or "upload.bin")
        role = (qs.get("role", [None])[0]
                or self.headers.get("X-Role")
                or "user")
        text = (qs.get("text", [None])[0]
                or self.headers.get("X-Text")
                or "")
        quoted_ts = (qs.get("quoted_ts", [None])[0]
                     or self.headers.get("X-Quoted-Ts")
                     or None)
        location = None
        lat = qs.get("lat", [None])[0]
        lon = qs.get("lon", [None])[0]
        if lat is not None and lon is not None:
            location = {"lat": lat, "lon": lon}
            accuracy = qs.get("accuracy", [None])[0]
            label = qs.get("label", [None])[0]
            if accuracy is not None:
                location["accuracy"] = accuracy
            if label:
                location["label"] = label

        # url decode for non-ascii filename / text (parse_qs 已经 decode 但 header 没)
        try:
            if filename:
                filename = unquote(filename)
        except Exception:
            pass

        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0 or length > 50 * 1024 * 1024:  # 50MB cap
            self._send_json(400, {"error": "invalid content-length (max 50MB)"})
            return

        # 推断 type
        ext = Path(filename).suffix.lower()
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
        atype = "image" if ext in image_exts else "file"

        # uuid 命名 + 保留 extension
        stored_name = f"{_uuid.uuid4().hex}{ext}"
        stored_path = self.state.attachments_dir / stored_name

        try:
            with stored_path.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:
            logger.exception("upload write fail")
            self._send_json(500, {"error": f"write fail: {e}"})
            return

        attachment_url = f"/attachments/{stored_name}"

        chat = self._chat_for_contact(contact_id)
        rec = chat.append(
            role=role,
            text=text,
            source=self._source_for_request() if contact_id == "xiaoke" else self._source_for_request(contact_id),
            quoted_ts=quoted_ts,
            attachment_url=attachment_url,
            attachment_type=atype,
            attachment_filename=filename,
            location=location,
        )

        # 如果是 user 上传 也往主 session 注入一条 hint 让 chain 感知有附件
        if role == "user" and contact_id == "xiaoke":
            hint = f"[用户发了{'图片' if atype == 'image' else '文件'}: {filename}]"
            if rec.get("location"):
                loc = rec["location"]
                label = loc.get("label", "")
                hint += f" [位置 lat={loc['lat']:.6f} lon={loc['lon']:.6f}{(' ' + label) if label else ''}]"
            if text:
                hint = hint + " " + text
            if rec.get("quoted_text"):
                hint = f"[引用 \"{rec['quoted_text']}\"]\n" + hint
            # 给主 session 一条 hint 让 chain 读 file (server 本地可读 stored_path)
            hint += f"\n本地路径: {stored_path}"
            # 优先走 channel transport (跟 _handle_chat_send 一致) — 否则附件 hint
            # 只进 tmux, channel 模式下 chain 收不到图片路径 (测试图片 bug).
            if self._channel_transport_enabled_for(contact_id):
                attach_meta = {
                    "transport": "channel",
                    "user_record_ts": rec.get("ts"),
                    "attachment_type": atype,
                    "attachment_filename": filename,
                    "attachment_url": attachment_url,
                    "image_path" if atype == "image" else "attachment_path": str(stored_path),
                }
                message_id = self._channel_message_id({}, contact_id, hint, quoted_ts)
                ok, err, _channel_response = self._send_to_channel_transport(
                    message_id=message_id,
                    contact_id=contact_id,
                    text=hint,
                    quoted_ts=quoted_ts,
                    user_record={**rec, "metadata": attach_meta},
                )
                if ok:
                    self._send_json(200, {
                        "ok": True,
                        "contact_id": contact_id,
                        "record": rec,
                        "transport": "channel",
                        "message_id": message_id,
                    })
                    return
                logger.warning(
                    "channel transport attachment failed contact_id=%s message_id=%s error=%s",
                    contact_id, message_id, err,
                )
                if not self.state.channel_transport_fallback_to_tmux:
                    self._send_json(502, {
                        "ok": False,
                        "error": f"channel transport attachment failed: {err}",
                        "record": rec,
                    })
                    return
                # fallback 继续走 tmux 注入
            target_session = (self.state.active_session or self.state.default_session).strip()
            ok, err = self._inject_to_session(target_session, hint, source=self._source_for_request(), sender="iphone")
            if not ok:
                # 附件已存盘 + 历史已 append 但 chain 注入失败 — 502 surface
                self._send_json(502, {
                    "ok": False,
                    "error": f"inject attachment hint to '{target_session}' failed: {err}",
                    "record": rec,
                })
                return

        if role == "user" and contact_id == "kairos":
            task_text = text.strip() or f"[用户发了{'图片' if atype == 'image' else '文件'}: {filename}]"
            image_paths = [str(stored_path)] if atype == "image" else []
            self._clear_chat_draft(contact_id)
            self._enqueue_kairos_task({
                "contact_id": contact_id,
                "text": task_text,
                "quoted_ts": quoted_ts,
                "user_ts": rec["ts"],
                "queued_at": rec["ts"],
                "image_paths": image_paths,
            })

        self._send_json(200, {"ok": True, "contact_id": contact_id, "record": rec})

    def _handle_ai_chat_upload(self):
        """Reject before storing bytes until a safe relay attachment bridge exists."""
        self.close_connection = True
        self._send_json(415, {
            "ok": False,
            "unsupported": True,
            "error": "夏以昼的隔离会话目前只支持文字；旧附件历史仍可查看",
        })

    def _run_stackchan_voice_helper(self, args: list[str], *, timeout: int) -> tuple[bool, dict[str, Any]]:
        helper = HERE / "stackchan_voice_call.py"
        python = Path(os.environ.get("STACKCHAN_XIAOZHI_PYTHON", "/root/stackchan-server-lite/main/xiaozhi-server/.venv/bin/python"))
        try:
            res = subprocess.run(
                [str(python), str(helper), *args],
                cwd=str(HERE),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, {"ok": False, "error": "stackchan voice api timed out"}
        except Exception as e:
            return False, {"ok": False, "error": str(e)}

        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()
        try:
            payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
        except Exception:
            payload = {"ok": False, "error": stdout or stderr or "invalid stackchan voice api response"}
        if res.returncode != 0 and not payload.get("error"):
            payload["error"] = stderr or f"stackchan voice api exited {res.returncode}"
        return res.returncode == 0 and bool(payload.get("ok")), payload

    def _handle_voice_call_asr(self):
        """Raw audio upload -> StackChan/xiaozhi ASR transcript."""
        import uuid as _uuid
        from urllib.parse import urlparse, parse_qs, unquote

        qs = parse_qs(urlparse(self.path).query)
        contact_id = self._clean_contact_id(qs.get("contact_id", qs.get("contactId", ["kairos"]))[0])
        filename = qs.get("filename", ["voice.m4a"])[0] or "voice.m4a"
        try:
            filename = unquote(filename)
        except Exception:
            pass
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0 or length > 15 * 1024 * 1024:
            self._send_json(400, {"ok": False, "error": "invalid content-length (max 15MB)"})
            return

        ext = Path(filename).suffix.lower() or ".m4a"
        if ext not in {".m4a", ".mp4", ".aac", ".wav", ".mp3", ".ogg", ".webm"}:
            ext = ".m4a"
        tmp_path = self.state.attachments_dir / f"voice_asr_input_{_uuid.uuid4().hex}{ext}"
        try:
            with tmp_path.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            ok, payload = self._run_stackchan_voice_helper(["asr", "--input", str(tmp_path)], timeout=90)
            self._send_json(
                200 if ok else 502,
                {
                    "ok": ok,
                    "contact_id": contact_id,
                    "transcript": str(payload.get("transcript") or ""),
                    "error": payload.get("error"),
                },
            )
        except Exception as e:
            logger.exception("voice-call asr fail")
            self._send_json(500, {"ok": False, "error": str(e)})
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _handle_voice_call_tts(self, body: dict[str, Any]):
        """Text -> StackChan/xiaozhi TTS audio file for app playback."""
        text = str(body.get("text") or "").strip()
        contact_id = self._contact_id_from_body(body)
        if not text:
            self._send_json(400, {"ok": False, "error": "text required"})
            return
        ok, payload = self._run_stackchan_voice_helper(
            ["tts", "--text", text, "--output-dir", str(self.state.attachments_dir)],
            timeout=90,
        )
        if not ok:
            self._send_json(502, {"ok": False, "contact_id": contact_id, "error": payload.get("error") or "tts failed"})
            return
        stored_name = str(payload.get("stored_name") or "")
        if not stored_name or "/" in stored_name or ".." in stored_name:
            self._send_json(502, {"ok": False, "contact_id": contact_id, "error": "bad tts output"})
            return
        self._send_json(200, {
            "ok": True,
            "contact_id": contact_id,
            "text": text,
            "audio_url": f"/attachments/{stored_name}",
            "mime_type": payload.get("mime_type") or "audio/wav",
            "bytes": payload.get("bytes"),
        })

    def _handle_voice_push(self, body: dict[str, Any]):
        """小克主动推语音消息 — TTS 生成 wav + 写 assistant chat record (type=voice).

        Body: {"text": "...", "contact_id": "xiaoke"} (default xiaoke)
        Auth: 走 do_POST 顶层 _require_write_auth, 已经强制了 X-Auth-Token。
        """
        text = str(body.get("text") or "").strip()
        if not text:
            self._send_json(400, {"ok": False, "error": "text required"})
            return
        contact_id = self._contact_id_from_body(body)

        ok, payload = self._run_stackchan_voice_helper(
            ["tts", "--text", text, "--output-dir", str(self.state.attachments_dir)],
            timeout=90,
        )
        if not ok:
            self._send_json(502, {"ok": False, "error": payload.get("error") or "tts failed"})
            return
        stored_name = str(payload.get("stored_name") or "")
        if not stored_name or "/" in stored_name or ".." in stored_name:
            self._send_json(502, {"ok": False, "error": "bad tts output"})
            return

        audio_url = f"/attachments/{stored_name}"
        mime_type = payload.get("mime_type") or "audio/wav"
        audio_bytes = payload.get("bytes") or 0

        chat = self._chat_for_contact(contact_id)
        try:
            rec = chat.append(
                role="assistant",
                text=text,
                source="xiaoke-voice-push",
                attachment_url=audio_url,
                attachment_type="audio",
                attachment_filename=stored_name,
                metadata={
                    "type": "voice",
                    "audio_url": audio_url,
                    "mime_type": mime_type,
                    "bytes": audio_bytes,
                },
            )
        except Exception as e:
            logger.exception("voice push chat append fail")
            self._send_json(500, {"ok": False, "error": f"chat append fail: {e}"})
            return

        try:
            preview = text[:80] if text else "[语音消息]"
            self._send_chat_notification("Cc", f"[语音] {preview}")
        except Exception as e:
            logger.warning("voice push notification fail: %s", e)

        self._send_json(200, {
            "ok": True,
            "contact_id": contact_id,
            "text": text,
            "audio_url": audio_url,
            "mime_type": mime_type,
            "bytes": audio_bytes,
            "record": rec,
        })

    def _handle_attachment_get(self):
        """静态服务 attachment 文件 — GET /attachments/<filename>"""
        from urllib.parse import unquote
        # path = /attachments/foo.jpg
        rel = self.path[len("/attachments/"):]
        rel = unquote(rel.split("?", 1)[0])
        # 防 path traversal
        if "/" in rel or ".." in rel or rel.startswith("."):
            self._send_json(400, {"error": "bad filename"})
            return
        target = self.state.attachments_dir / rel
        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        # MIME 简单推断
        ext = target.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp",
            ".heic": "image/heic", ".heif": "image/heif",
            ".pdf": "application/pdf",
            ".txt": "text/plain", ".md": "text/markdown",
            ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
            ".mp4": "video/mp4", ".mov": "video/quicktime",
        }
        mime = mime_map.get(ext, "application/octet-stream")
        try:
            length = target.stat().st_size
        except Exception:
            self._send_json(500, {"error": "read fail"})
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            with target.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.debug("attachment client disconnected path=%s err=%s", target.name, e)
        except Exception:
            logger.exception("attachment stream fail path=%s", target)

    # ------------------------------------------------------------------
    # Health Records API
    # ------------------------------------------------------------------

    _HEALTH_RECORDS_PATH = HERE / "state" / "health_records.json"
    _HEALTH_RECORDS_LOCK = threading.Lock()

    def _health_records_load(self) -> list[dict[str, Any]]:
        """Load health records from JSON file. Returns list of records."""
        path = self._HEALTH_RECORDS_PATH
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    def _health_records_save(self, records: list[dict[str, Any]]) -> None:
        """Atomically save health records to JSON file."""
        path = self._HEALTH_RECORDS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _health_records_cleanup(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove records older than 30 days."""
        cutoff_ms = (time.time() - 30 * 86400) * 1000
        return [r for r in records if r.get("timestamp", 0) > cutoff_ms]

    def _handle_health_records_post(self, body: dict[str, Any]):
        """POST /health-records - add a health record (glucose or food)."""
        import uuid as _uuid

        record_type = str(body.get("type", "")).strip().lower()
        if record_type not in ("glucose", "food"):
            self._send_json(400, {"error": "type must be 'glucose' or 'food'"})
            return

        record: dict[str, Any] = {
            "id": _uuid.uuid4().hex,
            "type": record_type,
            "note": str(body.get("note", "")),
            "timestamp": int(body.get("timestamp", 0)) or int(time.time() * 1000),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }

        if record_type == "glucose":
            value = body.get("value")
            if value is None:
                self._send_json(400, {"error": "value required for glucose record"})
                return
            try:
                record["value"] = float(value)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "value must be a number"})
                return
        elif record_type == "food":
            name = str(body.get("name", "")).strip()
            if not name:
                self._send_json(400, {"error": "name required for food record"})
                return
            record["name"] = name

        try:
            with self._HEALTH_RECORDS_LOCK:
                records = self._health_records_load()
                records = self._health_records_cleanup(records)
                records.append(record)
                self._health_records_save(records)
            self._send_json(200, {"ok": True, "record": record})
        except Exception as e:
            logger.exception("health records post fail")
            self._send_json(500, {"error": str(e)})

    def _handle_health_records_get(self):
        """GET /health-records?date=2026-05-31 - return records for a date."""
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(self.path).query)
        date_str = qs.get("date", [None])[0]

        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                self._send_json(400, {"error": "date must be YYYY-MM-DD format"})
                return
        else:
            target_date = datetime.now(timezone.utc).date()

        try:
            with self._HEALTH_RECORDS_LOCK:
                records = self._health_records_load()
            # Filter records for the target date
            day_records = []
            for r in records:
                ts_ms = r.get("timestamp", 0)
                if ts_ms:
                    record_date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
                    if record_date == target_date:
                        day_records.append(r)
            self._send_json(200, {"ok": True, "date": str(target_date), "records": day_records})
        except Exception as e:
            logger.exception("health records get fail")
            self._send_json(500, {"error": str(e)})

    # ------------------------------------------------------------------
    # Notion Proxy API (Android app reads/writes Notion without token)
    # ------------------------------------------------------------------

    _NOTION_TOKEN_PATH = Path("/root/.notion_token")
    _NOTION_API_VERSION = "2022-06-28"

    def _notion_token(self) -> str | None:
        """Read Notion API token from file. Returns None if not found."""
        try:
            if self._NOTION_TOKEN_PATH.exists():
                return self._NOTION_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return None

    def _notion_request(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """Make a request to Notion API. Returns (status_code, response_json)."""
        import urllib.request
        import urllib.error

        token = self._notion_token()
        if not token:
            return 500, {"error": "notion token not configured (missing /root/.notion_token)"}

        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self._NOTION_API_VERSION,
            "Content-Type": "application/json",
        }

        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_data = resp.read()
                return resp.status, json.loads(resp_data) if resp_data else {}
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read())
            except Exception:
                err_body = {"error": e.reason}
            return e.code, err_body
        except urllib.error.URLError as e:
            return 502, {"error": f"notion api unreachable: {e.reason}"}
        except Exception as e:
            return 502, {"error": f"notion request failed: {e}"}

    def _handle_notion_query(self, body: dict[str, Any]):
        """POST /notion/query - query a Notion database."""
        database_id = str(body.get("database_id", "")).strip()
        if not database_id:
            self._send_json(400, {"error": "database_id required"})
            return

        payload: dict[str, Any] = {}
        if body.get("filter"):
            payload["filter"] = body["filter"]
        if body.get("sorts"):
            payload["sorts"] = body["sorts"]
        if body.get("page_size"):
            payload["page_size"] = int(body["page_size"])
        if body.get("start_cursor"):
            payload["start_cursor"] = body["start_cursor"]

        url = f"https://api.notion.com/v1/databases/{database_id}/query"
        status, resp = self._notion_request("POST", url, payload)
        self._send_json(status, resp)

    def _handle_notion_create(self, body: dict[str, Any]):
        """POST /notion/create - create a page in a Notion database."""
        database_id = str(body.get("database_id", "")).strip()
        if not database_id:
            self._send_json(400, {"error": "database_id required"})
            return

        payload: dict[str, Any] = {
            "parent": {"database_id": database_id},
        }
        if body.get("properties"):
            payload["properties"] = body["properties"]
        if body.get("children"):
            payload["children"] = body["children"]
        if body.get("icon"):
            icon_value = body["icon"]
            if isinstance(icon_value, str):
                payload["icon"] = {"type": "emoji", "emoji": icon_value}
            elif isinstance(icon_value, dict):
                payload["icon"] = icon_value

        url = "https://api.notion.com/v1/pages"
        status, resp = self._notion_request("POST", url, payload)
        self._send_json(status, resp)

    def _handle_notion_append(self, body: dict[str, Any]):
        """POST /notion/append - append blocks to a page."""
        page_id = str(body.get("page_id", "")).strip()
        if not page_id:
            self._send_json(400, {"error": "page_id required"})
            return

        children = body.get("children")
        if not children or not isinstance(children, list):
            self._send_json(400, {"error": "children array required"})
            return

        payload = {"children": children}
        url = f"https://api.notion.com/v1/blocks/{page_id}/children"
        status, resp = self._notion_request("PATCH", url, payload)
        self._send_json(status, resp)

    def _handle_notion_search(self, body: dict[str, Any]):
        """POST /notion/search - search Notion."""
        query = str(body.get("query", "")).strip()

        payload: dict[str, Any] = {}
        if query:
            payload["query"] = query
        if body.get("filter"):
            payload["filter"] = body["filter"]
        if body.get("sort"):
            payload["sort"] = body["sort"]
        if body.get("page_size"):
            payload["page_size"] = int(body["page_size"])
        if body.get("start_cursor"):
            payload["start_cursor"] = body["start_cursor"]

        url = "https://api.notion.com/v1/search"
        status, resp = self._notion_request("POST", url, payload)
        self._send_json(status, resp)

    def _handle_notion_page_get(self):
        """GET /notion/page/{page_id} - get page blocks/content."""
        from urllib.parse import urlparse, parse_qs

        # Extract page_id from path: /notion/page/{page_id}
        parts = self.path.split("?", 1)
        path_part = parts[0]
        prefix = "/notion/page/"
        page_id = path_part[len(prefix):].strip()

        if not page_id:
            self._send_json(400, {"error": "page_id required in URL path"})
            return

        # Optional query params for pagination
        qs = parse_qs(parts[1]) if len(parts) > 1 else {}
        url = f"https://api.notion.com/v1/blocks/{page_id}/children"
        params = []
        if qs.get("page_size"):
            params.append(f"page_size={qs['page_size'][0]}")
        if qs.get("start_cursor"):
            params.append(f"start_cursor={qs['start_cursor'][0]}")
        if params:
            url += "?" + "&".join(params)

        status, resp = self._notion_request("GET", url)
        self._send_json(status, resp)

    # ------------------------------------------------------------------
    # Memory Library Proxy API (read-only forward to Singapore memory-mcp)
    #
    # V1 只读：app 只能看和搜，不缓存、不落盘任何记忆数据（纯转发）。
    # token 从 /root/.codex/config.toml 提取，绝不进日志/响应。
    # ------------------------------------------------------------------

    _MEMORY_UPSTREAM_BASE = "https://memory.xiaonancaleb.xyz"
    _MEMORY_CONFIG_PATH = Path("/root/.codex/config.toml")
    # /memory/<name> -> upstream /api path (GET-only whitelist)
    _MEMORY_ROUTES: dict[str, str] = {
        "/memory/stats": "/api/stats",
        "/memory/categories": "/api/categories",
        "/memory/list": "/api/memories",
        "/memory/semantic-search": "/api/semantic-search",
        "/memory/board": "/api/board",
    }
    _MEMORY_ALLOWED_PARAMS = ("query", "category", "limit", "tag")
    _memory_token_cache: str | None = None

    @classmethod
    def _memory_token(cls) -> str | None:
        """Extract memory library bearer token from codex config (cached)."""
        if cls._memory_token_cache:
            return cls._memory_token_cache
        try:
            text = cls._MEMORY_CONFIG_PATH.read_text(encoding="utf-8")
        except Exception:
            return None
        m = re.search(r'token=([^"&\s]+)', text)
        if not m:
            return None
        cls._memory_token_cache = m.group(1)
        return cls._memory_token_cache

    def _handle_memory_get(self):
        """GET /memory/* — read-only whitelist proxy to the memory library."""
        from urllib.parse import urlparse, parse_qs, urlencode

        parsed = urlparse(self.path)
        upstream_path = self._MEMORY_ROUTES.get(parsed.path)
        if not upstream_path:
            self._send_json(404, {"error": "not found"})
            return

        token = self._memory_token()
        if not token:
            self._send_json(502, {"error": "memory token not configured"})
            return

        # Whitelist-filter query params (anti SSRF / abuse).
        qs = parse_qs(parsed.query)
        params: list[tuple[str, str]] = []
        for key in self._MEMORY_ALLOWED_PARAMS:
            values = qs.get(key)
            if not values:
                continue
            value = str(values[0])[:500]
            if key == "limit":
                try:
                    value = str(max(1, min(int(value), 200)))
                except ValueError:
                    continue
            params.append((key, value))

        url = self._MEMORY_UPSTREAM_BASE + upstream_path
        if params:
            url += "?" + urlencode(params)

        status, payload = self._memory_request(url, token)
        self._send_json(status, payload)

    @staticmethod
    def _memory_request(url: str, token: str) -> tuple[int, Any]:
        """Forward one GET to the memory library. Never logs the token."""
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                # Cloudflare blocks python default UA — must impersonate curl.
                "User-Agent": "curl/7.81.0",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            logger.warning("memory proxy upstream http %s for %s", e.code, url.split("?")[0])
            return 502, {"error": f"memory upstream http {e.code}"}
        except Exception:
            logger.warning("memory proxy upstream unreachable for %s", url.split("?")[0])
            return 502, {"error": "memory upstream unreachable"}

    # ------------------------------------------------------------------
    # Appearance Settings Sync API (Android cloud backup)
    # ------------------------------------------------------------------

    _APPEARANCE_SETTINGS_PATH = HERE / "state" / "appearance_settings.json"
    _APPEARANCE_SETTINGS_LOCK = threading.Lock()
    _APPEARANCE_ASSETS_DIR = HERE / "state" / "appearance_assets"

    def _appearance_settings_load(self) -> dict[str, Any]:
        path = self._APPEARANCE_SETTINGS_PATH
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _appearance_settings_save(self, settings: dict[str, Any]) -> None:
        path = self._APPEARANCE_SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _handle_appearance_settings_get(self):
        """GET /appearance-settings — return stored appearance settings."""
        try:
            with self._APPEARANCE_SETTINGS_LOCK:
                settings = self._appearance_settings_load()
            if not settings:
                self._send_json(200, {"ok": True, "found": False, "settings": {}})
            else:
                self._send_json(200, {"ok": True, "found": True, "settings": settings})
        except Exception as e:
            logger.exception("appearance settings get fail")
            self._send_json(500, {"error": str(e)})

    def _handle_appearance_settings_post(self, body: dict[str, Any]):
        """POST /appearance-settings — save full appearance settings JSON."""
        if not body:
            self._send_json(400, {"error": "empty body"})
            return
        try:
            with self._APPEARANCE_SETTINGS_LOCK:
                self._appearance_settings_save(body)
            self._send_json(200, {"ok": True})
        except Exception as e:
            logger.exception("appearance settings post fail")
            self._send_json(500, {"error": str(e)})

    def _handle_appearance_assets_upload(self):
        """POST /appearance-assets — multipart upload for wallpaper/avatar images."""
        allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
        allowed_types = {"ai_avatar", "user_avatar", "wallpaper"}

        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        max_size = 10 * 1024 * 1024

        if length <= 0:
            self._send_json(400, {"error": "empty upload"})
            return
        if length > max_size:
            self._send_json(413, {"error": "file too large (max 10MB)"})
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            self._send_json(400, {"error": "multipart/form-data required"})
            return

        try:
            from email import policy
            from email.parser import BytesParser

            raw = self.rfile.read(length)
            msg = BytesParser(policy=policy.default).parsebytes(
                (
                    f"Content-Type: {content_type}\r\n"
                    "MIME-Version: 1.0\r\n\r\n"
                ).encode("utf-8") + raw
            )

            file_part = None
            asset_type = ""

            for part in msg.iter_parts():
                param_name = part.get_param("name", header="content-disposition")
                if param_name == "file":
                    file_part = part
                elif param_name == "type":
                    val = part.get_payload(decode=True)
                    if val:
                        asset_type = val.decode("utf-8", errors="replace").strip()

            if file_part is None:
                self._send_json(400, {"error": "file field required"})
                return

            dynamic_asset_type = bool(re.fullmatch(r"contact_avatar_[A-Za-z0-9_-]{1,64}", asset_type))
            if asset_type not in allowed_types and not (
                dynamic_asset_type or asset_type in {"light_app_bg", "dark_app_bg"}
            ):
                self._send_json(400, {"error": f"type must be one of: {', '.join(sorted(allowed_types))}, contact_avatar_*, light_app_bg, dark_app_bg"})
                return

            filename = file_part.get_filename() or "upload.bin"
            ext = Path(filename).suffix.lower()
            if ext not in allowed_exts:
                self._send_json(400, {"error": f"unsupported extension, allowed: {', '.join(sorted(allowed_exts))}"})
                return

            payload = file_part.get_payload(decode=True) or b""
            if not payload:
                self._send_json(400, {"error": "empty file"})
                return
            if len(payload) > max_size:
                self._send_json(413, {"error": "file too large"})
                return

            assets_dir = self._APPEARANCE_ASSETS_DIR
            assets_dir.mkdir(parents=True, exist_ok=True)

            stored_name = f"{asset_type}{ext}"
            stored_path = assets_dir / stored_name

            # Remove old file with same type but different extension
            for old in assets_dir.glob(f"{asset_type}.*"):
                if old != stored_path:
                    try:
                        old.unlink()
                    except Exception:
                        pass

            tmp_path = stored_path.with_suffix(".tmp")
            tmp_path.write_bytes(payload)
            tmp_path.replace(stored_path)

            url = f"/appearance-assets/{stored_name}"
            self._send_json(200, {"ok": True, "url": url, "filename": stored_name})

        except Exception as e:
            logger.exception("appearance assets upload fail")
            self._send_json(500, {"error": str(e)})

    def _handle_appearance_assets_get(self):
        """GET /appearance-assets/{filename} — serve stored appearance asset."""
        from urllib.parse import unquote

        rel = self.path[len("/appearance-assets/"):]
        rel = unquote(rel.split("?", 1)[0])

        if "/" in rel or ".." in rel or rel.startswith(".") or not rel:
            self._send_json(400, {"error": "bad filename"})
            return

        target = self._APPEARANCE_ASSETS_DIR / rel
        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return

        ext = target.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp",
            ".heic": "image/heic", ".heif": "image/heif",
        }
        mime = mime_map.get(ext, "application/octet-stream")

        try:
            file_size = target.stat().st_size
        except Exception:
            self._send_json(500, {"error": "read fail"})
            return

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()

        try:
            with target.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.debug("appearance_assets client disconnected path=%s err=%s", target.name, e)
        except Exception:
            logger.exception("appearance_assets stream fail path=%s", target)

    # ------------------------------------------------------------------
    # Image Upload API (avatar / background)
    # ------------------------------------------------------------------

    _UPLOADS_DIR = HERE / "state" / "uploads"

    def _handle_upload_multipart(self):
        """POST /upload - accept multipart/form-data with file + type + user_id fields."""
        allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}

        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        max_size = 10 * 1024 * 1024  # 10MB

        if length <= 0:
            self._send_json(400, {"error": "empty upload"})
            return
        if length > max_size:
            self._send_json(413, {"error": "file too large (max 10MB)"})
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            self._send_json(400, {"error": "multipart/form-data required"})
            return

        try:
            from email import policy
            from email.parser import BytesParser

            raw = self.rfile.read(length)
            msg = BytesParser(policy=policy.default).parsebytes(
                (
                    f"Content-Type: {content_type}\r\n"
                    "MIME-Version: 1.0\r\n\r\n"
                ).encode("utf-8") + raw
            )

            # Extract fields from multipart
            file_part = None
            upload_type = "avatar"
            user_id = "default"

            for part in msg.iter_parts():
                param_name = part.get_param("name", header="content-disposition")
                if param_name == "file":
                    file_part = part
                elif param_name == "type":
                    val = part.get_payload(decode=True)
                    if val:
                        upload_type = val.decode("utf-8", errors="replace").strip()
                elif param_name == "user_id":
                    val = part.get_payload(decode=True)
                    if val:
                        user_id = val.decode("utf-8", errors="replace").strip()

            if file_part is None:
                self._send_json(400, {"error": "file field required"})
                return

            # Validate type
            if upload_type not in ("avatar", "background"):
                self._send_json(400, {"error": "type must be 'avatar' or 'background'"})
                return

            # Sanitize user_id (alphanumeric + underscore + dash only)
            user_id = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)[:64] or "default"

            filename = file_part.get_filename() or "upload.bin"
            ext = Path(filename).suffix.lower()
            if ext not in allowed_exts:
                self._send_json(400, {"error": f"unsupported extension, allowed: {', '.join(sorted(allowed_exts))}"})
                return

            payload = file_part.get_payload(decode=True) or b""
            if not payload:
                self._send_json(400, {"error": "empty file"})
                return
            if len(payload) > max_size:
                self._send_json(413, {"error": "file too large"})
                return

            # Ensure uploads dir exists
            uploads_dir = self._UPLOADS_DIR
            uploads_dir.mkdir(parents=True, exist_ok=True)

            # Filename: {type}_{user_id}.{ext} — overwrites previous
            stored_name = f"{upload_type}_{user_id}{ext}"
            stored_path = uploads_dir / stored_name

            # Remove any old file with same prefix but different extension
            for old in uploads_dir.glob(f"{upload_type}_{user_id}.*"):
                if old != stored_path:
                    try:
                        old.unlink()
                    except Exception:
                        pass

            # Atomic write
            tmp_path = stored_path.with_suffix(".tmp")
            tmp_path.write_bytes(payload)
            tmp_path.replace(stored_path)

            url = f"/uploads/{stored_name}"
            self._send_json(200, {"ok": True, "url": url, "filename": stored_name})

        except Exception as e:
            logger.exception("upload multipart fail")
            self._send_json(500, {"error": str(e)})

    def _handle_uploads_get(self):
        """GET /uploads/{filename} - serve uploaded file from state/uploads/."""
        from urllib.parse import unquote

        rel = self.path[len("/uploads/"):]
        rel = unquote(rel.split("?", 1)[0])

        # Prevent path traversal
        if "/" in rel or ".." in rel or rel.startswith(".") or not rel:
            self._send_json(400, {"error": "bad filename"})
            return

        target = self._UPLOADS_DIR / rel
        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return

        ext = target.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp",
            ".heic": "image/heic", ".heif": "image/heif",
        }
        mime = mime_map.get(ext, "application/octet-stream")

        try:
            file_size = target.stat().st_size
        except Exception:
            self._send_json(500, {"error": "read fail"})
            return

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()

        try:
            with target.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.debug("uploads client disconnected path=%s err=%s", target.name, e)
        except Exception:
            logger.exception("uploads stream fail path=%s", target)

    # ------------------------------------------------------------------
    # User Settings Sync API
    # ------------------------------------------------------------------

    _USER_SETTINGS_PATH = HERE / "state" / "user_settings.json"
    _USER_SETTINGS_LOCK = threading.Lock()

    def _user_settings_load(self) -> dict[str, Any]:
        """Load user settings from JSON file."""
        path = self._USER_SETTINGS_PATH
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            return {}

    def _user_settings_save(self, settings: dict[str, Any]) -> None:
        """Atomically save user settings to JSON file."""
        path = self._USER_SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _handle_user_settings_post(self, body: dict[str, Any]):
        """POST /user-settings - merge provided fields into stored settings."""
        allowed_keys = {
            "avatar_url", "background_url", "font_size",
            "bubble_color", "theme_color", "display_name", "bot_name",
            "appearance",
        }
        updates = {k: v for k, v in body.items() if k in allowed_keys}
        if "appearance" not in updates:
            appearance_keys = {
                "bgUri", "aiName", "aiStatus", "aiAvatarUri", "meName", "meAvatarUri",
                "contactAvatarUris", "contactNicknames", "inputHint",
                "toolSummaryBeforeCount", "toolSummaryAfterCount",
                "aiColor", "aiOpacity", "aiGlass", "aiBlur", "aiText",
                "userColor", "userOpacity", "userGlass", "userBlur", "userText",
                "darkAiColor", "darkAiOpacity", "darkAiGlass", "darkAiBlur", "darkAiText",
                "darkUserColor", "darkUserOpacity", "darkUserGlass", "darkUserBlur", "darkUserText",
                "lightAiColor", "lightAiOpacity", "lightAiGlass", "lightAiBlur", "lightAiText",
                "lightUserColor", "lightUserOpacity", "lightUserGlass", "lightUserBlur", "lightUserText",
                "fontSize", "radius", "gap", "darkMode", "themeMode", "themeColor",
                "lightSettingsPageBg", "lightAppBgColor", "darkAppBgColor",
                "lightAppBgUri", "darkAppBgUri",
            }
            appearance = {k: v for k, v in body.items() if k in appearance_keys}
            if appearance:
                updates["appearance"] = appearance
        if "appearance" in updates and not isinstance(updates["appearance"], dict):
            self._send_json(400, {"error": "appearance must be an object"})
            return
        if not updates:
            self._send_json(400, {"error": f"no valid fields provided. allowed: {', '.join(sorted(allowed_keys))}"})
            return

        try:
            with self._USER_SETTINGS_LOCK:
                settings = self._user_settings_load()
                if "appearance" in updates and isinstance(settings.get("appearance"), dict):
                    merged_appearance = dict(settings.get("appearance") or {})
                    merged_appearance.update(updates["appearance"])
                    updates["appearance"] = merged_appearance
                settings.update(updates)
                self._user_settings_save(settings)
            self._send_json(200, {"ok": True, "settings": settings})
        except Exception as e:
            logger.exception("user settings post fail")
            self._send_json(500, {"error": str(e)})

    def _handle_user_settings_get(self):
        """GET /user-settings - return current user settings."""
        try:
            with self._USER_SETTINGS_LOCK:
                settings = self._user_settings_load()
            if "appearance" not in settings:
                with self._APPEARANCE_SETTINGS_LOCK:
                    appearance = self._appearance_settings_load()
                if appearance:
                    settings = {**settings, "appearance": appearance}
            self._send_json(200, {"ok": True, "settings": settings})
        except Exception as e:
            logger.exception("user settings get fail")
            self._send_json(500, {"error": str(e)})

    # ------------------------------------------------------------------

    def _handle_chat_delete(self, body: dict[str, Any]):
        ts = body.get("ts", "").strip()
        if not ts:
            self._send_json(400, {"error": "ts required"})
            return
        ok = self.state.chat.delete(ts)
        self._send_json(200, {"ok": ok, "ts": ts})

    def _handle_chat_react(self, body: dict[str, Any]):
        ts = body.get("ts", "").strip()
        emoji = body.get("emoji", "").strip()
        if not ts or not emoji:
            self._send_json(400, {"error": "ts and emoji required"})
            return
        ok = self.state.chat.add_reaction(ts, emoji)
        self._send_json(200, {"ok": ok, "ts": ts, "emoji": emoji})

    def _handle_todos_toggle(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        res = todos_mod.toggle(
            rel_path=body.get("path", ""),
            heading=body.get("heading", ""),
            text=body.get("text", ""),
            expected_done=body.get("expected_done"),
            file_mtime=body.get("file_mtime"),
            line_index=body.get("line_index"),
        )
        if res.get("ok"):
            done = res.get("new_done", False)
            verb = "勾完成" if done else "取消勾"
            self._notify_chain_todo(f"[用户 {verb}: {body.get('text', '')[:60]}]")
        self._send_json(200 if res.get("ok") else 400, res)

    def _handle_todos_add(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        res = todos_mod.add(
            rel_path=body.get("path", ""),
            heading=body.get("heading", ""),
            text=body.get("text", ""),
            actor=body.get("actor"),
            after_text=body.get("after_text"),
        )
        if res.get("ok"):
            heading = body.get("heading", "")
            self._notify_chain_todo(f"[用户 新增待办 ({heading}): {res.get('added_text', '')[:80]}]")
        self._send_json(200 if res.get("ok") else 400, res)

    def _handle_todos_edit(self, body: dict[str, Any]):
        if not self._check_auth():
            self._send_json(401, {"error": "auth required"})
            return
        res = todos_mod.edit(
            rel_path=body.get("path", ""),
            heading=body.get("heading", ""),
            text=body.get("text", ""),
            new_text=body.get("new_text", ""),
        )
        if res.get("ok"):
            old = res.get("old_text", "")[:50]
            new = res.get("new_text", "")[:50]
            self._notify_chain_todo(f"[用户 编辑待办: {old} → {new}]")
        self._send_json(200 if res.get("ok") else 400, res)

    def _notify_chain_todo(self, text: str):
        """todos toggle/add/edit 成功后 推一条 system 消息给主 chain — 让 Cc 立刻知道用户改了什么.
        走 bus_send.py UNIX socket — 同微信入站走的同一条路径"""
        try:
            subprocess.Popen(
                [
                    "python3",
                    self.state.bus_send_path,
                    "--source", "todos",
                    "--sender", "ios-app",
                    "--text", text,
                    "--mode", "user",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("notify_chain_todo fail: %s", e)

    # ---------- 五子棋 state endpoint ----------

    def _handle_gomoku_state(self):
        try:
            state = self._compute_gomoku_state()
            self._send_json(200, {"ok": True, **state})
        except Exception as e:
            logger.exception("gomoku state fail")
            self._send_json(500, {"error": str(e)})

    def _compute_gomoku_state(self) -> dict:
        """全量重建五子棋局面。revision = 当前局活跃 move 数，任何增删都改变它。"""
        board_size = 13
        board: list[list[str | None]] = [[None] * board_size for _ in range(board_size)]
        active_moves: list[dict] = []
        seq = 0
        next_turn = "black"
        winner: str | None = None

        move_records = self.state.chat.search(role="move", limit=10000)
        for rec in move_records:
            text = rec.get("text", "").strip()
            parts = text.split()
            if not parts:
                continue
            cmd = parts[0]
            if cmd == "reset":
                new_size = int(parts[1]) if len(parts) >= 2 else 13
                board_size = new_size
                board = [[None] * board_size for _ in range(board_size)]
                active_moves = []
                seq = 0
                next_turn = "black"
                winner = None
                continue
            if cmd not in ("black", "white") or len(parts) < 2:
                continue
            coord_parts = parts[1].split(",")
            if len(coord_parts) != 2:
                continue
            try:
                r, c = int(coord_parts[0]), int(coord_parts[1])
            except ValueError:
                continue
            if not (0 <= r < board_size and 0 <= c < board_size):
                continue
            if board[r][c] is not None:
                continue  # 已占 幂等跳过
            if winner is not None:
                continue  # 已有赢家 不再落子
            board[r][c] = cmd
            seq += 1
            active_moves.append({"ts": rec["ts"], "color": cmd, "row": r, "col": c, "seq": seq})
            if self._gomoku_check_winner(board, r, c, cmd, board_size):
                winner = cmd
            else:
                next_turn = "white" if cmd == "black" else "black"

        return {
            "revision": len(active_moves),
            "board_size": board_size,
            "moves": active_moves,
            "next_turn": next_turn,
            "winner": winner,
        }

    def _gomoku_check_winner(self, board: list, r: int, c: int, color: str, size: int) -> bool:
        dirs = [(0, 1), (1, 0), (1, 1), (1, -1)]
        for dr, dc in dirs:
            count = 1
            rr, cc = r + dr, c + dc
            while 0 <= rr < size and 0 <= cc < size and board[rr][cc] == color:
                count += 1; rr += dr; cc += dc
            rr, cc = r - dr, c - dc
            while 0 <= rr < size and 0 <= cc < size and board[rr][cc] == color:
                count += 1; rr -= dr; cc -= dc
            if count >= 5:
                return True
        return False

    # ---------- /usage 综合端点 ----------

    def _handle_usage_overview(self):
        """综合用量: ccusage active block + OTS 统计 + Anthropic 链接"""
        try:
            ccusage_data = self._get_ccusage_cached()
            ots_data = self._get_ots_stats()
            anthropic_url = (
                self.state.config.get("server", {})
                .get("anthropic_dashboard_url", "https://claude.ai/settings/usage")
            )
            self._send_json(200, {
                "ok": True,
                "ccusage": ccusage_data,
                "ots": ots_data,
                "anthropic_url": anthropic_url,
            })
        except Exception as e:
            logger.exception("usage overview fail")
            self._send_json(500, {"error": str(e)})

    def _get_ccusage_cached(self) -> dict:
        """调 ccusage blocks --json，结果缓存 5 分钟到 tokens/ccusage_cache.json"""
        cache_path = Path(self.state.token_store_path).parent / "ccusage_cache.json"
        # 读缓存
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if time.time() - cached.get("_cached_at", 0) < 300:
                    cached.pop("_cached_at", None)
                    return cached
            except Exception:
                pass
        # 跑 ccusage
        candidates = ["/opt/homebrew/bin/ccusage", "ccusage"]
        raw_data: dict | None = None
        for exe in candidates:
            try:
                res = subprocess.run(
                    [exe, "blocks", "--json"],
                    capture_output=True, text=True, timeout=15,
                )
                if res.returncode == 0:
                    raw_data = json.loads(res.stdout)
                    break
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning("ccusage run fail: %s", e)
                return {"available": False, "error": "ccusage run failed"}
        if raw_data is None:
            return {"available": False, "error": "ccusage not installed"}

        blocks = raw_data.get("blocks", [])
        active = next((b for b in blocks if b.get("isActive")), None)
        result: dict = {"available": True}
        if active:
            proj = active.get("projection") or {}
            result["active_block"] = {
                "cost_usd": round(active.get("costUSD", 0.0), 2),
                "tokens": active.get("totalTokens", 0),
                "end_time": active.get("endTime", ""),
                "minutes_until_reset": proj.get("remainingMinutes"),
                "models": active.get("models", []),
            }
        else:
            result["active_block"] = None
        # 写缓存
        try:
            cache_path.write_text(
                json.dumps({**result, "_cached_at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
        return result

    def _get_ots_stats(self) -> dict:
        """OTS 自身统计: chat 行数 / 今日 / active device / uptime"""
        chat_path = self.state.chat.path
        total = 0
        today_count = 0
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(chat_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    total += 1
                    # ts 总在行首 30 字节内: {"ts": "2026-05-02T...
                    if today_prefix in line[:30]:
                        today_count += 1
        except Exception:
            pass
        active_device_count = len(self.state.tokens.all_active())
        uptime_hours = round((time.time() - self.state.started_at) / 3600, 1)
        return {
            "chat_total": total,
            "chat_today": today_count,
            "active_device_count": active_device_count,
            "uptime_hours": uptime_hours,
        }

    def _handle_usage_active(self):
        snapshot = self.state.usage.get_active()
        self._send_json(200, snapshot)

    # ---------- tmux 终端 endpoints ----------

    def _handle_tmux_sessions(self):
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=3
            )
            sessions = [s.strip() for s in result.stdout.split("\n") if s.strip()]
            self._send_json(200, {"ok": True, "sessions": sessions})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_tmux_capture(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        session = qs.get("session", [self.state.default_session])[0]
        try:
            lines = int(qs.get("lines", ["120"])[0])
        except Exception:
            lines = 120
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p", "-S", str(-lines)],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode != 0:
                self._send_json(404, {"error": result.stderr.strip() or "session not found"})
                return
            self._send_json(200, {
                "ok": True,
                "session": session,
                "content": result.stdout
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_terminal_key(self, body: dict[str, Any]):
        """POST /terminal/key — send a special key directly via tmux send-keys.

        Body: {"key": "Escape"} or {"key": "C-c"}, {"key": "Tab"}, etc.
        Optional: {"session": "cctg"} (defaults to "cctg")

        This uses `tmux send-keys` directly, which is the correct way to send
        special/control keys (unlike /tmux/send which uses load-buffer for text).
        """
        key_name = str(body.get("key", "")).strip()
        session = str(body.get("session", "cctg")).strip() or "cctg"
        if not key_name:
            self._send_json(400, {"error": "key required"})
            return
        # Whitelist of allowed key names to prevent command injection
        allowed_keys = {
            "Escape", "Tab", "Enter", "Space", "BSpace",
            "Up", "Down", "Left", "Right",
            "Home", "End", "PageUp", "PageDown", "DC",
            "C-c", "C-d", "C-z", "C-a", "C-e", "C-k", "C-u", "C-l", "C-r", "C-w",
            "C-b", "C-f", "C-n", "C-p",
            "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
        }
        if key_name not in allowed_keys:
            self._send_json(400, {"error": f"key '{key_name}' not in allowed list", "allowed": sorted(allowed_keys)})
            return
        try:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", session, key_name],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode != 0:
                self._send_json(500, {"error": result.stderr.strip() or "send-keys failed"})
                return
            self._send_json(200, {"ok": True, "key": key_name, "session": session})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_tmux_send(self, body: dict[str, Any]):
        keys = body.get("keys", "")
        # 兜底 body 没传 session 时走当前 active_session 而不是写死 opia
        # (build 199 fix: /switch 后 iOS 没传 session 字段也能 follow active)
        session = body.get("session") or self.state.active_session or self.state.default_session
        enter = bool(body.get("enter", True))
        # 2026-05-19 新增 key 字段: 真发特殊键 (Escape / Up / Down / Enter / Tab / C-c)
        # 跟 keys (文本) 区分 — keys 走 paste-buffer 文本注入 / key 走 send-keys 键名
        # 解决之前 sendEscape sendRawKey("Escape") 字面粘到 shell 不是真按 esc 的问题
        special_key = body.get("key")
        SPECIAL_KEY_WHITELIST = {"Escape", "Up", "Down", "Enter", "Tab", "C-c", "C-l"}
        if special_key:
            if special_key not in SPECIAL_KEY_WHITELIST:
                self._send_json(400, {"error": f"key not in whitelist: {special_key}"})
                return
            try:
                subprocess.run(["tmux", "send-keys", "-t", session, special_key], check=False)
                self._send_json(200, {"ok": True, "session": session, "key": special_key})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if not keys and not enter:
            self._send_json(400, {"error": "keys or enter or key required"})
            return
        try:
            if keys:
                # 用 load-buffer + paste-buffer 安全注入 (避免 - 开头被当 flag)
                p = subprocess.Popen(
                    ["tmux", "load-buffer", "-"],
                    stdin=subprocess.PIPE,
                )
                p.communicate(input=keys.encode("utf-8"))
                subprocess.run(["tmux", "paste-buffer", "-t", session, "-p"], check=False)
            if enter:
                subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=False)
            self._send_json(200, {"ok": True, "session": session})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ---------- reminder 端点 ----------

    def _handle_reminder_schedule(self, body: dict[str, Any]):
        fire_at = body.get("fire_at", "").strip()
        prompt = body.get("prompt", "").strip()
        if not fire_at or not prompt:
            self._send_json(400, {"error": "fire_at and prompt required"})
            return
        try:
            from datetime import datetime
            datetime.fromisoformat(fire_at)  # 校验格式
        except ValueError:
            self._send_json(400, {"error": f"invalid fire_at format: {fire_at}"})
            return
        rec = self.state.reminders.schedule(
            fire_at=fire_at,
            prompt=prompt,
            created_by=body.get("created_by", "chain"),
        )
        logger.info("reminder scheduled id=%s fire_at=%s", rec["id"], fire_at)
        self._send_json(200, {"ok": True, "id": rec["id"], "reminder": rec})

    def _handle_reminder_update(self, body: dict[str, Any], action: str):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        reminder_id = qs.get("id", [None])[0] or body.get("id", "")
        if not reminder_id:
            self._send_json(400, {"error": "id required"})
            return
        if action == "cancel":
            ok = self.state.reminders.cancel(reminder_id)
        else:
            ok = self.state.reminders.mark_fired(reminder_id)
        self._send_json(200 if ok else 404, {"ok": ok, "id": reminder_id})

    def _handle_tool_trigger(self, body: dict[str, Any]):
        """POST /tool/trigger — manually fire a tool-dispatcher rule now.

        Body: {"rule_id": "morning_greeting"} OR {"text": "...", "contact_id": "xiaoke"}.
        Reuses the exact delivery path (chat history visibility + channel
        transport / tmux). Does NOT mark the rule served, so the real scheduled
        occurrence is unaffected — this is for testing/on-demand wakeups.
        """
        if not self._require_write_auth():
            self._send_json(401, {"error": "auth required"})
            return
        rule_id = str(body.get("rule_id") or "").strip()
        contact_id = str(body.get("contact_id") or "xiaoke").strip().lower() or "xiaoke"
        text = str(body.get("text") or "").strip()
        if rule_id and not text:
            match = next(
                (r for r in self.state.tool_schedule.rules() if str(r.get("id")) == rule_id),
                None,
            )
            if not match:
                self._send_json(404, {"ok": False, "error": f"rule not found: {rule_id}"})
                return
            text = str(match.get("text") or "").strip()
            contact_id = str(match.get("contact_id") or contact_id).strip().lower() or "xiaoke"
        if not text:
            self._send_json(400, {"error": "rule_id or text required"})
            return
        ok, err = self.state.deliver_trigger(contact_id, text, rule_id or "manual")
        self._send_json(200 if ok else 502, {
            "ok": ok,
            "rule_id": rule_id or "manual",
            "contact_id": contact_id,
            "error": None if ok else err,
        })

    def _handle_toolbot_broadcast(self, body: dict[str, Any]):
        """POST /toolbot/broadcast — append a system播报 into the toolbot window.

        Body: {"title": "...", "text": "..."}. Auth: X-Auth-Token == shared_secret.
        Used by model_sentinel / vps-access-monitor / auto_backup_all to mirror
        their Telegram broadcasts into the app's 工具版 archive window. Pure
        append; never injects into any session or runs shell.
        """
        if not self._require_write_auth():
            self._send_json(401, {"error": "auth required"})
            return
        title = str(body.get("title") or "").strip() or None
        text = str(body.get("text") or "").strip()
        if not text and not title:
            self._send_json(400, {"error": "title or text required"})
            return
        # Bound size so a runaway script can't bloat the archive.
        if len(text) > 8000:
            text = text[:8000] + "…（截断）"
        rec = self.state.toolbot_archive(
            text,
            title=title,
            source="toolbot-broadcast",
            metadata={"broadcast": True},
        )
        self._send_json(200 if rec else 502, {
            "ok": bool(rec),
            "ts": (rec or {}).get("ts"),
        })

    # 严格白名单：命令名 -> 动作。绝不 eval / 拼接任意 shell。
    # 仅这 8 个键可被 /toolbot/command 触发；未在表内一律 403。
    TOOLBOT_COMMANDS: frozenset = frozenset({
        "forge", "statusbar", "vps", "model",
        "morning_on", "morning_off", "diary_on", "diary_off",
        "sessions", "session_rename", "session_switch",
        "session_new", "session_preview", "status_set",
    })
    # morning/diary 开关映射到 dispatcher 注册表里的规则 id。
    _TOOLBOT_MORNING_RULES = ["morning_greeting"]
    _TOOLBOT_DIARY_RULES = ["diary_reminder", "diary_supplement"]

    def _handle_toolbot_command(self, body: dict[str, Any]):
        """POST /toolbot/command — run a whitelisted command, archive the result.

        Body: {"command": "<one of TOOLBOT_COMMANDS>", "args": "<optional str>"}.
        Auth: X-Auth-Token == shared_secret. Strict whitelist — the command name
        is matched against a fixed set and dispatched to a hard-coded handler.
        No shell string is ever built from user input; `args` is only used by
        `model` (validated against a model-name allowlist) and ignored elsewhere.
        Execution result is appended to the toolbot window so 方小南 sees
        "执行了 X，结果 Y" in the public-archive window.
        """
        if not self._require_write_auth():
            self._send_json(401, {"error": "auth required"})
            return
        command = str(body.get("command") or "").strip().lower()
        args = str(body.get("args") or "").strip()
        if command not in self.TOOLBOT_COMMANDS:
            self._send_json(403, {"ok": False, "error": f"command not allowed: {command or '(empty)'}"})
            return

        title = f"指令 · {command}"
        try:
            ok, result_text = self._run_toolbot_command(command, args)
        except Exception as e:
            logger.exception("toolbot command %r failed", command)
            ok, result_text = False, f"执行异常：{e}"

        status_icon = "✅" if ok else "⚠️"
        archive_text = f"{status_icon} 执行 `{command}`" + (f" {args}" if args else "") + f"\n{result_text}"
        self.state.toolbot_archive(
            archive_text,
            title=title,
            source="toolbot-command",
            metadata={"command": command, "ok": ok},
        )
        self._send_json(200 if ok else 502, {
            "ok": ok,
            "command": command,
            "result": result_text,
        })

    def _handle_ai_status_post(self, body: dict[str, Any]):
        """POST /ai-status {"contact_id": ..., "text": ...} — set the chat-header
        AI status text for one contact (contact_id defaults to xiaoke).

        Auth: shares the same _require_write_auth gate as other writes (already
        enforced in do_POST before dispatch). Clamps to 1.._AI_STATUS_MAX_LEN
        chars and strips control characters, then atomically persists."""
        contact_id = str(
            body.get("contact_id") or body.get("contactId") or "xiaoke"
        ).strip().lower() or "xiaoke"
        text = _sanitize_ai_status(str(body.get("text") or ""))
        if text is None:
            self._send_json(400, {
                "ok": False,
                "error": f"text 需为 1-{_AI_STATUS_MAX_LEN} 字符（去掉控制字符后）",
            })
            return
        try:
            _write_ai_status(text, contact_id)
        except Exception as e:
            logger.exception("write ai_status failed")
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "contact_id": contact_id, "text": text})

    def _run_toolbot_command(self, command: str, args: str) -> tuple[bool, str]:
        """Dispatch a whitelisted toolbot command. Returns (ok, result_text).

        Each branch maps to a fixed action. No arbitrary shell. tmux-touching
        commands reuse the same load-buffer/send-keys injection path the rest of
        the server uses; subprocess argv lists are fully literal.
        """
        session = "cctg"  # the Claude Code tmux session (same as ccbot-lite)

        if command == "statusbar":
            # Read-only. Prefer the @claude-code-status-json tmux variable (the
            # same source ccbot-lite's /statusbar uses) rendered pretty; fall
            # back to the raw capture-pane tail if that's missing/unparseable.
            try:
                jproc = subprocess.run(
                    ["tmux", "show-option", "-gqv", "@claude-code-status-json"],
                    capture_output=True, text=True, timeout=4,
                )
                if jproc.returncode == 0 and jproc.stdout.strip():
                    sdata = json.loads(jproc.stdout.strip())
                    pretty = _format_statusbar_from_json(sdata)
                    if pretty:
                        return True, pretty
            except FileNotFoundError:
                return False, "tmux 未安装"
            except Exception as e:
                logger.debug("statusbar json render failed, falling back: %s", e)
            # Fallback: raw pane tail.
            try:
                proc = subprocess.run(
                    ["tmux", "capture-pane", "-t", session, "-p"],
                    capture_output=True, text=True, timeout=5,
                )
            except FileNotFoundError:
                return False, "tmux 未安装"
            except Exception as e:
                return False, f"capture-pane 失败：{e}"
            if proc.returncode != 0:
                return False, f"capture-pane 失败：{proc.stderr.strip() or 'session 不存在'}"
            tail = "\n".join(proc.stdout.rstrip().splitlines()[-12:])
            return True, f"```\n{tail or '(空)'}\n```"

        if command == "vps":
            # Read-only: run the fixed /root/vps-status.sh script (no args).
            script = "/root/vps-status.sh"
            if not os.path.exists(script):
                return False, f"{script} 不存在"
            try:
                proc = subprocess.run(
                    ["/bin/bash", script],
                    capture_output=True, text=True, timeout=20,
                )
            except Exception as e:
                return False, f"运行 vps-status.sh 失败：{e}"
            out = (proc.stdout or "").strip() or "(无输出)"
            if proc.returncode != 0:
                out = f"{out}\n[exit {proc.returncode}] {(proc.stderr or '').strip()}".strip()
            if len(out) > 3500:
                out = out[-3500:]
            return True, f"```\n{out}\n```"

        if command == "model":
            # Switch model via Claude Code's built-in /model. args validated.
            model_id = self._resolve_toolbot_model(args)
            if model_id is None:
                return False, f"未知模型：{args or '(空)'}（允许：{', '.join(sorted(TOOLBOT_MODEL_ALLOWLIST))}）"
            ok, err = self._inject_to_session(session, f"/model {model_id}", source="toolbot", sender="toolbot")
            if not ok:
                return False, f"注入失败：{err}"
            return True, f"已发送 `/model {model_id}` 到会话。"

        if command == "forge":
            # Slide the context window. Runs the fixed forge wrapper. args is a
            # whitespace-separated "[retain] [model]" two-part form, both parts
            # optional in any order:
            #   retain := 'all' | <digits>   (context-retain tokens)
            #   model  := a TOOLBOT_MODEL_ALIASES key or TOOLBOT_MODEL_ALLOWLIST id
            # Each token must match exactly one of those rules; anything else is
            # rejected. The model is resolved to a concrete allowlisted id before
            # being passed as its own argv element — no free-form string reaches
            # the wrapper.
            forge_bin = "/usr/local/bin/forge-reload-claude"
            if not os.path.exists(forge_bin):
                return False, f"{forge_bin} 不存在"
            argv = [forge_bin]
            retain_val: str | None = None
            model_val: str | None = None
            for tok in args.split():
                low = tok.strip().lower()
                if not low:
                    continue
                if low == "all" or low.isdigit():
                    if retain_val is not None:
                        return False, "forge 参数里 retain（all/数字）只能给一个。"
                    retain_val = low
                    continue
                resolved = self._resolve_toolbot_model(low)
                if resolved is not None:
                    if model_val is not None:
                        return False, "forge 参数里 model 只能给一个。"
                    model_val = resolved
                    continue
                return False, (
                    f"forge 参数无效：`{tok}`。retain 只能是 all 或纯数字；"
                    f"model 需是允许的别名/全名（{', '.join(sorted(TOOLBOT_MODEL_ALIASES))}）。"
                )
            if retain_val is not None:
                argv.append(retain_val)
            if model_val is not None:
                argv.append(model_val)
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, timeout=150,
                )
            except subprocess.TimeoutExpired:
                return False, "forge 超时（150s）。"
            except Exception as e:
                return False, f"forge 执行失败：{e}"
            out = (proc.stdout or "").strip()
            if len(out) > 3000:
                out = out[-3000:]
            detail = f"保留上下文：{retain_val or '默认'}；模型：{model_val or '默认/当前'}"
            if proc.returncode == 0:
                return True, f"Forge-reload 完成（{detail}）。\n```\n{out or '(无输出)'}\n```"
            return False, f"Forge-reload 失败（exit {proc.returncode}，{detail}）：{(proc.stderr or '').strip()[:1000]}"

        if command in ("morning_on", "morning_off"):
            enabled = command.endswith("_on")
            changed = self.state.tool_schedule.set_rules_enabled(self._TOOLBOT_MORNING_RULES, enabled)
            rstate = self.state.tool_schedule.rule_enabled_state(self._TOOLBOT_MORNING_RULES)
            label = "开启" if enabled else "关闭"
            return True, f"早安定时已{label}。当前规则状态：{rstate}（本次改动：{changed or '无变化'}）"

        if command in ("diary_on", "diary_off"):
            enabled = command.endswith("_on")
            changed = self.state.tool_schedule.set_rules_enabled(self._TOOLBOT_DIARY_RULES, enabled)
            rstate = self.state.tool_schedule.rule_enabled_state(self._TOOLBOT_DIARY_RULES)
            label = "开启" if enabled else "关闭"
            return True, f"日记提醒已{label}。当前规则状态：{rstate}（本次改动：{changed or '无变化'}）"

        if command == "sessions":
            # List the 10 most recently active Claude session jsonl files.
            # Returns a JSON string in `result` (structured, not pretty text) so
            # the app can parse it directly via POST /toolbot/command.
            try:
                jsonls = [
                    p for p in CLAUDE_SESSION_DIR.glob("*.jsonl")
                    if p.is_file()
                ]
            except Exception as e:
                return False, f"扫描 session 目录失败：{e}"
            jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            jsonls = jsonls[:10]
            names = _load_session_names()
            try:
                active_sid = CCBOT_CURRENT_SESSION_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                active_sid = ""
            items: list[dict[str, Any]] = []
            for p in jsonls:
                sid = p.stem
                try:
                    st = p.stat()
                    mtime_iso = datetime.fromtimestamp(st.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds")
                    size_kb = round(st.st_size / 1024.0, 1)
                except Exception:
                    mtime_iso, size_kb = "", 0.0
                items.append({
                    "sid": sid,
                    "name": names.get(sid),  # None when no alias
                    "mtime_iso": mtime_iso,
                    "size_kb": size_kb,
                    "preview": _session_preview(p),
                    "active": sid == active_sid and bool(active_sid),
                })
            return True, json.dumps({"sessions": items, "active_sid": active_sid}, ensure_ascii=False)

        if command == "session_rename":
            # args = "<sid> <new name>" — first space splits sid from the name;
            # the name may itself contain spaces. The name only ever enters the
            # JSON alias table — never a shell argv.
            parts = args.split(" ", 1)
            if len(parts) < 2:
                return False, "用法：session_rename <sid> <新名字>"
            sid, raw_name = parts[0].strip(), parts[1]
            if _session_jsonl_path(sid) is None:
                return False, f"session 不存在或 id 非法：{sid}"
            name = _sanitize_session_name(raw_name)
            if name is None:
                return False, f"名字需为 1-{_SESSION_NAME_MAX_LEN} 个字符（去掉控制字符后）。"
            names = _load_session_names()
            names[sid] = name
            try:
                _save_session_names(names)
            except Exception as e:
                return False, f"写别名表失败：{e}"
            return True, f"已重命名 {sid[:8]} → 「{name}」"

        if command == "session_switch":
            # args = "<sid>". Validate jsonl exists → write current-session →
            # sync active_session.json → restart claude-tg.service (the same
            # kill+resume mechanism forge-reload-claude uses). push.py runs as a
            # separate service (cc-companion.service), so restarting claude-tg
            # does NOT kill us — the response is sent reliably even though the
            # cctg claude that may have triggered this gets killed. We archive
            # below in _handle_toolbot_command after this returns.
            sid = args.strip()
            if _session_jsonl_path(sid) is None:
                return False, f"session 不存在或 id 非法：{sid}"
            try:
                _atomic_write_text(CCBOT_CURRENT_SESSION_FILE, sid + "\n")
            except Exception as e:
                return False, f"写 current-session 失败：{e}"
            # Keep the deprecated active_session.json in sync for any old reader.
            self.state.active_session = sid
            _persist_active_session(self.state)
            try:
                proc = subprocess.run(
                    ["systemctl", "restart", "claude-tg.service"],
                    capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                return False, f"已切到 {sid[:8]}，但重启 claude-tg 超时（60s）。请手动检查。"
            except Exception as e:
                return False, f"已切到 {sid[:8]}，但重启失败：{e}"
            if proc.returncode != 0:
                return False, (
                    f"已写入目标 session {sid[:8]}，但 systemctl restart 失败"
                    f"（exit {proc.returncode}）：{(proc.stderr or '').strip()[:500]}"
                )
            names = _load_session_names()
            label = names.get(sid) or sid[:8]
            return True, f"已切换到「{label}」并重启会话。新对话将 resume 该 session。"

        if command == "session_new":
            # args = optional model alias. Creates a brand-new session (no
            # context retained) by touching a start-fresh flag, clearing
            # current-session, and optionally switching the model.
            model_arg = args.strip() if args else ""
            model_label = ""
            if model_arg:
                model_id = self._resolve_toolbot_model(model_arg)
                if model_id is None:
                    return False, (
                        f"未知模型：{model_arg}。可选："
                        f"{', '.join(sorted(TOOLBOT_MODEL_ALIASES))}"
                    )
                try:
                    _atomic_write_text(CCBOT_CURRENT_MODEL_FILE, model_id + "\n")
                    model_label = f"（模型→{model_arg}）"
                except Exception as e:
                    return False, f"写 current-model 失败：{e}"
            try:
                _atomic_write_text(CCBOT_CURRENT_SESSION_FILE, "")
            except Exception as e:
                return False, f"清空 current-session 失败：{e}"
            # Touch the start-fresh flag so the start script skips --resume/--continue
            fresh_flag = CCBOT_CURRENT_SESSION_FILE.parent / "start-fresh"
            try:
                fresh_flag.touch()
            except Exception as e:
                return False, f"创建 start-fresh flag 失败：{e}"
            try:
                proc = subprocess.run(
                    ["systemctl", "restart", "claude-tg.service"],
                    capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                return False, f"已清空 session{model_label}，但重启 claude-tg 超时（60s）。"
            except Exception as e:
                return False, f"已清空 session{model_label}，但重启失败：{e}"
            if proc.returncode != 0:
                return False, (
                    f"已清空 session{model_label}，但 systemctl restart 失败"
                    f"（exit {proc.returncode}）：{(proc.stderr or '').strip()[:500]}"
                )
            return True, f"已新建全新会话{model_label}，服务已重启。"

        if command == "session_preview":
            # args = "<sid>". Read the tail of the session jsonl and return the
            # last few user/assistant text messages as structured JSON in
            # `result` (the app parses it directly). Read-only.
            sid = args.strip()
            p = _session_jsonl_path(sid)
            if p is None:
                return False, f"session 不存在或 id 非法：{sid}"
            try:
                msgs = _session_preview_messages(p, limit=6, max_chars=80)
            except Exception as e:
                return False, f"读取 session 预览失败：{e}"
            return True, json.dumps({"sid": sid, "messages": msgs}, ensure_ascii=False)

        if command == "status_set":
            # args = the new AI-status text. Writes ONLY xiaoke's per-contact
            # entry in ai_status.json (小克只动自己的状态). Lets 方小南 change it
            # from inside a session via a local curl to /toolbot/command.
            text = _sanitize_ai_status(args)
            if text is None:
                return False, f"文案需为 1-{_AI_STATUS_MAX_LEN} 字符（去掉控制字符后）。"
            try:
                _write_ai_status(text, "xiaoke")
            except Exception as e:
                return False, f"写 ai_status 失败：{e}"
            return True, f"AI 状态文案已更新为「{text}」（小克）"

        return False, f"未实现的命令：{command}"

    def _resolve_toolbot_model(self, choice: str) -> str | None:
        """Map a model alias/id to a concrete model id, or None if not allowed."""
        normalized = (choice or "").strip().lower()
        if not normalized:
            return None
        if normalized in TOOLBOT_MODEL_ALIASES:
            return TOOLBOT_MODEL_ALIASES[normalized]
        if normalized in TOOLBOT_MODEL_ALLOWLIST:
            return normalized
        return None

    def _handle_clear_unread(self):
        """chat tab 打开时调 — 把灵动岛 unread 归零，保留活跃任务状态"""
        active_tokens = self.state.tokens.all_active()
        if not active_tokens:
            self._send_json(200, {"ok": True, "sent": 0})
            return
        snap = self.state.tasks.snapshot()
        active_task = snap.get("active")
        cs: dict = {"status": "spoke", "unreadCount": 0, "lastMessagePreview": "", "sourceChannel": ""}
        if active_task:
            total = max(int(active_task.get("total", 1)), 1)
            current = int(active_task.get("current", 0))
            cs["taskTitle"] = active_task["title"]
            cs["taskCurrent"] = current
            cs["taskTotal"] = total
            cs["taskProgress"] = current / total
            if active_task.get("step"):
                cs["taskStep"] = str(active_task["step"])[:80]
        sent = 0
        for tok in active_tokens:
            try:
                self.state.client.push_live_activity(
                    push_token=tok.token, event="update", content_state=cs
                )
                sent += 1
            except Exception as e:
                logger.debug("clear_unread push skip: %s", e)
        self._send_json(200, {"ok": True, "sent": sent})

    def _handle_push(self, body: dict[str, Any]):
        event = body.get("event", "update")
        if event not in {"update", "end"}:
            self._send_json(400, {"error": f"unsupported event: {event}"})
            return
        if not self.state.apns_enabled:
            self._send_json(200, {"ok": True, "delivered": 0, "skipped": True, "note": "APNs not configured"})
            return

        content_state = _state_to_payload(body)
        alert_title = body.get("alert_title")
        alert_body = body.get("alert_body")
        stale_in = body.get("stale_in_seconds")
        dismiss_in = body.get("dismiss_in_seconds")
        force_alert = bool(body.get("force_alert", False))

        active = self.state.tokens.all_active()
        if not active:
            self._send_json(
                200,
                {"ok": True, "delivered": 0, "active": 0, "note": "no active tokens"},
            )
            return

        results = []
        purged = []

        for tok in active:
            # 选 client: token 已经学过 endpoint 就直接用 / unknown 走 primary
            if tok.endpoint == self.state._alt_endpoint:
                primary_client = self.state.client_alt
                alt_client = self.state.client
                primary_label = self.state._alt_endpoint
                alt_label = self.state._primary_endpoint
            else:
                primary_client = self.state.client
                alt_client = self.state.client_alt
                primary_label = self.state._primary_endpoint
                alt_label = self.state._alt_endpoint

            def _push_with(client_obj):
                return client_obj.push_live_activity(
                    push_token=tok.token,
                    event=event,
                    content_state=content_state,
                    alert_title=alert_title,
                    alert_body=alert_body,
                    stale_in_seconds=stale_in,
                    dismiss_in_seconds=dismiss_in,
                    force_alert=force_alert,
                )

            try:
                resp: APNsResponse = _push_with(primary_client)
            except Exception as e:
                logger.exception("push exception activity=%s", tok.activity_id)
                results.append(
                    {
                        "activity_id": tok.activity_id,
                        "ok": False,
                        "status": 0,
                        "reason": f"exception: {e}",
                    }
                )
                continue

            # BadDeviceToken / 400 → fallback 试 alt endpoint 通了就 set_endpoint 锁定
            tried_alt = False
            if (
                not resp.ok
                and resp.status == 400
                and "BadDeviceToken" in (resp.reason or "")
            ):
                logger.info(
                    "BadDeviceToken on %s endpoint — fallback to %s for activity=%s",
                    primary_label, alt_label, tok.activity_id,
                )
                try:
                    resp_alt: APNsResponse = _push_with(alt_client)
                    tried_alt = True
                    if resp_alt.ok:
                        self.state.tokens.set_endpoint(tok.activity_id, alt_label)
                        logger.info(
                            "fallback ok activity=%s now locked to endpoint=%s",
                            tok.activity_id, alt_label,
                        )
                        resp = resp_alt
                    else:
                        # alt 也失败 — 用 alt 的 resp 让上层看到完整失败原因
                        resp = resp_alt
                except Exception as e:
                    logger.exception("alt-endpoint push exception activity=%s", tok.activity_id)

            if resp.status == 410:
                # token revoked / expired - remove from store
                self.state.tokens.unregister(tok.activity_id)
                purged.append(tok.activity_id)
            elif resp.ok:
                self.state.tokens.touch(tok.activity_id)
                # primary 第一次通了 也记录 endpoint (lock unknown → primary)
                if tok.endpoint == "unknown" and not tried_alt:
                    self.state.tokens.set_endpoint(tok.activity_id, primary_label)

            results.append(
                {
                    "activity_id": tok.activity_id,
                    "device_label": tok.device_label,
                    "ok": resp.ok,
                    "status": resp.status,
                    "apns_id": resp.apns_id,
                    "reason": resp.reason if not resp.ok else "ok",
                    "endpoint": primary_label if not tried_alt else alt_label,
                }
            )

        delivered = sum(1 for r in results if r["ok"])
        logger.info(
            "push event=%s delivered=%d/%d purged=%d",
            event,
            delivered,
            len(results),
            len(purged),
        )
        self._send_json(
            200,
            {
                "ok": True,
                "event": event,
                "delivered": delivered,
                "active": len(results),
                "purged": purged,
                "results": results,
            },
        )


# ---------- entry ----------


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path}\n"
            f"copy config.example.toml -> config.toml + 填入 .p8 / Team ID / Key ID"
        )
    return tomllib.loads(path.read_text())


def cleanup_loop(state: ServerState, interval: float = 1800):
    """每 30 min cleanup stale tokens"""
    while True:
        try:
            time.sleep(interval)
            n = state.tokens.cleanup_stale()
            if n:
                logger.info("cleanup removed %d stale tokens", n)
        except Exception:
            logger.exception("cleanup loop error")


# ──────────────────────────────────────────────────────────────────────
# Claude session switch / rename (工具版 session 管理)
# ──────────────────────────────────────────────────────────────────────
#
# Unlike the deprecated /chain/* endpoints (which list tmux sessions and
# write active_session.json that nothing reads), these operate on the real
# Claude Code session jsonl files in CLAUDE_SESSION_DIR. The active session
# id lives in CCBOT_CURRENT_SESSION_FILE — that's the file
# /root/claude-telegram-start.sh reads to decide `claude --resume <sid>`.
# Switching = atomically write that file then restart claude-tg.service
# (same mechanical kill+resume forge-reload-claude relies on).

CLAUDE_SESSION_DIR = Path("/root/.claude/projects/-root")
CCBOT_CURRENT_SESSION_FILE = Path("/root/.ccbot/current-session")
CCBOT_CURRENT_MODEL_FILE = Path("/root/.ccbot/current-model")
SESSION_NAMES_FILE = Path("/root/.ccbot/session_names.json")
_SESSION_NAME_MAX_LEN = 24
# UUID-ish session id: hex + dashes only. Belt-and-suspenders so a sid never
# carries shell metacharacters even though we never build a shell string.
_SESSION_SID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _session_jsonl_path(sid: str) -> Path | None:
    """Return the jsonl Path for *sid* if it's a valid id and the file exists.

    Validates the sid shape and ensures the resolved path is a direct child of
    CLAUDE_SESSION_DIR (no traversal, no memory/ subdir)."""
    sid = (sid or "").strip()
    if not _SESSION_SID_RE.match(sid):
        return None
    p = CLAUDE_SESSION_DIR / f"{sid}.jsonl"
    try:
        if p.parent.resolve() != CLAUDE_SESSION_DIR.resolve():
            return None
    except Exception:
        return None
    if not p.is_file():
        return None
    return p


def _atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _load_session_names() -> dict[str, str]:
    """Read the {sid: name} alias table; tolerate missing/corrupt file."""
    try:
        if SESSION_NAMES_FILE.exists():
            data = json.loads(SESSION_NAMES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.warning("load session_names failed: %s", e)
    return {}


def _save_session_names(names: dict[str, str]) -> None:
    """Atomically persist the alias table."""
    _atomic_write_text(SESSION_NAMES_FILE, json.dumps(names, ensure_ascii=False, indent=0))


def _sanitize_session_name(raw: str) -> str | None:
    """Strip control chars, trim; return None if not 1..MAX chars after cleanup."""
    if raw is None:
        return None
    # Drop control characters (incl. newlines/tabs); keep normal printable text.
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    cleaned = cleaned.strip()
    if not cleaned or len(cleaned) > _SESSION_NAME_MAX_LEN:
        return None
    return cleaned


# ──────────────────────────────────────────────────────────────────────
# AI 状态文案 (chat header) — shared between app + sessions via ai_status.json
# ──────────────────────────────────────────────────────────────────────
AI_STATUS_FILE = Path("/root/CcCompanion/apns-server/tokens/ai_status.json")
_AI_STATUS_MAX_LEN = 60


def _sanitize_ai_status(raw: str) -> str | None:
    """Strip control chars + trim; return None if not 1.._AI_STATUS_MAX_LEN chars."""
    if raw is None:
        return None
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    cleaned = cleaned.strip()
    if not cleaned or len(cleaned) > _AI_STATUS_MAX_LEN:
        return None
    return cleaned


def _load_ai_status_map() -> dict[str, dict]:
    """Load the per-contact AI-status map from ai_status.json.

    New format: {"<contact_id>": {"text":..., "updated_at":...}, ...}
    Legacy format (read-compat): {"text":..., "updated_at":...} is treated as
    the xiaoke entry. Returns {} on missing/corrupt file.
    """
    try:
        if AI_STATUS_FILE.exists():
            data = json.loads(AI_STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # legacy single-status file → xiaoke
                if "text" in data and not any(
                    isinstance(v, dict) for v in data.values()
                ):
                    text = str(data.get("text") or "")
                    if not text:
                        return {}
                    return {"xiaoke": {"text": text, "updated_at": data.get("updated_at")}}
                # new per-contact map — keep only dict entries
                return {
                    str(k): v
                    for k, v in data.items()
                    if isinstance(v, dict)
                }
    except Exception as e:
        logger.warning("read ai_status failed: %s", e)
    return {}


def _read_ai_status(contact_id: str = "xiaoke") -> str:
    """Read the stored AI-status text for a contact; "" on missing/corrupt."""
    contact_id = (contact_id or "xiaoke").strip().lower() or "xiaoke"
    entry = _load_ai_status_map().get(contact_id)
    if isinstance(entry, dict):
        return str(entry.get("text") or "")
    return ""


def _write_ai_status(text: str, contact_id: str = "xiaoke") -> None:
    """Atomically persist the AI-status text for one contact (others kept)."""
    contact_id = (contact_id or "xiaoke").strip().lower() or "xiaoke"
    data = _load_ai_status_map()
    data[contact_id] = {
        "text": text,
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    _atomic_write_text(AI_STATUS_FILE, json.dumps(data, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────────
# statusbar 美化 (mirrors ccbot-lite's _format_status_pretty)
# ──────────────────────────────────────────────────────────────────────
CCBOT_MODEL_FILE = Path("/root/.ccbot/current-model")


def _read_ccbot_model_file() -> str:
    """Read the configured model id from ~/.ccbot/current-model (best-effort)."""
    try:
        if CCBOT_MODEL_FILE.exists():
            return CCBOT_MODEL_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _format_reset_beijing(reset_at: object) -> str:
    """Format a Claude statusline epoch value as Beijing-time 'MM-DD HH:MM' (no zone prefix)."""
    if reset_at is None:
        return ""
    try:
        reset_ts = float(reset_at)
    except (TypeError, ValueError):
        return ""
    utc8 = timezone(timedelta(hours=8))
    reset = datetime.fromtimestamp(reset_ts, timezone.utc).astimezone(utc8)
    return reset.strftime("%m-%d %H:%M")


def _status_bar_glyph(pct: float, width: int = 10) -> str:
    """Render a 0-100 percentage as a width-char █/░ progress bar."""
    try:
        filled = round(float(pct) / 100 * width)
    except (TypeError, ValueError):
        filled = 0
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _format_statusbar_from_json(data: dict) -> str:
    """Pretty-format the @claude-code-status-json payload (ccbot-lite style).

    Fields used (confirmed live):
      model.display_name / model.id
      context_window.used_percentage
      rate_limits.five_hour.used_percentage / .resets_at
      rate_limits.seven_day.used_percentage / .resets_at
    Returns "" when there's nothing renderable so the caller can fall back."""
    parts: list[str] = []
    model = data.get("model") or {}
    model_name = model.get("display_name") or model.get("id") or ""
    configured = _read_ccbot_model_file()
    if model_name:
        label = model_name
        if "[1m]" in configured.lower():
            label += " (1M)"
        parts.append(f"🤖 {label}")
    elif configured:
        parts.append(f"🤖 {configured}")

    ctx = data.get("context_window") or {}
    ctx_pct = ctx.get("used_percentage")
    if ctx_pct is not None:
        try:
            cp = float(ctx_pct)
            parts.append(f"📊 Ctx {_status_bar_glyph(cp)} {cp:.0f}%")
        except (TypeError, ValueError):
            pass

    rl = data.get("rate_limits") or {}
    five = rl.get("five_hour") or {}
    week = rl.get("seven_day") or {}
    five_pct = five.get("used_percentage")
    if five_pct is not None:
        try:
            fp = float(five_pct)
            reset = _format_reset_beijing(five.get("resets_at"))
            suffix = f" → {reset}" if reset else ""
            parts.append(f"⏱ 5h {_status_bar_glyph(fp)} {fp:.0f}%{suffix}")
        except (TypeError, ValueError):
            pass
    week_pct = week.get("used_percentage")
    if week_pct is not None:
        try:
            wp = float(week_pct)
            reset = _format_reset_beijing(week.get("resets_at"))
            suffix = f" → {reset}" if reset else ""
            parts.append(f"📅 7d {_status_bar_glyph(wp)} {wp:.0f}%{suffix}")
        except (TypeError, ValueError):
            pass

    return "\n".join(parts)


def _session_preview(path: Path) -> str:
    """Best-effort preview: last user/assistant text in the jsonl, first 40 chars.

    Reads the file tail (~64KB) and scans the last lines in reverse; tolerant of
    malformed lines."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 65536:
                f.seek(-65536, os.SEEK_END)
            chunk = f.read()
        lines = chunk.decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in reversed(lines[-80:]):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") not in ("user", "assistant"):
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        break
        text = " ".join(text.split())
        # Skip CC boilerplate / channel wrappers — they're not useful previews.
        if not text or text.startswith("<"):
            continue
        return text[:40]
    return ""


def _session_preview_messages(
    path: Path, limit: int = 6, max_chars: int = 80
) -> list[dict[str, str]]:
    """Return the last *limit* user/assistant text messages from a session jsonl.

    Reads the file tail (~64KB), scans lines newest→oldest collecting plain-text
    user/assistant messages (skips tool calls / system / channel-wrapper lines),
    truncates each to *max_chars*, then returns them oldest→newest as
    [{"role": "user"|"assistant", "text": ...}]. Tolerant of malformed lines —
    mirrors the tail-read + lenient-parse pattern used by _session_preview."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 65536:
                f.seek(-65536, os.SEEK_END)
            chunk = f.read()
        lines = chunk.decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    out: list[dict[str, str]] = []
    # Scan a generous window of recent lines newest-first until we have `limit`.
    for line in reversed(lines[-400:]):
        if len(out) >= limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        role = ev.get("type")
        if role not in ("user", "assistant"):
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    candidate = block.get("text", "")
                    if candidate.strip():
                        text = candidate
                        break
        text = " ".join(text.split())
        # Skip empties, CC boilerplate / channel wrappers, and tool-only turns.
        if not text or text.startswith("<"):
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        out.append({"role": role, "text": text})
    out.reverse()  # chronological order: oldest → newest
    return out


def _persist_active_session(state: "ServerState") -> None:
    """Write active_session.json for persistence across server restarts."""
    try:
        from datetime import datetime as _dt
        data = {"active_sid": state.active_session, "updated_at": _dt.now().isoformat(timespec="seconds")}
        state.active_session_path.write_text(json.dumps(data))
    except Exception as e:
        logger.warning("persist_active_session failed: %s", e)


def run_server(state: ServerState):
    # P0-1: refuse to bind to 0.0.0.0 unless allow_public_bind = true in config
    if state.host == "0.0.0.0" and not state.allow_public_bind:
        logger.error(
            "P0-1 SECURITY: bind=0.0.0.0 but allow_public_bind=false. "
            "Set allow_public_bind=true in config.toml only if you understand the exposure. "
            "Server not started."
        )
        raise SystemExit(1)
    PushHandler.state = state
    server = ThreadingHTTPServer((state.host, state.port), PushHandler)
    logger.info("listening on http://%s:%d", state.host, state.port)
    cleanup_thread = threading.Thread(
        target=cleanup_loop, args=(state,), daemon=True, name="cleanup"
    )
    cleanup_thread.start()
    # 小克·工具版 dispatcher — rule-driven scheduler injecting triggers into the
    # main session at scheduled times (no AI here; the session does the thinking).
    if getattr(state, "tool_schedule_enabled", False):
        try:
            state.tool_dispatcher.start()
        except Exception:
            logger.exception("failed to start tool dispatcher")
    else:
        logger.info("tool dispatcher disabled (tool_dispatcher_enabled=false)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("interrupt - shutting down")
    finally:
        server.shutdown()
        state.shutdown()


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--sandbox", action="store_true", help="force sandbox APNs")
    p.add_argument("--prod", action="store_true", help="force prod APNs")
    args = p.parse_args(argv)

    sandbox: bool | None = None
    if args.sandbox:
        sandbox = True
    elif args.prod:
        sandbox = False

    cfg = load_config(args.config)
    state = ServerState(cfg, sandbox_override=sandbox)
    run_server(state)


if __name__ == "__main__":
    main()
