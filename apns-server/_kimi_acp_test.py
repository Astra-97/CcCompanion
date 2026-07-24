import json
import io
from pathlib import Path
import tempfile
import threading
import unittest

from kimi_acp import (
    KIMI_APP_MODEL,
    KimiACPAuthRequired,
    KimiACPClient,
    KimiACPCancelled,
    KimiACPError,
    _text_from_update,
)


class KimiACPProtocolTest(unittest.TestCase):
    def test_only_agent_text_chunks_are_extracted(self):
        self.assertEqual(
            "hello",
            _text_from_update(
                {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    },
                }
            ),
        )
        self.assertEqual(
            "",
            _text_from_update(
                {
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "private"},
                    }
                }
            ),
        )

    def test_session_state_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            client = KimiACPClient(state_path=path)
            client._save_session_id("kimi-session-1")
            self.assertEqual("kimi-session-1", client._load_session_id())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_cancel_notification_has_no_request_id(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        sent = []
        client._process_alive = lambda: True
        client._write = sent.append
        client._active_turn_id = "turn-1"
        client._active_session_id = "s1"
        self.assertTrue(client.cancel("turn-1", "s1"))
        self.assertEqual("session/cancel", sent[0]["method"])
        self.assertNotIn("id", sent[0])
        self.assertFalse(client.cancel("", "s1"))
        self.assertFalse(client.cancel("turn-2", "s1"))
        self.assertEqual(1, len(sent))

    def test_authentication_error_is_classified_without_echoing_payload(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client._write = lambda message: client._pending[message["id"]][1].update(
            {
                "message": {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32000, "message": "provider-specific private detail"},
                }
            }
        ) or client._pending[message["id"]][0].set()
        with self.assertRaisesRegex(KimiACPAuthRequired, "login is required"):
            client._request("session/new", {}, timeout=1)

    def test_permission_auto_allows_only_unambiguous_one_turn_tool_action(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        sent = []
        client._write = sent.append
        client._answer_permission(
            {
                "id": 7,
                "params": {
                    "toolCall": {"toolCallId": "tool-1", "title": "Read file"},
                    "options": [
                        {"optionId": "once", "kind": "allow_once"},
                        {"optionId": "always", "kind": "allow_always"},
                        {"optionId": "no", "kind": "reject_once"},
                    ]
                },
            }
        )
        self.assertEqual("once", sent[-1]["result"]["outcome"]["optionId"])

        client._answer_permission(
            {
                "id": 8,
                "params": {
                    "toolCall": {"toolCallId": "tool-question", "title": "Question"},
                    "options": [
                        {"optionId": "answer-a", "kind": "allow_once"},
                        {"optionId": "answer-b", "kind": "allow_once"},
                        {"optionId": "skip", "kind": "reject_always"},
                    ]
                },
            }
        )
        self.assertEqual("cancelled", sent[-1]["result"]["outcome"]["outcome"])

        client._answer_permission(
            {
                "id": 10,
                "params": {
                    "options": [
                        {"optionId": "once", "kind": "allow_once"},
                        {"optionId": "no", "kind": "reject_once"},
                    ]
                },
            }
        )
        self.assertEqual("cancelled", sent[-1]["result"]["outcome"]["outcome"])

        client._answer_permission(
            {
                "id": 9,
                "params": {
                    "toolCall": {"toolCallId": "tool-2"},
                    "options": [
                        {"optionId": "once", "kind": "allow_once"},
                        {"optionId": "legacy-no", "kind": "reject"},
                    ]
                },
            }
        )
        self.assertEqual("cancelled", sent[-1]["result"]["outcome"]["outcome"])

    def test_prompt_cancel_raises_after_protocol_cancel(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._process_alive = lambda: True
        client._loaded_session_id = "s1"
        client._highspeed_model_session_id = "s1"
        cancelled = []
        gate = threading.Event()
        release = threading.Event()

        def request(method, params, timeout, ensure_started=True):
            if method == "session/prompt":
                release.wait(1)
                return {"stopReason": "cancelled"}
            return {}

        client._request = request
        client.cancel = lambda turn_id, session_id: cancelled.append((turn_id, session_id)) or True
        threading.Timer(0.02, gate.set).start()
        threading.Timer(0.05, release.set).start()
        with self.assertRaises(KimiACPCancelled):
            client.prompt_existing(
                "hello",
                session_id="s1",
                turn_id="turn-1",
                cancel_event=gate,
            )
        self.assertEqual([("turn-1", "s1")], cancelled)

    def test_cancel_and_deadline_in_same_poll_send_exactly_one_cancel(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._process_alive = lambda: True
        client._loaded_session_id = "s1"
        client._highspeed_model_session_id = "s1"
        client.prompt_timeout = 0.0
        cancelled = []
        release = threading.Event()

        class BoundaryGate:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                # The pre-prompt check is clear; the first polling cycle sees
                # cancellation at the same instant as the expired deadline.
                return self.calls >= 2

        def request(method, params, timeout, ensure_started=True):
            if method == "session/prompt":
                release.wait(1)
                return {"stopReason": "cancelled"}
            return {}

        client._request = request
        client.cancel = lambda turn_id, session_id: cancelled.append((turn_id, session_id)) or True
        try:
            with self.assertRaisesRegex(KimiACPError, "timed out"):
                client.prompt_existing(
                    "hello",
                    session_id="s1",
                    turn_id="turn-boundary",
                    cancel_event=BoundaryGate(),
                )
        finally:
            release.set()
        self.assertEqual([("turn-boundary", "s1")], cancelled)

    def test_cancelled_before_prompt_never_sends_the_prompt(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._process_alive = lambda: True
        client._loaded_session_id = "s1"
        client._highspeed_model_session_id = "s1"
        client._request = lambda *_args, **_kwargs: self.fail("prompt must not be sent")
        gate = threading.Event()
        gate.set()

        with self.assertRaises(KimiACPCancelled):
            client.prompt_existing(
                "hello",
                session_id="s1",
                turn_id="turn-1",
                cancel_event=gate,
            )

    def test_persisted_session_load_failure_never_silently_starts_new_context(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._load_session_id = lambda: "persisted-session"
        methods = []

        def request(method, _params, **_kwargs):
            methods.append(method)
            raise KimiACPError("transport unavailable")

        client._request = request
        with self.assertRaises(KimiACPError):
            client._new_or_load_session()
        self.assertEqual(["session/load"], methods)

    def test_prepare_new_session_sets_highspeed_model_once(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client._load_session_id = lambda: ""
        client._save_session_id = lambda _sid: None
        calls = []
        options_default = [{
            "id": "model",
            "type": "select",
            "currentValue": "kimi-code/k3",
            "options": [
                {"value": "kimi-code/k3"},
                {"value": KIMI_APP_MODEL},
            ],
        }]
        options_highspeed = [{**options_default[0], "currentValue": KIMI_APP_MODEL}]

        def request(method, params, **_kwargs):
            calls.append((method, params))
            if method == "session/new":
                return {"sessionId": "s1", "configOptions": options_default}
            if method == "session/set_config_option":
                return {"configOptions": options_highspeed}
            raise AssertionError(method)

        client._request = request
        self.assertEqual("s1", client.prepare_session())
        client._process_alive = lambda: True
        client._load_session_id = lambda: "s1"
        self.assertEqual("s1", client.prepare_session())
        self.assertEqual(
            ["session/new", "session/set_config_option"],
            [method for method, _params in calls],
        )
        self.assertEqual(
            {"sessionId": "s1", "configId": "model", "value": KIMI_APP_MODEL},
            calls[1][1],
        )

    def test_prepare_loaded_session_also_sets_highspeed_model(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client._load_session_id = lambda: "persisted"
        client._save_session_id = lambda _sid: None
        methods = []
        options = [{
            "id": "model",
            "type": "select",
            "currentValue": "kimi-code/k3",
            "options": [{"value": "kimi-code/k3"}, {"value": KIMI_APP_MODEL}],
        }]

        def request(method, _params, **_kwargs):
            methods.append(method)
            if method == "session/load":
                return {"configOptions": options}
            if method == "session/set_config_option":
                return {"configOptions": [{**options[0], "currentValue": KIMI_APP_MODEL}]}
            raise AssertionError(method)

        client._request = request
        self.assertEqual("persisted", client.prepare_session())
        self.assertEqual(["session/load", "session/set_config_option"], methods)

    def test_prepare_does_not_reset_model_when_highspeed_is_already_current(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client._load_session_id = lambda: ""
        client._save_session_id = lambda _sid: None
        methods = []
        options = [{
            "id": "model",
            "type": "select",
            "currentValue": KIMI_APP_MODEL,
            "options": [{"value": "kimi-code/k3"}, {"value": KIMI_APP_MODEL}],
        }]

        def request(method, _params, **_kwargs):
            methods.append(method)
            if method == "session/new":
                return {"sessionId": "s1", "configOptions": options}
            raise AssertionError(method)

        client._request = request
        self.assertEqual("s1", client.prepare_session())
        self.assertEqual(["session/new"], methods)

    def test_prompt_existing_rejects_blank_or_unprepared_session(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        with self.assertRaisesRegex(KimiACPError, "identities"):
            client.prompt_existing("hello", session_id="", turn_id="turn-1")
        client._process_alive = lambda: True
        with self.assertRaisesRegex(KimiACPError, "not prepared"):
            client.prompt_existing("hello", session_id="s1", turn_id="turn-1")

    def test_delayed_old_cancel_cannot_touch_new_turn_in_same_session(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        sent = []
        client._process_alive = lambda: True
        client._write = sent.append
        client._active_session_id = "shared-session"
        client._active_turn_id = "new-turn"

        self.assertFalse(client.cancel("old-turn", "shared-session"))
        self.assertEqual([], sent)
        self.assertTrue(client.cancel("new-turn", "shared-session"))
        self.assertEqual("shared-session", sent[-1]["params"]["sessionId"])

    def test_old_reader_exit_after_rapid_restart_wakes_only_old_generation(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        old_event = threading.Event()
        new_event = threading.Event()
        old_bucket = {}
        new_bucket = {}
        client._process_generation = 2
        client._pending = {
            1: (old_event, old_bucket, 1),
            2: (new_event, new_bucket, 2),
        }
        old_process = type("OldProcess", (), {"stdout": io.StringIO("")})()

        client._read_stdout(old_process, 1)

        self.assertTrue(old_event.is_set())
        self.assertEqual("Kimi ACP exited", old_bucket["failure"])
        self.assertFalse(new_event.is_set())
        self.assertEqual({}, new_bucket)


if __name__ == "__main__":
    unittest.main()
