"""应用使用感知（2026-09-27 v4）：被监 App 开/关气泡 + AI 查询用最新快照。

- App 上报被勾应用的开/关事件（POST /device/app-event），这里做归一化与
  气泡判定；事件经 push.py 进小克会话。
- 2026-10-25 去抖动：游戏启动时前台会短暂切走（闪屏/加载页/系统弹层），
  客户端会报 open→close→open。close 不再立刻冒泡，而是挂起
  APP_CLOSE_CONFIRM_SECONDS 秒：期间同包名又 open → 取消这条 close，这次
  open 也不冒泡（她从没真正离开）；到期仍没 open → 由 push.py 的后台
  sweeper 调 pop_due_closes() 补发 close（文案带真实关闭时间）。open 在
  「已认为在前台」时不重复冒泡，否则照常冒泡（保留 60 秒同类型防抖兜底）。
  挂起状态落盘；重启后由 sweeper 照常到期补发，若已过期超过
  APP_CLOSE_STALE_DROP_SECONDS（服务停机太久）则静默丢弃、只标记为已关闭。
- App 在电池/应用事件上报或轻量心跳（POST /device/activity）里捎带
  「当前前台 App + 最近窗口前台 Top N」快照；只存最新值
  （tokens/device_activity.json），AI 查询（GET /device/status）读它，
  App 不在线就给最近已知值 + 时间戳。
- 只当下用：不留历史、不进日记/健康页/统计。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

APP_EVENT_OPEN = "open"
APP_EVENT_CLOSE = "close"
APP_EVENTS = frozenset({APP_EVENT_OPEN, APP_EVENT_CLOSE})
# 同一 App 同一类型（open/close 分别计）相邻气泡的最小间隔（防快速切换刷屏）。
APP_EVENT_BUBBLE_DEBOUNCE_SECONDS = 60.0
# close 延迟确认窗口：这段时间内同包名重新 open 视为「没离开过」。
APP_CLOSE_CONFIRM_SECONDS = 90.0
# 到期的挂起 close 超过「确认窗口 + 这个值」还没发出（服务停机过久）就丢弃。
APP_CLOSE_STALE_DROP_SECONDS = 600.0
# 「在前台」状态的最长驻留：防客户端漏报 close 导致之后的 open 永远被吞。
APP_FOREGROUND_STALE_SECONDS = 6 * 3600.0
# 防抖表的驻留上限：超过 10 分钟未再事件的包名清出防抖表（防抖表只是
# 瞬态状态，不是历史记录）。
_APP_EVENT_DEBOUNCE_KEEP_SECONDS = 600.0
MAX_APP_LABEL_CHARS = 60
MAX_PACKAGE_CHARS = 128
MAX_USAGE_TOP_ENTRIES = 10
MAX_WINDOW_MINUTES = 24 * 60


def normalize_app_event(value: object) -> dict | None:
    """校验应用开/关事件：{"event": open|close, "package": str, "label": str}。"""

    if not isinstance(value, dict):
        return None
    event = str(value.get("event") or "").strip().lower()
    if event not in APP_EVENTS:
        return None
    package = str(value.get("package") or "").strip()
    if not package or len(package) > MAX_PACKAGE_CHARS or any(c in package for c in "\r\n\0"):
        return None
    label = str(value.get("label") or "").strip()[:MAX_APP_LABEL_CHARS] or package
    label = label.replace("\r", " ").replace("\n", " ").replace("\0", "")
    return {"event": event, "package": package, "label": label}


def _normalize_usage_entry(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    package = str(value.get("package") or "").strip()
    if not package or len(package) > MAX_PACKAGE_CHARS or any(c in package for c in "\r\n\0"):
        return None
    label = str(value.get("label") or "").strip()[:MAX_APP_LABEL_CHARS] or package
    label = label.replace("\r", " ").replace("\n", " ").replace("\0", "")
    try:
        minutes = float(value.get("minutes") or 0.0)
    except (TypeError, ValueError):
        return None
    if minutes < 0 or minutes > MAX_WINDOW_MINUTES:
        return None
    return {"package": package, "label": label, "minutes": round(minutes, 1)}


def normalize_activity_snapshot(value: object) -> dict | None:
    """校验活动快照：{"foreground": {...}|None, "usage_top": [...], "window_minutes": int}。

    foreground 形如 {"package", "label"}；usage_top 最多 10 条按分钟数降序。
    """

    if not isinstance(value, dict):
        return None
    foreground_raw = value.get("foreground")
    foreground = None
    if isinstance(foreground_raw, dict):
        package = str(foreground_raw.get("package") or "").strip()
        if package and len(package) <= MAX_PACKAGE_CHARS and not any(c in package for c in "\r\n\0"):
            label = str(foreground_raw.get("label") or "").strip()[:MAX_APP_LABEL_CHARS] or package
            foreground = {"package": package, "label": label.replace("\r", " ").replace("\n", " ")}
    usage_top: list[dict] = []
    raw_top = value.get("usage_top")
    if isinstance(raw_top, list):
        for item in raw_top[:MAX_USAGE_TOP_ENTRIES]:
            entry = _normalize_usage_entry(item)
            if entry is not None:
                usage_top.append(entry)
        usage_top.sort(key=lambda item: item["minutes"], reverse=True)
    raw_window = value.get("window_minutes")
    try:
        window_minutes = int(raw_window) if raw_window is not None else 60  # type: ignore[arg-type]
    except (TypeError, ValueError):
        window_minutes = 60
    window_minutes = max(1, min(MAX_WINDOW_MINUTES, window_minutes))
    return {
        "foreground": foreground,
        "usage_top": usage_top,
        "window_minutes": window_minutes,
    }


class DeviceActivityStore:
    """最新活动快照 + 应用事件气泡防抖位，原子落盘，只存最新值。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._updated_at = 0.0
        self._last_bubble_at: dict[str, float] = {}
        # 包名 -> {"label", "foreground": bool, "seen_at": float,
        #          "pending_close_at": float|None}
        self._apps: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        self._snapshot = normalize_activity_snapshot(raw.get("snapshot"))
        try:
            self._updated_at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            self._updated_at = 0.0
        last = raw.get("last_bubble_at")
        if isinstance(last, dict):
            now = time.time()
            for key, ts in last.items():
                try:
                    ts_value = float(ts)
                except (TypeError, ValueError):
                    continue
                if isinstance(key, str) and key and now - ts_value <= _APP_EVENT_DEBOUNCE_KEEP_SECONDS:
                    self._last_bubble_at[key] = ts_value
        apps = raw.get("apps")
        if isinstance(apps, dict):
            for package, item in apps.items():
                entry = self._normalize_app_state(package, item)
                if entry is not None:
                    self._apps[package] = entry

    @staticmethod
    def _normalize_app_state(package: object, item: object) -> dict | None:
        if not isinstance(package, str) or not package or len(package) > MAX_PACKAGE_CHARS:
            return None
        if not isinstance(item, dict):
            return None
        try:
            seen_at = float(item.get("seen_at") or 0.0)
            pending_raw = item.get("pending_close_at")
            pending = None if pending_raw is None else float(pending_raw)
        except (TypeError, ValueError):
            return None
        foreground = item.get("foreground") is True
        if not foreground and pending is None:
            return None
        label = str(item.get("label") or "").strip()[:MAX_APP_LABEL_CHARS] or package
        return {
            "label": label,
            "foreground": foreground,
            "seen_at": seen_at,
            "pending_close_at": pending,
        }
    def _persist_locked(self) -> None:
        payload = {
            "snapshot": self._snapshot,
            "updated_at": self._updated_at,
            "last_bubble_at": self._last_bubble_at,
            "apps": self._apps,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "snapshot": self._snapshot,
                "updated_at": self._updated_at,
            }

    def record_snapshot(self, value: dict) -> dict:
        """写入最新快照（心跳/捎带），返回落盘后的最新状态。"""

        normalized = normalize_activity_snapshot(value)
        if normalized is None:
            raise ValueError("invalid activity snapshot")
        with self._lock:
            self._snapshot = normalized
            self._updated_at = time.time()
            self._persist_locked()
            return {"snapshot": self._snapshot, "updated_at": self._updated_at}

    def _debounce_ok_locked(self, key: str, now: float) -> bool:
        # 顺手清掉早已过期的防抖位，表保持有界。
        for stale in [k for k, ts in self._last_bubble_at.items() if now - ts > _APP_EVENT_DEBOUNCE_KEEP_SECONDS]:
            del self._last_bubble_at[stale]
        last = self._last_bubble_at.get(key)
        return last is None or now - last >= APP_EVENT_BUBBLE_DEBOUNCE_SECONDS

    def record_app_event(self, event: dict, *, now: float | None = None) -> bool:
        """登记一次开/关事件，返回这次是否应**立刻**冒泡。

        - close：永远不立刻冒泡，挂起等 APP_CLOSE_CONFIRM_SECONDS 秒确认
          （重复 close 保留最早的关闭时间），到期由 pop_due_closes() 交出。
        - open：有挂起 close → 取消它且不冒泡；已认为在前台 → 不冒泡；
          否则按 包名|open 60 秒防抖兜底后冒泡。
        """

        normalized = normalize_app_event(event)
        if normalized is None:
            raise ValueError("invalid app event")
        now = time.time() if now is None else float(now)
        package = normalized["package"]
        with self._lock:
            state = self._apps.get(package)
            if state is None:
                state = {"label": normalized["label"], "foreground": False,
                         "seen_at": now, "pending_close_at": None}
                self._apps[package] = state
            state["label"] = normalized["label"]
            should_bubble = False
            if normalized["event"] == APP_EVENT_CLOSE:
                if state["pending_close_at"] is None:
                    state["pending_close_at"] = now
                state["seen_at"] = now
            else:
                if state["pending_close_at"] is not None:
                    # 闪退回来：撤销 close，她从没真正离开。
                    state["pending_close_at"] = None
                    state["foreground"] = True
                elif state["foreground"] and now - state["seen_at"] < APP_FOREGROUND_STALE_SECONDS:
                    pass  # 已在前台，不重复冒泡
                else:
                    debounce_key = f"{package}|{APP_EVENT_OPEN}"
                    should_bubble = self._debounce_ok_locked(debounce_key, now)
                    if should_bubble:
                        self._last_bubble_at[debounce_key] = now
                    state["foreground"] = True
                state["seen_at"] = now
            self._persist_locked()
            return should_bubble

    def has_pending_closes(self) -> bool:
        with self._lock:
            return any(s["pending_close_at"] is not None for s in self._apps.values())

    def pop_due_closes(self, *, now: float | None = None) -> list[dict]:
        """取出确认窗口已到期的挂起 close，返回应冒泡的列表。

        每项 {"package", "label", "closed_at"}，closed_at 是真实关闭时间。
        过期太久（停机）的静默丢弃；同包名 60 秒内已冒过 close 的也不再冒。
        """

        now = time.time() if now is None else float(now)
        due: list[dict] = []
        with self._lock:
            changed = False
            for package in list(self._apps):
                state = self._apps[package]
                closed_at = state["pending_close_at"]
                if closed_at is None or now - closed_at < APP_CLOSE_CONFIRM_SECONDS:
                    continue
                changed = True
                state["pending_close_at"] = None
                state["foreground"] = False
                del self._apps[package]
                if now - closed_at > APP_CLOSE_CONFIRM_SECONDS + APP_CLOSE_STALE_DROP_SECONDS:
                    continue
                debounce_key = f"{package}|{APP_EVENT_CLOSE}"
                if not self._debounce_ok_locked(debounce_key, now):
                    continue
                self._last_bubble_at[debounce_key] = now
                due.append({"package": package, "label": state["label"], "closed_at": closed_at})
            if changed:
                self._persist_locked()
        return due
