import json
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
from push import PushHandler


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
    def test_kimi_alias_is_rejected_by_all_tmux_write_and_capture_routes(self, run, popen):
        handler = object.__new__(PushHandler)
        handler.path = "/tmux/capture?session=kimi"
        handler.state = types.SimpleNamespace(default_session="cctg", active_session="cctg")
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._handle_tmux_capture()
        self.assertEqual(403, handler.responses[-1][0])

        handler._handle_terminal_key({"session": "kimi", "key": "Enter"})
        self.assertEqual(403, handler.responses[-1][0])
        handler._handle_tmux_send({"session": "kimi", "keys": "do not execute"})
        self.assertEqual(403, handler.responses[-1][0])
        handler._handle_terminal_release({"target": "kimi"})
        self.assertEqual(403, handler.responses[-1][0])
        run.assert_not_called()
        popen.assert_not_called()

    def test_http_tmux_alias_rejection_precedes_remote_control_gate(self):
        capture = object.__new__(PushHandler)
        capture.path = "/tmux/capture?session=kimi"
        capture.command = "GET"
        capture.state = types.SimpleNamespace(default_session="cctg", allow_remote_control=False)
        capture.responses = []
        capture._is_public_get = lambda: False
        capture._check_ip_allowed = lambda: True
        capture._require_auth = lambda: True
        capture._send_json = lambda status, payload: capture.responses.append((status, payload))
        capture.do_GET()
        self.assertEqual(403, capture.responses[-1][0])
        self.assertEqual("read_only", capture.responses[-1][1]["mode"])

        for path, body in (
            ("/terminal/key", {"session": "kimi", "key": "Enter"}),
            ("/tmux/send", {"session": "kimi", "keys": "do not execute"}),
            ("/terminal/release", {"target": "kimi"}),
        ):
            with self.subTest(path=path):
                handler = object.__new__(PushHandler)
                handler.path = path
                handler.command = "POST"
                handler.state = types.SimpleNamespace(allow_remote_control=False)
                handler.responses = []
                handler._check_ip_allowed = lambda: True
                handler._require_write_auth = lambda: True
                handler._read_body = lambda: body
                handler._send_json = lambda status, payload: handler.responses.append((status, payload))
                handler.do_POST()
                self.assertEqual(403, handler.responses[-1][0])
                self.assertEqual("read_only", handler.responses[-1][1]["mode"])


if __name__ == "__main__":
    unittest.main()
