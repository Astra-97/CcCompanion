import json
from contextlib import nullcontext
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kimi_terminal_observer import (
    KIMI_TERMINAL_MAX_BYTES,
    KIMI_TERMINAL_MAX_EVENTS,
    KimiTerminalObserver,
)
from push import KIMI_TERMINAL_OWNER_VALUE, KimiTerminalBridge, PushHandler


class KimiTerminalObserverTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.observer = KimiTerminalObserver(clock=lambda: self.now)

    def test_strict_bounded_prompt_free_projection(self):
        epoch = self.observer.begin("session-one", "turn-one")
        self.assertIsNotNone(epoch)
        raw = {
            "kind": "activity",
            "label": "正在使用工具",
            "command": "cat /root/secret --token=never-show",
            "arguments": {"prompt": "private thought"},
            "output": "private output",
        }
        self.now = 105.0
        self.assertTrue(self.observer.record_activity("session-one", "turn-one", epoch, raw))
        for _ in range(KIMI_TERMINAL_MAX_EVENTS + 15):
            self.now += 1
            self.observer.record_activity("session-one", "turn-one", epoch, raw)
        self.assertTrue(self.observer.finish("session-one", "turn-one", epoch, "completed"))

        payload = self.observer.snapshot("session-one")
        self.assertEqual(
            {"ok", "target", "mode", "state", "content", "events"},
            set(payload),
        )
        self.assertEqual("kimi", payload["target"])
        self.assertEqual("read_only", payload["mode"])
        self.assertEqual("idle", payload["state"])
        self.assertLessEqual(len(payload["events"]), KIMI_TERMINAL_MAX_EVENTS)
        self.assertLessEqual(
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            KIMI_TERMINAL_MAX_BYTES,
        )
        rendered = str(payload)
        for forbidden in ("/root/secret", "never-show", "private thought", "private output", "session-one", "turn-one"):
            self.assertNotIn(forbidden, rendered)
        self.assertTrue(all(
            isinstance(event["elapsed_seconds"], int)
            and event["elapsed_seconds"] >= 0
            and event["label"] in {
                "已接收任务，正在准备",
                "正在思考（内容已隐藏）",
                "正在使用工具（名称与参数已隐藏）",
                "Kimi 协作 worker 已开始",
                "Kimi 协作 worker 已完成",
                "Kimi 协作 worker 未完成",
                "本轮已完成",
            }
            for event in payload["events"]
        ))

    def test_session_and_turn_epochs_drop_stale_callbacks(self):
        old_epoch = self.observer.begin("session-one", "turn-old")
        new_epoch = self.observer.begin("session-one", "turn-new")
        self.assertFalse(self.observer.record_activity(
            "session-one", "turn-old", old_epoch,
            {"kind": "activity", "label": "正在思考"},
        ))
        self.assertTrue(self.observer.record_activity(
            "session-one", "turn-new", new_epoch,
            {"kind": "activity", "label": "正在思考"},
        ))
        other_epoch = self.observer.begin("session-two", "turn-other")
        self.assertTrue(self.observer.record_activity(
            "session-two", "turn-other", other_epoch,
            {"kind": "activity", "label": "正在使用工具"},
        ))
        one = self.observer.snapshot("session-one")
        two = self.observer.snapshot("session-two")
        self.assertIn("正在思考（内容已隐藏）", one["content"])
        self.assertNotIn("正在使用工具", one["content"])
        self.assertIn("正在使用工具（名称与参数已隐藏）", two["content"])
        self.assertNotIn("正在思考", two["content"])

    def test_unknown_activity_and_post_terminal_callbacks_are_ignored(self):
        epoch = self.observer.begin("session-one", "turn-one")
        self.assertFalse(self.observer.record_activity(
            "session-one", "turn-one", epoch,
            {"kind": "activity", "label": "reasoning: secret"},
        ))
        self.assertTrue(self.observer.finish("session-one", "turn-one", epoch, "cancelled"))
        self.assertFalse(self.observer.record_activity(
            "session-one", "turn-one", epoch,
            {"kind": "activity", "label": "正在思考"},
        ))
        payload = self.observer.snapshot("session-one")
        self.assertEqual("idle", payload["state"])
        self.assertIn("本轮已中断", payload["content"])
        self.assertNotIn("reasoning", payload["content"])

        failed_epoch = self.observer.begin("session-two", "turn-two")
        self.assertTrue(self.observer.finish("session-two", "turn-two", failed_epoch, "failed"))
        failed = self.observer.snapshot("session-two")
        self.assertEqual("idle", failed["state"])
        self.assertIn("本轮未完成", failed["content"])


class KimiTerminalObserverRouteTest(unittest.TestCase):
    def _handler(self, observer, session_id="session-one"):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            kimi_terminal_observer=observer,
            kimi_acp=types.SimpleNamespace(load_session_id=lambda: session_id),
            kimi_turn_lock=__import__("threading").RLock(),
            kimi_active_turn={},
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    def test_route_returns_only_observer_dto(self):
        observer = KimiTerminalObserver()
        epoch = observer.begin("session-one", "turn-one")
        observer.record_activity("session-one", "turn-one", epoch, {
            "kind": "activity", "label": "正在使用工具", "output": "SECRET"
        })
        handler = self._handler(observer)
        handler._handle_kimi_terminal_observer()
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual({"ok", "target", "mode", "state", "content", "events"}, set(payload))
        self.assertNotIn("session-one", str(payload))
        self.assertNotIn("SECRET", str(payload))

    def test_http_boundary_rebuilds_even_a_malformed_observer_snapshot(self):
        bad_observer = types.SimpleNamespace(snapshot=lambda _session: {
            "state": "working",
            "content": "raw ACP command /root/private --token=SECRET",
            "events": [
                {"elapsed_seconds": 1, "label": "正在思考（内容已隐藏）"},
                {"elapsed_seconds": 2, "label": "raw tool output SECRET"},
            ],
            "session_id": "session-one",
        })
        handler = self._handler(bad_observer)
        handler._handle_kimi_terminal_observer()
        _status, payload = handler.responses[-1]
        self.assertEqual({"ok", "target", "mode", "state", "content", "events"}, set(payload))
        self.assertIn("正在思考（内容已隐藏）", payload["content"])
        self.assertNotIn("raw ACP", str(payload))
        self.assertNotIn("SECRET", str(payload))
        self.assertNotIn("session-one", str(payload))

    def test_native_route_rejects_pwa_and_dispatches_only_after_native_auth(self):
        handler = object.__new__(PushHandler)
        handler.path = "/kimi/terminal/observer"
        handler.command = "GET"
        handler.responses = []
        handler._is_public_get = lambda: False
        handler._check_ip_allowed = lambda: True
        handler._native_pairing_auth_matches = lambda: False
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.do_GET()
        self.assertEqual((401, {"ok": False, "error": "unauthorized"}), handler.responses[-1])

        handler = object.__new__(PushHandler)
        handler.path = "/kimi/terminal/observer"
        handler.command = "GET"
        handler._is_public_get = lambda: False
        handler._check_ip_allowed = lambda: True
        handler._native_pairing_auth_matches = lambda: True
        handler._require_auth = lambda: True
        called = []
        handler._handle_kimi_terminal_observer = lambda: called.append(True)
        handler.do_GET()
        self.assertEqual([True], called)

        cookie_handler = object.__new__(PushHandler)
        cookie_handler.path = "/kimi/terminal/observer"
        cookie_handler.command = "GET"
        cookie_handler.state = types.SimpleNamespace(web_session_enabled=True)
        cookie_handler._web_session_token = lambda: "valid-cookie"
        cookie_handler.state.web_sessions = types.SimpleNamespace(valid=lambda _token: True)
        self.assertFalse(cookie_handler._web_session_route_allowed())
        self.assertFalse(cookie_handler._web_session_matches())

    @mock.patch("push.subprocess.Popen")
    @mock.patch("push.subprocess.run")
    def test_kimi_alias_uses_its_own_tui_for_capture_key_send_and_release(self, run, popen):
        handler = object.__new__(PushHandler)
        handler.path = "/tmux/capture?session=kimi"
        lease = "a" * 43
        bridge = types.SimpleNamespace(
            ensure=lambda: "%42",
            lease_for_pane=lambda pane: lease if pane == "%42" else "",
            release=lambda candidate: candidate == lease,
            input_transaction=lambda: nullcontext(),
            touch=lambda: None,
        )
        handler.state = types.SimpleNamespace(
            default_session="cctg", active_session="cctg", kimi_terminal=bridge,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        run.return_value = types.SimpleNamespace(returncode=0, stdout="Kimi ready\n", stderr="")
        handler._handle_tmux_capture()
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("kimi", handler.responses[-1][1]["session"])
        self.assertEqual(lease, handler.responses[-1][1]["lease"])
        self.assertNotIn("%42", json.dumps(handler.responses[-1][1]))

        handler._handle_terminal_key({"session": "kimi", "key": "Enter"})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("kimi", handler.responses[-1][1]["session"])
        handler._handle_terminal_release({"target": "kimi", "lease": lease})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["released"])

    def test_kimi_bridge_starts_plain_cli_without_acp_resume_flags(self):
        calls = []
        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[1:3] == ["has-session", "-t"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="")
            if "show-options" in argv:
                stdout = KIMI_TERMINAL_OWNER_VALUE
            elif "display-message" in argv:
                stdout = "%42|0"
            else:
                stdout = ""
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with mock.patch("push.Path.is_file", return_value=True), mock.patch("push.os.access", return_value=True), mock.patch("push.Path.is_dir", return_value=True):
            bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), runner=runner)
            self.assertEqual("%42", bridge.ensure())
        self.assertIsNotNone(bridge._lease)
        self.assertRegex(str(bridge._lease), r"^[A-Za-z0-9_-]{32,128}$")
        self.assertNotIn("%42", str(bridge._lease))
        launch = next(argv for argv in calls if "respawn-pane" in argv)
        self.assertIn("CCC_KIMI_TERMINAL_BRIDGE=1", launch)
        self.assertIn("/fake/kimi", launch)
        self.assertIn("kimi-code/k3-256k", launch)
        self.assertNotIn("--session", launch)
        self.assertNotIn("--continue", launch)

    def test_stale_lease_cannot_release_new_kimi_pane(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        old_lease = "a" * 43
        new_lease = "b" * 43
        bridge._lease = new_lease
        bridge._lease_pane = "%42"
        bridge._kill_exact_pane_locked = mock.Mock(return_value=True)

        self.assertFalse(bridge.release(old_lease))
        bridge._kill_exact_pane_locked.assert_not_called()
        self.assertTrue(bridge.release(new_lease))
        bridge._kill_exact_pane_locked.assert_called_once_with("%42")
        self.assertIsNone(bridge._lease)
        self.assertIsNone(bridge._lease_pane)

    def test_kimi_release_route_reports_stale_without_killing_current_pane(self):
        lease = "a" * 43
        bridge = types.SimpleNamespace(
            input_transaction=lambda: nullcontext(),
            release=mock.Mock(return_value=False),
        )
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(kimi_terminal=bridge)
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))

        handler._handle_terminal_release({"target": "kimi", "lease": lease})

        bridge.release.assert_called_once_with(lease)
        self.assertEqual((200, {"ok": True, "target": "kimi", "released": False}), handler.responses[-1])

    def test_kimi_release_targets_only_bound_pane_and_replacement_is_stale(self):
        calls = []
        state = {"pane": "%42", "exists": True, "owner": True}

        def runner(argv, **_kwargs):
            calls.append(argv)
            action = argv[1]
            if action == "has-session":
                return types.SimpleNamespace(returncode=0 if state["exists"] else 1, stdout="", stderr="")
            if action == "show-options":
                return types.SimpleNamespace(
                    returncode=0 if state["owner"] else 1,
                    stdout=KIMI_TERMINAL_OWNER_VALUE + "\n" if state["owner"] else "",
                    stderr="",
                )
            if action == "display-message":
                return types.SimpleNamespace(returncode=0, stdout=f"{state['pane']}|0\n", stderr="")
            if action == "kill-pane":
                if argv[-1] == state["pane"]:
                    state["exists"] = False
                    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
                return types.SimpleNamespace(returncode=1, stdout="", stderr="wrong pane")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), runner=runner)
        bridge._lease, bridge._lease_pane = "a" * 43, "%41"
        self.assertFalse(bridge.release("a" * 43))
        self.assertFalse(any(argv[1] == "kill-pane" for argv in calls))

        bridge._lease_pane = "%42"
        self.assertTrue(bridge.release("a" * 43))
        self.assertIn(["tmux", "kill-pane", "-t", "%42"], calls)
        self.assertFalse(any(argv[1] == "kill-session" for argv in calls))

    def test_kimi_idle_reaper_uses_bound_pane_after_idle(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), idle_seconds=1)
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge._last_activity = 10.0
        bridge._kill_exact_pane_locked = mock.Mock(return_value=True)
        with mock.patch("push.time.monotonic", return_value=12.0):
            bridge._reap_if_idle()
        bridge._kill_exact_pane_locked.assert_called_once_with("%42")
        self.assertIsNone(bridge._lease)
        self.assertIsNone(bridge._lease_pane)

    def test_kimi_restart_adoption_binds_a_fresh_lease_to_existing_owned_pane(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%55", False))

        self.assertEqual("%55", bridge.ensure())
        self.assertEqual("%55", bridge._lease_pane)
        self.assertRegex(str(bridge._lease), r"^[A-Za-z0-9_-]{32,128}$")
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_dead_owned_pane_recreates_a_new_lease_generation(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        old_lease = "a" * 43
        bridge._lease, bridge._lease_pane = old_lease, "%41"
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(side_effect=[("%41", True), ("%42", False)])
        bridge._kill_exact_pane_locked = mock.Mock(return_value=True)
        bridge._run_tmux = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        with mock.patch("push.Path.is_file", return_value=True), mock.patch("push.os.access", return_value=True), mock.patch("push.Path.is_dir", return_value=True):
            self.assertEqual("%42", bridge.ensure())
        bridge._kill_exact_pane_locked.assert_called_once_with("%41")
        self.assertEqual("%42", bridge._lease_pane)
        self.assertNotEqual(old_lease, bridge._lease)
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_kimi_terminal_routes_require_native_pairing_before_body_or_bridge(self):
        capture = object.__new__(PushHandler)
        capture.path = "/tmux/capture?session=kimi"
        capture.command = "GET"
        capture.state = types.SimpleNamespace(strict_auth=False, shared_secret="required")
        capture.responses = []
        capture._is_public_get = lambda: False
        capture._check_ip_allowed = lambda: True
        capture._native_pairing_auth_matches = lambda: False
        capture._handle_tmux_capture = mock.Mock()
        capture._send_json = lambda status, payload: capture.responses.append((status, payload))
        capture.do_GET()
        self.assertEqual(401, capture.responses[-1][0])
        capture._handle_tmux_capture.assert_not_called()

        for path in ("/terminal/key", "/tmux/send", "/terminal/release"):
            with self.subTest(path=path):
                handler = object.__new__(PushHandler)
                handler.path = path
                handler.command = "POST"
                handler.state = types.SimpleNamespace(strict_auth=False, shared_secret="required")
                handler.responses = []
                handler._check_ip_allowed = lambda: True
                handler._native_pairing_auth_matches = lambda: False
                handler._read_body = mock.Mock()
                handler._send_json = lambda status, payload: handler.responses.append((status, payload))
                handler.do_POST()
                self.assertEqual(401, handler.responses[-1][0])
                handler._read_body.assert_not_called()

    def test_http_tmux_alias_respects_remote_control_gate(self):
        capture = object.__new__(PushHandler)
        capture.path = "/tmux/capture?session=kimi"
        capture.command = "GET"
        capture.state = types.SimpleNamespace(
            default_session="cctg", allow_remote_control=False,
            kimi_terminal=types.SimpleNamespace(ensure=lambda: "%42"),
        )
        capture.responses = []
        capture._is_public_get = lambda: False
        capture._check_ip_allowed = lambda: True
        capture._native_pairing_auth_matches = lambda: True
        capture._require_auth = lambda: True
        capture._send_json = lambda status, payload: capture.responses.append((status, payload))
        capture.do_GET()
        self.assertEqual(403, capture.responses[-1][0])

        for path, body in (
            ("/terminal/key", {"session": "kimi", "key": "Enter"}),
            ("/tmux/send", {"session": "kimi", "keys": "safe text"}),
            ("/terminal/release", {"target": "kimi"}),
        ):
            with self.subTest(path=path):
                handler = object.__new__(PushHandler)
                handler.path = path
                handler.command = "POST"
                handler.state = types.SimpleNamespace(allow_remote_control=False)
                handler.responses = []
                handler._check_ip_allowed = lambda: True
                handler._native_pairing_auth_matches = lambda: True
                handler._require_write_auth = lambda: True
                handler._read_body = lambda: body
                handler._send_json = lambda status, payload: handler.responses.append((status, payload))
                with mock.patch("push.subprocess.run") as run, mock.patch("push.subprocess.Popen") as popen:
                    handler.do_POST()
                self.assertEqual(403, handler.responses[-1][0])
                run.assert_not_called()
                popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
