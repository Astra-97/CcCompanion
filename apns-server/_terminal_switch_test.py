"""Terminal identity routing tests for XiaoKe/Kairos switching.

No test starts tmux, Codex, or the live 8291 service.
"""

from __future__ import annotations

import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import push
from push import (
    KAIROS_TERMINAL_OWNER_OPTION,
    KAIROS_TERMINAL_OWNER_VALUE,
    KAIROS_TERMINAL_READY_OPTION,
    KAIROS_TERMINAL_READY_VALUE,
    KairosTerminalBridge,
    KairosTerminalNotReady,
    KairosTerminalUnavailable,
    PushHandler,
)


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class BridgeLifecycleTest(unittest.TestCase):
    def executable(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "qiaokairos"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o700)
        return tmp, path

    def test_starts_reserved_qiaokairos_pane_and_releases_it(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []

        state = {"exists": False, "marked": False}

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            if argv[1] == "has-session":
                return completed(0 if state["exists"] else 1)
            if argv[1] == "new-session":
                state["exists"] = True
            elif argv[1] == "set-option":
                state["marked"] = True
            elif argv[1] == "show-options":
                return completed(0, "cccompanion:qiaokairos:v1\n") if state["marked"] else completed(1)
            elif argv[1] == "kill-session":
                state["exists"] = False
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        self.addCleanup(bridge.release)
        self.assertEqual(bridge.ensure(), "ccc-kairos-terminal")
        new_call = next(argv for argv in calls if argv[1] == "new-session")
        self.assertEqual(new_call[-2:], ["/bin/sleep", "30"])
        self.assertIn("@ccc_kairos_terminal_owner", next(argv for argv in calls if argv[1] == "set-option"))
        respawn = next(argv for argv in calls if argv[1] == "respawn-pane")
        self.assertEqual(respawn[-1:], [str(command)])
        self.assertNotIn("--no-wait", respawn)
        self.assertEqual(respawn[-3:-1], ["/usr/bin/env", "CCC_KAIROS_TERMINAL_BRIDGE=1"])

        self.assertTrue(bridge.release())
        self.assertIn(["tmux", "kill-session", "-t", "ccc-kairos-terminal"], calls)

    def test_refuses_reserved_name_owned_by_another_process(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            if argv[1] == "has-session":
                return completed(0)
            if argv[1] == "show-options":
                return completed(0, stdout="someone-else\n")
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        with self.assertRaises(KairosTerminalUnavailable):
            bridge.ensure()
        self.assertFalse(any(argv[1] == "kill-session" for argv in calls))

    def test_same_name_reused_without_exact_marker_is_never_killed(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []
        state = {"exists": False, "marked": False}

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0 if state["exists"] else 1)
            if action == "new-session":
                state["exists"] = True
            elif action == "set-option":
                state["marked"] = True
            elif action == "show-options":
                return completed(0, "cccompanion:qiaokairos:v1\n") if state["marked"] else completed(1)
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        bridge.ensure()
        # Original session exits; another process immediately reuses its name.
        state["exists"] = True
        state["marked"] = False
        kill_count = sum(argv[1] == "kill-session" for argv in calls)

        self.assertFalse(bridge.release())
        self.assertEqual(sum(argv[1] == "kill-session" for argv in calls), kill_count)

    def test_kill_failure_is_reported_and_remains_retryable(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []
        state = {"exists": False, "marked": False, "fail_kill": True}

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0 if state["exists"] else 1)
            if action == "new-session":
                state["exists"] = True
            elif action == "set-option":
                state["marked"] = True
            elif action == "show-options":
                return completed(0, "cccompanion:qiaokairos:v1\n") if state["marked"] else completed(1)
            elif action == "kill-session":
                if state["fail_kill"]:
                    return completed(1)
                state["exists"] = False
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        bridge.ensure()
        with self.assertRaisesRegex(KairosTerminalUnavailable, "释放失败"):
            bridge.release()
        state["fail_kill"] = False
        self.assertTrue(bridge.release())
        self.assertEqual(sum(argv[1] == "kill-session" for argv in calls), 2)

    def test_missing_console_entry_fails_without_starting_tmux(self) -> None:
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            return completed(1)

        bridge = KairosTerminalBridge(
            command=Path("/definitely/missing/qiaokairos"),
            idle_seconds=60,
            runner=runner,
        )
        with self.assertRaisesRegex(KairosTerminalUnavailable, "未安装"):
            bridge.ensure()
        self.assertFalse(any(argv[1] == "new-session" for argv in calls))

    def test_dead_exact_owner_pane_is_cleaned_and_rebuilt(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []
        state = {"exists": True, "marked": True, "dead": True}

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0 if state["exists"] else 1)
            if action == "show-options":
                return completed(0, KAIROS_TERMINAL_OWNER_VALUE + "\n") if state["marked"] else completed(1)
            if action == "display-message":
                return completed(0, f"%71|{1 if state['dead'] else 0}\n")
            if action == "kill-session":
                state["exists"] = False
                state["marked"] = False
            elif action == "new-session":
                state["exists"] = True
                state["dead"] = False
            elif action == "set-option":
                state["marked"] = True
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        self.addCleanup(bridge.release)
        self.assertEqual(bridge.ensure(), "ccc-kairos-terminal")
        actions = [argv[1] for argv in calls]
        self.assertLess(actions.index("kill-session"), actions.index("new-session"))
        self.assertEqual(actions.count("kill-session"), 1)
        self.assertEqual(actions.count("new-session"), 1)

    def test_readiness_requires_live_exact_owner_and_exact_pane_marker(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []
        state = {"ready": False}

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0)
            if action == "display-message":
                return completed(0, "%81|0\n")
            if action == "show-options" and argv[-1] == KAIROS_TERMINAL_OWNER_OPTION:
                return completed(0, KAIROS_TERMINAL_OWNER_VALUE + "\n")
            if action == "show-options" and argv[-1] == KAIROS_TERMINAL_READY_OPTION:
                return completed(0, KAIROS_TERMINAL_READY_VALUE + "\n") if state["ready"] else completed(0, "")
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        self.assertEqual(bridge.terminal_state(), ("%81", "waiting"))
        with self.assertRaises(KairosTerminalUnavailable):
            bridge.terminal_state(expected_pane="%82")
        with self.assertRaises(KairosTerminalNotReady):
            bridge.require_ready()
        state["ready"] = True
        self.assertEqual(bridge.terminal_state(expected_pane="%81"), ("%81", "ready"))
        self.assertEqual(bridge.require_ready(), "%81")
        self.assertTrue(any(argv[-1] == KAIROS_TERMINAL_READY_OPTION for argv in calls))

    def test_release_if_pane_does_not_kill_replacement(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0)
            if action == "show-options":
                return completed(0, KAIROS_TERMINAL_OWNER_VALUE + "\n")
            if action == "display-message":
                return completed(0, "%92|0\n")
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        self.assertFalse(bridge.release_if_pane("%91"))
        self.assertFalse(any(argv[1] in {"kill-pane", "kill-session"} for argv in calls))

    def test_release_if_pane_kills_only_matching_exact_pane(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0)
            if action == "show-options":
                return completed(0, KAIROS_TERMINAL_OWNER_VALUE + "\n")
            if action == "display-message":
                return completed(0, "%93|0\n")
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        self.assertTrue(bridge.release_if_pane("%93"))
        self.assertIn(["tmux", "kill-pane", "-t", "%93"], calls)
        self.assertFalse(any(argv[1] == "kill-session" for argv in calls))

    def test_state_recheck_never_cleans_new_dead_replacement(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0)
            if action == "show-options":
                return completed(0, KAIROS_TERMINAL_OWNER_VALUE + "\n")
            if action == "display-message":
                return completed(0, "%95|1\n")
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        with self.assertRaises(KairosTerminalUnavailable):
            bridge.terminal_state(expected_pane="%94")
        self.assertFalse(any(argv[1] in {"kill-pane", "kill-session"} for argv in calls))

    def test_failed_exact_kill_reschedules_reaper_for_replacement(self) -> None:
        tmp, command = self.executable()
        self.addCleanup(tmp.cleanup)
        calls: list[list[str]] = []
        displays = iter(["%96|0\n", "%97|0\n"])

        def runner(argv: list[str], **_kwargs: object) -> types.SimpleNamespace:
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return completed(0)
            if action == "show-options":
                return completed(0, KAIROS_TERMINAL_OWNER_VALUE + "\n")
            if action == "display-message":
                return completed(0, next(displays, "%97|0\n"))
            if action == "kill-pane":
                return completed(1, stderr="pane changed")
            return completed(0)

        bridge = KairosTerminalBridge(command=command, idle_seconds=60, runner=runner)
        self.addCleanup(bridge.release)
        self.assertFalse(bridge.release_if_pane("%96"))
        self.assertIsNotNone(bridge._timer)
        self.assertTrue(any(argv[1] == "kill-pane" for argv in calls))


class FakeBridge:
    def __init__(
        self,
        *,
        unavailable: bool = False,
        release_unavailable: bool = False,
        ready: bool = True,
    ) -> None:
        self.unavailable = unavailable
        self.release_unavailable = release_unavailable
        self.ready = ready
        self.ensure_calls = 0
        self.ready_calls = 0
        self.state_calls: list[str | None] = []
        self.release_calls = 0
        self.release_if_calls: list[str] = []
        self.input_lock = threading.Lock()

    def input_transaction(self):
        return self.input_lock

    def ensure(self) -> str:
        self.ensure_calls += 1
        if self.unavailable:
            raise KairosTerminalUnavailable("Kairos 暂时不可用")
        return "ccc-kairos-terminal"

    def require_ready(self) -> str:
        self.ready_calls += 1
        if not self.ready:
            raise KairosTerminalNotReady("Kairos 正在回复；等待当前回复结束后即可操作终端")
        return "%42"

    def terminal_state(self, *, expected_pane: str | None = None) -> tuple[str, str]:
        self.state_calls.append(expected_pane)
        if self.unavailable:
            raise KairosTerminalUnavailable("Kairos 暂时不可用")
        if expected_pane is not None and expected_pane != "%42":
            raise KairosTerminalUnavailable("Kairos 终端 pane 已更换")
        return "%42", "ready" if self.ready else "waiting"

    def release(self) -> bool:
        self.release_calls += 1
        if self.release_unavailable:
            raise KairosTerminalUnavailable("release failed")
        return True

    def release_if_pane(self, expected_pane: str) -> bool:
        self.release_if_calls.append(expected_pane)
        return True


class TrackingInputBridge(FakeBridge):
    """Expose when a second handler has reached the shared input guard."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.second_transaction_attempted = threading.Event()
        self._transaction_count = 0
        self._transaction_count_lock = threading.Lock()

    def input_transaction(self):
        with self._transaction_count_lock:
            self._transaction_count += 1
            if self._transaction_count >= 2:
                self.second_transaction_attempted.set()
        return super().input_transaction()


class HandlerRoutingTest(unittest.TestCase):
    def handler(
        self,
        bridge: FakeBridge,
        path: str = "/tmux/capture?session=kairos&lines=50",
        observer: object | None = None,
    ) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.state = types.SimpleNamespace(
            default_session="cctg",
            active_session="cctg",
            kairos_terminal=bridge,
            codex_app_bridge=observer,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    @mock.patch("push.subprocess.run")
    def test_capture_uses_physical_pane_but_returns_public_identity(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="kairos pane\n")
        bridge = FakeBridge()
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        self.assertEqual(handler.responses[-1], (
            200,
            {
                "ok": True,
                "session": "kairos",
                "content": "kairos pane\n",
                "state": "ready",
            },
        ))
        self.assertEqual(run.call_args.args[0][3], "%42")
        self.assertEqual(bridge.ensure_calls, 1)
        self.assertEqual(bridge.ready_calls, 0)
        self.assertEqual(bridge.state_calls, [None, "%42"])
        self.assertEqual(bridge.release_calls, 0)

    @mock.patch("push.subprocess.run")
    def test_capture_reports_waiting_from_exact_pane_marker(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="qiaokairos is waiting\n")
        bridge = FakeBridge(ready=False)
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "waiting")
        self.assertEqual(payload["content"], "qiaokairos is waiting\n")
        self.assertEqual(bridge.state_calls, [None, "%42"])

    @mock.patch("push.subprocess.run")
    def test_waiting_capture_renders_only_redacted_live_observer(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="qiaokairos raw waiting pane\n" + "\n" * 57)

        class Observer:
            @staticmethod
            def observer_snapshot() -> dict[str, object]:
                return {
                    "busy": True,
                    "phase": "正在处理",
                    "events": [
                        {"elapsed_seconds": 2, "label": "运行命令（参数与输出已隐藏）"},
                        {"elapsed_seconds": 3, "label": "SECRET must not pass"},
                    ],
                }

        bridge = FakeBridge(ready=False)
        handler = self.handler(bridge, observer=Observer())

        handler._handle_tmux_capture()

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "waiting")
        self.assertIn("实时观察", payload["content"])
        self.assertIn("[00:02] 运行命令（参数与输出已隐藏）", payload["content"])
        self.assertNotIn("SECRET", payload["content"])
        self.assertNotIn("qiaokairos raw", payload["content"])

    @mock.patch("push.subprocess.run")
    def test_capture_trims_unused_tmux_bottom_rows(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="line one\nline two\n" + "\n" * 57)
        bridge = FakeBridge()
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        self.assertEqual(handler.responses[-1][1]["content"], "line one\nline two\n")

    @mock.patch("push.subprocess.run")
    def test_capture_pane_replacement_fails_closed_and_releases(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="stale pane output\n")

        class ReplacedPaneBridge(FakeBridge):
            def terminal_state(
                self, *, expected_pane: str | None = None,
            ) -> tuple[str, str]:
                if expected_pane is not None:
                    raise KairosTerminalUnavailable("Kairos 终端 pane 已更换")
                return super().terminal_state(expected_pane=expected_pane)

            def release_if_pane(self, expected_pane: str) -> bool:
                self.release_if_calls.append(expected_pane)
                return False

        bridge = ReplacedPaneBridge()
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        self.assertEqual(handler.responses[-1][0], 503)
        self.assertEqual(handler.responses[-1][1]["target"], "kairos")
        self.assertEqual(bridge.release_calls, 0)
        self.assertEqual(bridge.release_if_calls, ["%42"])

    @mock.patch("push.subprocess.run")
    def test_capture_subprocess_failure_uses_exact_pane_cleanup(self, run: mock.Mock) -> None:
        run.return_value = completed(1, stderr="capture failed")
        bridge = FakeBridge()
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        self.assertEqual(handler.responses[-1][0], 503)
        self.assertEqual(bridge.release_calls, 0)
        self.assertEqual(bridge.release_if_calls, ["%42"])

    @mock.patch("push.subprocess.run")
    def test_capture_never_reports_success_with_blank_kairos_content(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="\n")
        bridge = FakeBridge()
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"], "kairos")
        self.assertEqual(payload["state"], "ready")
        self.assertIn("终端已连接", payload["content"])
        self.assertTrue(payload["content"].strip())
        self.assertEqual(bridge.release_calls, 0)

    @mock.patch("push.subprocess.run")
    def test_blank_waiting_capture_returns_read_only_fallback(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="\n")
        bridge = FakeBridge(ready=False)
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "waiting")
        self.assertIn("只读", payload["content"])
        self.assertTrue(payload["content"].strip())

    @mock.patch("push.subprocess.run")
    def test_unavailable_kairos_never_falls_through_to_xiaoke(self, run: mock.Mock) -> None:
        bridge = FakeBridge(unavailable=True)
        handler = self.handler(bridge)

        handler._handle_tmux_capture()

        self.assertEqual(handler.responses[-1][0], 503)
        self.assertEqual(handler.responses[-1][1]["target"], "kairos")
        run.assert_not_called()

    @mock.patch("push.subprocess.run")
    def test_selecting_xiaoke_releases_kairos_and_preserves_target(self, run: mock.Mock) -> None:
        run.return_value = completed(0, stdout="xiaoke pane\n")
        bridge = FakeBridge()
        handler = self.handler(bridge, "/tmux/capture?session=cctg&lines=20")

        handler._handle_tmux_capture()

        self.assertEqual(bridge.release_calls, 1)
        self.assertEqual(handler.responses[-1][1]["session"], "cctg")
        self.assertEqual(handler.responses[-1][1]["state"], "ready")
        self.assertIn("cctg", run.call_args.args[0])

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_kairos_text_uses_private_buffer_and_physical_target(
        self,
        run: mock.Mock,
        popen: mock.Mock,
    ) -> None:
        run.return_value = completed(0)
        process = popen.return_value
        process.communicate.return_value = (b"", b"")
        process.returncode = 0
        bridge = FakeBridge()
        handler = self.handler(bridge)

        handler._handle_tmux_send({"session": "kairos", "keys": "hello", "enter": True})

        self.assertEqual(handler.responses[-1], (200, {"ok": True, "session": "kairos"}))
        load_argv = popen.call_args.args[0]
        self.assertEqual(load_argv[:2], ["tmux", "load-buffer"])
        self.assertIn("-b", load_argv)
        paste_argv = run.call_args_list[0].args[0]
        self.assertEqual(paste_argv[:2], ["tmux", "paste-buffer"])
        self.assertIn("%42", paste_argv)
        self.assertIn("-d", paste_argv)
        process.communicate.assert_called_once_with(input=b"hello", timeout=3)

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_waiting_kairos_rejects_text_and_enter_without_any_injection(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        for body in (
            {"session": "kairos", "keys": "do not queue", "enter": True},
            {"session": "kairos", "keys": "", "enter": True},
        ):
            bridge = FakeBridge(ready=False)
            handler = self.handler(bridge)
            handler._handle_tmux_send(body)
            self.assertEqual(handler.responses[-1][0], 423)
            self.assertEqual(handler.responses[-1][1]["state"], "waiting")
            self.assertEqual(bridge.ready_calls, 1)
        run.assert_not_called()
        popen.assert_not_called()

    @mock.patch("push.subprocess.run")
    def test_waiting_kairos_rejects_both_key_endpoints_without_send_keys(
        self, run: mock.Mock,
    ) -> None:
        bridge = FakeBridge(ready=False)
        handler = self.handler(bridge)
        handler._handle_terminal_key({"session": "kairos", "key": "Escape"})
        self.assertEqual(handler.responses[-1][0], 423)

        bridge = FakeBridge(ready=False)
        handler = self.handler(bridge)
        handler._handle_tmux_send({"session": "kairos", "key": "Escape"})
        self.assertEqual(handler.responses[-1][0], 423)
        run.assert_not_called()

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_concurrent_kairos_text_transactions_cannot_interleave(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        bridge = TrackingInputBridge()
        first_handler = self.handler(bridge)
        second_handler = self.handler(bridge)
        first_load_started = threading.Event()
        release_first_load = threading.Event()
        second_load_started = threading.Event()
        buffers: dict[str, str] = {}
        operations: list[tuple[str, str]] = []

        def make_process(argv: list[str], **_kwargs: object):
            buffer_name = argv[argv.index("-b") + 1]

            class Process:
                returncode = 0

                def communicate(self, *, input: bytes | None = None, timeout: float | None = None):
                    del timeout
                    text = (input or b"").decode("utf-8")
                    buffers[buffer_name] = text
                    operations.append(("load", text))
                    if text == "first":
                        first_load_started.set()
                        self.assert_released()
                    else:
                        second_load_started.set()
                    return b"", b""

                def assert_released(self):
                    if not release_first_load.wait(2.0):
                        raise AssertionError("test did not release first load")

                def kill(self):
                    return None

            return Process()

        def run_tmux(argv: list[str], **_kwargs: object):
            if argv[1] == "paste-buffer":
                buffer_name = argv[argv.index("-b") + 1]
                operations.append(("paste", buffers[buffer_name]))
            elif argv[1] == "send-keys":
                operations.append(("key", argv[-1]))
            return completed(0)

        popen.side_effect = make_process
        run.side_effect = run_tmux
        first = threading.Thread(target=lambda: first_handler._handle_tmux_send({
            "session": "kairos", "keys": "first", "enter": True,
        }))
        second = threading.Thread(target=lambda: second_handler._handle_tmux_send({
            "session": "kairos", "keys": "second", "enter": True,
        }))
        first.start()
        self.assertTrue(first_load_started.wait(1.0))
        second.start()
        self.assertTrue(bridge.second_transaction_attempted.wait(1.0))
        self.assertFalse(second_load_started.is_set())
        self.assertEqual(bridge.ready_calls, 1)
        release_first_load.set()
        first.join(2.0)
        second.join(2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(bridge.ready_calls, 2)
        self.assertEqual(operations, [
            ("load", "first"),
            ("paste", "first"),
            ("key", "Enter"),
            ("load", "second"),
            ("paste", "second"),
            ("key", "Enter"),
        ])
        self.assertEqual(first_handler.responses[-1][0], 200)
        self.assertEqual(second_handler.responses[-1][0], 200)

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_kairos_special_key_waits_for_text_paste_and_enter(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        bridge = TrackingInputBridge()
        text_handler = self.handler(bridge)
        key_handler = self.handler(bridge)
        load_started = threading.Event()
        release_load = threading.Event()
        key_sent = threading.Event()
        operations: list[str] = []
        process = popen.return_value
        process.returncode = 0

        def communicate(*, input: bytes | None = None, timeout: float | None = None):
            del input, timeout
            operations.append("load")
            load_started.set()
            if not release_load.wait(2.0):
                raise AssertionError("test did not release load")
            return b"", b""

        def run_tmux(argv: list[str], **_kwargs: object):
            if argv[1] == "paste-buffer":
                operations.append("paste")
            elif argv[1] == "send-keys":
                operations.append(str(argv[-1]))
                if argv[-1] == "Escape":
                    key_sent.set()
            return completed(0)

        process.communicate.side_effect = communicate
        run.side_effect = run_tmux
        text = threading.Thread(target=lambda: text_handler._handle_tmux_send({
            "session": "kairos", "keys": "text", "enter": True,
        }))
        key = threading.Thread(target=lambda: key_handler._handle_terminal_key({
            "session": "kairos", "key": "Escape",
        }))
        text.start()
        self.assertTrue(load_started.wait(1.0))
        key.start()
        self.assertTrue(bridge.second_transaction_attempted.wait(1.0))
        self.assertFalse(key_sent.is_set())
        self.assertEqual(bridge.ready_calls, 1)
        release_load.set()
        text.join(2.0)
        key.join(2.0)

        self.assertFalse(text.is_alive())
        self.assertFalse(key.is_alive())
        self.assertEqual(operations, ["load", "paste", "Enter", "Escape"])
        self.assertEqual(bridge.ready_calls, 2)

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_non_kairos_key_is_not_blocked_by_non_kairos_text_load(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        bridge = FakeBridge()
        text_handler = self.handler(bridge)
        key_handler = self.handler(bridge)
        load_started = threading.Event()
        release_load = threading.Event()
        key_sent = threading.Event()
        process = popen.return_value
        process.returncode = 0

        def communicate(*, input: bytes | None = None, timeout: float | None = None):
            del input, timeout
            load_started.set()
            if not release_load.wait(2.0):
                raise AssertionError("test did not release load")
            return b"", b""

        def run_tmux(argv: list[str], **_kwargs: object):
            if argv[1] == "send-keys" and argv[-1] == "Escape":
                key_sent.set()
            return completed(0)

        process.communicate.side_effect = communicate
        run.side_effect = run_tmux
        text = threading.Thread(target=lambda: text_handler._handle_tmux_send({
            "session": "cctg", "keys": "text", "enter": True,
        }))
        key = threading.Thread(target=lambda: key_handler._handle_terminal_key({
            "session": "other-pane", "key": "Escape",
        }))
        text.start()
        self.assertTrue(load_started.wait(1.0))
        key.start()
        self.assertTrue(key_sent.wait(0.5))
        release_load.set()
        text.join(2.0)
        key.join(2.0)

        self.assertFalse(text.is_alive())
        self.assertFalse(key.is_alive())

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_switch_to_regular_target_waits_for_kairos_input_transaction(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        bridge = TrackingInputBridge()
        kairos_handler = self.handler(bridge)
        regular_handler = self.handler(bridge)
        load_started = threading.Event()
        release_load = threading.Event()
        regular_key_sent = threading.Event()
        operations: list[str] = []
        process = popen.return_value
        process.returncode = 0

        def communicate(*, input: bytes | None = None, timeout: float | None = None):
            del input, timeout
            operations.append("kairos-load")
            load_started.set()
            if not release_load.wait(2.0):
                raise AssertionError("test did not release Kairos load")
            return b"", b""

        def run_tmux(argv: list[str], **_kwargs: object):
            if argv[1] == "paste-buffer":
                operations.append("kairos-paste")
            elif argv[1] == "send-keys":
                operations.append(str(argv[-1]))
                if argv[-1] == "Escape":
                    regular_key_sent.set()
            return completed(0)

        process.communicate.side_effect = communicate
        run.side_effect = run_tmux
        kairos = threading.Thread(target=lambda: kairos_handler._handle_tmux_send({
            "session": "kairos", "keys": "text", "enter": True,
        }))
        regular = threading.Thread(target=lambda: regular_handler._handle_terminal_key({
            "session": "cctg", "key": "Escape",
        }))
        kairos.start()
        self.assertTrue(load_started.wait(1.0))
        regular.start()
        self.assertTrue(bridge.second_transaction_attempted.wait(1.0))
        self.assertFalse(regular_key_sent.is_set())
        self.assertEqual(bridge.release_calls, 0)
        release_load.set()
        kairos.join(2.0)
        regular.join(2.0)

        self.assertFalse(kairos.is_alive())
        self.assertFalse(regular.is_alive())
        self.assertEqual(bridge.release_calls, 1)
        self.assertEqual(operations, [
            "kairos-load", "kairos-paste", "Enter", "Escape",
        ])

    def assert_kairos_input_failure_cleans_and_releases(
        self,
        handler: PushHandler,
        bridge: FakeBridge,
        run: mock.Mock,
    ) -> None:
        self.assertEqual(handler.responses[-1][0], 503)
        self.assertEqual(handler.responses[-1][1]["target"], "kairos")
        self.assertEqual(bridge.release_calls, 0)
        self.assertEqual(bridge.release_if_calls, ["%42"])
        self.assertTrue(any(call.args[0][1] == "delete-buffer" for call in run.call_args_list))

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_kairos_load_failure_is_503_cleans_buffer_and_releases(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        run.return_value = completed(0)
        popen.return_value.returncode = 1
        bridge = FakeBridge()
        handler = self.handler(bridge)
        handler._handle_tmux_send({"session": "kairos", "keys": "hello", "enter": True})
        self.assert_kairos_input_failure_cleans_and_releases(handler, bridge, run)

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_kairos_paste_failure_is_503_cleans_buffer_and_releases(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        popen.return_value.returncode = 0
        run.side_effect = lambda argv, **_kwargs: completed(1 if argv[1] == "paste-buffer" else 0)
        bridge = FakeBridge()
        handler = self.handler(bridge)
        handler._handle_tmux_send({"session": "kairos", "keys": "hello", "enter": True})
        self.assert_kairos_input_failure_cleans_and_releases(handler, bridge, run)

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_kairos_enter_failure_is_503_cleans_buffer_and_releases(
        self, run: mock.Mock, popen: mock.Mock,
    ) -> None:
        popen.return_value.returncode = 0
        run.side_effect = lambda argv, **_kwargs: completed(
            1 if argv[1] == "send-keys" and argv[-1] == "Enter" else 0
        )
        bridge = FakeBridge()
        handler = self.handler(bridge)
        handler._handle_tmux_send({"session": "kairos", "keys": "hello", "enter": True})
        self.assert_kairos_input_failure_cleans_and_releases(handler, bridge, run)

    @mock.patch("push.subprocess.run")
    def test_kairos_key_failure_is_503_and_releases(self, run: mock.Mock) -> None:
        run.return_value = completed(1)
        bridge = FakeBridge()
        handler = self.handler(bridge)
        handler._handle_terminal_key({"session": "kairos", "key": "Escape"})
        self.assertEqual(handler.responses[-1][0], 503)
        self.assertEqual(bridge.release_calls, 0)
        self.assertEqual(bridge.release_if_calls, ["%42"])

    def test_release_rejects_non_kairos_alias(self) -> None:
        bridge = FakeBridge()
        handler = self.handler(bridge)
        handler._handle_terminal_release({"target": "cctg"})
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(bridge.release_calls, 0)

    def test_release_reports_exact_kairos_result(self) -> None:
        bridge = FakeBridge()
        handler = self.handler(bridge)
        handler._handle_terminal_release({"target": "kairos"})
        self.assertEqual(handler.responses[-1], (
            200, {"ok": True, "target": "kairos", "released": True},
        ))


class TerminalAuthTest(unittest.TestCase):
    def handler(self, path: str) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.state = types.SimpleNamespace(
            allowed_ips=[],
            shared_secret="required-secret",
            strict_auth=True,
            allow_remote_control=True,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    def test_kairos_capture_requires_auth_before_terminal_resolution(self) -> None:
        handler = self.handler("/tmux/capture?session=kairos")
        handler._handle_tmux_capture = mock.Mock()

        handler.do_GET()

        self.assertEqual(handler.responses, [(401, {"error": "unauthorized"})])
        handler._handle_tmux_capture.assert_not_called()

    def test_kairos_key_requires_write_auth_before_body_or_routing(self) -> None:
        handler = self.handler("/terminal/key")
        handler.command = "POST"
        handler._read_body = mock.Mock()
        handler._handle_terminal_key = mock.Mock()

        handler.do_POST()

        self.assertEqual(handler.responses, [(401, {"error": "unauthorized"})])
        handler._read_body.assert_not_called()
        handler._handle_terminal_key.assert_not_called()

    def test_kairos_release_requires_write_auth_before_body_or_routing(self) -> None:
        handler = self.handler("/terminal/release")
        handler.command = "POST"
        handler._read_body = mock.Mock()
        handler._handle_terminal_release = mock.Mock()

        handler.do_POST()

        self.assertEqual(handler.responses, [(401, {"error": "unauthorized"})])
        handler._read_body.assert_not_called()
        handler._handle_terminal_release.assert_not_called()

    def test_authenticated_release_routes_only_through_remote_control_gate(self) -> None:
        handler = self.handler("/terminal/release")
        handler.command = "POST"
        handler.headers = {"X-Auth-Token": "required-secret"}
        handler._read_body = mock.Mock(return_value={"target": "kairos"})
        handler._handle_terminal_release = mock.Mock()

        handler.do_POST()

        handler._handle_terminal_release.assert_called_once_with({"target": "kairos"})

        blocked = self.handler("/terminal/release")
        blocked.command = "POST"
        blocked.headers = {"X-Auth-Token": "required-secret"}
        blocked.state.allow_remote_control = False
        blocked._read_body = mock.Mock(return_value={"target": "kairos"})
        blocked._handle_terminal_release = mock.Mock()

        blocked.do_POST()

        self.assertEqual(blocked.responses, [(403, {"error": "remote_control disabled"})])
        blocked._handle_terminal_release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
