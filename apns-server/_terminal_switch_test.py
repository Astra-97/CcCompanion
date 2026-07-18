"""Terminal identity routing tests for XiaoKe/Kairos switching.

No test starts tmux, Codex, or the live 8291 service.
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import push
from push import KairosTerminalBridge, KairosTerminalUnavailable, PushHandler


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
        self.assertEqual(respawn[-2:], [str(command), "--no-wait"])

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


class FakeBridge:
    def __init__(self, *, unavailable: bool = False, release_unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.release_unavailable = release_unavailable
        self.ensure_calls = 0
        self.release_calls = 0

    def ensure(self) -> str:
        self.ensure_calls += 1
        if self.unavailable:
            raise KairosTerminalUnavailable("Kairos 暂时不可用")
        return "ccc-kairos-terminal"

    def release(self) -> bool:
        self.release_calls += 1
        if self.release_unavailable:
            raise KairosTerminalUnavailable("release failed")
        return True


class HandlerRoutingTest(unittest.TestCase):
    def handler(self, bridge: FakeBridge, path: str = "/tmux/capture?session=kairos&lines=50") -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.state = types.SimpleNamespace(
            default_session="cctg",
            active_session="cctg",
            kairos_terminal=bridge,
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
            {"ok": True, "session": "kairos", "content": "kairos pane\n"},
        ))
        self.assertEqual(run.call_args.args[0][3], "ccc-kairos-terminal")
        self.assertEqual(bridge.ensure_calls, 1)
        self.assertEqual(bridge.release_calls, 0)

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
        self.assertIn("ccc-kairos-terminal", paste_argv)
        self.assertIn("-d", paste_argv)
        process.communicate.assert_called_once_with(input=b"hello", timeout=3)

    def assert_kairos_input_failure_cleans_and_releases(
        self,
        handler: PushHandler,
        bridge: FakeBridge,
        run: mock.Mock,
    ) -> None:
        self.assertEqual(handler.responses[-1][0], 503)
        self.assertEqual(handler.responses[-1][1]["target"], "kairos")
        self.assertEqual(bridge.release_calls, 1)
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
        self.assertEqual(bridge.release_calls, 1)

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
