"""应用使用感知（2026-09-27 v4）：被监 App 开/关气泡 + AI 查询用最新快照。

- App 上报被勾应用的开/关事件（POST /device/app-event），这里做归一化与
  每包名 60 秒气泡防抖（防快速切换刷屏）；事件经 push.py 进小克会话。
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
# 同一 App 开/关气泡的最小间隔（防抖，防快速切换刷屏）。
APP_EVENT_BUBBLE_DEBOUNCE_SECONDS = 60.0
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

    def _persist_locked(self) -> None:
        payload = {
            "snapshot": self._snapshot,
            "updated_at": self._updated_at,
            "last_bubble_at": self._last_bubble_at,
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

    def record_app_event(self, event: dict, *, now: float | None = None) -> bool:
        """登记一次开/关事件，返回是否应冒泡（每包名 60 秒防抖）。"""

        normalized = normalize_app_event(event)
        if normalized is None:
            raise ValueError("invalid app event")
        now = time.time() if now is None else float(now)
        with self._lock:
            # 顺手清掉早已过期的防抖位，表保持有界。
            for key in [k for k, ts in self._last_bubble_at.items() if now - ts > _APP_EVENT_DEBOUNCE_KEEP_SECONDS]:
                del self._last_bubble_at[key]
            last = self._last_bubble_at.get(normalized["package"])
            should_bubble = last is None or now - last >= APP_EVENT_BUBBLE_DEBOUNCE_SECONDS
            if should_bubble:
                self._last_bubble_at[normalized["package"]] = now
                self._persist_locked()
            return should_bubble
