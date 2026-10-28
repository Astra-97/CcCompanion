"""Tests for kiro 常驻 (2026-10-28): KiroKeepWarm schedule + push.py safety gates.

No test spawns kiro-cli or ssh: the ACP client and terminal bridge are fakes.
"""
from collections import deque
from pathlib import Path
import sys
import threading
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kiro_acp import KiroACPAuthRequired, KiroACPError
from kiro_keep_warm import KiroKeepWarm
from push import _kiro_keep_warm_attempt


class FakeACP:
    def __init__(self, *, pointer="sess_1", warm=False, busy=False, fail=None):
        self.pointer = pointer
        self.warm = warm
        self.busy = busy
        self.fail = fail
        self.prepares = []
        self.closes = 0
        self.prompts = 0
        self.token_during_prepare = None
        self.state = None

    def load_session_id(self):
        return self.pointer

    def is_warm(self, session_id):
        return self.warm and session_id == self.pointer

    def prepare_session(self, *, model=None, effort=None):
        self.token_during_prepare = self.state.kiro_prepare_token if self.state else None
        self.prepares.append((model, effort))
        if self.fail is not None:
            raise self.fail
        self.warm = True
        return self.pointer

    def prompt_existing(self, *args, **kwargs):  # must never be called
        self.prompts += 1

    def close(self):
        self.closes += 1
        self.warm = False


class FakeTerminal:
    def __init__(self, *, live=False):
        self.live = live
        self.lock = threading.Lock()
        self.checks = 0

    def input_transaction(self):
        return self.lock

    def has_live_pane(self):
        assert self.lock.locked(), "pane check must run inside the input transaction"
        self.checks += 1
        return self.live


class FakePrefs:
    def snapshot_model(self):
        return "claude-sonnet-x"

    def snapshot_effort(self):
        return ""


def make_state(acp, terminal=None):
    state = types.SimpleNamespace(
        kiro_acp=acp,
        kiro_terminal=terminal if terminal is not None else FakeTerminal(),
        kiro_turn_lock=threading.RLock(),
        kiro_active_turn={},
        kiro_prepare_token="",
        kiro_terminal_acquire_token="",
        kiro_chat_queue=deque(),
        kiro_preferences=FakePrefs(),
    )
    acp.state = state
    return state


class AttemptGateTest(unittest.TestCase):
    def test_warms_idle_kiro_without_prompt_and_releases_reservation(self):
        acp = FakeACP()
        state = make_state(acp)
        self.assertEqual(_kiro_keep_warm_attempt(state), "warmed")
        self.assertEqual(acp.prepares, [("claude-sonnet-x", None)])
        self.assertTrue(acp.token_during_prepare, "warm must hold a prepare reservation")
        self.assertEqual(state.kiro_prepare_token, "")
        self.assertEqual(acp.prompts, 0)

    def test_already_warm_is_a_noop(self):
        acp = FakeACP(warm=True)
        terminal = FakeTerminal()
        state = make_state(acp, terminal)
        self.assertEqual(_kiro_keep_warm_attempt(state), "warm")
        self.assertEqual(acp.prepares, [])
        self.assertEqual(terminal.checks, 0)

    def test_no_pointer_never_creates_a_session(self):
        acp = FakeACP(pointer="")
        self.assertEqual(_kiro_keep_warm_attempt(make_state(acp)), "no_session")
        self.assertEqual(acp.prepares, [])

    def test_terminal_holding_session_blocks_warm(self):
        acp = FakeACP()
        terminal = FakeTerminal(live=True)
        state = make_state(acp, terminal)
        self.assertEqual(_kiro_keep_warm_attempt(state), "terminal")
        self.assertEqual(acp.prepares, [])
        self.assertEqual(acp.closes, 0)
        self.assertEqual(state.kiro_prepare_token, "")

    def test_turn_prepare_forge_acquire_or_queue_blocks_warm(self):
        for field, value in (
            ("kiro_active_turn", {"user_ts": "1"}),
            ("kiro_prepare_token", "forge-or-chat"),
            ("kiro_terminal_acquire_token", "acq"),
            ("kiro_chat_queue", deque([{"record": {"ts": "1"}}])),
        ):
            with self.subTest(field=field):
                acp = FakeACP()
                state = make_state(acp)
                setattr(state, field, value)
                self.assertEqual(_kiro_keep_warm_attempt(state), "busy")
                self.assertEqual(acp.prepares, [])
                self.assertEqual(getattr(state, field), value)

    def test_running_prompt_blocks_warm(self):
        acp = FakeACP(busy=True)
        self.assertEqual(_kiro_keep_warm_attempt(make_state(acp)), "busy")
        self.assertEqual(acp.prepares, [])

    def test_failed_warm_closes_and_raises_and_releases(self):
        acp = FakeACP(fail=KiroACPAuthRequired("Kiro login is required"))
        state = make_state(acp)
        with self.assertRaises(KiroACPAuthRequired):
            _kiro_keep_warm_attempt(state)
        self.assertEqual(acp.closes, 1)
        self.assertEqual(state.kiro_prepare_token, "")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScheduleTest(unittest.TestCase):
    def make(self, outcomes):
        clock = Clock()
        calls = []

        def attempt():
            calls.append(clock.now)
            item = outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        warm = KiroKeepWarm(attempt=attempt, interval=20, backoff_initial=60, backoff_max=240, clock=clock)
        return warm, clock, calls

    def test_backoff_doubles_caps_and_resets_on_success(self):
        err = KiroACPError("Kiro ACP exited")
        warm, clock, calls = self.make([err, err, err, err, "warmed", "warm"])
        with self.assertLogs("kiro_keep_warm", level="DEBUG") as logs:
            self.assertEqual(warm.tick(), "failed")       # next in 60
            clock.now += 20
            self.assertEqual(warm.tick(), "backoff")
            clock.now += 40
            self.assertEqual(warm.tick(), "failed")       # next in 120
            clock.now += 119
            self.assertEqual(warm.tick(), "backoff")
            clock.now += 1
            self.assertEqual(warm.tick(), "failed")       # next in 240
            clock.now += 240
            self.assertEqual(warm.tick(), "failed")       # capped at 240
            clock.now += 240
            self.assertEqual(warm.tick(), "warmed")
            clock.now += 20
            self.assertEqual(warm.tick(), "warm")
        self.assertEqual(len(calls), 6)
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1, "same failure kind must warn once")
        self.assertEqual(warm._failures, 0)

    def test_terminal_and_busy_skips_do_not_back_off(self):
        warm, clock, calls = self.make(["terminal", "terminal", "busy", "warmed"])
        for _ in range(4):
            warm.tick()
            clock.now += 20
        self.assertEqual(len(calls), 4)

    def test_flapping_process_backs_off(self):
        warm, clock, calls = self.make(["warmed", "warmed", "warmed"])
        self.assertEqual(warm.tick(), "warmed")
        clock.now += 20
        self.assertEqual(warm.tick(), "warmed")   # died within one interval
        clock.now += 20
        self.assertEqual(warm.tick(), "backoff")
        clock.now += 40
        self.assertEqual(warm.tick(), "warmed")
        self.assertEqual(len(calls), 3)

    def test_rewarm_after_terminal_release(self):
        acp = FakeACP()
        terminal = FakeTerminal(live=True)
        state = make_state(acp, terminal)
        clock = Clock()
        warm = KiroKeepWarm(attempt=lambda: _kiro_keep_warm_attempt(state), clock=clock)
        self.assertEqual(warm.tick(), "terminal")
        terminal.live = False  # tab released / reaped
        clock.now += 20
        self.assertEqual(warm.tick(), "warmed")
        clock.now += 20
        self.assertEqual(warm.tick(), "warm")
        self.assertEqual(len(acp.prepares), 1)
        self.assertEqual(acp.prompts, 0)

    def test_thread_stops(self):
        hits = threading.Event()

        def attempt():
            hits.set()
            return "warm"

        warm = KiroKeepWarm(attempt=attempt, interval=1, boot_delay=0)
        warm.start()
        self.assertTrue(hits.wait(2))
        warm.stop()
        warm._thread.join(3)
        self.assertFalse(warm._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
