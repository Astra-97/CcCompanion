"""Prompt-free, read-only activity observer for Kimi ACP chat turns.

This is intentionally *not* a terminal bridge.  Kimi ACP is a stdio JSON-RPC
client and its notifications can carry prompts, tool arguments, filesystem
paths, reasoning and tool output.  The only public projection is a short list
of labels selected by this module; no value from ACP is copied into the DTO or
the rendered terminal text.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import json
import threading
import time
from typing import Any, Callable


KIMI_TERMINAL_TARGET = "kimi"
KIMI_TERMINAL_MAX_EVENTS = 40
KIMI_TERMINAL_MAX_BYTES = 8 * 1024
KIMI_TERMINAL_MAX_ELAPSED_SECONDS = 7 * 24 * 60 * 60
KIMI_TERMINAL_MAX_SESSION_RECORDS = 8

# The exact display vocabulary.  Do not accept labels supplied by ACP or by a
# caller: observer content is security-sensitive, not a generic activity log.
_EVENT_LABELS = {
    "started": "已接收任务，正在准备",
    "thinking": "正在思考（内容已隐藏）",
    "tool": "正在使用工具（名称与参数已隐藏）",
    "worker_started": "Kimi 协作 worker 已开始",
    "worker_completed": "Kimi 协作 worker 已完成",
    "worker_failed": "Kimi 协作 worker 未完成",
    "completed": "本轮已完成",
    "interrupted": "本轮已中断",
    "failed": "本轮未完成",
}


@dataclass
class _Run:
    turn_id: str
    epoch: int
    started_at: float
    busy: bool = True
    events: deque[tuple[int, str]] = field(
        default_factory=lambda: deque(maxlen=KIMI_TERMINAL_MAX_EVENTS)
    )


class KimiTerminalObserver:
    """Keep a bounded, session-isolated observer history in memory.

    The private ACP session ID and chat turn identity only fence callbacks and
    select a record.  Neither becomes part of :meth:`snapshot`.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._records: OrderedDict[str, _Run] = OrderedDict()
        self._next_epoch = 0

    @staticmethod
    def _valid_session_id(value: Any) -> str:
        session_id = str(value or "").strip()
        if (
            not session_id
            or len(session_id) > 200
            or not all(char.isalnum() or char in {"-", "_"} for char in session_id)
        ):
            return ""
        return session_id

    @staticmethod
    def _valid_turn_id(value: Any) -> str:
        # App timestamps are opaque and can contain punctuation.  They never
        # leave the process through this observer, so only bound their length.
        turn_id = str(value or "").strip()
        return turn_id if 0 < len(turn_id) <= 256 else ""

    @classmethod
    def unavailable_snapshot(cls) -> dict[str, Any]:
        return {
            "ok": True,
            "target": KIMI_TERMINAL_TARGET,
            "mode": "read_only",
            "state": "unavailable",
            "content": "Kimi 观察器暂不可用。\n",
            "events": [],
        }

    def begin(self, session_id: Any, turn_id: Any) -> int | None:
        """Start a new private epoch and return its opaque in-process fence."""
        session = self._valid_session_id(session_id)
        turn = self._valid_turn_id(turn_id)
        if not session or not turn:
            return None
        with self._lock:
            self._next_epoch += 1
            run = _Run(turn_id=turn, epoch=self._next_epoch, started_at=self._clock())
            run.events.append((0, _EVENT_LABELS["started"]))
            self._records[session] = run
            self._records.move_to_end(session)
            while len(self._records) > KIMI_TERMINAL_MAX_SESSION_RECORDS:
                self._records.popitem(last=False)
            return run.epoch

    def record_activity(
        self,
        session_id: Any,
        turn_id: Any,
        epoch: Any,
        activity: Any,
    ) -> bool:
        """Map the already-sanitized ACP event into one fixed observer label."""
        event_type = ""
        if isinstance(activity, dict):
            kind = str(activity.get("kind") or "")
            if kind == "activity":
                # This repeats the allowlist even though kimi_acp already
                # projects safely; a future caller cannot smuggle text here.
                label = str(activity.get("label") or "")
                event_type = {
                    "正在思考": "thinking",
                    "正在使用工具": "tool",
                }.get(label, "")
            elif kind == "collaboration_worker":
                event_type = {
                    "running": "worker_started",
                    "completed": "worker_completed",
                    "failed": "worker_failed",
                    "interrupted": "worker_failed",
                }.get(str(activity.get("status") or ""), "")
        return self.record(session_id, turn_id, epoch, event_type)

    def record(self, session_id: Any, turn_id: Any, epoch: Any, event_type: str) -> bool:
        label = _EVENT_LABELS.get(str(event_type or ""))
        if label is None or event_type in {"completed", "interrupted", "failed"}:
            return False
        with self._lock:
            run = self._matching_run_locked(session_id, turn_id, epoch, require_busy=True)
            if run is None:
                return False
            run.events.append((self._elapsed_seconds(run), label))
            return True

    def finish(self, session_id: Any, turn_id: Any, epoch: Any, outcome: str) -> bool:
        event_type = {
            "completed": "completed",
            "interrupted": "interrupted",
            "cancelled": "interrupted",
            "failed": "failed",
        }.get(str(outcome or ""), "failed")
        with self._lock:
            run = self._matching_run_locked(session_id, turn_id, epoch, require_busy=True)
            if run is None:
                return False
            run.events.append((self._elapsed_seconds(run), _EVENT_LABELS[event_type]))
            run.busy = False
            return True

    def snapshot(self, session_id: Any) -> dict[str, Any]:
        """Return the strict public DTO for exactly one active Kimi session."""
        session = self._valid_session_id(session_id)
        if not session:
            return self.unavailable_snapshot()
        with self._lock:
            run = self._records.get(session)
            if run is not None:
                self._records.move_to_end(session)
                state = "working" if run.busy else "idle"
                events = [
                    {"elapsed_seconds": elapsed, "label": label}
                    for elapsed, label in list(run.events)[-KIMI_TERMINAL_MAX_EVENTS:]
                    if isinstance(elapsed, int)
                    and 0 <= elapsed <= KIMI_TERMINAL_MAX_ELAPSED_SECONDS
                    and label in _EVENT_LABELS.values()
                ]
            else:
                state = "idle"
                events = []
        return self.project_snapshot({"state": state, "events": events})

    @classmethod
    def project_snapshot(cls, value: Any) -> dict[str, Any]:
        """Rebuild a public DTO from only safe state/event primitives.

        HTTP uses this at its trust boundary too.  Even an accidental future
        observer implementation cannot send pre-rendered ACP diagnostics,
        extra keys, or arbitrary event labels to an Android client.
        """
        if not isinstance(value, dict):
            return cls.unavailable_snapshot()
        state = str(value.get("state") or "")
        if state == "unavailable":
            return cls.unavailable_snapshot()
        safe_state = state if state in {"idle", "working"} else "unavailable"
        if safe_state == "unavailable":
            return cls.unavailable_snapshot()
        safe_events: list[dict[str, Any]] = []
        raw_events = value.get("events")
        if isinstance(raw_events, list):
            for raw in raw_events[-KIMI_TERMINAL_MAX_EVENTS:]:
                if not isinstance(raw, dict):
                    continue
                elapsed = raw.get("elapsed_seconds")
                label = raw.get("label")
                if (
                    isinstance(elapsed, int)
                    and not isinstance(elapsed, bool)
                    and 0 <= elapsed <= KIMI_TERMINAL_MAX_ELAPSED_SECONDS
                    and label in _EVENT_LABELS.values()
                ):
                    safe_events.append({"elapsed_seconds": elapsed, "label": label})
        return cls._bounded_snapshot(safe_state, safe_events)

    def _matching_run_locked(
        self,
        session_id: Any,
        turn_id: Any,
        epoch: Any,
        *,
        require_busy: bool,
    ) -> _Run | None:
        session = self._valid_session_id(session_id)
        turn = self._valid_turn_id(turn_id)
        try:
            expected_epoch = int(epoch)
        except (TypeError, ValueError):
            return None
        run = self._records.get(session) if session and turn else None
        if (
            run is None
            or run.turn_id != turn
            or run.epoch != expected_epoch
            or (require_busy and not run.busy)
        ):
            return None
        return run

    def _elapsed_seconds(self, run: _Run) -> int:
        try:
            elapsed = int(max(0.0, self._clock() - run.started_at))
        except Exception:
            elapsed = 0
        return min(elapsed, KIMI_TERMINAL_MAX_ELAPSED_SECONDS)

    @classmethod
    def _bounded_snapshot(cls, state: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        safe_state = state if state in {"idle", "working"} else "unavailable"
        safe_events = list(events[-KIMI_TERMINAL_MAX_EVENTS:])
        while True:
            content = cls._render_content(safe_state, safe_events)
            payload = {
                "ok": True,
                "target": KIMI_TERMINAL_TARGET,
                "mode": "read_only",
                "state": safe_state,
                "content": content,
                "events": safe_events,
            }
            if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= KIMI_TERMINAL_MAX_BYTES:
                return payload
            if not safe_events:
                # The fixed empty response is much smaller than the cap; this
                # is defensive against a future change to a fixed label.
                return cls.unavailable_snapshot()
            safe_events.pop(0)

    @staticmethod
    def _render_content(state: str, events: list[dict[str, Any]]) -> str:
        phase = "正在处理" if state == "working" else "空闲"
        lines = [
            "Kimi 实时观察 · 只读",
            "命令参数、路径、思考内容与工具输出已隐藏",
            "",
            f"状态：{phase}",
        ]
        for event in events:
            elapsed = int(event["elapsed_seconds"])
            minutes, seconds = divmod(max(0, elapsed), 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] {event['label']}")
        if not events:
            lines.append("等待安全活动事件…")
        return "\n".join(lines) + "\n"
