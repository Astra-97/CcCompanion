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

v1.9.234 起手机端有第二个后端: Shizuku 不可用时自动改走 App 自己的 AccessibilityService
(点按/滑动/返回/Home/打开 App/截屏/前台 App/输入文字 + ui / clicktext); 文件 / Shell 仍只认
Shizuku。手机轮询带 a11y=on|enabled|off, /phone/device 给出 backend = shizuku/accessibility/none。

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

TIERS: tuple[str, ...] = ("status", "screen", "input", "files", "shell", "local")
TIER_ZH = {
    "status": "查看状态",
    "screen": "截屏",
    "input": "点按输入",
    "files": "文件",
    "shell": "Shell",
    "local": "本地能力",
}

# 「小克的本地能力」(2026-10-31, App v1.9.233): 不需要 Shizuku、App 只在后台开着通知
# 也能做的事。投递走同一条任务队列, 但手机端另有一条送达路: 入队时经
# /chat/stream?contacts=all 推 phone_task 事件 (RealtimeNotificationService 那条常驻 SSE),
# App 收到后 POST /phone/tasks/claim 按 id 领取 → 执行 → /result (+ 之后的 /followup)。
# 每个本地能力在手机端有自己的开关 (设置 → 小克的本地能力), 和「手机操控」总开关无关。
LOCAL_TIER = "local"
SSE_EVENT = "phone_task"  # /chat/stream?contacts=all 上的唤醒事件 {event, task_id, action, ts}
LOCAL_FEATURES: dict[str, str] = {
    "alarm": "设闹钟 / 计时器",
    "health": "健康数据",
    "calendar": "日历「小克日程」",
    "ring": "强提醒 / 找手机",
    "clip": "剪贴板",
    "widget": "桌面小部件",
    "notifs": "通知转发",
    "places": "到达提醒",
}
# action → feature
LOCAL_ACTION_FEATURE: dict[str, str] = {
    "alarm": "alarm",
    "timer": "alarm",
    "show_alarms": "alarm",
    "health": "health",
    "calendar_sync": "calendar",
    "ring": "ring",
    "ring_stop": "ring",
    "clip": "clip",
    "widget_refresh": "widget",
    "places": "places",
}
# 只保留最新一条待领的 action (旧的 pending 直接 superseded)
COALESCE_ACTIONS = frozenset({"calendar_sync", "widget_refresh"})
LOCAL_CLAIM_TIMEOUT_S = 90
LONG_CLAIM_TIMEOUT_S = {"calendar_sync": 30 * 60, "widget_refresh": 30 * 60}
MAX_CLIP_CHARS = 20000
MAX_MSG_CHARS = 200
MAX_CALENDAR_EVENTS = 600
MAX_FOLLOWUPS = 5
LOCAL_SEEN_FRESH_S = 7 * 24 * 3600
# 本地任务的结果留久一点: 闹钟「点一下确认」/ 强提醒「谁停的」这类后续可能隔很久才来
LOCAL_RESULT_TTL_S = 6 * 3600
FOLLOWUP_ACTIONS = frozenset({"alarm", "timer", "show_alarms", "ring"})
LOCAL_NOT_CLAIMED_MESSAGE = (
    "App 没来领本地任务：「实时提醒」没开 / App 被系统杀了后台 / App 版本低于 1.9.233"
)

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
    # ── 无障碍后端专属 (App v1.9.234): 读界面节点 / 按文字点 ──
    "ui": ("screen", 30, 90),
    "clicktext": ("input", 20, 60),
    # ── 本地能力 (不需要 Shizuku) ──
    "alarm": ("local", 30, 120),
    "timer": ("local", 30, 120),
    "show_alarms": ("local", 20, 60),
    "health": ("local", 60, 180),
    "calendar_sync": ("local", 120, 600),
    "ring": ("local", 20, 60),
    "ring_stop": ("local", 15, 60),
    "clip": ("local", 15, 60),
    "widget_refresh": ("local", 30, 120),
    "places": ("local", 15, 60),
}

# ── 操控后端 (App v1.9.234) ──────────────────────────────────────────────────
# Shizuku 不可用 (她的 OPPO/ColorOS 上 shell 拿不到 GRANT_RUNTIME_PERMISSIONS, 永远授权不了)
# 时, App 自动改走自己的 AccessibilityService: 点按/滑动 dispatchGesture、返回/Home/多任务
# performGlobalAction、打开 App 用 launch intent、截屏 takeScreenshot (Android 11+)、
# 前台 App 来自无障碍事件、输入文字 ACTION_SET_TEXT (兜底剪贴板 + ACTION_PASTE)。
# 文件 / Shell 只能 Shizuku; ui / clicktext 只能无障碍。
BACKENDS: tuple[str, ...] = ("shizuku", "accessibility", "none")
SHIZUKU_ONLY_ACTIONS = frozenset({"download", "ls", "stat", "pull", "shell"})
A11Y_ONLY_ACTIONS = frozenset({"ui", "clicktext"})
# Shizuku 的这些状态说明一时半会儿好不了 (binding / service_error 可能马上恢复, 不提前拒)
SHIZUKU_DOWN_STATES = frozenset({"not_installed", "not_running", "no_permission", "too_old"})
# 手机上报的无障碍状态: on=已连上; enabled=系统里开着但服务还没连上; off=没开; unsupported
A11Y_STATE_RE = re.compile(r"^[a-z_]{1,24}$")
A11Y_ZH = {
    "on": "已开启",
    "enabled": "系统里开着，服务还没连上（重开一次开关试试）",
    "off": "没开",
    "unsupported": "这台手机不支持",
}
NEEDS_SHIZUKU_MESSAGE = (
    "需要 Shizuku：文件传输 / 列目录 / 拉文件 / Shell 只能走 Shizuku，"
    "无障碍模式做不了（她的 ColorOS 上 Shizuku 授权不了）"
)
NEEDS_A11Y_MESSAGE = "需要无障碍服务：App → 设置 → 小克的本地能力 →「屏幕操作（无障碍）」去开"
MAX_UI_NODES = 1000
MAX_CLICKTEXT_CHARS = 200


def active_backend(shizuku: Any, accessibility: Any) -> str:
    """手机现在会用哪个后端执行点按/截屏类操作 (和 App 里 PhoneControlPolicy.chooseBackend 同规则)。"""
    if shizuku in ("ready", "binding"):
        return "shizuku"
    if accessibility == "on":
        return "accessibility"
    return "none"


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
    if action == "ui":
        out["max_nodes"] = _int_arg(args, "max_nodes", 1, MAX_UI_NODES, default=300)
        out["all"] = args.get("all") is True
        out["windows"] = args.get("windows") is True
        return out
    if action == "clicktext":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise PhoneTaskError(400, "bad_text", "text 不能为空")
        text = text.strip()
        if len(text) > MAX_CLICKTEXT_CHARS or any(ord(ch) < 0x20 for ch in text):
            raise PhoneTaskError(400, "bad_text", f"text 最长 {MAX_CLICKTEXT_CHARS} 字、不能含控制字符")
        out["text"] = text
        out["index"] = _int_arg(args, "index", 0, 99, default=0)
        out["exact"] = args.get("exact") is True
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
    if action in LOCAL_ACTION_FEATURE:
        return _validate_local(action, args)
    raise PhoneTaskError(400, "unknown_action", f"不支持的操作: {action!r}")  # pragma: no cover


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _opt_text(args: dict[str, Any], key: str, limit: int) -> str:
    value = args.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PhoneTaskError(400, "bad_args", f"{key} 必须是字符串")
    value = value.strip()
    if len(value) > limit or "\x00" in value:
        raise PhoneTaskError(400, "bad_args", f"{key} 最长 {limit} 字")
    return value


def _valid_date(raw: Any, key: str = "date") -> str:
    if not isinstance(raw, str) or not _DATE_RE.match(raw):
        raise PhoneTaskError(400, "bad_args", f"{key} 必须是 YYYY-MM-DD")
    try:
        time.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise PhoneTaskError(400, "bad_args", f"{key} 不是有效日期") from None
    return raw


def normalize_days(raw: Any) -> list[int]:
    """闹钟重复日: ISO 星期 1=周一 … 7=周日 (手机端换算成 Calendar.SUNDAY=1 那套)。"""
    if raw is None or raw == [] or raw == "":
        return []
    if isinstance(raw, str):
        alias = raw.strip().lower()
        if alias in ("workdays", "weekdays", "工作日"):
            return [1, 2, 3, 4, 5]
        if alias in ("weekend", "weekends", "周末"):
            return [6, 7]
        if alias in ("daily", "everyday", "每天"):
            return [1, 2, 3, 4, 5, 6, 7]
        raw = [p for p in re.split(r"[,，\s]+", alias) if p]
    if not isinstance(raw, list):
        raise PhoneTaskError(400, "bad_days", "days 必须是 1-7 的列表（1=周一）")
    out: set[int] = set()
    for item in raw:
        try:
            n = int(item)
        except (TypeError, ValueError):
            raise PhoneTaskError(400, "bad_days", f"days 里有非数字: {item!r}") from None
        if isinstance(item, bool) or not 1 <= n <= 7:
            raise PhoneTaskError(400, "bad_days", "days 只能是 1-7（1=周一，7=周日）")
        out.add(n)
    return sorted(out)


def normalize_calendar_event(raw: Any) -> dict[str, Any] | None:
    """schedule-ctl events.json 的一条 → 下发给手机的最小字段; 坏条目返回 None (跳过不报错)。"""
    if not isinstance(raw, dict):
        return None
    event_id = raw.get("id")
    title = raw.get("title")
    date = raw.get("date")
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(date, str) or not _DATE_RE.match(date):
        return None
    hhmm = raw.get("time")
    if not (isinstance(hhmm, str) and _HHMM_RE.match(hhmm)):
        hhmm = None
    remind = raw.get("remind_minutes_before")
    if isinstance(remind, bool) or not isinstance(remind, int) or not 0 <= remind <= 7 * 24 * 60:
        remind = None
    note = raw.get("note") if isinstance(raw.get("note"), str) else ""
    return {
        "id": event_id,
        "title": title.strip()[:200],
        "date": date,
        "time": hhmm,
        "remind_minutes_before": remind,
        "note": note[:2000],
        "done": raw.get("done") is True,
    }


def _validate_local(action: str, args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"feature": LOCAL_ACTION_FEATURE[action]}
    if action == "alarm":
        out["hour"] = _int_arg(args, "hour", 0, 23)
        out["minutes"] = _int_arg(args, "minutes", 0, 59)
        out["message"] = _opt_text(args, "message", MAX_MSG_CHARS)
        out["days"] = normalize_days(args.get("days"))
        out["skip_ui"] = args.get("skip_ui", True) is not False
        if "vibrate" in args:
            out["vibrate"] = bool(args.get("vibrate"))
        return out
    if action == "timer":
        out["seconds"] = _int_arg(args, "seconds", 1, 24 * 3600)
        out["message"] = _opt_text(args, "message", MAX_MSG_CHARS)
        out["skip_ui"] = args.get("skip_ui", True) is not False
        return out
    if action == "health":
        out["date"] = _valid_date(args.get("date"))
        out["days"] = _int_arg(args, "days", 1, 31, default=1)
        return out
    if action == "calendar_sync":
        events = args.get("events")
        if not isinstance(events, list):
            raise PhoneTaskError(400, "bad_events", "events 必须是列表")
        clean = [e for e in (normalize_calendar_event(x) for x in events) if e is not None]
        if len(clean) > MAX_CALENDAR_EVENTS:
            clean = sorted(clean, key=lambda e: (e["date"], e["time"] or ""))[-MAX_CALENDAR_EVENTS:]
        out["events"] = clean
        out["window_start"] = _valid_date(args.get("window_start"), "window_start")
        return out
    if action == "ring":
        out["message"] = _opt_text(args, "message", MAX_MSG_CHARS)
        out["seconds"] = _int_arg(args, "seconds", 5, 600, default=60)
        out["volume_pct"] = _int_arg(args, "volume_pct", 10, 100, default=80)
        return out
    if action == "clip":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise PhoneTaskError(400, "bad_text", "text 不能为空")
        if len(text) > MAX_CLIP_CHARS or "\x00" in text:
            raise PhoneTaskError(400, "bad_text", f"text 最长 {MAX_CLIP_CHARS} 字")
        out["text"] = text
        out["notify"] = args.get("notify", True) is not False
        return out
    # show_alarms / ring_stop / widget_refresh / places: 无参数
    return out


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

    def __init__(self, state_dir: str | Path, *, clock: Callable[[], float] = time.time,
                 calendar_loader: Callable[[], dict[str, Any]] | None = None,
                 on_local_enqueue: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.state_dir = Path(state_dir)
        # calendar_sync 没带 events 时从这里取 (push.py 接 /root/schedule/events.json)
        self.calendar_loader = calendar_loader
        # 本地能力任务入队后的送达钩子 (push.py: 推 phone_task SSE 事件)
        self.on_local_enqueue = on_local_enqueue
        self._local: dict[str, Any] = {}
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
            "followup": list(task.get("followup") or []),
            "claimed_via": task.get("claimed_via"),
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
        if "events" in args:
            args["events"] = f"<{len(args['events'])} 条>"
        if args.get("message"):
            args["message"] = f"<{len(args['message'])} 字>"
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
                        message=(LOCAL_NOT_CLAIMED_MESSAGE if task["tier"] == LOCAL_TIER else
                                 "手机没来领任务：App 不在线 / 手机操控总开关没开 / 被系统杀了后台"),
                    )
                elif status == STATUS_CLAIMED and now - (task["claimed_at"] or now) > task["timeout_s"]:
                    self._finish(task, STATUS_TIMEOUT, error="phone_no_result",
                                 message=f"手机领走后 {task['timeout_s']} 秒没回结果")
                elif status in TERMINAL and now - (task["finished_at"] or now) > (
                        LOCAL_RESULT_TTL_S if task["action"] in FOLLOWUP_ACTIONS else RESULT_TTL_S):
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
        if action == "calendar_sync" and args is None:
            args = {}
        if action == "calendar_sync" and isinstance(args, dict) and "events" not in args:
            if self.calendar_loader is None:
                raise PhoneTaskError(503, "no_calendar_source", "服务端没配置日程来源")
            args = {**args, **self.calendar_loader()}
        clean = validate_task(action, args)
        tier, default_timeout, max_timeout = ACTIONS[action]
        timeout = default_timeout
        if timeout_s is not None:
            timeout = _int_arg({"timeout_s": timeout_s}, "timeout_s", 1, max_timeout)
        if action == "shell":
            timeout = max(timeout, clean["timeout_s"] + 15)
        claim_timeout = CLAIM_TIMEOUT_S
        if tier == LOCAL_TIER:
            claim_timeout = LONG_CLAIM_TIMEOUT_S.get(action, LOCAL_CLAIM_TIMEOUT_S)
        if claim_timeout_s is not None:
            claim_timeout = _int_arg({"claim_timeout_s": claim_timeout_s}, "claim_timeout_s", 1, 3600)
        with self._cond:
            self.sweep()
            device = self._device_view_locked()
            if tier == LOCAL_TIER:
                feature = LOCAL_ACTION_FEATURE[action]
                reported = (self._local.get("features") or {}).get(feature)
                if isinstance(reported, dict) and reported.get("enabled") is False and device["local"]["fresh"]:
                    raise PhoneTaskError(
                        403, "feature_disabled",
                        f"手机端关着「{LOCAL_FEATURES[feature]}」（App → 设置 → 小克的本地能力）",
                    )
                if action in COALESCE_ACTIONS:
                    for old in self._tasks.values():
                        if old["action"] == action and old["status"] == STATUS_PENDING:
                            self._finish(old, STATUS_CANCELLED, error="superseded",
                                         message="被更新的同类任务取代")
            elif device["online"] and device["caps"] is not None and tier not in device["caps"]:
                raise PhoneTaskError(
                    403, "tier_disabled",
                    f"手机端没开「{TIER_ZH[tier]}」权限（App → 设置 → 手机操控）",
                )
            elif device["online"] and action in SHIZUKU_ONLY_ACTIONS and device["shizuku"] in SHIZUKU_DOWN_STATES:
                raise PhoneTaskError(409, "needs_shizuku", NEEDS_SHIZUKU_MESSAGE)
            elif (device["online"] and action in A11Y_ONLY_ACTIONS and device["accessibility"] is not None
                  and device["accessibility"] != "on"):
                raise PhoneTaskError(409, "needs_accessibility", NEEDS_A11Y_MESSAGE)
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
                "followup": [],
                "claimed_via": None,
            }
            if action == "download":
                self._outbox[clean["file_id"]]["task_id"] = task_id
            self._tasks[task_id] = task
            self._cond.notify_all()
            view = self._public(task)
            view["device"] = device
        if tier == LOCAL_TIER and self.on_local_enqueue is not None:
            try:
                view["stream_listeners"] = self.on_local_enqueue(self._public(task))
            except Exception:
                logger.warning("local task wake hook failed", exc_info=True)
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
        online = self._pollers > 0 or (ago is not None and ago <= DEVICE_ONLINE_S)
        caps = self._device.get("caps")
        shizuku = self._device.get("shizuku")
        accessibility = self._device.get("accessibility")
        # 手机操控总开关关着时轮询停了; 无障碍状态退而看本地能力那条通道最近一次上报
        a11y_any = accessibility or self._local.get("accessibility")
        if not online:
            backend, reason = "none", "手机操控没在线（总开关没开 / App 被杀后台）"
        elif not caps:
            backend, reason = "none", "手机操控总开关关着"
        else:
            backend = active_backend(shizuku, accessibility)
            reason = {
                "shizuku": "Shizuku 就绪",
                "accessibility": "Shizuku 不可用，改走无障碍服务",
            }.get(backend, "Shizuku 不可用，无障碍服务也没开")
        return {
            "seen": last_seen is not None,
            "last_seen": last_seen,
            "seconds_ago": ago,
            "online": online,
            "polling_now": self._pollers > 0,
            "caps": caps,
            "shizuku": shizuku,
            "accessibility": accessibility,
            "accessibility_any": a11y_any,
            "backend": backend,
            "backend_reason": reason,
            "app_version": self._device.get("app_version"),
            "shizuku_version": self._device.get("shizuku_version"),
            "local": self._local_view_locked(now),
        }

    def _local_view_locked(self, now: float) -> dict[str, Any]:
        seen = self._local.get("last_seen")
        ago = None if seen is None else max(0, int(now - seen))
        return {
            "seen": seen is not None,
            "seconds_ago": ago,
            "fresh": ago is not None and ago <= LOCAL_SEEN_FRESH_S,
            "app_version": self._local.get("app_version"),
            "features": self._local.get("features"),
        }

    @staticmethod
    def _clean_features(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        out: dict[str, Any] = {}
        for name, value in raw.items():
            if name not in LOCAL_FEATURES or not isinstance(value, dict):
                continue
            item: dict[str, Any] = {"enabled": value.get("enabled") is True}
            missing = value.get("missing")
            if isinstance(missing, list):
                item["missing"] = [str(m)[:80] for m in missing[:12]]
            note = value.get("note")
            if isinstance(note, str) and note:
                item["note"] = note[:200]
            out[name] = item
        return out

    @staticmethod
    def _clean_a11y(raw: Any) -> str | None:
        return raw if isinstance(raw, str) and A11Y_STATE_RE.match(raw) else None

    def note_local(self, *, app_version: Any = None, features: Any = None,
                   accessibility: Any = None) -> None:
        with self._cond:
            self._local["last_seen"] = self._now()
            if isinstance(app_version, str):
                self._local["app_version"] = app_version[:40]
            a11y = self._clean_a11y(accessibility)
            if a11y is not None:
                self._local["accessibility"] = a11y
            clean = self._clean_features(features)
            if clean is not None:
                self._local["features"] = clean

    # ── 手机侧 ────────────────────────────────────────────────────────────

    def note_device(self, *, caps: Any = None, shizuku: Any = None,
                    app_version: Any = None, shizuku_version: Any = None,
                    accessibility: Any = None) -> None:
        with self._cond:
            info: dict[str, Any] = {"last_seen": self._now()}
            a11y = self._clean_a11y(accessibility)
            if a11y is not None:
                info["accessibility"] = a11y
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
              app_version: Any = None, shizuku_version: Any = None,
              include_local: bool = False, accessibility: Any = None) -> dict[str, Any] | None:
        """手机领最早的 pending 任务; 没有就最多等 wait_s 秒 (长轮询)。"""
        wait = max(0.0, min(float(wait_s), float(MAX_POLL_WAIT_S)))
        deadline = time.monotonic() + wait
        self.note_device(caps=caps, shizuku=shizuku, app_version=app_version,
                         shizuku_version=shizuku_version, accessibility=accessibility)
        with self._cond:
            self._pollers += 1
            try:
                while True:
                    self.sweep()
                    # 老 App 的轮询不认识本地能力; 新 App 带 local=1 才顺手领本地任务。
                    pending = [t for t in self._tasks.values() if t["status"] == STATUS_PENDING
                               and (include_local or t["tier"] != LOCAL_TIER)]
                    if pending:
                        task = min(pending, key=lambda t: t["created_at"])
                        return self._mark_claimed(task, "poll")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(timeout=min(remaining, 5.0))
            finally:
                self._pollers -= 1
                self._device["last_seen"] = self._now()

    def _mark_claimed(self, task: dict[str, Any], via: str) -> dict[str, Any]:
        task["status"] = STATUS_CLAIMED
        task["claimed_at"] = self._now()
        task["claimed_via"] = via
        self._cond.notify_all()
        return {
            "id": task["id"],
            "action": task["action"],
            "tier": task["tier"],
            "args": task["args"],
            "timeout_s": task["timeout_s"],
        }

    def claim_local(self, *, task_id: Any = None, app_version: Any = None,
                    features: Any = None, accessibility: Any = None) -> dict[str, Any] | None:
        """App 收到 phone_task 事件 / SSE 重连后领本地能力任务 (不需要 Shizuku, 不阻塞)。

        task_id 给了就只领那一条 (已被领 / 不存在返回 None); 没给就领最早的一条本地任务。
        顺带记下 App 上报的本地能力开关状态 (投递前预检用)。
        """
        self.note_local(app_version=app_version, features=features, accessibility=accessibility)
        with self._cond:
            self.sweep()
            if task_id is not None:
                if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
                    raise PhoneTaskError(400, "bad_task_id", "task_id 不合法")
                task = self._tasks.get(task_id)
                if task is None or task["status"] != STATUS_PENDING or task["tier"] != LOCAL_TIER:
                    return None
                return self._mark_claimed(task, "stream")
            pending = [t for t in self._tasks.values()
                       if t["status"] == STATUS_PENDING and t["tier"] == LOCAL_TIER]
            if not pending:
                return None
            return self._mark_claimed(min(pending, key=lambda t: t["created_at"]), "stream")

    def add_followup(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """结果交了之后的后续 (她点了确认通知 / 强提醒被谁停了), 最多 5 条。"""
        with self._cond:
            self.sweep()
            task = self._tasks.get(task_id)
            if task is None:
                raise PhoneTaskError(404, "task_not_found", "任务不存在或已过期")
            if task["tier"] != LOCAL_TIER:
                raise PhoneTaskError(400, "followup_not_expected", "只有本地能力任务有后续")
            followups = task.setdefault("followup", [])
            if len(followups) >= MAX_FOLLOWUPS:
                raise PhoneTaskError(429, "too_many_followups", "后续太多了")
            entry: dict[str, Any] = {"at": self._now()}
            for key in ("event", "ok", "error", "message", "state"):
                value = body.get(key)
                if isinstance(value, (str, bool)):
                    entry[key] = value[:500] if isinstance(value, str) else value
            result = body.get("result")
            if isinstance(result, dict) and len(json.dumps(result)) <= 8192:
                entry["result"] = result
            followups.append(entry)
            self._cond.notify_all()
            return {"ok": True, "count": len(followups)}

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
