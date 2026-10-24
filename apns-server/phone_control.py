"""手机操控任务队列 (2026-10-24, 配合 App v1.9.227 「Shizuku 手机操控」).

小克 (VPS 上的 Claude Code) 通过 ``phone-ctl`` 投递任务 → 本模块内存队列 →
方小南手机上的 CC Companion 前台服务长轮询 ``GET /phone/tasks/next`` 领任务 →
Shizuku UserService (uid 2000/shell) 执行 → 回传结果 (JSON + 可选二进制, 如截图)。

连接一律由手机发起 (换网络 / 走流量 / NAT 后都能用), 思路参考
susu-phone-agent (MIT, Copyright (c) 2026 2005selene2005-a11y) 的
"服务器任务队列 + 手机主动轮询 + Shizuku UserService"; 本文件为独立实现。

安全边界 (push.py 路由层负责鉴权, 本模块只管状态):
  * 所有 /phone/* 只认 native shared secret (X-Auth-Token), 网页 cookie 一律 401。
  * 投递 / 查结果 / 取截图 / 挂文件 只许本机 (loopback 且非 cloudflared/nginx 转发)。
  * 手机端另有总开关 + 分档权限, 是最终权威; 服务端只按手机上报的档位提前拒。

任务只存内存 (服务重启即清空); 二进制结果与待下发文件落 state 目录, 有 TTL 清理。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

logger = logging.getLogger("phone_control")

TIERS: tuple[str, ...] = ("status", "screen", "input", "files", "shell")
TIER_ZH = {
    "status": "查看状态",
    "screen": "截屏",
    "input": "点按输入",
    "files": "文件",
    "shell": "Shell",
}

# action → (tier, default timeout seconds, max timeout seconds)
ACTIONS: dict[str, tuple[str, int, int]] = {
    "status": ("status", 20, 60),
    "foreground": ("status", 20, 60),
    "apps": ("status", 30, 60),
    "open": ("input", 20, 60),
    "key": ("input", 15, 60),
    "tap": ("input", 15, 60),
    "swipe": ("input", 20, 60),
    "text": ("input", 20, 60),
    "screenshot": ("screen", 60, 120),
    "download": ("files", 3600, 4 * 3600),
    "ls": ("files", 20, 60),
    "stat": ("files", 20, 60),
    "pull": ("files", 900, 3600),
    "shell": ("shell", 60, 600),
}

# 键名 → Android KeyEvent keycode (手机端用同一张表, 见 PhoneControlPolicy.KEYS)
KEYS: dict[str, int] = {
    "home": 3,
    "back": 4,
    "recent": 187,
    "app_switch": 187,
    "power": 26,
    "menu": 82,
    "enter": 66,
    "delete": 67,
    "del": 67,
    "forward_del": 112,
    "tab": 61,
    "space": 62,
    "escape": 111,
    "search": 84,
    "notification": 83,
    "volume_up": 24,
    "volume_down": 25,
    "volume_mute": 164,
    "play_pause": 85,
    "media_next": 87,
    "media_previous": 88,
    "wakeup": 224,
    "sleep": 223,
    "dpad_up": 19,
    "dpad_down": 20,
    "dpad_left": 21,
    "dpad_right": 22,
    "paste": 279,
    "copy": 278,
    "cut": 277,
}

PHONE_PATH_ROOTS: tuple[str, ...] = ("/sdcard", "/storage/emulated/0")
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
TERMINAL = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_TIMEOUT, STATUS_CANCELLED})

CLAIM_TIMEOUT_S = 45          # 手机这么久没来领 → 判定不在线
RESULT_TTL_S = 15 * 60        # 结束后结果/截图保留多久
OUTBOX_TTL_S = 24 * 3600      # 挂出去待手机下载的文件最长保留
OUTBOX_AFTER_DONE_S = 10 * 60 # 下载任务结束后文件再留多久 (便于重下)
DEVICE_ONLINE_S = 120         # 最近多少秒内来轮询过算在线
MAX_POLL_WAIT_S = 55          # cloudflared 100s 闲置上限内
MAX_TASKS = 300
MAX_TEXT_CHARS = 2000
MAX_SHELL_CHARS = 4000
MAX_BLOB_BYTES = 128 * 1024 * 1024
MAX_STAGE_BYTES = 4 * 1024 * 1024 * 1024
MAX_RESULT_JSON_BYTES = 2 * 1024 * 1024
BLOB_ACTIONS = frozenset({"screenshot", "pull"})


class PhoneTaskError(Exception):
    """可直接回给调用方的错误 (status, code, 中文说明)。"""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


# ── 参数校验 (纯函数, 手机端 PhoneControlPolicy 同规则再校一次) ─────────────


def normalize_phone_path(raw: Any, *, allow_root: bool = True) -> str:
    """只允许 /sdcard 与 /storage/emulated/0 之下的绝对路径, 拒 .. / NUL / 控制字符。"""
    if not isinstance(raw, str):
        raise PhoneTaskError(400, "bad_path", "手机路径必须是字符串")
    path = raw.strip()
    if not path.startswith("/") or len(path) > 1024:
        raise PhoneTaskError(400, "bad_path", "手机路径必须是绝对路径（/sdcard/...）")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in path):
        raise PhoneTaskError(400, "bad_path", "手机路径含控制字符")
    if any(part == ".." for part in path.split("/")):
        raise PhoneTaskError(400, "bad_path", "手机路径不能含 ..")
    norm = posixpath.normpath(path)
    for root in PHONE_PATH_ROOTS:
        if norm == root:
            if allow_root:
                return norm
            raise PhoneTaskError(400, "bad_path", "这里需要具体文件路径，不能是存储根目录")
        if norm.startswith(root + "/"):
            return norm
    raise PhoneTaskError(400, "bad_path", "只允许 /sdcard/ 或 /storage/emulated/0/ 下的路径")


def _int_arg(args: dict[str, Any], key: str, lo: int, hi: int, default: int | None = None) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or value is None:
        raise PhoneTaskError(400, "bad_args", f"{key} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise PhoneTaskError(400, "bad_args", f"{key} 必须是整数") from None
    if isinstance(value, float) and value != number:
        raise PhoneTaskError(400, "bad_args", f"{key} 必须是整数")
    if not lo <= number <= hi:
        raise PhoneTaskError(400, "bad_args", f"{key} 超出范围 [{lo}, {hi}]")
    return number


def validate_task(action: Any, args: Any) -> dict[str, Any]:
    """返回规范化后的 args; 不认识的 action / 越界参数一律 400。"""
    if not isinstance(action, str) or action not in ACTIONS:
        raise PhoneTaskError(400, "unknown_action", f"不支持的操作: {action!r}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise PhoneTaskError(400, "bad_args", "args 必须是 JSON 对象")
    out: dict[str, Any] = {}
    if action in ("status", "foreground"):
        return out
    if action == "apps":
        out["third_party_only"] = bool(args.get("third_party_only", True))
        return out
    if action == "open":
        pkg = args.get("package")
        if not isinstance(pkg, str) or len(pkg) > 200 or not PKG_RE.match(pkg):
            raise PhoneTaskError(400, "bad_package", f"包名不合法: {pkg!r}")
        out["package"] = pkg
        return out
    if action == "key":
        key = str(args.get("key", "")).strip().lower().replace("-", "_")
        if key not in KEYS:
            raise PhoneTaskError(400, "bad_key", f"不认识的按键: {key!r}（可用: {', '.join(sorted(KEYS))}）")
        out["key"] = key
        out["keycode"] = KEYS[key]
        out["long_press"] = bool(args.get("long_press", False))
        return out
    if action == "tap":
        out["x"] = _int_arg(args, "x", 0, 20000)
        out["y"] = _int_arg(args, "y", 0, 20000)
        return out
    if action == "swipe":
        for key in ("x1", "y1", "x2", "y2"):
            out[key] = _int_arg(args, key, 0, 20000)
        out["duration_ms"] = _int_arg(args, "duration_ms", 1, 10000, default=300)
        return out
    if action == "text":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise PhoneTaskError(400, "bad_text", "text 不能为空")
        if len(text) > MAX_TEXT_CHARS:
            raise PhoneTaskError(400, "bad_text", f"text 最长 {MAX_TEXT_CHARS} 字")
        mode = str(args.get("mode", "auto"))
        if mode not in ("auto", "input", "paste"):
            raise PhoneTaskError(400, "bad_args", "mode 只能是 auto / input / paste")
        out["text"] = text
        out["mode"] = mode
        out["enter"] = bool(args.get("enter", False))
        return out
    if action == "screenshot":
        out["max_side"] = _int_arg(args, "max_side", 0, 8000, default=1280)
        out["quality"] = _int_arg(args, "quality", 10, 100, default=70)
        fmt = str(args.get("format", "jpeg")).lower()
        if fmt not in ("jpeg", "png"):
            raise PhoneTaskError(400, "bad_args", "format 只能是 jpeg / png")
        out["format"] = fmt
        return out
    if action in ("ls", "stat"):
        out["path"] = normalize_phone_path(args.get("path", "/sdcard"), allow_root=True)
        if action == "ls":
            out["limit"] = _int_arg(args, "limit", 1, 5000, default=500)
        return out
    if action == "pull":
        out["path"] = normalize_phone_path(args.get("path"), allow_root=False)
        out["max_bytes"] = _int_arg(args, "max_bytes", 1, MAX_BLOB_BYTES, default=MAX_BLOB_BYTES)
        return out
    if action == "download":
        file_id = args.get("file_id")
        if not isinstance(file_id, str) or not FILE_ID_RE.match(file_id):
            raise PhoneTaskError(400, "bad_file_id", "file_id 不合法（先 POST /phone/files 挂文件）")
        out["file_id"] = file_id
        out["dest"] = normalize_phone_path(args.get("dest"), allow_root=False)
        out["overwrite"] = bool(args.get("overwrite", False))
        return out
    if action == "shell":
        cmd = args.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise PhoneTaskError(400, "bad_cmd", "cmd 不能为空")
        if len(cmd) > MAX_SHELL_CHARS or "\x00" in cmd:
            raise PhoneTaskError(400, "bad_cmd", f"cmd 最长 {MAX_SHELL_CHARS} 字符")
        out["cmd"] = cmd
        out["timeout_s"] = _int_arg(args, "timeout_s", 1, 600, default=30)
        return out
    raise PhoneTaskError(400, "unknown_action", f"不支持的操作: {action!r}")  # pragma: no cover


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析单段 ``Range: bytes=a-b`` / ``bytes=a-`` / ``bytes=-n``。

    返回闭区间 (start, end); 无 Range 返回 None; 不可满足抛 PhoneTaskError(416)。
    多段 range 不支持 (按 RFC 可以忽略 Range 返回整份, 这里同样返回 None)。
    """
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].strip()
    if "," in spec:
        return None
    m = re.fullmatch(r"(\d*)-(\d*)", spec)
    if not m or (m.group(1) == "" and m.group(2) == ""):
        raise PhoneTaskError(416, "bad_range", "Range 格式不对")
    first, last = m.group(1), m.group(2)
    if first == "":
        suffix = int(last)
        if suffix <= 0:
            raise PhoneTaskError(416, "bad_range", "Range 不可满足")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
        end = min(end, size - 1)
    if size <= 0 or start >= size or start > end:
        raise PhoneTaskError(416, "range_not_satisfiable", "Range 不可满足")
    return start, end


class PhoneControlHub:
    """线程安全的内存任务队列 + 二进制结果 / 待下发文件的磁盘存放。"""

    def __init__(self, state_dir: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = Path(state_dir)
        self.blob_dir = self.state_dir / "phone_blobs"
        self.outbox_dir = self.state_dir / "phone_outbox"
        self.audit_path = self.state_dir / "phone_control_audit.jsonl"
        self._clock = clock
        self._cond = threading.Condition(threading.RLock())
        self._tasks: dict[str, dict[str, Any]] = {}
        self._outbox: dict[str, dict[str, Any]] = {}
        self._device: dict[str, Any] = {}
        self._pollers = 0
        for directory in (self.blob_dir, self.outbox_dir):
            # 任务只存内存: 上一次进程留下的截图 / 待下发文件都已无主, 清掉。
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    # ── 内部工具 ──────────────────────────────────────────────────────────

    def _now(self) -> float:
        return float(self._clock())

    def _public(self, task: dict[str, Any]) -> dict[str, Any]:
        view = {
            "id": task["id"],
            "action": task["action"],
            "tier": task["tier"],
            "args": task["args"],
            "status": task["status"],
            "created_at": task["created_at"],
            "claimed_at": task["claimed_at"],
            "finished_at": task["finished_at"],
            "timeout_s": task["timeout_s"],
            "result": task["result"],
            "error": task["error"],
            "message": task["message"],
            "progress": task["progress"],
        }
        blob = task.get("blob")
        view["blob"] = (
            {"size": blob["size"], "content_type": blob["content_type"], "sha256": blob["sha256"]}
            if blob
            else None
        )
        return view

    def _finish(self, task: dict[str, Any], status: str, *, error: str | None = None,
                message: str | None = None, result: Any = None) -> None:
        task["status"] = status
        task["finished_at"] = self._now()
        if error is not None:
            task["error"] = error
        if message is not None:
            task["message"] = message
        if result is not None:
            task["result"] = result
        if task["action"] == "download":
            entry = self._outbox.get(task["args"].get("file_id", ""))
            if entry is not None:
                entry["expires_at"] = min(entry["expires_at"], self._now() + OUTBOX_AFTER_DONE_S)
        self._audit(task)
        self._cond.notify_all()

    def _audit(self, task: dict[str, Any]) -> None:
        args = dict(task["args"])
        if "text" in args:
            args["text"] = f"<{len(args['text'])} 字>"
        if "cmd" in args:
            args["cmd"] = args["cmd"][:120]
        line = {
            "id": task["id"],
            "action": task["action"],
            "args": args,
            "status": task["status"],
            "error": task["error"],
            "created_at": round(task["created_at"], 3),
            "finished_at": round(task["finished_at"] or 0, 3),
        }
        try:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("phone audit write failed", exc_info=True)

    def _drop_blob(self, task: dict[str, Any]) -> None:
        blob = task.pop("blob", None)
        if blob:
            try:
                Path(blob["path"]).unlink()
            except OSError:
                pass

    def sweep(self) -> None:
        """超时 / 过期清理; 每次进出队列时顺手跑。"""
        with self._cond:
            now = self._now()
            for task in list(self._tasks.values()):
                status = task["status"]
                if status == STATUS_PENDING and now - task["created_at"] > task["claim_timeout_s"]:
                    self._finish(
                        task, STATUS_TIMEOUT, error="phone_not_claimed",
                        message="手机没来领任务：App 不在线 / 手机操控总开关没开 / 被系统杀了后台",
                    )
                elif status == STATUS_CLAIMED and now - (task["claimed_at"] or now) > task["timeout_s"]:
                    self._finish(task, STATUS_TIMEOUT, error="phone_no_result",
                                 message=f"手机领走后 {task['timeout_s']} 秒没回结果")
                elif status in TERMINAL and now - (task["finished_at"] or now) > RESULT_TTL_S:
                    self._drop_blob(task)
                    del self._tasks[task["id"]]
            for file_id, entry in list(self._outbox.items()):
                if now > entry["expires_at"]:
                    try:
                        Path(entry["path"]).unlink()
                    except OSError:
                        pass
                    del self._outbox[file_id]

    # ── 本机侧 (小克 / phone-ctl) ────────────────────────────────────────

    def enqueue(self, action: Any, args: Any, *, timeout_s: Any = None,
                claim_timeout_s: Any = None) -> dict[str, Any]:
        clean = validate_task(action, args)
        tier, default_timeout, max_timeout = ACTIONS[action]
        timeout = default_timeout
        if timeout_s is not None:
            timeout = _int_arg({"timeout_s": timeout_s}, "timeout_s", 1, max_timeout)
        if action == "shell":
            timeout = max(timeout, clean["timeout_s"] + 15)
        claim_timeout = CLAIM_TIMEOUT_S
        if claim_timeout_s is not None:
            claim_timeout = _int_arg({"claim_timeout_s": claim_timeout_s}, "claim_timeout_s", 1, 3600)
        with self._cond:
            self.sweep()
            device = self._device_view_locked()
            if device["online"] and device["caps"] is not None and tier not in device["caps"]:
                raise PhoneTaskError(
                    403, "tier_disabled",
                    f"手机端没开「{TIER_ZH[tier]}」权限（App → 设置 → 手机操控）",
                )
            if action == "download":
                entry = self._outbox.get(clean["file_id"])
                if entry is None:
                    raise PhoneTaskError(404, "file_not_found", "挂出的文件不存在或已过期")
                clean["url"] = f"/phone/files/{entry['id']}"
                clean["size"] = entry["size"]
                clean["sha256"] = entry["sha256"]
                clean["name"] = entry["name"]
            live = sum(1 for t in self._tasks.values() if t["status"] not in TERMINAL)
            if live >= 50 or len(self._tasks) >= MAX_TASKS:
                raise PhoneTaskError(429, "queue_full", "队列满了，等等手机先做完")
            task_id = secrets.token_hex(16)
            task = {
                "id": task_id,
                "action": action,
                "tier": tier,
                "args": clean,
                "status": STATUS_PENDING,
                "created_at": self._now(),
                "claimed_at": None,
                "finished_at": None,
                "timeout_s": timeout,
                "claim_timeout_s": claim_timeout,
                "result": None,
                "error": None,
                "message": None,
                "progress": None,
            }
            if action == "download":
                self._outbox[clean["file_id"]]["task_id"] = task_id
            self._tasks[task_id] = task
            self._cond.notify_all()
            view = self._public(task)
            view["device"] = device
            return view

    def get(self, task_id: str, *, wait_s: float = 0.0) -> dict[str, Any]:
        """取任务状态; wait_s>0 时阻塞到任务结束或超时 (长轮询)。"""
        deadline = time.monotonic() + max(0.0, min(float(wait_s), 60.0))
        with self._cond:
            while True:
                self.sweep()
                task = self._tasks.get(task_id)
                if task is None:
                    raise PhoneTaskError(404, "task_not_found", "任务不存在或已过期")
                if task["status"] in TERMINAL:
                    return self._public(task)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._public(task)
                self._cond.wait(timeout=min(remaining, 1.0))

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._cond:
            self.sweep()
            task = self._tasks.get(task_id)
            if task is None:
                raise PhoneTaskError(404, "task_not_found", "任务不存在或已过期")
            if task["status"] not in TERMINAL:
                self._finish(task, STATUS_CANCELLED, error="cancelled", message="已取消")
            return self._public(task)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._cond:
            self.sweep()
            return [self._public(t) for t in sorted(self._tasks.values(), key=lambda t: t["created_at"])]

    def blob(self, task_id: str) -> dict[str, Any]:
        with self._cond:
            self.sweep()
            task = self._tasks.get(task_id)
            if task is None or not task.get("blob"):
                raise PhoneTaskError(404, "blob_not_found", "没有二进制结果")
            return dict(task["blob"])

    def stage_file(self, source: str | os.PathLike[str], *, name: str | None = None) -> dict[str, Any]:
        """把本机文件复制进 outbox (边拷边算 sha256), 返回 {file_id, size, sha256, name, url}。"""
        src = Path(source)
        try:
            st = src.stat()
        except OSError as exc:
            raise PhoneTaskError(404, "source_not_found", f"本机文件不存在: {src}") from exc
        if not src.is_file():
            raise PhoneTaskError(400, "source_not_file", f"不是普通文件: {src}")
        if st.st_size > MAX_STAGE_BYTES:
            raise PhoneTaskError(413, "source_too_large", "文件太大（上限 4GB）")
        file_id = secrets.token_hex(16)
        target = self.outbox_dir / file_id
        tmp = self.outbox_dir / f".{file_id}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with src.open("rb") as fin, tmp.open("wb") as fout:
                while True:
                    chunk = fin.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    fout.write(chunk)
                    size += len(chunk)
            os.chmod(tmp, 0o600)
            tmp.replace(target)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise PhoneTaskError(500, "stage_failed", f"复制文件失败: {exc}") from exc
        display = (name or src.name).replace("/", "_")[:200] or file_id
        entry = {
            "id": file_id,
            "path": str(target),
            "size": size,
            "sha256": digest.hexdigest(),
            "name": display,
            "created_at": self._now(),
            "expires_at": self._now() + OUTBOX_TTL_S,
            "task_id": None,
        }
        with self._cond:
            self.sweep()
            self._outbox[file_id] = entry
        return {
            "file_id": file_id,
            "size": size,
            "sha256": entry["sha256"],
            "name": display,
            "url": f"/phone/files/{file_id}",
        }

    def device(self) -> dict[str, Any]:
        with self._cond:
            return self._device_view_locked()

    def _device_view_locked(self) -> dict[str, Any]:
        last_seen = self._device.get("last_seen")
        now = self._now()
        ago = None if last_seen is None else max(0, int(now - last_seen))
        return {
            "seen": last_seen is not None,
            "last_seen": last_seen,
            "seconds_ago": ago,
            "online": self._pollers > 0 or (ago is not None and ago <= DEVICE_ONLINE_S),
            "polling_now": self._pollers > 0,
            "caps": self._device.get("caps"),
            "shizuku": self._device.get("shizuku"),
            "app_version": self._device.get("app_version"),
            "shizuku_version": self._device.get("shizuku_version"),
        }

    # ── 手机侧 ────────────────────────────────────────────────────────────

    def note_device(self, *, caps: Any = None, shizuku: Any = None,
                    app_version: Any = None, shizuku_version: Any = None) -> None:
        with self._cond:
            info: dict[str, Any] = {"last_seen": self._now()}
            if isinstance(caps, str):
                info["caps"] = [c for c in caps.split(",") if c in TIERS]
            if isinstance(shizuku, str):
                info["shizuku"] = shizuku[:40]
            if isinstance(app_version, str):
                info["app_version"] = app_version[:40]
            if isinstance(shizuku_version, str):
                info["shizuku_version"] = shizuku_version[:20]
            self._device.update(info)

    def claim(self, *, wait_s: float = 0.0, caps: Any = None, shizuku: Any = None,
              app_version: Any = None, shizuku_version: Any = None) -> dict[str, Any] | None:
        """手机领最早的 pending 任务; 没有就最多等 wait_s 秒 (长轮询)。"""
        wait = max(0.0, min(float(wait_s), float(MAX_POLL_WAIT_S)))
        deadline = time.monotonic() + wait
        self.note_device(caps=caps, shizuku=shizuku, app_version=app_version,
                         shizuku_version=shizuku_version)
        with self._cond:
            self._pollers += 1
            try:
                while True:
                    self.sweep()
                    pending = [t for t in self._tasks.values() if t["status"] == STATUS_PENDING]
                    if pending:
                        task = min(pending, key=lambda t: t["created_at"])
                        task["status"] = STATUS_CLAIMED
                        task["claimed_at"] = self._now()
                        self._cond.notify_all()
                        return {
                            "id": task["id"],
                            "action": task["action"],
                            "tier": task["tier"],
                            "args": task["args"],
                            "timeout_s": task["timeout_s"],
                        }
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=min(remaining, 5.0))
            finally:
                self._pollers -= 1
                self._device["last_seen"] = self._now()

    def _claimed(self, task_id: str) -> dict[str, Any]:
        self.sweep()
        task = self._tasks.get(task_id)
        if task is None:
            raise PhoneTaskError(404, "task_not_found", "任务不存在或已过期")
        if task["status"] != STATUS_CLAIMED:
            raise PhoneTaskError(409, "task_not_claimed", f"任务状态是 {task['status']}，不再接收结果")
        return task

    def progress(self, task_id: str, done: Any, total: Any) -> dict[str, Any]:
        with self._cond:
            task = self._tasks.get(task_id)
            if task is not None and task["status"] in (STATUS_CANCELLED, STATUS_TIMEOUT):
                return {"ok": False, "cancelled": True, "status": task["status"]}
            task = self._claimed(task_id)
            try:
                done_i = max(0, int(done))
                total_i = max(0, int(total))
            except (TypeError, ValueError):
                raise PhoneTaskError(400, "bad_progress", "done/total 必须是整数") from None
            task["progress"] = {"done": done_i, "total": total_i, "at": self._now()}
            self._cond.notify_all()
            return {"ok": True, "cancelled": False}

    def put_blob(self, task_id: str, stream: BinaryIO, length: int, content_type: str) -> dict[str, Any]:
        """手机上传截图 / 拉回的文件 (原始 body), 边收边落盘边算 sha256。"""
        with self._cond:
            task = self._claimed(task_id)
            if task["action"] not in BLOB_ACTIONS:
                raise PhoneTaskError(400, "blob_not_expected", "这个任务没有二进制结果")
        if not 0 < length <= MAX_BLOB_BYTES:
            raise PhoneTaskError(413, "bad_length", "二进制结果大小不对（上限 128MB）")
        tmp = self.blob_dir / f".{task_id}.{secrets.token_hex(4)}.tmp"
        digest = hashlib.sha256()
        remaining = length
        try:
            with tmp.open("wb") as fout:
                while remaining > 0:
                    chunk = stream.read(min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    digest.update(chunk)
                    fout.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                raise PhoneTaskError(400, "short_body", "上传中断，body 不完整")
        except PhoneTaskError:
            tmp.unlink(missing_ok=True)
            raise
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise PhoneTaskError(500, "blob_save_failed", str(exc)) from exc
        ctype = (content_type or "application/octet-stream").split(";")[0].strip()[:100]
        with self._cond:
            try:
                task = self._claimed(task_id)
            except PhoneTaskError:
                tmp.unlink(missing_ok=True)
                raise
            self._drop_blob(task)
            final = self.blob_dir / f"{task_id}.bin"
            tmp.replace(final)
            task["blob"] = {
                "path": str(final),
                "size": length,
                "content_type": ctype or "application/octet-stream",
                "sha256": digest.hexdigest(),
            }
            return {"ok": True, "size": length, "sha256": task["blob"]["sha256"]}

    def submit_result(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._cond:
            task = self._claimed(task_id)
            ok = body.get("ok") is True
            result = body.get("result")
            error = body.get("error")
            message = body.get("message")
            if ok and task["action"] in BLOB_ACTIONS and not task.get("blob"):
                self._finish(task, STATUS_FAILED, error="missing_blob", message="手机说成功了但没传二进制结果")
                return {"ok": True, "status": task["status"]}
            if ok:
                self._finish(task, STATUS_DONE, result=result)
            else:
                self._finish(
                    task, STATUS_FAILED,
                    error=str(error or "failed")[:200],
                    message=str(message or error or "手机端执行失败")[:2000],
                    result=result,
                )
            return {"ok": True, "status": task["status"]}

    def outbox_file(self, file_id: str) -> dict[str, Any]:
        with self._cond:
            self.sweep()
            entry = self._outbox.get(file_id)
            if entry is None:
                raise PhoneTaskError(404, "file_not_found", "文件不存在或已过期")
            return dict(entry)
