import json
from contextlib import nullcontext
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kimi_terminal_observer import (
    KIMI_TERMINAL_MAX_BYTES,
    KIMI_TERMINAL_MAX_EVENTS,
    KimiTerminalObserver,
)
from push import KIMI_TERMINAL_OWNER_VALUE, KimiTerminalBridge, KimiTerminalBusy, PushHandler


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

    def test_web_assistant_stream_is_bounded_and_redacted(self):
        epoch = self.observer.begin("session-one", "turn-one")
        self.assertTrue(self.observer.record_assistant_text(
            "session-one", "turn-one", epoch,
            "第一段。 token=never-show /root/private/project/file.py",
        ))
        self.assertTrue(self.observer.record_assistant_text("session-one", "turn-one", epoch, "第二段。"))

        payload = self.observer.snapshot("session-one")

        self.assertIn("助手回复（实时）：", payload["content"])
        self.assertIn("第一段。", payload["content"])
        self.assertIn("第二段。", payload["content"])
        self.assertNotIn("never-show", str(payload))
        self.assertNotIn("/root/private", str(payload))

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

    @mock.patch("push.subprocess.run")
    def test_kimi_capture_holds_terminal_transaction_for_exact_web_session(self, run):
        handler = object.__new__(PushHandler)
        handler.path = "/tmux/capture?session=kimi"
        input_lock = threading.Lock()
        observations = []

        class Bridge:
            def input_transaction(self): return input_lock
            def ensure(self, session_id):
                observations.append(("ensure", input_lock.locked(), session_id))
                return "%42"
            def lease_for_pane(self, pane):
                observations.append(("lease", input_lock.locked(), pane))
                return "a" * 43
            def touch(self): pass

        def capture(argv, **_kwargs):
            observations.append(("capture", input_lock.locked(), argv[3]))
            return types.SimpleNamespace(returncode=0, stdout="ready\n", stderr="")

        run.side_effect = capture
        web = types.SimpleNamespace(
            cwd="/workspace", start=lambda: None,
            load_active_session_id=lambda: "session-current",
            _valid_session_id=lambda value: value,
            list_sessions=lambda: [{"id": "session-current", "metadata": {"cwd": "/workspace"}}],
            get_session_status=lambda _session: {"busy": False},
        )
        handler.state = types.SimpleNamespace(
            default_session="cctg", active_session="cctg",
            kimi_turn_lock=threading.RLock(), kimi_active_turn={}, kimi_prepare_token="",
            kimi_recovery_token="", kimi_web=web, kimi_terminal=Bridge(),
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._handle_tmux_capture()
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("kimi", handler.responses[-1][1]["session"])
        self.assertEqual("a" * 43, handler.responses[-1][1]["lease"])
        self.assertTrue(all(item[1] for item in observations))
        self.assertEqual(["ensure", "lease", "capture"], [item[0] for item in observations])

    @mock.patch("push.subprocess.run")
    def test_physical_kimi_tmux_name_is_redacted_and_cannot_bypass_alias(self, run):
        bridge = types.SimpleNamespace(tmux_session="ccc-kimi-terminal")
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            default_session="cctg", active_session="cctg", kimi_terminal=bridge,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        run.return_value = types.SimpleNamespace(returncode=0, stdout="cctg\nccc-kimi-terminal\n", stderr="")
        handler._handle_tmux_sessions()
        self.assertEqual(["cctg", "kimi"], handler.responses[-1][1]["sessions"])

        for operation in (
            lambda: setattr(handler, "path", "/tmux/capture?session=ccc-kimi-terminal") or handler._handle_tmux_capture(),
            lambda: handler._handle_terminal_key({"session": "ccc-kimi-terminal", "key": "Enter"}),
            lambda: handler._handle_tmux_send({"session": "ccc-kimi-terminal", "keys": "/new", "enter": True}),
            lambda: handler._handle_tmux_send({"session": "ccc-kimi-terminal", "keys": "/continue", "enter": True}),
            lambda: handler._handle_terminal_resize({"session": "ccc-kimi-terminal", "columns": 120, "rows": 40}),
        ):
            with self.subTest(operation=operation):
                handler.responses.clear()
                operation()
                self.assertEqual(503, handler.responses[-1][0])
                self.assertEqual("kimi", handler.responses[-1][1]["target"])
        handler.responses.clear()
        handler._handle_chain_switch({"sid": "ccc-kimi-terminal"})
        self.assertEqual(400, handler.responses[-1][0])
        self.assertEqual("kimi_terminal_physical_target_blocked", handler.responses[-1][1]["error"])
        # Only the listing call ran; no physical target reaches tmux I/O.
        self.assertEqual(1, run.call_count)

    def test_kimi_bridge_resumes_exact_durable_session_without_ambiguous_flags(self):
        calls = []
        options = {}
        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[1:3] == ["has-session", "-t"]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="")
            if "show-options" in argv:
                stdout = options.get(argv[-1], "")
            elif "set-option" in argv:
                options[argv[-2]] = argv[-1]
                stdout = ""
            elif "display-message" in argv:
                stdout = "%42|0"
            else:
                stdout = ""
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with mock.patch("push.Path.is_file", return_value=True), mock.patch("push.os.access", return_value=True), mock.patch("push.Path.is_dir", return_value=True):
            bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), runner=runner)
            self.assertEqual("%42", bridge.ensure("session-current"))
        self.assertIsNotNone(bridge._lease)
        self.assertRegex(str(bridge._lease), r"^[A-Za-z0-9_-]{32,128}$")
        self.assertNotIn("%42", str(bridge._lease))
        launch = next(argv for argv in calls if "respawn-pane" in argv)
        self.assertIn("CCC_KIMI_TERMINAL_BRIDGE=1", launch)
        self.assertIn("/fake/kimi", launch)
        self.assertEqual(["--session", "session-current"], launch[-2:])
        self.assertNotIn("--continue", launch)
        self.assertNotIn("--model", launch)

    def test_stale_lease_cannot_release_new_kimi_pane(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        old_lease = "a" * 43
        new_lease = "b" * 43
        bridge._lease = new_lease
        bridge._lease_pane = "%42"
        bridge._shutdown_exact_pane_locked = mock.Mock(return_value=True)

        self.assertFalse(bridge.release(old_lease))
        bridge._shutdown_exact_pane_locked.assert_not_called()
        self.assertTrue(bridge.release(new_lease))
        bridge._shutdown_exact_pane_locked.assert_called_once_with("%42")
        self.assertIsNone(bridge._lease)
        self.assertIsNone(bridge._lease_pane)

    def test_kimi_release_route_uses_only_the_opaque_lease(self):
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
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("kimi", handler.responses[-1][1]["target"])

    @mock.patch("push.subprocess.run")
    def test_kimi_resize_is_serialized_and_never_returns_pane_identity(self, run):
        input_lock = threading.Lock()
        bridge = types.SimpleNamespace(
            input_transaction=lambda: input_lock,
            touch=mock.Mock(),
        )
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(kimi_terminal=bridge)
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._resolve_terminal_session = mock.Mock(side_effect=lambda *_args, **_kwargs: (
            self.assertTrue(input_lock.locked()) or "%42", "kimi", False,
        ))
        run.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")

        handler._handle_terminal_resize({"session": "kimi", "columns": 120, "rows": 40})

        self.assertEqual((200, {"ok": True, "session": "kimi", "columns": 120, "rows": 40}), handler.responses[-1])
        self.assertEqual(
            ["tmux", "resize-window", "-t", "%42", "-x", "120", "-y", "40"],
            run.call_args.args[0],
        )
        self.assertNotIn("%42", str(handler.responses[-1][1]))
        bridge.touch.assert_called_once_with()

    @mock.patch("push.subprocess.run")
    def test_kimi_enter_is_serialized_and_marks_web_handoff_uncertain(self, run):
        input_lock = threading.Lock()
        bridge = types.SimpleNamespace(
            input_transaction=lambda: input_lock,
            mark_prompt_submitted=mock.Mock(),
            touch=mock.Mock(),
        )
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(kimi_terminal=bridge)
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._resolve_terminal_session = mock.Mock(side_effect=lambda *_args, **_kwargs: (
            self.assertTrue(input_lock.locked()) or "%42", "kimi", False,
        ))
        run.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")

        handler._handle_terminal_key({"session": "kimi", "key": "Enter"})

        self.assertEqual((200, {"ok": True, "key": "Enter", "session": "kimi"}), handler.responses[-1])
        self.assertEqual(["tmux", "send-keys", "-t", "%42", "Enter"], run.call_args.args[0])
        bridge.mark_prompt_submitted.assert_called_once_with()
        bridge.touch.assert_called_once_with()

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

        bridge = KimiTerminalBridge(
            command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), runner=runner,
            process_killer=lambda _pid, _signal: None, shutdown_wait_seconds=0.05,
        )
        bridge._lease, bridge._lease_pane = "a" * 43, "%41"
        bridge._shutdown_exact_pane_locked = mock.Mock(return_value=False)
        self.assertFalse(bridge.release("a" * 43))
        self.assertFalse(any(argv[1] == "kill-pane" for argv in calls))

        bridge._lease_pane = "%42"
        bridge._shutdown_exact_pane_locked.return_value = True
        self.assertTrue(bridge.release("a" * 43))
        bridge._shutdown_exact_pane_locked.assert_called_with("%42")
        self.assertFalse(any(argv[1] == "kill-session" for argv in calls))

    def test_kimi_idle_reaper_releases_live_pane_before_any_enter(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), idle_seconds=1)
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge._last_activity = 10.0
        bridge._kill_exact_pane_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%42", False))
        with mock.patch("push.time.monotonic", return_value=12.0):
            bridge._reap_if_idle()
        bridge._kill_exact_pane_locked.assert_called_once_with("%42")
        self.assertIsNone(bridge._lease)
        self.assertIsNone(bridge._lease_pane)
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_kimi_idle_reaper_never_kills_uncertain_submitted_prompt(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"), idle_seconds=1)
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge._prompt_active_uncertain = True
        bridge._last_activity = 10.0
        bridge._kill_exact_pane_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%42", False))
        with mock.patch("push.time.monotonic", return_value=12.0):
            bridge._reap_if_idle()
        bridge._kill_exact_pane_locked.assert_not_called()
        self.assertEqual("a" * 43, bridge._lease)
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_owns_live_session_accepts_only_matching_uncertain_tui(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge._session_fingerprint = bridge._fingerprint_session("session-current")
        bridge._prompt_active_uncertain = True
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%42", False))

        self.assertTrue(bridge.owns_live_session("session-current"))

    def test_owns_live_session_rejects_no_lease(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._session_fingerprint = bridge._fingerprint_session("session-current")
        bridge._prompt_active_uncertain = True

        self.assertFalse(bridge.owns_live_session("session-current"))

    def test_owns_live_session_rejects_mismatched_session_fingerprint(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge._session_fingerprint = bridge._fingerprint_session("session-other")
        bridge._prompt_active_uncertain = True

        self.assertFalse(bridge.owns_live_session("session-current"))

    def test_owns_live_session_rejects_idle_matching_lease(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge._session_fingerprint = bridge._fingerprint_session("session-current")
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%42", False))

        self.assertFalse(bridge.owns_live_session("session-current"))

    def test_kimi_restart_adoption_binds_a_fresh_lease_to_existing_owned_pane(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%55", False))
        bridge._bound_session_locked = mock.Mock(
            return_value=bridge._fingerprint_session("session-current")
        )

        self.assertEqual("%55", bridge.ensure("session-current"))
        self.assertEqual("%55", bridge._lease_pane)
        self.assertRegex(str(bridge._lease), r"^[A-Za-z0-9_-]{32,128}$")
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_restart_owned_live_pane_blocks_writer_until_capture_adopts_exact_lease(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        fingerprint = bridge._fingerprint_session("session-current")
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%55", False))
        bridge._bound_session_locked = mock.Mock(return_value=fingerprint)
        bridge._shutdown_exact_pane_locked = mock.Mock(return_value=True)

        with self.assertRaises(KimiTerminalBusy):
            bridge.release_for_acp()
        bridge._shutdown_exact_pane_locked.assert_not_called()
        lease = bridge.lease_for_pane("%55")
        self.assertRegex(lease, r"^[A-Za-z0-9_-]{32,128}$")
        self.assertTrue(bridge.release(lease))
        bridge._shutdown_exact_pane_locked.assert_called_once_with("%55")
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_dead_owned_pane_recreates_a_new_lease_generation(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        old_lease = "a" * 43
        bridge._lease, bridge._lease_pane = old_lease, "%41"
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(side_effect=[("%41", True), ("%42", False)])
        bridge._bound_session_locked = mock.Mock(return_value="")
        bridge._shutdown_exact_pane_locked = mock.Mock(return_value=True)
        bridge._run_tmux = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        bridge._bound_session_locked = mock.Mock(
            side_effect=["", bridge._fingerprint_session("session-next")]
        )
        with mock.patch("push.Path.is_file", return_value=True), mock.patch("push.os.access", return_value=True), mock.patch("push.Path.is_dir", return_value=True):
            self.assertEqual("%42", bridge.ensure("session-next"))
        bridge._shutdown_exact_pane_locked.assert_called_once_with("%41")
        self.assertEqual("%42", bridge._lease_pane)
        self.assertNotEqual(old_lease, bridge._lease)
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_active_durable_session_change_respawns_and_rotates_lease(self):
        calls = []
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        old_lease = "a" * 43
        bridge._lease, bridge._lease_pane = old_lease, "%41"
        bridge._session_fingerprint = bridge._fingerprint_session("session-old")
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(side_effect=[("%41", False), ("%42", False)])
        bridge._bound_session_locked = mock.Mock(side_effect=[
            bridge._fingerprint_session("session-old"),
            bridge._fingerprint_session("session-new"),
        ])
        bridge._shutdown_exact_pane_locked = mock.Mock(return_value=True)
        bridge._run_tmux = mock.Mock(side_effect=lambda argv: (
            calls.append(argv)
            or types.SimpleNamespace(returncode=0, stdout="", stderr="")
        ))
        with mock.patch("push.Path.is_file", return_value=True), mock.patch("push.os.access", return_value=True), mock.patch("push.Path.is_dir", return_value=True):
            self.assertEqual("%42", bridge.ensure("session-new"))
        launch = next(argv for argv in calls if "respawn-pane" in argv)
        self.assertEqual(["--session", "session-new"], launch[-2:])
        self.assertNotIn("--continue", launch)
        self.assertNotIn("--model", launch)
        self.assertNotEqual(old_lease, bridge._lease)
        bridge._shutdown_exact_pane_locked.assert_called_once_with("%41")
        if bridge._timer is not None:
            bridge._timer.cancel()

    def test_graceful_shutdown_signals_verified_pid_then_exact_pane_fallback(self):
        calls = []
        signals = []
        state = {"dead": False, "exists": True}

        def runner(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "has-session":
                return types.SimpleNamespace(returncode=0 if state["exists"] else 1, stdout="", stderr="")
            if argv[1] == "show-options":
                return types.SimpleNamespace(returncode=0, stdout=KIMI_TERMINAL_OWNER_VALUE + "\n", stderr="")
            if argv[1] == "display-message":
                if argv[-1] == "#{pane_id}|#{pane_pid}":
                    return types.SimpleNamespace(returncode=0, stdout="%42|4321\n", stderr="")
                return types.SimpleNamespace(returncode=0, stdout=f"%42|{1 if state['dead'] else 0}\n", stderr="")
            if argv[1] == "kill-pane":
                self.assertEqual(["tmux", "kill-pane", "-t", "%42"], argv)
                state["exists"] = False
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(argv)

        def terminate(pid, sig):
            signals.append((pid, sig))
            state["dead"] = True

        bridge = KimiTerminalBridge(
            command=Path("/fake/kimi"), cwd=Path("/fake/workspace"),
            runner=runner, process_killer=terminate, shutdown_wait_seconds=0.1,
        )
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        self.assertTrue(bridge.release("a" * 43))
        self.assertEqual([(4321, __import__("signal").SIGTERM)], signals)
        self.assertIn(["tmux", "kill-pane", "-t", "%42"], calls)
        self.assertFalse(any(argv[1] == "kill-session" for argv in calls))

    def test_busy_kimi_capture_does_not_touch_web_or_terminal(self):
        handler = object.__new__(PushHandler)
        handler.path = "/tmux/capture?session=kimi"
        web = mock.Mock()
        bridge = types.SimpleNamespace(
            input_transaction=lambda: nullcontext(),
            ensure=mock.Mock(return_value="%42"),
        )
        handler.state = types.SimpleNamespace(
            default_session="cctg", active_session="cctg",
            kimi_turn_lock=threading.RLock(),
            kimi_active_turn={"user_ts": "busy"}, kimi_prepare_token="", kimi_recovery_token="",
            kimi_web=web, kimi_terminal=bridge,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._handle_tmux_capture()
        self.assertEqual(423, handler.responses[-1][0])
        self.assertEqual("kimi_busy", handler.responses[-1][1]["error"])
        self.assertEqual([], web.mock_calls)
        bridge.ensure.assert_not_called()

    def test_terminal_web_validation_does_not_hold_turn_lock_or_allow_chat_race(self):
        entered, release = threading.Event(), threading.Event()
        input_lock = threading.Lock()

        class Web:
            cwd = "/workspace"
            _valid_session_id = staticmethod(lambda value: value)
            def start(self):
                entered.set()
                if not release.wait(1):
                    raise AssertionError("test did not release Web validation")
            def load_active_session_id(self): return "session-current"
            def list_sessions(self): return [{"id": "session-current", "metadata": {"cwd": self.cwd}}]
            def get_session_status(self, _session): return {"busy": False}

        bridge = types.SimpleNamespace(
            input_transaction=lambda: input_lock,
            ensure=mock.Mock(return_value="%42"),
        )
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            kimi_turn_lock=threading.RLock(), kimi_active_turn={}, kimi_prepare_token="",
            kimi_recovery_token="", kimi_terminal_acquire_token="", kimi_web=Web(), kimi_terminal=bridge,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))

        def acquire():
            with input_lock:
                handler._acquire_kimi_terminal()

        worker = threading.Thread(target=acquire)
        worker.start()
        self.assertTrue(entered.wait(1))
        acquired = handler.state.kimi_turn_lock.acquire(blocking=False)
        self.assertTrue(acquired, "slow Web validation must not hold kimi_turn_lock")
        if acquired:
            handler.state.kimi_turn_lock.release()
        # A chat-side reservation sees the terminal acquire token and fails
        # rather than beginning a second writer while validation is in flight.
        self.assertIsNone(handler._reserve_kimi_control("chat"))
        self.assertEqual("", handler.state.kimi_prepare_token)
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        bridge.ensure.assert_called_once_with("session-current")
        self.assertEqual("", handler.state.kimi_terminal_acquire_token)

    def test_busy_web_session_allows_only_the_same_live_kimi_tui_to_capture(self):
        web = types.SimpleNamespace(
            cwd="/workspace",
            start=lambda: None,
            load_active_session_id=lambda: "session-current",
            _valid_session_id=lambda value: value,
            list_sessions=lambda: [{"id": "session-current", "metadata": {"cwd": "/workspace"}}],
            get_session_status=lambda _session: {"busy": True},
        )

        def handler_for(owns_live):
            bridge = types.SimpleNamespace(
                ensure=mock.Mock(return_value="%42"),
                owns_live_session=mock.Mock(return_value=owns_live),
            )
            handler = object.__new__(PushHandler)
            handler.state = types.SimpleNamespace(
                kimi_turn_lock=threading.RLock(), kimi_active_turn={}, kimi_prepare_token="",
                kimi_recovery_token="", kimi_terminal_acquire_token="", kimi_web=web, kimi_terminal=bridge,
            )
            return handler, bridge

        handler, bridge = handler_for(True)
        self.assertEqual("%42", handler._acquire_kimi_terminal())
        bridge.owns_live_session.assert_called_once_with("session-current")
        bridge.ensure.assert_called_once_with("session-current")

        handler, bridge = handler_for(False)
        with self.assertRaises(KimiTerminalBusy):
            handler._acquire_kimi_terminal()
        bridge.ensure.assert_not_called()
        self.assertEqual("", handler.state.kimi_terminal_acquire_token)

    def test_kimi_capture_does_not_fall_back_to_an_acp_session(self):
        handler = object.__new__(PushHandler)
        handler.path = "/tmux/capture?session=kimi"
        acp = types.SimpleNamespace(load_session_id=lambda: "foreign-session")
        bridge = types.SimpleNamespace(
            input_transaction=lambda: nullcontext(),
            ensure=mock.Mock(return_value="%42"),
        )
        handler.state = types.SimpleNamespace(
            default_session="cctg", active_session="cctg",
            kimi_turn_lock=threading.RLock(), kimi_active_turn={},
            kimi_prepare_token="", kimi_recovery_token="", kimi_acp=acp, kimi_terminal=bridge,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._handle_tmux_capture()
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("no_active_kimi_session", handler.responses[-1][1]["error"])
        self.assertNotIn("foreign-session", json.dumps(handler.responses[-1][1]))
        bridge.ensure.assert_not_called()

    def test_prepare_reservation_beats_terminal_acquire_without_deadlock(self):
        handler = object.__new__(PushHandler)
        input_lock = threading.Lock()
        released = threading.Event()

        class Bridge:
            def input_transaction(self): return input_lock
            def release_for_acp(self): released.set(); return True
            ensure = mock.Mock(return_value="%42")

        web = mock.Mock()
        handler.state = types.SimpleNamespace(
            kimi_turn_lock=threading.RLock(), kimi_active_turn={},
            kimi_prepare_token="", kimi_recovery_token="", kimi_web=web, kimi_terminal=Bridge(),
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))

        input_lock.acquire()
        result = []
        worker = threading.Thread(target=lambda: result.append(handler._reserve_kimi_control("switch")))
        worker.start()
        deadline = time.monotonic() + 1
        while not handler.state.kimi_prepare_token and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(handler.state.kimi_prepare_token)
        with self.assertRaises(Exception) as caught:
            handler._acquire_kimi_terminal()
        self.assertEqual("KimiTerminalBusy", type(caught.exception).__name__)
        input_lock.release()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive(), "handoff lock order must not deadlock")
        self.assertTrue(result[0])
        self.assertFalse(released.is_set(), "Web controls must not touch terminal ownership")
        self.assertEqual([], web.mock_calls)

    def test_web_control_reservations_never_handoff_an_acp_terminal(self):
        handler = object.__new__(PushHandler)
        releases = []
        handler.state = types.SimpleNamespace(
            kimi_turn_lock=threading.RLock(), kimi_active_turn={},
            kimi_prepare_token="", kimi_acp=types.SimpleNamespace(busy=False),
            kimi_terminal=types.SimpleNamespace(
                input_transaction=lambda: nullcontext(),
                release_for_acp=lambda: releases.append("release") or True,
            ),
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        for action in ("new_session", "switch_session", "forge"):
            token = handler._reserve_kimi_control(action)
            self.assertTrue(token)
            handler._release_kimi_control(token)
        self.assertEqual([], releases)

    def test_uncertain_tui_prompt_requires_explicit_release_before_acp_handoff(self):
        bridge = KimiTerminalBridge(command=Path("/fake/kimi"), cwd=Path("/fake/workspace"))
        bridge._lease, bridge._lease_pane = "a" * 43, "%42"
        bridge.mark_prompt_submitted()
        bridge._has_session_locked = mock.Mock(return_value=True)
        bridge._owns_session_locked = mock.Mock(return_value=True)
        bridge._pane_status_locked = mock.Mock(return_value=("%42", False))
        bridge._shutdown_exact_pane_locked = mock.Mock(return_value=True)
        with self.assertRaises(Exception) as caught:
            bridge.release_for_acp()
        self.assertEqual("KimiTerminalBusy", type(caught.exception).__name__)
        bridge._shutdown_exact_pane_locked.assert_not_called()
        self.assertTrue(bridge.release("a" * 43))
        self.assertFalse(bridge._prompt_active_uncertain)

    def test_identity_changing_kimi_commands_are_rejected_before_terminal_acquire(self):
        for command in ("/new", "/sessions", "/fork branch", "/resume abc"):
            with self.subTest(command=command):
                handler = object.__new__(PushHandler)
                handler.state = types.SimpleNamespace(active_session="cctg", default_session="cctg")
                handler.responses = []
                handler._send_json = lambda status, payload: handler.responses.append((status, payload))
                handler._resolve_terminal_session = mock.Mock()
                handler._handle_tmux_send_transaction({
                    "session": "kimi", "keys": command, "enter": True,
                })
                self.assertEqual(400, handler.responses[-1][0])
                self.assertEqual("kimi_session_command_blocked", handler.responses[-1][1]["error"])
                handler._resolve_terminal_session.assert_not_called()

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

        for path in ("/terminal/key", "/terminal/resize", "/tmux/send", "/terminal/release"):
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

    def test_authenticated_kimi_http_routes_reach_the_interactive_surface(self):
        capture = object.__new__(PushHandler)
        capture.path = "/tmux/capture?session=kimi"
        capture.command = "GET"
        capture.state = types.SimpleNamespace(allow_remote_control=True)
        capture.responses = []
        capture._is_public_get = lambda: False
        capture._check_ip_allowed = lambda: True
        capture._native_pairing_auth_matches = lambda: True
        capture._require_auth = lambda: True
        capture._handle_tmux_capture = mock.Mock()
        capture._send_json = lambda status, payload: capture.responses.append((status, payload))
        capture.do_GET()
        capture._handle_tmux_capture.assert_called_once_with()

        for path, body, method in (
            ("/terminal/key", {"session": "kimi", "key": "Enter"}, "_handle_terminal_key"),
            ("/terminal/resize", {"session": "kimi", "columns": 120, "rows": 40}, "_handle_terminal_resize"),
            ("/tmux/send", {"session": "kimi", "keys": "hello", "enter": True}, "_handle_tmux_send"),
            ("/terminal/release", {"target": "kimi", "lease": "a" * 43}, "_handle_terminal_release"),
        ):
            with self.subTest(path=path):
                handler = object.__new__(PushHandler)
                handler.path = path
                handler.command = "POST"
                handler.state = types.SimpleNamespace(allow_remote_control=True)
                handler.responses = []
                handler._check_ip_allowed = lambda: True
                handler._native_pairing_auth_matches = lambda: True
                handler._require_write_auth = lambda: True
                handler._read_body = mock.Mock(return_value=body)
                handler._send_json = lambda status, payload: handler.responses.append((status, payload))
                setattr(handler, method, mock.Mock())
                handler.do_POST()
                getattr(handler, method).assert_called_once_with(body)

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
            ("/terminal/resize", {"session": "kimi", "columns": 120, "rows": 40}),
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
