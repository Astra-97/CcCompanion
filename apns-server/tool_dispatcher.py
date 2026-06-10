"""小克·工具版 (tool-version dispatcher).

A rule-driven, NO-AI scheduler that lives inside the apns-server. At scheduled
times it injects trigger messages into the main Claude session ("小克") through
the *exact same* delivery path the user's chat messages use (channel transport,
with tmux fallback). The assistant then acts on the trigger — e.g. writes a
morning greeting — using its own full context/memory.

Design constraints (per spec):

  * NO AI / NO LLM calls happen here. This module only matches wall-clock time
    against static rules and fires a fixed trigger string. All "intelligence"
    lives downstream in the Claude session that receives the trigger.
  * Every dispatched trigger MUST be visible to the user in the chat history,
    just like a message they typed themselves. Delivery therefore goes through
    the server-provided callback, which writes a chat_history record AND injects
    into the session.
  * Survives restarts: each rule records the last occurrence it fired (keyed by
    local date) so the server can be bounced without double-firing or missing a
    slot it already served.

Schedule file format (JSON), default `<state>/tool_dispatcher.json`:

    {
      "enabled": true,
      "rules": [
        {
          "id": "morning_greeting",
          "enabled": true,
          "time": "07:30",              # HH:MM, local to `tz`
          "tz": "Asia/Shanghai",        # IANA tz; falls back to system local
          "weekdays": [0,1,2,3,4,5,6],  # 0=Mon .. 6=Sun; omit/empty = every day
          "contact_id": "xiaoke",       # which chat the trigger lands in
          "text": "[工具版·早安触发] ...",
          "catch_up_grace_minutes": 30  # fire late if server was down at the slot
        }
      ]
    }

The dispatcher itself does not know about tmux or HTTP — the server injects a
`deliver(contact_id, text, rule_id) -> (ok, err)` callback so there is a single
source of truth for the delivery path.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - py<3.9 safety
    ZoneInfo = None  # type: ignore

logger = logging.getLogger(__name__)

# deliver(contact_id, text, rule_id) -> (ok: bool, error: str)
DeliverFn = Callable[[str, str, str], "tuple[bool, str]"]


def _parse_hhmm(value: Any) -> tuple[int, int] | None:
    try:
        raw = str(value).strip()
        hh, mm = raw.split(":", 1)
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


def _resolve_tz(name: str | None):
    """Return a tzinfo for `name`, or None to mean 'use system local time'."""
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(str(name))
    except Exception:
        logger.warning("tool_dispatcher: unknown tz %r, using system local", name)
        return None


class ScheduleStore:
    """Loads and persists the schedule file. Thread-safe.

    The file is treated as the source of truth for rules; `last_fired` markers
    are written back in place so restarts don't double-fire.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"enabled": True, "rules": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("schedule root must be a JSON object")
            return data
        except Exception as e:
            logger.error("tool_dispatcher: failed to read %s: %s", self.path, e)
            return {"enabled": True, "rules": [], "_read_error": str(e)}

    def _write_raw(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def globally_enabled(self) -> bool:
        with self._lock:
            return bool(self._read_raw().get("enabled", True))

    def rules(self) -> list[dict[str, Any]]:
        with self._lock:
            raw = self._read_raw()
        out: list[dict[str, Any]] = []
        for r in raw.get("rules", []) or []:
            if isinstance(r, dict):
                out.append(r)
        return out

    def mark_fired(self, rule_id: str, occurrence_key: str) -> None:
        """Record that `rule_id` fired for the given occurrence (local date)."""
        with self._lock:
            raw = self._read_raw()
            changed = False
            for r in raw.get("rules", []) or []:
                if isinstance(r, dict) and str(r.get("id")) == rule_id:
                    r["last_fired"] = occurrence_key
                    r["last_fired_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    changed = True
                    break
            if changed:
                try:
                    self._write_raw(raw)
                except Exception as e:
                    logger.error("tool_dispatcher: persist last_fired failed: %s", e)

    def set_rules_enabled(self, rule_ids: list[str], enabled: bool) -> list[str]:
        """Flip the `enabled` flag on the given rule ids. Returns ids changed.

        Used by the toolbot command menu (morning_on/off, diary_on/off) to turn
        scheduled reminders on or off without editing the file by hand.
        """
        changed: list[str] = []
        wanted = {str(r) for r in rule_ids}
        with self._lock:
            raw = self._read_raw()
            for r in raw.get("rules", []) or []:
                if isinstance(r, dict) and str(r.get("id")) in wanted:
                    if bool(r.get("enabled", True)) != bool(enabled):
                        r["enabled"] = bool(enabled)
                        changed.append(str(r.get("id")))
            if changed:
                try:
                    self._write_raw(raw)
                except Exception as e:
                    logger.error("tool_dispatcher: persist enabled toggle failed: %s", e)
                    return []
        return changed

    def rule_enabled_state(self, rule_ids: list[str]) -> dict[str, bool]:
        """Return {rule_id: enabled} for the given ids (missing ids omitted)."""
        wanted = {str(r) for r in rule_ids}
        out: dict[str, bool] = {}
        for r in self.rules():
            rid = str(r.get("id"))
            if rid in wanted:
                out[rid] = bool(r.get("enabled", True))
        return out

    def ensure_seed(self, seed: dict[str, Any]) -> None:
        """Create the schedule file with `seed` contents if it does not exist."""
        with self._lock:
            if self.path.exists():
                return
            try:
                self._write_raw(seed)
                logger.info("tool_dispatcher: seeded schedule file at %s", self.path)
            except Exception as e:
                logger.error("tool_dispatcher: seed write failed: %s", e)


class ToolDispatcher:
    """Background daemon: ticks every `tick_seconds`, fires due rules once each.

    `deliver` is the server-provided delivery callback. It is responsible for
    making the trigger visible to the user (chat history) and injecting it into
    the live session (channel transport / tmux). The dispatcher only decides
    *when* and *what text*.
    """

    def __init__(
        self,
        store: ScheduleStore,
        deliver: DeliverFn,
        *,
        tick_seconds: float = 20.0,
    ):
        self.store = store
        self.deliver = deliver
        self.tick_seconds = max(5.0, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="tool-dispatcher", daemon=True
        )
        self._thread.start()
        logger.info("tool_dispatcher: started (tick=%.0fs)", self.tick_seconds)

    def stop(self) -> None:
        self._stop.set()

    # ---- core ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("tool_dispatcher: tick error")
            self._stop.wait(self.tick_seconds)

    def _due(self, rule: dict[str, Any], now_utc: datetime) -> tuple[bool, str]:
        """Decide whether `rule` should fire right now.

        Returns (should_fire, occurrence_key). occurrence_key is the local date
        of the scheduled slot; it is used both for dedup and as the persisted
        `last_fired` marker.
        """
        if not bool(rule.get("enabled", True)):
            return False, ""
        hm = _parse_hhmm(rule.get("time"))
        if hm is None:
            logger.warning("tool_dispatcher: rule %r has bad time %r", rule.get("id"), rule.get("time"))
            return False, ""
        hour, minute = hm

        tz = _resolve_tz(rule.get("tz"))
        now_local = now_utc.astimezone(tz) if tz is not None else now_utc.astimezone()

        # weekday filter (0=Mon..6=Sun). Empty/missing = every day.
        weekdays = rule.get("weekdays")
        if isinstance(weekdays, list) and weekdays:
            try:
                allowed = {int(d) for d in weekdays}
            except Exception:
                allowed = set()
            if allowed and now_local.weekday() not in allowed:
                return False, ""

        scheduled = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        occurrence_key = scheduled.strftime("%Y-%m-%d")

        # Already served this occurrence? (survives restarts via persisted marker)
        if str(rule.get("last_fired") or "") == occurrence_key:
            return False, ""

        # Only fire at or after the scheduled minute, and within a grace window
        # so a server that was down at the exact slot still catches up — but a
        # server that has been down for hours doesn't fire a stale greeting.
        if now_local < scheduled:
            return False, ""
        grace = rule.get("catch_up_grace_minutes", 30)
        try:
            grace_min = max(0, int(grace))
        except Exception:
            grace_min = 30
        if now_local > scheduled + timedelta(minutes=grace_min):
            # Missed the window. Mark it served so it doesn't fire late, and so
            # tomorrow's occurrence is the next candidate.
            return False, occurrence_key + "\x00missed"

        return True, occurrence_key

    def tick(self, now_utc: datetime | None = None) -> int:
        """Evaluate all rules once. Returns the number of triggers dispatched.

        Exposed (and pure-ish) so it can be unit-tested by passing `now_utc`.
        """
        if now_utc is None:
            now_utc = datetime.now().astimezone()
        if not self.store.globally_enabled():
            return 0

        fired = 0
        for rule in self.store.rules():
            should, key = self._due(rule, now_utc)
            rule_id = str(rule.get("id") or "")
            if not rule_id:
                continue

            # Handle "missed window" bookkeeping without dispatching.
            if not should:
                if key.endswith("\x00missed"):
                    real_key = key.split("\x00", 1)[0]
                    logger.info(
                        "tool_dispatcher: rule %r missed window for %s, marking served",
                        rule_id, real_key,
                    )
                    self.store.mark_fired(rule_id, real_key)
                continue

            contact_id = str(rule.get("contact_id") or "xiaoke").strip().lower() or "xiaoke"
            text = str(rule.get("text") or "").strip()
            if not text:
                logger.warning("tool_dispatcher: rule %r has empty text, skipping", rule_id)
                self.store.mark_fired(rule_id, key)
                continue

            logger.info(
                "tool_dispatcher: firing rule %r (occurrence=%s contact=%s)",
                rule_id, key, contact_id,
            )
            try:
                ok, err = self.deliver(contact_id, text, rule_id)
            except Exception as e:
                ok, err = False, f"deliver raised: {e}"
                logger.exception("tool_dispatcher: deliver raised for rule %r", rule_id)

            if ok:
                # Mark served only on success so a transient delivery failure
                # (session not up yet) retries on the next tick within the grace
                # window instead of being silently lost.
                self.store.mark_fired(rule_id, key)
                fired += 1
                logger.info("tool_dispatcher: rule %r delivered", rule_id)
            else:
                logger.warning(
                    "tool_dispatcher: rule %r delivery failed: %s (will retry within grace)",
                    rule_id, err,
                )
        return fired


# A reasonable default schedule used to seed the file on first run. Mirrors the
# existing morning-greeting behaviour: at 07:30 Beijing, wake 小克 so it writes
# the real greeting to 方小南 with full context.
DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": True,
    "rules": [
        {
            "id": "morning_greeting",
            "enabled": True,
            "time": "07:30",
            "tz": "Asia/Shanghai",
            "weekdays": [],
            "contact_id": "xiaoke",
            "text": (
                "[工具版·早安触发] 现在是早上 7:30。给方小南写一条早安问候，"
                "结合记忆库里她的近况、今天的安排和北京天气，自然一点，别像模板，"
                "顺便提醒她跑 Tasker 发睡眠数据给你。"
            ),
            "catch_up_grace_minutes": 60,
        },
        {
            "id": "diary_reminder",
            "enabled": True,
            "time": "22:00",
            "tz": "Asia/Shanghai",
            "weekdays": [],
            "contact_id": "xiaoke",
            "text": (
                "[工具版·日记触发] 现在是晚上 22:00。提醒方小南写今天的日记："
                "先搜记忆库「Notion日记注意事项」学写法/更新方法，再问她今天发生了什么；"
                "她不回应说明在忙，直接帮她写，她有需要再改。"
            ),
            "catch_up_grace_minutes": 60,
        },
        {
            "id": "diary_supplement",
            "enabled": True,
            "time": "22:50",
            "tz": "Asia/Shanghai",
            "weekdays": [],
            "contact_id": "xiaoke",
            "text": (
                "[工具版·日记补充触发] 现在是 22:50。检查今天的日记有没有遗漏，"
                "把 22 点之后发生的事补进去。"
            ),
            "catch_up_grace_minutes": 60,
        },
        {
            "id": "hotclinic_xiehe",
            "enabled": True,
            "time": "22:50",
            "tz": "Asia/Shanghai",
            "weekdays": [],
            "contact_id": "xiaoke",
            "text": (
                "[工具版·抢号触发·协和] 现在是 22:50。提醒方小南协和到点抢号了，"
                "确认她准备好了。"
            ),
            "catch_up_grace_minutes": 20,
        },
        {
            "id": "hotclinic_301",
            "enabled": True,
            "time": "06:50",
            "tz": "Asia/Shanghai",
            "weekdays": [],
            "contact_id": "xiaoke",
            "text": (
                "[工具版·抢号触发·301] 现在是 06:50。提醒方小南 301 到点抢号了，"
                "确认她准备好了。"
            ),
            "catch_up_grace_minutes": 20,
        },
    ],
}
