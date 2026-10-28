"""Keep Kiro's ACP connection resident (2026-10-28, kiro_keep_warm).

Kiro used to start lazily on the first App message (~2.6 s ssh + kiro-cli
startup on Tokyo).  With ``kiro_keep_warm = true`` a daemon thread checks
every ``interval`` seconds whether the ACP process is up with the persisted
session loaded and, if not, warms it: spawn + initialize + session/load +
model/effort pin replay — exactly the prepare the chat path does before its
first prompt.  It never prompts, never touches chat history, never notifies.

This module only owns the schedule (boot delay, interval, backoff, flap
detection, log de-duplication).  Whether warming is *safe* right now
(terminal tab holding the session, App turn/forge in flight, queued sends)
is decided by the injected ``attempt`` callable in push.py, which returns
one of the outcome strings below or raises on a failed warm.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

# attempt() outcomes
WARMED = "warmed"          # this tick spawned/loaded Kiro
ALREADY_WARM = "warm"      # process up, persisted session loaded
NO_SESSION = "no_session"  # no session pointer yet: nothing to resume
BUSY = "busy"              # App turn / prepare / forge / queue in flight
TERMINAL = "terminal"      # terminal tab's TUI holds the session
# tick() only
BACKOFF = "backoff"
FAILED = "failed"
DISABLED = "disabled"


class KiroKeepWarm:
    def __init__(
        self,
        *,
        attempt: Callable[[], str],
        logger: logging.Logger | None = None,
        interval: float = 20.0,
        boot_delay: float = 5.0,
        backoff_initial: float = 60.0,
        backoff_max: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._attempt = attempt
        self.logger = logger or logging.getLogger(__name__)
        self.interval = max(1.0, float(interval))
        self.boot_delay = max(0.0, float(boot_delay))
        self.backoff_initial = max(1.0, float(backoff_initial))
        self.backoff_max = max(self.backoff_initial, float(backoff_max))
        self._clock = clock
        self._failures = 0
        self._next_attempt_at = 0.0
        self._last_failure_kind = ""
        self._last_outcome = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _schedule_backoff(self, now: float) -> float:
        self._failures += 1
        delay = min(self.backoff_max, self.backoff_initial * (2 ** (self._failures - 1)))
        self._next_attempt_at = now + delay
        return delay

    def tick(self) -> str:
        now = self._clock()
        if now < self._next_attempt_at:
            return BACKOFF
        try:
            outcome = self._attempt()
        except Exception as exc:
            delay = self._schedule_backoff(now)
            kind = type(exc).__name__
            self._last_outcome = FAILED
            if kind != self._last_failure_kind:
                # One warning per failure kind; repeats (same cause, longer
                # backoff) stay at debug so a logged-out Kiro never spams.
                self.logger.warning("Kiro keep-warm failed (%s); next try in %ds", kind, int(delay))
                self._last_failure_kind = kind
            else:
                self.logger.debug("Kiro keep-warm failed again (%s); next try in %ds", kind, int(delay))
            return FAILED
        if outcome == WARMED and self._last_outcome == WARMED:
            # Warmed two ticks in a row with nothing in between: the process
            # died right after the last warm.  Back off instead of respawning
            # it every interval.
            delay = self._schedule_backoff(now)
            if self._last_failure_kind != "flap":
                self.logger.warning("Kiro keep-warm: ACP keeps exiting after warm; next try in %ds", int(delay))
                self._last_failure_kind = "flap"
        elif outcome == WARMED:
            if self._failures:
                self.logger.info("Kiro keep-warm recovered after %d failed tries", self._failures)
            else:
                self.logger.info("Kiro keep-warm: ACP session warmed")
            self._failures = 0
            self._last_failure_kind = ""
        elif outcome == ALREADY_WARM:
            self._failures = 0
            self._last_failure_kind = ""
        self._last_outcome = outcome
        return outcome

    def run(self) -> None:
        if self._stop.wait(self.boot_delay):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.logger.debug("Kiro keep-warm tick crashed", exc_info=True)
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="kiro-keep-warm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
