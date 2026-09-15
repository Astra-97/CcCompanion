import json
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import push
from push import PushHandler


def _make_handler(tmp: Path, *, status: dict | None = None) -> PushHandler:
    """对象级夹具（同 _kimi_control_test.py）：不跑 socket，只截获 _send_json。"""
    handler = object.__new__(PushHandler)
    handler.state = types.SimpleNamespace(
        xiaoke_stop_lock=threading.RLock(),
        typing_state={"is_typing": False, "since": None},
        xiaoke_stopping_claim={},
        xiaoke_send_reservation={},
    )
    handler.responses = []
    handler._send_json = lambda code, payload: handler.responses.append((code, payload))
    handler._xiaoke_claude_status = lambda: dict(status or {})
    handler.commands = []
    handler._run_toolbot_command = lambda command, args: (
        handler.commands.append((command, args)) or (True, f"ran {command} {args}".strip())
    )
    return handler


def _model_pin_patches(tmp: Path, pin_text: str = "", effort: str = ""):
    """把钉子/settings.json/current-session 的读写到临时目录，绝不碰真实文件。"""
    pin = tmp / "current-model"
    if pin_text:
        pin.write_text(pin_text, encoding="utf-8")
    return (
        patch.object(push, "CCBOT_CURRENT_MODEL_FILE", pin),
        patch.object(push, "CCBOT_MODEL_FILE", pin),
        patch.object(push, "CCBOT_CURRENT_SESSION_FILE", tmp / "current-session"),
        patch.object(push, "CLAUDE_SETTINGS_FILE", tmp / "settings.json"),
        patch.object(push, "_read_claude_settings_effort", lambda: effort),
    )


class XiaokeControlRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_projects_runtime_statusbar_with_pin_fallback(self):
        status = {
            "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
            "context_window": {"used_percentage": 42.4},
            "rate_limits": {
                "five_hour": {"used_percentage": 12.0, "resets_at": 1789000000},
                "seven_day": {"used_percentage": 55.0, "resets_at": 1789500000},
            },
        }
        handler = _make_handler(self.root, status=status)
        (self.root / "current-session").write_text("abcd1234-\n", encoding="utf-8")
        patches = _model_pin_patches(self.root, pin_text="claude-fable-5\n", effort="high")
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            handler._handle_xiaoke_status()
        code, payload = handler.responses[-1]
        self.assertEqual(200, code)
        self.assertEqual("Fable 5", payload["model"])
        self.assertEqual("claude-fable-5", payload["model_id"])
        self.assertEqual("high", payload["effort"])
        self.assertFalse(payload["busy"])
        self.assertEqual("abcd1234-", payload["active_session_id"])
        self.assertTrue(payload["context"]["available"])
        self.assertAlmostEqual(42.4, payload["context"]["used_percent"])
        labels = [w["label"] for w in payload["quota"]["windows"]]
        self.assertEqual(["Claude 5h", "Claude 7d"], labels)
        self.assertTrue(all(w["text"].endswith("%") for w in payload["quota"]["windows"]))
        self.assertTrue(payload["capabilities"]["can_forge"])

    def test_status_without_statusbar_falls_back_to_model_pin(self):
        handler = _make_handler(self.root, status={})
        patches = _model_pin_patches(self.root, pin_text="claude-opus-4-8\n")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            handler._handle_xiaoke_status()
        code, payload = handler.responses[-1]
        self.assertEqual(200, code)
        self.assertEqual("claude-opus-4-8", payload["model"])
        self.assertEqual("", payload["effort"])
        self.assertFalse(payload["context"]["available"])
        self.assertEqual([], payload["quota"]["windows"])

    def test_preferences_get_reports_pin_allowlist_and_effort_levels(self):
        handler = _make_handler(self.root)
        patches = _model_pin_patches(self.root, pin_text="claude-fable-5\n", effort="max")
        with patches[0], patches[1], patches[3], patches[4]:
            handler._handle_xiaoke_preferences_get()
        code, payload = handler.responses[-1]
        self.assertEqual(200, code)
        self.assertEqual("claude-fable-5", payload["model"])
        self.assertEqual("max", payload["effort"])
        self.assertIn("claude-fable-5", payload["available_models"])
        self.assertEqual(["low", "medium", "high", "xhigh", "max"], payload["available_efforts"])
        self.assertEqual("current_session_only", payload["applies_from"]["effort"])
        self.assertEqual("current_session_and_next_start", payload["applies_from"]["model"])

    def test_preferences_post_injects_model_and_effort_and_pins_model(self):
        handler = _make_handler(self.root)
        appended, pushed, writes = [], [], []
        handler._chat_for_contact = lambda contact: types.SimpleNamespace(
            append=lambda **row: appended.append(row)
        )
        handler._send_chat_notification = lambda title, body: pushed.append((title, body))
        patches = _model_pin_patches(self.root, pin_text="claude-sonnet-5\n")
        with patches[0], patches[1], patches[3], patches[4], \
                patch.object(push, "_atomic_write_text", lambda path, text: writes.append((str(path), text))):
            handler._handle_xiaoke_preferences_post({"model": "fable", "effort": "xhigh"})
        code, payload = handler.responses[-1]
        self.assertEqual(200, code)
        self.assertEqual(
            [("model", "claude-fable-5"), ("effort", "xhigh")],
            handler.commands,
        )
        self.assertEqual(1, len(writes))
        self.assertTrue(writes[0][1].startswith("claude-fable-5"))
        self.assertEqual(1, len(appended))
        self.assertEqual("assistant", appended[0]["role"])
        self.assertEqual("system:xiaoke-model-switch", appended[0]["source"])
        self.assertIn("claude-fable-5", appended[0]["text"])
        self.assertIn("xhigh", appended[0]["text"])
        self.assertEqual(1, len(pushed))
        self.assertIn("claude-fable-5", pushed[0][1])

    def test_preferences_post_same_model_is_a_quiet_noop_for_notices(self):
        handler = _make_handler(self.root)
        appended = []

        def pushed_error(*_args):
            raise AssertionError("same-model re-save must not push")

        handler._chat_for_contact = lambda contact: types.SimpleNamespace(
            append=lambda **row: appended.append(row)
        )
        handler._send_chat_notification = pushed_error
        patches = _model_pin_patches(self.root, pin_text="claude-fable-5\n")
        with patches[0], patches[1], patches[3], patches[4], \
                patch.object(push, "_atomic_write_text", lambda path, text: None):
            handler._handle_xiaoke_preferences_post({"model": "claude-fable-5"})
            self.assertEqual(200, handler.responses[-1][0])
            # 只改 effort 也不产模型切换通知。
            handler._handle_xiaoke_preferences_post({"effort": "low"})
            self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual([], appended)
        self.assertEqual([("model", "claude-fable-5"), ("effort", "low")], handler.commands)

    def test_preferences_post_validation_and_injection_failure(self):
        handler = _make_handler(self.root)
        patches = _model_pin_patches(self.root)
        with patches[0], patches[1], patches[3], patches[4]:
            handler._handle_xiaoke_preferences_post({})
            self.assertEqual(400, handler.responses[-1][0])
            handler._handle_xiaoke_preferences_post({"model": "not-a-real-model"})
            self.assertEqual(400, handler.responses[-1][0])
            self.assertEqual("invalid_model", handler.responses[-1][1]["error"])
            handler._handle_xiaoke_preferences_post({"effort": "ludicrous"})
            self.assertEqual(400, handler.responses[-1][0])
            self.assertEqual("invalid_effort", handler.responses[-1][1]["error"])
            # 注入失败：不落钉、不通知。
            handler._run_toolbot_command = lambda command, args: (False, "注入失败：tmux 不在")
            handler._handle_xiaoke_preferences_post({"model": "fable"})
            self.assertEqual(502, handler.responses[-1][0])
            self.assertEqual("model_inject_failed", handler.responses[-1][1]["error"])

    def test_sessions_pass_through_structured_toolbot_payload(self):
        handler = _make_handler(self.root)

        def fake_sessions(command, args):
            handler.commands.append((command, args))
            return True, json.dumps({
                "sessions": [{"sid": "abcd1234-", "name": "闲聊", "mtime_iso": "2026-09-15T08:00:00+08:00",
                              "size_kb": 12.5, "preview": "你好", "active": True}],
                "active_sid": "abcd1234-",
            }, ensure_ascii=False)

        handler._run_toolbot_command = fake_sessions
        handler._handle_xiaoke_sessions()
        code, payload = handler.responses[-1]
        self.assertEqual(200, code)
        self.assertEqual("abcd1234-", payload["active_session_id"])
        self.assertEqual("闲聊", payload["sessions"][0]["name"])
        self.assertTrue(payload["sessions"][0]["active"])
        self.assertEqual([("sessions", "")], handler.commands)

    def test_switch_and_new_and_forge_happy_paths(self):
        handler = _make_handler(self.root)
        with patch.object(push, "_session_jsonl_path", lambda sid: self.root / f"{sid}.jsonl"):
            handler._handle_xiaoke_switch_session({"session_id": "abcd1234-"})
            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual(("session_switch", "abcd1234-"), handler.commands[-1])

            handler._handle_xiaoke_switch_session({})
            self.assertEqual(400, handler.responses[-1][0])

        with patch.object(push, "_session_jsonl_path", lambda sid: None):
            handler._handle_xiaoke_switch_session({"session_id": "deadbeef-"})
            self.assertEqual(404, handler.responses[-1][0])

        handler._handle_xiaoke_new_session({"model": "opus4.8"})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual(("session_new", "opus4.8"), handler.commands[-1])

        handler._handle_xiaoke_new_session({"model": "not-a-real-model"})
        self.assertEqual(400, handler.responses[-1][0])

        handler._handle_xiaoke_forge({"retain": "all", "model": "fable"})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual(("forge", "all fable"), handler.commands[-1])

        handler._handle_xiaoke_forge({})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual(("forge", ""), handler.commands[-1])

        handler._handle_xiaoke_forge({"retain": "lots"})
        self.assertEqual(400, handler.responses[-1][0])
        handler._handle_xiaoke_forge({"model": "not-a-real-model"})
        self.assertEqual(400, handler.responses[-1][0])

    def test_lifecycle_writes_are_busy_gated_409_and_never_run(self):
        handler = _make_handler(self.root)
        handler.state.typing_state = {"is_typing": True, "since": "2026-09-15T08:00:00"}
        with patch.object(push, "_session_jsonl_path", lambda sid: self.root / f"{sid}.jsonl"):
            handler._handle_xiaoke_switch_session({"session_id": "abcd1234-"})
            self.assertEqual(409, handler.responses[-1][0])
            self.assertEqual("xiaoke_busy", handler.responses[-1][1]["error"])
        handler._handle_xiaoke_new_session({})
        self.assertEqual(409, handler.responses[-1][0])
        handler._handle_xiaoke_forge({})
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual([], handler.commands)

        handler.state.typing_state = {"is_typing": False, "since": None}
        handler.state.xiaoke_stopping_claim = {"turn_token": "t"}
        handler._handle_xiaoke_new_session({})
        self.assertEqual(409, handler.responses[-1][0])
        handler.state.xiaoke_stopping_claim = {}
        handler.state.xiaoke_send_reservation = {"turn_token": "t"}
        handler._handle_xiaoke_new_session({})
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual([], handler.commands)

    def test_command_failure_surfaces_502_with_result_text(self):
        handler = _make_handler(self.root)
        handler._run_toolbot_command = lambda command, args: (False, "forge 超时（150s）。")
        handler._handle_xiaoke_forge({})
        code, payload = handler.responses[-1]
        self.assertEqual(502, code)
        self.assertFalse(payload["ok"])
        self.assertIn("超时", payload["result"])

    def test_xiaoke_routes_require_the_shared_secret_before_cookie_auth(self):
        for method, path in (("GET", "/xiaoke/status"), ("POST", "/xiaoke/preferences")):
            handler = object.__new__(PushHandler)
            handler.path = path
            handler.command = method
            handler.responses = []
            handler._is_public_get = lambda: False
            handler._check_ip_allowed = lambda: True
            handler._native_pairing_auth_matches = lambda: False
            handler._send_json = lambda status, payload: handler.responses.append((status, payload))
            if method == "GET":
                handler.do_GET()
            else:
                handler.do_POST()
            self.assertEqual((401, {"ok": False, "error": "unauthorized"}), handler.responses[-1])


if __name__ == "__main__":
    unittest.main()
