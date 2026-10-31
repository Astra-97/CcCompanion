"""Remote terminal capture cache (2026-10-31): Tokyo terminals feel local.

Kimi, Kiro and Crew live on Tokyo (≈100 ms RTT from 216).  One App poll of
``GET /tmux/capture`` used to cost several ssh round trips (session / pane
identity, lease, capture-pane, busy probe): Kimi ≈1.6 s, Kiro ≈1.0 s, Crew
≈0.75 s per uncached poll, while the App polls every ≈1.5 s.  The capture also
holds the target's input transaction for that whole time, so a keystroke had
to wait behind whatever poll happened to be running.

This cache sits in front of the *unchanged* per-target capture code (the
producer still does the real acquire / lease / capture-pane / state, so Kimi
and Kiro keep their exact tmux semantics):

* **Background refresher** per (target, lines) while a viewer is polling: it
  re-runs the producer back to back (``refresh_gap_s`` apart), so a poll is
  answered from memory in microseconds with content at most one capture old.
  It stops ``viewer_window_s`` after the last poll — deliberately short, so a
  refresher never keeps (re)acquiring a Kimi / Kiro terminal after the App has
  left the terminal tab (that would steal the session from the chat path).
* **Single-flight**: concurrent polls of one key share one producer run.
* **Input first**: ``input_scope(target)`` (around send / key / resize /
  release) pauses new refreshes for that target, and on exit invalidates its
  entries, so the next poll shows the effect of the input instead of a frame
  captured before it.  A keystroke waits for at most one in-flight capture,
  never for a queue of polls.

The ssh side is already multiplexed (ControlMaster, ControlPersist=10m); while
a viewer is active the refresher's back-to-back calls keep that master warm.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

logger = logging.getLogger("terminal_capture_cache")

Producer = Callable[[], tuple[int, Any]]

FRESH_S = 2.5            # serve a cached frame this old at most (refresher keeps it ≈ capture-time old)
VIEWER_WINDOW_S = 5.0    # refresher keeps going this long after the last poll (App polls every ≈1.5 s)
REFRESH_GAP_S = 0.25     # pause between back-to-back refreshes
WAIT_TIMEOUT_S = 20.0    # longest a poll waits for someone else's in-flight capture
STATS_LOG_S = 300.0


@dataclass
class _Entry:
    at: float
    gen: int
    status: int
    payload: Any


@dataclass
class _Stats:
    requests: int = 0
    hits: int = 0
    shared: int = 0
    fetches: int = 0
    refreshes: int = 0
    fetch_ms_total: float = 0.0
    fetch_ms_max: float = 0.0
    serve_ms_total: float = 0.0
    serve_ms_max: float = 0.0
    since: float = field(default_factory=time.monotonic)

    def view(self) -> dict[str, Any]:
        fetches = self.fetches + self.refreshes
        return {
            "requests": self.requests,
            "cache_hits": self.hits,
            "shared_inflight": self.shared,
            "request_fetches": self.fetches,
            "background_refreshes": self.refreshes,
            "fetch_ms_avg": round(self.fetch_ms_total / fetches, 1) if fetches else None,
            "fetch_ms_max": round(self.fetch_ms_max, 1),
            "serve_ms_avg": round(self.serve_ms_total / self.requests, 1) if self.requests else None,
            "serve_ms_max": round(self.serve_ms_max, 1),
            "window_s": round(time.monotonic() - self.since, 1),
        }


class TerminalCaptureCache:
    def __init__(
        self,
        *,
        fresh_s: float = FRESH_S,
        viewer_window_s: float = VIEWER_WINDOW_S,
        refresh_gap_s: float = REFRESH_GAP_S,
        wait_timeout_s: float = WAIT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self.fresh_s = fresh_s
        self.viewer_window_s = viewer_window_s
        self.refresh_gap_s = refresh_gap_s
        self.wait_timeout_s = wait_timeout_s
        self._clock = clock
        self._background = background
        self._cond = threading.Condition(threading.Lock())
        self._entries: dict[tuple[str, int], _Entry] = {}
        self._inflight: dict[tuple[str, int], int] = {}      # key -> generation it started at
        self._gen: dict[str, int] = {}
        self._input_pending: dict[str, int] = {}
        self._last_request: dict[tuple[str, int], float] = {}
        self._producers: dict[tuple[str, int], Producer] = {}
        self._refreshers: dict[tuple[str, int], threading.Thread] = {}
        self._stats: dict[str, _Stats] = {}
        self._stats_logged_at = clock()

    # ── public ──────────────────────────────────────────────────────────

    def get(self, target: str, lines: int, producer: Producer) -> tuple[int, Any]:
        """(status, payload) for one poll — cached, shared, or freshly produced."""
        key = (target, int(lines))
        started = time.monotonic()
        deadline = started + self.wait_timeout_s
        with self._cond:
            stats = self._stats.setdefault(target, _Stats())
            stats.requests += 1
            self._last_request[key] = self._clock()
            self._producers[key] = producer
            self._ensure_refresher_locked(key)
            waited = False
            while True:
                gen = self._gen.get(target, 0)
                entry = self._entries.get(key)
                if entry is not None and entry.gen == gen and self._clock() - entry.at <= self.fresh_s:
                    if waited:
                        stats.shared += 1
                    else:
                        stats.hits += 1
                    self._note_serve_locked(stats, started)
                    return entry.status, entry.payload
                if key in self._inflight and time.monotonic() < deadline:
                    waited = True
                    self._cond.wait(min(1.0, max(0.01, deadline - time.monotonic())))
                    continue
                self._inflight[key] = gen
                stats.fetches += 1
                break
        status, payload = self._run(key, producer, gen)
        with self._cond:
            self._note_serve_locked(self._stats[target], started)
        return status, payload

    @contextmanager
    def input_scope(self, target: str | None) -> Iterator[None]:
        """Wrap a send / key / resize / release so refreshes yield and the next poll is fresh."""
        if not target:
            yield
            return
        with self._cond:
            self._input_pending[target] = self._input_pending.get(target, 0) + 1
        try:
            yield
        finally:
            self.invalidate(target, _input_done=True)

    def invalidate(self, target: str, *, _input_done: bool = False) -> None:
        with self._cond:
            if _input_done:
                left = self._input_pending.get(target, 1) - 1
                if left > 0:
                    self._input_pending[target] = left
                else:
                    self._input_pending.pop(target, None)
            self._gen[target] = self._gen.get(target, 0) + 1
            self._cond.notify_all()

    def stats(self) -> dict[str, Any]:
        with self._cond:
            return {
                target: {**s.view(), "refreshing": sorted(k[1] for k in self._refreshers if k[0] == target)}
                for target, s in self._stats.items()
            }

    def active_refreshers(self) -> int:
        with self._cond:
            return len(self._refreshers)

    # ── internals ───────────────────────────────────────────────────────

    def _run(self, key: tuple[str, int], producer: Producer, gen: int, *, background: bool = False) -> tuple[int, Any]:
        started = time.monotonic()
        try:
            status, payload = producer()
        except Exception:
            logger.exception("terminal capture producer failed target=%s", key[0])
            status, payload = 503, {"error": "终端捕获失败", "target": key[0]}
        elapsed_ms = (time.monotonic() - started) * 1000.0
        with self._cond:
            self._entries[key] = _Entry(self._clock(), gen, int(status), payload)
            self._inflight.pop(key, None)
            stats = self._stats.setdefault(key[0], _Stats())
            if background:
                stats.refreshes += 1
            stats.fetch_ms_total += elapsed_ms
            stats.fetch_ms_max = max(stats.fetch_ms_max, elapsed_ms)
            self._cond.notify_all()
        return int(status), payload

    @staticmethod
    def _note_serve_locked(stats: _Stats, started: float) -> None:
        ms = (time.monotonic() - started) * 1000.0
        stats.serve_ms_total += ms
        stats.serve_ms_max = max(stats.serve_ms_max, ms)

    def _ensure_refresher_locked(self, key: tuple[str, int]) -> None:
        if not self._background:
            return
        thread = self._refreshers.get(key)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=self._refresh_loop, args=(key,), name=f"term-capture-{key[0]}", daemon=True,
        )
        self._refreshers[key] = thread
        thread.start()

    def _refresh_loop(self, key: tuple[str, int]) -> None:
        target = key[0]
        try:
            while True:
                with self._cond:
                    now = self._clock()
                    if now - self._last_request.get(key, 0.0) > self.viewer_window_s:
                        self._retire_locked(key)
                        return
                    if self._input_pending.get(target) or key in self._inflight:
                        self._cond.wait(0.05)
                        continue
                    gen = self._gen.get(target, 0)
                    entry = self._entries.get(key)
                    if entry is not None and entry.gen == gen:
                        rest = self.refresh_gap_s - (now - entry.at)
                        if rest > 0:
                            self._cond.wait(rest)
                            continue
                    producer = self._producers.get(key)
                    if producer is None:
                        return
                    self._inflight[key] = gen
                self._run(key, producer, gen, background=True)
                self._maybe_log_stats()
        except BaseException:
            with self._cond:
                self._retire_locked(key)
            raise

    def _retire_locked(self, key: tuple[str, int]) -> None:
        if self._refreshers.get(key) is threading.current_thread():
            del self._refreshers[key]
            self._producers.pop(key, None)

    def _maybe_log_stats(self) -> None:
        now = self._clock()
        with self._cond:
            if now - self._stats_logged_at < STATS_LOG_S:
                return
            self._stats_logged_at = now
            snapshot = {t: s.view() for t, s in self._stats.items()}
        for target, view in snapshot.items():
            logger.info(
                "terminal capture %s: requests=%s hits=%s shared=%s fetch_avg=%sms fetch_max=%sms serve_avg=%sms",
                target, view["requests"], view["cache_hits"], view["shared_inflight"],
                view["fetch_ms_avg"], view["fetch_ms_max"], view["serve_ms_avg"],
            )
