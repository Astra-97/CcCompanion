"""肥波 (Fable) 周额度估计 — statusline 采样口径 (2026-09-25 Astra 最终口径).

记账规则 (Astra 原话定稿): 「当 7day+1% 且当前是肥波时，肥波额度+2%」。

- 只估计肥波额度, 其他模型完全不记 (不是 ×1, 是不记)。
- 数据源: Claude Code statusline 缓存到 tmux 全局 option
  ``@claude-code-status-json`` 的原始 JSON (statusline-command.sh 每次渲染时
  set-option -gq 写入; 渲染文本有 ANSI/宽度截断风险, 不解析文本)。字段:
  ``model.display_name`` / ``rate_limits.seven_day.used_percentage`` /
  ``rate_limits.seven_day.resets_at`` / ``rate_limits.five_hour.*``。
- 7d% 是账号级数据: 多个 Claude 终端看到同一个 7d%, 且都写同一个全局
  option (last-write-wins)。采样这一个 option 天然按账号去重 —— 任何时候
  只有一条样本流, 绝不跨终端累加 delta。
- 周期采样得 (时间, 模型, 7d%): 每当 7d% 上涨 delta, 若该样本模型是
  Fable 家族 (大小写不敏感, 含版本号如 "Fable 5.1" / "claude-fable-5-1")
  → 肥波估计 += delta × 2; 非 Fable → 忽略 (锚点照常推进)。
- 7d% 下降或 resets_at 变化 → 视为周重置: 肥波计数清零重记, 当前样本的
  7d% 作为新周已发生量, 按当前样本模型归属 (Fable 则 ×2 计入)。
- 周重置判定只看 statusline 实测锚点: ``week_reset_at`` 记录来源
  (``week_reset_source`` = statusline / fallback), 仅当新旧两端 resets_at
  都来自 statusline 实测且不等才判跨周; fallback → 实测的首次迁移只静默
  校正锚点, 不清零不暴冲 (resets_at 间歇缺失时 known 在真实值与兜底值间
  flap 也不再触发伪重置)。
- 周窗起止以 statusline 的 seven_day.resets_at 为准 ([reset-7d, reset]);
  拿不到时回落到配置的每周刷新点 (默认周五 19:00, UTC+8)。
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("cc-apns-server.fable_quota")

STATUS_OPTION = "@claude-code-status-json"
WEEK_SECONDS = 7 * 86400
METHODOLOGY = (
    "肥波估计口径 (2026-09-25 Astra 定稿): 周期采样 Claude Code statusline 的"
    " 7d% (账号级, 经 tmux @claude-code-status-json 单源去重); 7d% 每上涨 delta"
    " 且该样本模型为 Fable 家族时, 肥波估计 += delta×2; 非 Fable 不记; 7d% 下降"
    " 或实测周重置点变化时清零重记。估计值 ≠ 官方额度, 仅供参考。"
)

_DEFAULT_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, 配置兜底用


def is_fable_model(model: str) -> bool:
    """Fable 家族判定: 大小写不敏感, 含版本号 (Fable 5.1 / claude-fable-5-1)。"""
    return "fable" in str(model or "").lower()


def week_window_fallback(
    now: float,
    *,
    reset_weekday: int = 4,  # 周五 (Monday=0)
    reset_hour: int = 19,
    reset_minute: int = 0,
    tz: timezone = _DEFAULT_TZ,
) -> tuple[float, float]:
    """statusline 没带 resets_at 时的兜底周窗 [start, end): 最近刷新点起 7 天。"""
    now_dt = datetime.fromtimestamp(now, tz)
    days_back = (now_dt.weekday() - reset_weekday) % 7
    start_dt = now_dt.replace(
        hour=reset_hour, minute=reset_minute, second=0, microsecond=0
    ) - timedelta(days=days_back)
    if start_dt.timestamp() > now:
        start_dt -= timedelta(days=7)
    start = start_dt.timestamp()
    return start, start + WEEK_SECONDS


def parse_status_payload(payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """从 statusline 原始 JSON 提取样本; 缺 7d% 时返回 None (不记账)。"""
    if not isinstance(payload, dict):
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None
    seven = rate_limits.get("seven_day")
    if not isinstance(seven, dict):
        return None
    try:
        seven_pct = float(seven.get("used_percentage"))
    except (TypeError, ValueError):
        return None
    model_info = payload.get("model")
    model = ""
    if isinstance(model_info, dict):
        model = str(model_info.get("display_name") or model_info.get("id") or "")
    five = rate_limits.get("five_hour")
    five_pct: float | None = None
    if isinstance(five, dict):
        try:
            five_pct = float(five.get("used_percentage"))
        except (TypeError, ValueError):
            five_pct = None
    resets_at: int | None = None
    try:
        raw_reset = seven.get("resets_at")
        if raw_reset is not None:
            resets_at = int(float(raw_reset))
    except (TypeError, ValueError):
        resets_at = None
    return {
        "ts": float(now if now is not None else time.time()),
        "model": model,
        "is_fable": is_fable_model(model),
        "seven_day_pct": seven_pct,
        "five_hour_pct": five_pct,
        "seven_day_resets_at": resets_at,
    }


def read_claude_status_option(
    runner: Callable[..., Any] = subprocess.run,
    *,
    timeout: float = 3.0,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    """有界读取 tmux 全局 option 里的 statusline JSON; 任何失败返回 {}。"""
    try:
        proc = runner(
            ["tmux", "show-option", "-gqv", STATUS_OPTION],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        logger.debug("fable quota: tmux status option read failed", exc_info=True)
        return {}
    if getattr(proc, "returncode", 1) != 0:
        return {}
    raw = str(getattr(proc, "stdout", "") or "")
    if len(raw) > max_bytes:
        logger.warning("fable quota: status option exceeds %d bytes, ignored", max_bytes)
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.debug("fable quota: status option is not valid JSON")
        return {}
    return parsed if isinstance(parsed, dict) else {}


class FableQuotaTracker:
    """肥波周额度估计器: 样本流 → 增量记账 → JSON 持久化。线程安全。"""

    def __init__(
        self,
        data_path: str | Path,
        *,
        multiplier: float = 2.0,
        max_samples: int = 20,
        reset_weekday: int = 4,
        reset_hour: int = 19,
        reset_minute: int = 0,
    ) -> None:
        self.data_path = Path(data_path)
        self.multiplier = float(multiplier)
        self.max_samples = max(1, int(max_samples))
        self.reset_weekday = int(reset_weekday) % 7
        self.reset_hour = int(reset_hour)
        self.reset_minute = int(reset_minute)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "week_start_at": None,
            "week_reset_at": None,
            "week_reset_source": None,  # statusline 实测 / fallback 推算
            "fable_estimate_pct": 0.0,
            "last_seven_day_pct": None,
            "last_sample": None,
            "samples": [],
            "updated_at": None,
        }
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        try:
            raw = self.data_path.read_text(encoding="utf-8")
            stored = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("fable quota: state file unreadable, starting fresh", exc_info=True)
            return
        if not isinstance(stored, dict):
            return
        with self._lock:
            for key in self._state:
                if key in stored:
                    self._state[key] = stored[key]
            if not isinstance(self._state.get("samples"), list):
                self._state["samples"] = []

    def _save_locked(self) -> None:
        try:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.data_path.with_suffix(self.data_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.data_path)
        except Exception:
            logger.warning("fable quota: state save failed", exc_info=True)

    # ------------------------------------------------------------- accounting

    def _week_window_locked(self, sample: dict[str, Any]) -> tuple[float, float]:
        resets_at = sample.get("seven_day_resets_at")
        if resets_at:
            end = float(resets_at)
            return end - WEEK_SECONDS, end
        return week_window_fallback(
            float(sample["ts"]),
            reset_weekday=self.reset_weekday,
            reset_hour=self.reset_hour,
            reset_minute=self.reset_minute,
        )

    def ingest(self, sample: dict[str, Any]) -> dict[str, Any]:
        """记一个样本, 返回事件描述 (便于日志/测试)。sample 结构见 parse_status_payload。"""
        with self._lock:
            state = self._state
            pct = float(sample["seven_day_pct"])
            is_fable = bool(sample.get("is_fable"))
            week_start, week_reset = self._week_window_locked(sample)
            last_pct = state.get("last_seven_day_pct")
            known_reset = state.get("week_reset_at")
            known_source = state.get("week_reset_source")
            sample_reset = sample.get("seven_day_resets_at")
            has_reset = bool(sample_reset)
            event = {
                "kind": "noop", "delta": 0.0, "credited": 0.0,
                "anchor_corrected": False,
            }

            is_first = last_pct is None
            # 仅当新旧两端 resets_at 都来自 statusline 实测且不等才判跨周。
            # fallback → 实测的首次迁移 (或 resets_at 间歇缺失导致的 flap)
            # 只静默校正锚点, 不清零、不把存量 ×2 记到当前模型头上。
            week_rolled = bool(
                not is_first
                and has_reset
                and known_reset
                and known_source == "statusline"
                and float(sample_reset) != float(known_reset)
            )
            anchor_corrected = bool(
                not is_first
                and not week_rolled
                and has_reset
                and known_reset
                and float(sample_reset) != float(known_reset)
            )
            event["anchor_corrected"] = anchor_corrected
            pct_dropped = bool(not is_first and pct < float(last_pct) - 1e-9)

            if is_first:
                # 无历史可考: 只锚定, 不把存量 7d% 算到任何模型头上。
                event["kind"] = "init"
            elif week_rolled or pct_dropped:
                # 周重置: 清零重记。当前样本的 7d% 是新周已发生量,
                # 归属当前样本模型 (与增量口径一致: 取后一个样本的模型)。
                state["fable_estimate_pct"] = 0.0
                event["kind"] = "week_reset"
                if is_fable and pct > 0:
                    credited = pct * self.multiplier
                    state["fable_estimate_pct"] = credited
                    event["credited"] = credited
            else:
                delta = pct - float(last_pct)
                event["delta"] = max(0.0, delta)
                if delta > 0 and is_fable:
                    credited = delta * self.multiplier
                    state["fable_estimate_pct"] = float(state["fable_estimate_pct"]) + credited
                    event["kind"] = "credit"
                    event["credited"] = credited
                elif delta > 0:
                    event["kind"] = "ignored_non_fable"

            state["fable_estimate_pct"] = min(
                100.0, max(0.0, float(state["fable_estimate_pct"]))
            )
            state["week_start_at"] = week_start
            state["week_reset_at"] = week_reset
            state["week_reset_source"] = "statusline" if has_reset else "fallback"
            state["last_seven_day_pct"] = pct
            sample_view = {
                "ts": sample["ts"],
                "model": sample.get("model") or "",
                "is_fable": is_fable,
                "seven_day_pct": pct,
                "five_hour_pct": sample.get("five_hour_pct"),
            }
            state["last_sample"] = sample_view
            samples = state["samples"]
            samples.append(sample_view)
            del samples[: max(0, len(samples) - self.max_samples)]
            state["updated_at"] = time.time()
            self._save_locked()
            return event

    # --------------------------------------------------------------- snapshot

    @staticmethod
    def _fmt_bj(epoch: float | int | None) -> str | None:
        if not epoch:
            return None
        return datetime.fromtimestamp(float(epoch), _DEFAULT_TZ).strftime("%m-%d %H:%M")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = {k: v for k, v in self._state.items() if k != "samples"}
            sample_count = len(self._state["samples"])
        estimate = float(state.get("fable_estimate_pct") or 0.0)
        last_sample = state.get("last_sample") or None
        official = state.get("last_seven_day_pct")
        return {
            "fable_estimate_pct": round(estimate, 2),
            "official_seven_day_pct": official,
            "week": {
                "start_at": state.get("week_start_at"),
                "reset_at": state.get("week_reset_at"),
                "reset_source": state.get("week_reset_source"),
                "start_bj": self._fmt_bj(state.get("week_start_at")),
                "reset_bj": self._fmt_bj(state.get("week_reset_at")),
            },
            "last_sample": last_sample,
            "current_model": (last_sample or {}).get("model") or None,
            "sample_count": sample_count,
            "multiplier": self.multiplier,
            "updated_at": state.get("updated_at"),
            "methodology": METHODOLOGY,
        }


def sampler_loop(
    tracker: FableQuotaTracker,
    *,
    interval_seconds: float = 60.0,
    stop_event: threading.Event | None = None,
    read_status: Callable[[], dict[str, Any]] = read_claude_status_option,
    now: Callable[[], float] = time.time,
) -> None:
    """周期采样 statusline → 记账。随服务进程退出 (daemon 线程)。"""
    stop = stop_event or threading.Event()
    interval = max(5.0, float(interval_seconds))
    while not stop.is_set():
        try:
            payload = read_status()
            sample = parse_status_payload(payload, now=now())
            if sample is None:
                logger.debug("fable quota: no usable statusline sample this tick")
            else:
                event = tracker.ingest(sample)
                if event["kind"] in {"credit", "week_reset"}:
                    logger.info(
                        "fable quota: %s delta=%.2f credited=%.2f model=%s",
                        event["kind"], event["delta"], event["credited"],
                        sample.get("model") or "?",
                    )
        except Exception:
            logger.exception("fable quota: sampler tick failed")
        stop.wait(interval)
