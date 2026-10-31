"""「小克的本地能力」服务端配件 (2026-10-31, 配合 App v1.9.233)。

phone_control.py 管任务 (闹钟 / 健康 / 日历 / 强提醒 / 剪贴板 / 小部件 / 地点列表);
本模块管手机主动推上来的东西和几个纯函数:

  * NotifBuffer —— 通知转发的环形缓冲 (只留最近 200 条 / 48 小时, 文件 0600,
    不进聊天记录、不写日志正文)。手机端已经按白名单 + 验证码过滤过, 这里再滤一次。
  * PlaceEvents —— 到达 / 离开事件去抖 (只收地点名字, 不收坐标)。
  * load_schedule_events —— schedule-ctl 的 events.json → calendar_sync 任务参数。
  * build_widget_payload —— 桌面小部件要的「接下来 3 条日程 + 小克最新一句」。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import phone_control

logger = logging.getLogger("phone_local")

TZ_BEIJING = ZoneInfo("Asia/Shanghai")

NOTIF_MAX_ITEMS = 200
NOTIF_TTL_S = 48 * 3600
NOTIF_BATCH_MAX = 20
NOTIF_NOTICE_GAP_S = 180          # 同一个 App 的低调注入最短间隔 (外卖进度会刷屏)
PLACE_DEBOUNCE_S = 5 * 60         # 同一地点同一方向 5 分钟内只算一次
PLACE_NAME_MAX = 20
CALENDAR_HISTORY_DAYS = 14

# 验证码 / 密码 / OTP: 宁可多丢不可漏 (手机端 LocalNotifPolicy 同一套规则)
_SECRET_RE = re.compile(
    r"(验证码|校验码|动态码|动态密码|确认码|安全码|授权码|口令|密码|"
    r"passcode|password|verification|one[- ]time|\botp\b|\bpin\b|\bcode\b)",
    re.IGNORECASE,
)
_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def looks_secret(*texts: str) -> bool:
    joined = " ".join(t for t in texts if t)
    return bool(_SECRET_RE.search(joined))


def _clean_line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


class NotifBuffer:
    """线程安全环形缓冲, 落盘 0600 (服务重启后还能 phone-ctl notifs 看到最近的)。"""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=NOTIF_MAX_ITEMS)
        self._last_notice: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, list):
            now = self._clock()
            for item in raw[-NOTIF_MAX_ITEMS:]:
                if isinstance(item, dict) and now - float(item.get("at") or 0) <= NOTIF_TTL_S:
                    self._items.append(item)

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(list(self._items), fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _expire_locked(self) -> None:
        now = self._clock()
        while self._items and now - float(self._items[0].get("at") or 0) > NOTIF_TTL_S:
            self._items.popleft()

    @staticmethod
    def normalize(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        pkg = raw.get("pkg")
        if not isinstance(pkg, str) or not _PKG_RE.match(pkg) or len(pkg) > 200:
            return None
        title = _clean_line(raw.get("title"), 120)
        text = _clean_line(raw.get("text"), 400)
        if not title and not text:
            return None
        if looks_secret(title, text):
            return None
        posted = raw.get("posted_at")
        item = {
            "pkg": pkg,
            "app": _clean_line(raw.get("app"), 40) or pkg,
            "title": title,
            "text": text,
        }
        if isinstance(posted, (int, float)) and not isinstance(posted, bool) and posted > 0:
            item["posted_at"] = float(posted) / 1000.0 if posted > 1e11 else float(posted)
        return item

    def add(self, items: Iterable[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """收一批; 返回 (存下的, 需要低调注入的)。"""
        kept: list[dict[str, Any]] = []
        notify: list[dict[str, Any]] = []
        with self._lock:
            self._expire_locked()
            now = self._clock()
            recent = {(i["pkg"], i["title"], i["text"]) for i in list(self._items)[-50:]}
            for raw in list(items)[:NOTIF_BATCH_MAX]:
                item = self.normalize(raw)
                if item is None:
                    continue
                key = (item["pkg"], item["title"], item["text"])
                if key in recent:
                    continue
                recent.add(key)
                item["at"] = now
                self._items.append(item)
                kept.append(item)
                if now - self._last_notice.get(item["pkg"], 0.0) >= NOTIF_NOTICE_GAP_S:
                    self._last_notice[item["pkg"]] = now
                    notify.append(item)
            if kept:
                try:
                    self._save_locked()
                except OSError:
                    logger.warning("notif buffer save failed", exc_info=True)
        return kept, notify

    def since(self, seconds: float | None = None, limit: int = NOTIF_MAX_ITEMS) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_locked()
            items = list(self._items)
        if seconds is not None:
            cutoff = self._clock() - max(0.0, float(seconds))
            items = [i for i in items if float(i.get("at") or 0) >= cutoff]
        return items[-max(1, int(limit)):]

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
            try:
                self._save_locked()
            except OSError:
                pass
        return n


def notif_notice_text(item: dict[str, Any]) -> str:
    body = item.get("title") or item.get("text") or ""
    if item.get("title") and item.get("text"):
        body = f"{item['title']}｜{item['text']}"
    # 取件码之类的长数字不进会话注入 (phone-ctl notifs 里有原文)
    body = re.sub(r"\d{4,}", "****", body)
    return f"外卖/快递通知：{item.get('app') or item.get('pkg')}: {body[:80]}"


class PlaceEvents:
    """到达/离开去抖; 只记地点名 + 方向 + 时间 (最近 50 条, 只在内存)。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str], float] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=50)

    @staticmethod
    def normalize(body: Any) -> tuple[str, str] | None:
        if not isinstance(body, dict):
            return None
        place = body.get("place")
        transition = body.get("transition")
        if not isinstance(place, str) or transition not in ("enter", "exit"):
            return None
        place = " ".join(place.split())
        if not place or len(place) > PLACE_NAME_MAX:
            return None
        return place, transition

    def accept(self, place: str, transition: str) -> bool:
        with self._lock:
            now = self._clock()
            key = (place, transition)
            if now - self._last.get(key, -1e18) < PLACE_DEBOUNCE_S:
                return False
            self._last[key] = now
            self.recent.append({"place": place, "transition": transition, "at": now})
            return True


def place_notice_text(place: str, transition: str) -> str:
    if transition == "enter":
        return f"方小南到了「{place}」附近"
    return f"方小南离开了「{place}」"


# ── 日程 → 日历 / 小部件 ─────────────────────────────────────────────────


def _today(now: datetime | None = None) -> Date:
    return (now or datetime.now(TZ_BEIJING)).astimezone(TZ_BEIJING).date()


def read_schedule(path: str | Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise phone_control.PhoneTaskError(500, "schedule_unreadable", f"日程本读不出来: {exc}") from exc
    return raw if isinstance(raw, list) else []


def load_schedule_events(path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """calendar_sync 参数: 近 14 天起的全部日程 (含已完成, 手机端标 ✓ 去提醒)。

    window_start 之前的旧事件手机端不动 (留作历史); 之后的以这份为准 (多删少补)。
    """
    start = _today(now) - timedelta(days=CALENDAR_HISTORY_DAYS)
    start_s = start.isoformat()
    events = []
    for raw in read_schedule(path):
        event = phone_control.normalize_calendar_event(raw)
        if event is not None and event["date"] >= start_s:
            events.append(event)
    events.sort(key=lambda e: (e["date"], e["time"] or ""))
    return {"events": events[-phone_control.MAX_CALENDAR_EVENTS:], "window_start": start_s}


_BQB_RE = re.compile(r"\[bqb:[^\]]*\]")
_MARKER_RE = re.compile(r"\[\[[A-Z0-9_:]+\]\]")


def latest_message_line(records: Iterable[dict[str, Any]], limit: int = 80) -> dict[str, Any] | None:
    for rec in reversed(list(records)):
        if not isinstance(rec, dict) or rec.get("role") != "assistant" or rec.get("hidden_in_ui"):
            continue
        meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
        if meta.get("mihome_event") or meta.get("device_event"):
            continue
        text = _MARKER_RE.sub("", _BQB_RE.sub("", str(rec.get("text") or "")))
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            continue
        line = lines[0]
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        return {"text": line, "ts": rec.get("ts")}
    return None


def build_widget_payload(schedule_path: str | Path, records: Iterable[dict[str, Any]], *,
                         now: datetime | None = None) -> dict[str, Any]:
    moment = (now or datetime.now(TZ_BEIJING)).astimezone(TZ_BEIJING)
    today = moment.date()
    tomorrow = today + timedelta(days=1)
    hhmm_now = moment.strftime("%H:%M")
    items = []
    for raw in read_schedule(schedule_path):
        event = phone_control.normalize_calendar_event(raw)
        if event is None or event["done"]:
            continue
        if event["date"] not in (today.isoformat(), tomorrow.isoformat()):
            continue
        # 今天已经过点的定时日程不再占位 (全天的留着)
        if event["date"] == today.isoformat() and event["time"] and event["time"] < hhmm_now:
            continue
        items.append({
            "title": event["title"][:40],
            "date": event["date"],
            "time": event["time"],
            "day": "今天" if event["date"] == today.isoformat() else "明天",
        })
    items.sort(key=lambda e: (e["date"], e["time"] or "00:00"))
    return {
        "ok": True,
        "generated_at": moment.isoformat(),
        "schedule": items[:3],
        "schedule_more": max(0, len(items) - 3),
        "message": latest_message_line(records),
    }
