"""手机电量感知（2026-09-27 v4 Astra 决议：打扰级气泡 + AI 主动查询，无 metadata 水印）。

- App 通过 POST /device/battery 边缘触发上报最新电量；这里只做状态与事件
  判定，气泡投递在 push.py（进小克会话的系统气泡，不走 APNs banner）。
- 事件（平时完全安静）：
  - charging_started / charging_stopped：充↔不充切换各报一次；转换检测天然
    边缘触发（拔电后才会重新武装充电事件，反之亦然）。首次上报（无先前
    状态）只落状态、不报切换事件。
  - low_battery：电量低于阈值（默认 25%）且未充电报一次；回升到 阈值+5
    （默认 30%）或开始充电才重新武装。
- 状态文件 tokens/device_battery.json 只存最新值 + 武装位，不做历史/统计。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

EVENT_CHARGING_STARTED = "charging_started"
EVENT_CHARGING_STOPPED = "charging_stopped"
EVENT_LOW_BATTERY = "low_battery"

DEFAULT_LOW_THRESHOLD_PERCENT = 25
LOW_THRESHOLD_MIN_PERCENT = 5
LOW_THRESHOLD_MAX_PERCENT = 95
# 低电事件的滞回区间：触发一次后，电量回升到 阈值+5（默认 30%）以上才重新
# 武装，避免在阈值附近来回抖动反复打扰。
REARM_HYSTERESIS_PERCENT = 5


def normalize_low_threshold_percent(value: object) -> int:
    """配置值归一化；非法值回退默认 25，钳位到 5–95。"""

    try:
        threshold = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LOW_THRESHOLD_PERCENT
    return max(LOW_THRESHOLD_MIN_PERCENT, min(LOW_THRESHOLD_MAX_PERCENT, threshold))


def normalize_device_battery(value: object) -> dict | None:
    """校验 App 上报的电量快照；只接受 {"battery_pct": 0-100, "charging": bool}。"""

    if not isinstance(value, dict):
        return None
    raw_pct = value.get("battery_pct")
    # bool 是 int 的子类，先排掉，避免 True 被当成 100%。
    if isinstance(raw_pct, bool) or not isinstance(raw_pct, (int, float)):
        return None
    pct = int(raw_pct)
    if not 0 <= pct <= 100 or pct != raw_pct:
        return None
    charging = value.get("charging")
    if not isinstance(charging, bool):
        return None
    return {"battery_pct": pct, "charging": charging}


class DeviceBatteryStore:
    """最新电量 + 低电武装位 + 上次充电态，原子落盘，只存最新值。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._battery: dict | None = None
        self._updated_at = 0.0
        # 初始即武装：服务重启后第一次低于阈值的上报应当提醒。
        self._low_armed = True
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        self._battery = normalize_device_battery(raw)
        try:
            self._updated_at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            self._updated_at = 0.0
        self._low_armed = bool(raw.get("low_armed", True))

    def _persist_locked(self) -> None:
        payload = {
            **(self._battery or {}),
            "updated_at": self._updated_at,
            "low_armed": self._low_armed,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                **(self._battery or {}),
                "updated_at": self._updated_at,
                "low_armed": self._low_armed,
            }

    def update(self, battery: dict, low_threshold_percent: int) -> tuple[dict, list[str]]:
        """写入最新值并做边缘触发判定，返回 (快照, 事件列表)。

        事件见模块头注释；一次上报最多各出一次 charging_started /
        charging_stopped 之一与 low_battery。
        """

        normalized = normalize_device_battery(battery)
        if normalized is None:
            raise ValueError("invalid battery snapshot")
        threshold = normalize_low_threshold_percent(low_threshold_percent)
        events: list[str] = []
        with self._lock:
            previous = self._battery
            if previous is not None and normalized["charging"] != previous["charging"]:
                events.append(
                    EVENT_CHARGING_STARTED if normalized["charging"] else EVENT_CHARGING_STOPPED
                )
            if normalized["charging"] or normalized["battery_pct"] >= threshold + REARM_HYSTERESIS_PERCENT:
                self._low_armed = True
            if (
                self._low_armed
                and not normalized["charging"]
                and normalized["battery_pct"] < threshold
            ):
                events.append(EVENT_LOW_BATTERY)
                self._low_armed = False
            self._battery = normalized
            self._updated_at = time.time()
            self._persist_locked()
            return {
                **normalized,
                "updated_at": self._updated_at,
                "low_armed": self._low_armed,
            }, events
