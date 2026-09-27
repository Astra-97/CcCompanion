"""手机电量感知（2026-09-27 Astra 决议：安静级 + 打扰级，不做历史记录）。

- 安静级：Android App 发聊天消息时在 metadata.device 带当前电量，
  ``format_device_battery_prompt`` 渲染成一行小字进本轮 AI 上下文；
  该字段绝不写入聊天历史（_handle_chat_send 在入库前摘除）。
- 打扰级：POST /device/battery 上报最新值，低于阈值（默认 20%）且未充电时
  边缘触发一次 APNs 提醒；回升到阈值 + 5 以上（或开始充电）才重新武装。
- 状态文件 tokens/device_battery.json 只存最新值，不做历史/统计。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

DEVICE_BATTERY_METADATA_KEY = "device"
DEFAULT_LOW_THRESHOLD_PERCENT = 20
LOW_THRESHOLD_MIN_PERCENT = 5
LOW_THRESHOLD_MAX_PERCENT = 95
# 低电提醒的滞回区间：触发一次后，电量回升到 阈值+5 以上才重新武装，
# 避免在阈值附近来回抖动反复轰炸。
REARM_HYSTERESIS_PERCENT = 5


def normalize_low_threshold_percent(value: object) -> int:
    """配置值归一化；非法值回退默认 20，钳位到 5–95。"""

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


def format_device_battery_prompt(battery: object) -> str:
    """把电量快照渲染成 AI 上下文里的一行小字；无数据时为空串（老 App 不受影响）。"""

    normalized = normalize_device_battery(battery)
    if normalized is None:
        return ""
    charging_text = "充电中" if normalized["charging"] else "未充电"
    return f"[设备状态] 手机电量 {normalized['battery_pct']}%（{charging_text}）"


class DeviceBatteryStore:
    """最新电量 + 低电提醒武装位，原子落盘，只存最新值。"""

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

    def update(self, battery: dict, low_threshold_percent: int) -> tuple[dict, bool]:
        """写入最新值并做边缘触发判定，返回 (快照, 是否应提醒)。

        提醒条件：已武装、未充电、电量低于阈值。提醒后解除武装；
        电量回升到 阈值+5 以上或开始充电时重新武装。
        """

        normalized = normalize_device_battery(battery)
        if normalized is None:
            raise ValueError("invalid battery snapshot")
        threshold = normalize_low_threshold_percent(low_threshold_percent)
        with self._lock:
            if normalized["charging"] or normalized["battery_pct"] >= threshold + REARM_HYSTERESIS_PERCENT:
                self._low_armed = True
            should_notify = (
                self._low_armed
                and not normalized["charging"]
                and normalized["battery_pct"] < threshold
            )
            if should_notify:
                self._low_armed = False
            self._battery = normalized
            self._updated_at = time.time()
            self._persist_locked()
            return {
                **normalized,
                "updated_at": self._updated_at,
                "low_armed": self._low_armed,
            }, should_notify
