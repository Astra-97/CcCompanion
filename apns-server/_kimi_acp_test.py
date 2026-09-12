import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kimi_acp import (
    DEFAULT_KIMI_CWD,
    KIMI_APP_MODEL,
    KimiACPAuthRequired,
    KimiACPClient,
    KimiACPCancelled,
    KimiACPError,
    KimiMemoryWriteTracker,
    _text_from_update,
)


class KimiACPProtocolTest(unittest.TestCase):
    def test_app_model_is_k3_256k(self):
        self.assertEqual("kimi-code/k3-256k", KIMI_APP_MODEL)

    def test_terminal_session_validation_requires_syntax_and_local_allowlist(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-terminal-validation")
        client.list_local_sessions = lambda **_kwargs: [
            {"session_id": "karami-session", "updated_at": 1},
        ]
        self.assertEqual(
            "karami-session",
            client.validated_local_session_id("karami-session"),
        )
        self.assertEqual("", client.validated_local_session_id("foreign-session"))
        self.assertEqual("", client.validated_local_session_id("../karami-session"))

    def test_kimi_036_thinking_option_is_read_back_without_mutation(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-thinking-readback")
        client._start = lambda: None
        client.load_session_id = lambda: ""
        client._save_session_id = lambda _session: None
        requests: list[tuple[str, dict]] = []

        def request(method, params, **_kwargs):
            requests.append((method, params))
            if method == "session/new":
                return {
                    "sessionId": "thinking-session",
                    "configOptions": [
                        {
                            "id": "model",
                            "currentValue": KIMI_APP_MODEL,
                            "options": [{"value": KIMI_APP_MODEL}],
                        },
                        {
                            "id": "thinking",
                            "currentValue": "high",
                            "options": [
                                {"value": "low"},
                                {"value": "high"},
                                {"value": "max"},
                            ],
                        },
                    ],
                }
            raise AssertionError(method)

        client._request = request
        self.assertEqual(
            "thinking-session",
            client.prepare_session(model=KIMI_APP_MODEL, reasoning_effort="high"),
        )
        self.assertEqual(
            (KIMI_APP_MODEL, "high"),
            client.prepared_selection("thinking-session"),
        )
        self.assertEqual(["session/new"], [method for method, _params in requests])

    def test_kimi_036_thinking_change_requires_exact_readback(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-thinking-change")
        client._start = lambda: None
        client.load_session_id = lambda: ""
        client._save_session_id = lambda _session: None
        calls: list[tuple[str, dict]] = []
        options = [
            {
                "id": "model",
                "currentValue": KIMI_APP_MODEL,
                "options": [{"value": KIMI_APP_MODEL}],
            },
            {
                "id": "thinking",
                "currentValue": "high",
                "options": [{"value": "low"}, {"value": "high"}],
            },
        ]

        def request(method, params, **_kwargs):
            calls.append((method, params))
            if method == "session/new":
                return {"sessionId": "thinking-session", "configOptions": options}
            if method == "session/set_config_option":
                self.assertEqual("thinking", params["configId"])
                self.assertEqual("low", params["value"])
                # A 200 response is not confirmation: the CLI must return the
                # new value in configOptions or preparation fails closed.
                return {"configOptions": options}
            raise AssertionError(method)

        client._request = request
        with self.assertRaises(KimiACPError):
            client.prepare_session(model=KIMI_APP_MODEL, reasoning_effort="low")
        self.assertIsNone(client.prepared_selection("thinking-session"))
        self.assertEqual(
            ["session/new", "session/set_config_option"],
            [method for method, _params in calls],
        )

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

    def test_default_workspace_is_isolated_from_kairos(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        self.assertEqual(Path(DEFAULT_KIMI_CWD).resolve(), client.cwd)
        self.assertNotEqual(Path("/root/Windows-Codex-TG").resolve(), client.cwd)

    def test_session_state_is_private_cwd_bound_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            client = KimiACPClient(state_path=path, cwd=cwd)
            client._save_session_id("kimi-session-1")
            self.assertEqual("kimi-session-1", client.load_session_id())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["version"])
            self.assertEqual(str(cwd.resolve()), payload["cwd"])

    def test_legacy_or_other_workspace_session_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            current = base / "current"
            other = base / "other"
            current.mkdir()
            other.mkdir()
            path = base / "session.json"
            client = KimiACPClient(state_path=path, cwd=current)

            path.write_text(
                json.dumps({"version": 1, "session_id": "legacy"}),
                encoding="utf-8",
            )
            self.assertEqual("", client.load_session_id())

            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "session_id": "other-session",
                        "cwd": str(other),
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual("", client.load_session_id())

            path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "session_id": "future-session",
                        "cwd": str(current),
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual("", client.load_session_id())

    def test_workspace_mismatch_starts_new_session_instead_of_loading_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            current = base / "current"
            old = base / "old"
            current.mkdir()
            old.mkdir()
            state_path = base / "session.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "session_id": "old-session",
                        "cwd": str(old),
                    }
                ),
                encoding="utf-8",
            )
            client = KimiACPClient(state_path=state_path, cwd=current)
            calls = []

            def request(method, params, **_kwargs):
                calls.append((method, params))
                if method == "session/new":
                    return {"sessionId": "new-session", "configOptions": []}
                raise AssertionError(method)

            client._request = request
            session_id, _options = client._new_or_load_session()

            self.assertEqual("new-session", session_id)
            self.assertEqual(["session/new"], [method for method, _params in calls])
            self.assertEqual(str(current.resolve()), calls[0][1]["cwd"])

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
        client._app_model_session_id = "s1"
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
        client._app_model_session_id = "s1"
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
        client._app_model_session_id = "s1"
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
        client.load_session_id = lambda: "persisted-session"
        methods = []

        def request(method, _params, **_kwargs):
            methods.append(method)
            raise KimiACPError("transport unavailable")

        client._request = request
        with self.assertRaises(KimiACPError):
            client._new_or_load_session()
        self.assertEqual(["session/load"], methods)

    def test_prepare_new_session_sets_app_model_once(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client.load_session_id = lambda: ""
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
        options_app_model = [{**options_default[0], "currentValue": KIMI_APP_MODEL}]

        def request(method, params, **_kwargs):
            calls.append((method, params))
            if method == "session/new":
                return {"sessionId": "s1", "configOptions": options_default}
            if method == "session/set_config_option":
                return {"configOptions": options_app_model}
            raise AssertionError(method)

        client._request = request
        self.assertEqual("s1", client.prepare_session())
        client._process_alive = lambda: True
        client.load_session_id = lambda: "s1"
        self.assertEqual("s1", client.prepare_session())
        self.assertEqual(
            ["session/new", "session/set_config_option"],
            [method for method, _params in calls],
        )
        self.assertEqual(
            {"sessionId": "s1", "configId": "model", "value": KIMI_APP_MODEL},
            calls[1][1],
        )

    def test_prepare_loaded_session_also_sets_app_model(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client.load_session_id = lambda: "persisted"
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

    def test_prepare_does_not_reset_model_when_app_model_is_already_current(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client.load_session_id = lambda: ""
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

    def test_forge_repins_app_model_for_new_session_before_seeding_summary(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client._process_alive = lambda: True
        persisted = {"session_id": "old-session"}
        client.load_session_id = lambda: persisted["session_id"]
        client._save_session_id = lambda session_id: persisted.update(session_id=session_id)
        calls = []
        prompt_calls = []
        options = [{
            "id": "model",
            "type": "select",
            "currentValue": "kimi-code/k3",
            "options": [
                {"value": "kimi-code/k3"},
                {"value": KIMI_APP_MODEL},
            ],
        }]
        pinned_options = [{**options[0], "currentValue": KIMI_APP_MODEL}]

        def request(method, params, **_kwargs):
            calls.append((method, params))
            if method == "session/load":
                return {"sessionId": params["sessionId"], "configOptions": options}
            if method == "session/set_config_option":
                return {"configOptions": pinned_options}
            if method == "session/new":
                return {"sessionId": "new-session", "configOptions": options}
            raise AssertionError(method)

        def prompt_existing(text, *, session_id, turn_id, on_update=None, **_kwargs):
            prompt_calls.append((text, session_id, turn_id))
            if len(prompt_calls) == 1:
                on_update("summary")

        client._request = request
        client.prompt_existing = prompt_existing

        result = []
        failure = []

        def run_forge():
            try:
                result.append(client.forge_new_session(summarize_prompt="summarize"))
            except Exception as exc:  # surfaced below without hiding deadlocks
                failure.append(exc)

        worker = threading.Thread(target=run_forge, daemon=True)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "forge must not deadlock while preparing the new session")
        if failure:
            raise failure[0]

        self.assertEqual(("new-session", "summary"), result[0])
        self.assertEqual(
            [
                "session/load",
                "session/set_config_option",
                "session/new",
                "session/set_config_option",
            ],
            [method for method, _params in calls],
        )
        self.assertEqual(
            ["old-session", "new-session"],
            [params["sessionId"] for method, params in calls if method == "session/set_config_option"],
        )
        self.assertTrue(
            all(params["value"] == KIMI_APP_MODEL
                for method, params in calls
                if method == "session/set_config_option")
        )
        self.assertEqual("new-session", client._app_model_session_id)
        self.assertEqual("new-session", prompt_calls[1][1])
        self.assertTrue(prompt_calls[1][0].startswith("【上下文继承】"))

    def test_preference_change_on_loaded_session_reloads_options_and_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "workspace"
            cwd.mkdir()
            client = KimiACPClient(state_path=root / "session.json", cwd=cwd)
            client._save_session_id("same-session")
            client._start = lambda: None
            client._process_alive = lambda: True
            client._loaded_session_id = "same-session"
            client._app_model_session_id = "same-session"
            client._app_effort_session_id = "same-session"
            client._prepared_selection["same-session"] = (KIMI_APP_MODEL, "high")
            options = [
                {
                    "id": "model", "currentValue": KIMI_APP_MODEL,
                    "options": [{"value": KIMI_APP_MODEL}, {"value": "kimi-code/k3"}],
                },
                {
                    "id": "thinking_effort", "currentValue": "high",
                    "options": [{"value": "low"}, {"value": "high"}],
                },
            ]
            calls = []

            def request(method, params, **_kwargs):
                calls.append((method, params))
                if method == "session/load":
                    return {"sessionId": "same-session", "configOptions": options}
                if params["configId"] == "model":
                    return {"configOptions": [
                        {**options[0], "currentValue": "kimi-code/k3"}, options[1],
                    ]}
                if params["configId"] == "thinking_effort":
                    return {"configOptions": [
                        {**options[0], "currentValue": "kimi-code/k3"},
                        {**options[1], "currentValue": "low"},
                    ]}
                raise AssertionError(method)

            client._request = request
            self.assertEqual(
                "same-session",
                client.prepare_session(model="kimi-code/k3", reasoning_effort="low"),
            )
            self.assertEqual(("kimi-code/k3", "low"), client.prepared_selection("same-session"))
            self.assertEqual(
                ["session/load", "session/set_config_option", "session/set_config_option"],
                [method for method, _params in calls],
            )

    def test_failed_partial_selection_forces_a_fresh_readback_before_reusing_old_values(self):
        """A failed effort pin cannot leave a stale model confirmation cached."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "workspace"
            cwd.mkdir()
            client = KimiACPClient(state_path=root / "session.json", cwd=cwd)
            client._save_session_id("same-session")
            client._start = lambda: None
            client._process_alive = lambda: True
            client._loaded_session_id = "same-session"
            client._app_model_session_id = "same-session"
            client._app_effort_session_id = "same-session"
            client._prepared_selection["same-session"] = (KIMI_APP_MODEL, "high")
            remote = {"model": KIMI_APP_MODEL, "effort": "high"}
            methods: list[str] = []

            def options():
                return [
                    {
                        "id": "model", "currentValue": remote["model"],
                        "options": [{"value": KIMI_APP_MODEL}, {"value": "kimi-code/k3"}],
                    },
                    {
                        "id": "thinking_effort", "currentValue": remote["effort"],
                        "options": [{"value": "low"}, {"value": "high"}],
                    },
                ]

            def request(method, params, **_kwargs):
                methods.append(method)
                if method == "session/load":
                    return {"sessionId": "same-session", "configOptions": options()}
                if method == "session/set_config_option":
                    if params["configId"] == "model":
                        remote["model"] = params["value"]
                    elif params["configId"] == "thinking_effort":
                        # The remote accepts the model change but fails to
                        # confirm the new effort, modelling a partial ACP
                        # mutation before the local operation raises.
                        self.assertEqual("low", params["value"])
                    return {"configOptions": options()}
                raise AssertionError(method)

            client._request = request
            with self.assertRaises(KimiACPError):
                client.prepare_session(model="kimi-code/k3", reasoning_effort="low")

            self.assertEqual("kimi-code/k3", remote["model"])
            self.assertIsNone(client.prepared_selection("same-session"))
            self.assertEqual("", client._app_model_session_id)
            self.assertEqual("", client._app_effort_session_id)

            self.assertEqual(
                "same-session",
                client.prepare_session(model=KIMI_APP_MODEL, reasoning_effort="high"),
            )
            self.assertEqual((KIMI_APP_MODEL, "high"), client.prepared_selection("same-session"))
            self.assertEqual(2, methods.count("session/load"))
            self.assertEqual(KIMI_APP_MODEL, remote["model"])

    def test_failed_selection_never_commits_candidate_pointer(self):
        options_without_app_model = [{
            "id": "model", "currentValue": "kimi-code/k3",
            "options": [{"value": "kimi-code/k3"}],
        }]

        def build_client(root: Path, pointer: str = ""):
            cwd = root / "workspace"
            cwd.mkdir()
            client = KimiACPClient(state_path=root / "session.json", cwd=cwd)
            if pointer:
                client._save_session_id(pointer)
            client._start = lambda: None
            return client

        with self.subTest(path="new"):
            with tempfile.TemporaryDirectory() as tmp:
                client = build_client(Path(tmp))
                client._request = lambda method, _params, **_kwargs: {
                    "sessionId": "candidate-new", "configOptions": options_without_app_model,
                } if method == "session/new" else self.fail(method)
                with self.assertRaises(KimiACPError):
                    client.prepare_session()
                self.assertEqual("", client.load_session_id())

        with self.subTest(path="load"):
            with tempfile.TemporaryDirectory() as tmp:
                client = build_client(Path(tmp), "old-session")
                client._request = lambda method, params, **_kwargs: {
                    "sessionId": params["sessionId"], "configOptions": options_without_app_model,
                } if method == "session/load" else self.fail(method)
                with self.assertRaises(KimiACPError):
                    client.prepare_session()
                self.assertEqual("old-session", client.load_session_id())

        with self.subTest(path="switch"):
            with tempfile.TemporaryDirectory() as tmp:
                client = build_client(Path(tmp), "old-session")
                client._request = lambda method, params, **_kwargs: {
                    "sessionId": params["sessionId"], "configOptions": options_without_app_model,
                } if method == "session/load" else self.fail(method)
                with self.assertRaises(KimiACPError):
                    client.prepare_existing_session(
                        "candidate-switch", model=KIMI_APP_MODEL, reasoning_effort="high",
                    )
                self.assertEqual("old-session", client.load_session_id())

        with self.subTest(path="forge"):
            with tempfile.TemporaryDirectory() as tmp:
                client = build_client(Path(tmp), "old-session")
                client._process_alive = lambda: True
                old_options = [{
                    "id": "model", "currentValue": KIMI_APP_MODEL,
                    "options": [{"value": KIMI_APP_MODEL}],
                }]

                def request(method, params, **_kwargs):
                    if method == "session/load":
                        return {"sessionId": params["sessionId"], "configOptions": old_options}
                    if method == "session/new":
                        return {"sessionId": "candidate-forge", "configOptions": options_without_app_model}
                    raise AssertionError(method)

                client._request = request
                client._prompt_and_collect_text = lambda *_args, **_kwargs: "summary"
                with self.assertRaises(KimiACPError):
                    client.forge_new_session()
                self.assertEqual("old-session", client.load_session_id())

    def test_invalid_acp_session_ids_are_rejected_before_pointer_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "workspace"
            cwd.mkdir()
            client = KimiACPClient(state_path=root / "session.json", cwd=cwd)
            client._request = lambda method, _params, **_kwargs: {
                "sessionId": "../../not-a-session", "configOptions": [],
            } if method == "session/new" else self.fail(method)
            with self.assertRaises(KimiACPError):
                client._new_or_load_session()
            self.assertEqual("", client.load_session_id())

            client._save_session_id("old-session")
            client._request = lambda method, _params, **_kwargs: {
                "sessionId": "invalid/id", "configOptions": [],
            } if method == "session/load" else self.fail(method)
            with self.assertRaises(KimiACPError):
                client._new_or_load_session()
            self.assertEqual("old-session", client.load_session_id())

    def test_list_local_sessions_accepts_legacy_and_workdir_formats_without_leaking_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "workspace"
            cwd.mkdir()
            sessions_root = root / ".kimi-code" / "sessions" / "wd_test"
            legacy = sessions_root / "session_legacy-id" / "state.json"
            current = sessions_root / "session_current-id" / "state.json"
            zero = sessions_root / "session_zero-id" / "state.json"
            foreign = sessions_root / "session_foreign-id" / "state.json"
            invalid = sessions_root / "session_bad!id" / "state.json"
            for state_file in (legacy, current, zero, foreign, invalid):
                state_file.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps({
                "id": "legacy-id", "cwd": str(cwd), "updatedAt": 1_700_000_000_000,
                "title": "do not expose", "lastPrompt": "do not expose",
            }), encoding="utf-8")
            current.write_text(json.dumps({
                "workDir": str(cwd), "updatedAt": "2026-08-16T01:02:03Z",
                "agents": ["do not expose"],
            }), encoding="utf-8")
            zero.write_text(json.dumps({"workDir": str(cwd), "updatedAt": "not-a-date"}), encoding="utf-8")
            foreign.write_text(json.dumps({"workDir": str(root / "other"), "updatedAt": "bad"}), encoding="utf-8")
            invalid.write_text(json.dumps({"workDir": str(cwd), "updatedAt": "bad"}), encoding="utf-8")

            client = KimiACPClient(state_path=root / "pointer.json", cwd=cwd)
            with patch("kimi_acp.Path.home", return_value=root):
                records = client.list_local_sessions()

            self.assertEqual(
                ["current-id", "legacy-id", "zero-id"],
                [item["session_id"] for item in records],
            )
            self.assertGreater(records[0]["updated_at"], records[1]["updated_at"])
            self.assertEqual(0, records[-1]["updated_at"])
            self.assertNotIn("title", str(records))
            self.assertNotIn("lastPrompt", str(records))
            self.assertNotIn("agents", str(records))

    def test_close_clears_app_model_cache_before_same_session_is_prepared_again(self):
        client = KimiACPClient(state_path="/tmp/unused-kimi-test-state")
        client._start = lambda: None
        client._process_alive = lambda: True
        client._app_model_session_id = "same-session"
        client._loaded_session_id = "same-session"
        client.close()
        self.assertEqual("", client._app_model_session_id)

        client.load_session_id = lambda: "same-session"
        client._save_session_id = lambda _session_id: None
        calls = []
        options = [{
            "id": "model",
            "type": "select",
            "currentValue": "kimi-code/k3",
            "options": [
                {"value": "kimi-code/k3"},
                {"value": KIMI_APP_MODEL},
            ],
        }]
        pinned_options = [{**options[0], "currentValue": KIMI_APP_MODEL}]

        def request(method, params, **_kwargs):
            calls.append((method, params))
            if method == "session/load":
                return {"sessionId": "same-session", "configOptions": options}
            if method == "session/set_config_option":
                return {"configOptions": pinned_options}
            raise AssertionError(method)

        client._request = request
        self.assertEqual("same-session", client.prepare_session())
        self.assertEqual(
            ["session/load", "session/set_config_option"],
            [method for method, _params in calls],
        )
        self.assertEqual(KIMI_APP_MODEL, calls[1][1]["value"])
        self.assertEqual("same-session", client._app_model_session_id)

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


class KimiACPMemoryWriteProjectionTest(unittest.TestCase):
    def _client(self) -> KimiACPClient:
        return KimiACPClient(state_path="/tmp/unused-kimi-memory-write-test")

    def test_completed_write_memory_projects_bounded_card_event(self):
        event = self._client()._memory_write_from_update({
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-1",
                "title": "mcp__memory__write_memory",
                "status": "completed",
                "rawInput": {
                    "content": "景甜×孙宇晨天价彩礼瓜条\n\n追瓜实录" + "长" * 200,
                    "category": "daily",
                    "subcategory": "瓜条",
                },
                "output": '{"ok": true, "id": "bee5817d"}',
            },
        })
        self.assertIsNotNone(event)
        self.assertEqual("memory_write", event["kind"])
        self.assertEqual("write", event["action"])
        self.assertEqual("call-1", event["tool_call_id"])
        self.assertEqual("bee5817d", event["memory_id"])
        self.assertEqual("daily", event["category"])
        self.assertEqual("瓜条", event["subcategory"])
        self.assertEqual("景甜×孙宇晨天价彩礼瓜条", event["title"])
        self.assertLessEqual(len(event["snippet"]), 80)

    def test_update_memory_takes_id_from_raw_input_and_json_title(self):
        event = self._client()._memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-2",
                "title": "mcp__memory__update_memory",
                "status": "completed",
                "rawInput": {
                    "id": "7b64ed1e",
                    "content": '{"title": "碳基哥档案", "context": "更新摘要"}',
                },
            },
        })
        self.assertEqual("update", event["action"])
        self.assertEqual("7b64ed1e", event["memory_id"])
        self.assertEqual("碳基哥档案", event["title"])
        self.assertEqual("更新摘要", event["snippet"])

    def test_non_terminal_or_failed_calls_never_project(self):
        client = self._client()
        for status in ("", "pending", "in_progress", "failed"):
            with self.subTest(status=status):
                self.assertIsNone(client._memory_write_from_update({
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "title": "mcp__memory__write_memory",
                        "status": status,
                        "rawInput": {"content": "x"},
                    },
                }))

    def test_unrelated_tools_and_malformed_updates_never_project(self):
        client = self._client()
        self.assertIsNone(client._memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call_update",
                "title": "mcp__memory__read_memory",
                "status": "completed",
            },
        }))
        self.assertIsNone(client._memory_write_from_update({
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "x"},
            },
        }))
        self.assertIsNone(client._memory_write_from_update(None))
        self.assertIsNone(client._memory_write_from_update({"update": "not-a-dict"}))

    def test_bare_tool_name_and_title_suffix_variants_project(self):
        client = self._client()
        bare = client._memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call_update",
                "title": "write_memory",
                "status": "completed",
                "rawInput": {"content": "hello"},
                "output": "no json here",
            },
        })
        self.assertEqual("write", bare["action"])
        self.assertEqual("", bare["memory_id"])
        suffixed = client._memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call_update",
                "title": "mcp__memory__update_memory · 7b64ed1e",
                "status": "completed",
                "rawInput": {"id": "7b64ed1e"},
            },
        })
        self.assertEqual("update", suffixed["action"])
        self.assertEqual("7b64ed1e", suffixed["memory_id"])

    def test_unsafe_memory_id_is_dropped(self):
        event = self._client()._memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call_update",
                "title": "mcp__memory__write_memory",
                "status": "completed",
                "rawInput": {"content": "hello"},
                "output": '{"id": "../escape"}',
            },
        })
        self.assertEqual("", event["memory_id"])


class KimiACPMcpMemoryWriteCacheTest(unittest.TestCase):
    """kimi-code MCP 记忆写入的三段式 ACP 序列也要投 memory_write 事件。

    真实 ACP 抓包（2026-09-12，/tmp/acp_dump.jsonl 同形状）：pending 的
    tool_call 带 title 无 rawInput → in_progress 的 tool_call_update 带
    title + rawInput（dict 或 JSON 字符串，两种都兼容）→ 终态 completed
    只剩 toolCallId / status / content / rawOutput，无 title 无 rawInput，
    必须按 toolCallId 跨 update 关联（与 Bash 路径同一范式）。
    """

    def _client(self) -> KimiACPClient:
        return KimiACPClient(state_path="/tmp/unused-kimi-mcp-memory-test")

    @staticmethod
    def _pending(tool_call_id: str, title: str) -> dict:
        return {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
                "title": title,
                "kind": "other",
                "status": "pending",
                "content": [{"type": "content", "content": {"type": "text", "text": ""}}],
            },
        }

    @staticmethod
    def _in_progress(tool_call_id: str, title: str, raw_input) -> dict:
        return {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "title": title,
                "kind": "other",
                "status": "in_progress",
                "rawInput": raw_input,
            },
        }

    @staticmethod
    def _terminal(tool_call_id: str, status: str, output: str = "") -> dict:
        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "status": status,
            "content": [
                {"type": "content", "content": {"type": "text", "text": output}},
            ],
        }
        if output:
            update["rawOutput"] = output
        return {"update": update}

    def test_kimi_three_phase_write_memory_projects_card_event(self):
        client = self._client()
        tool_call_id = "0:tool_1VXgaVOjiIh7xzfeikxYITfK"
        title = "mcp__memory__write_memory"
        self.assertIsNone(client._memory_write_from_update(
            self._pending(tool_call_id, title)
        ))
        self.assertIsNone(client._memory_write_from_update(
            self._in_progress(
                tool_call_id,
                title,
                '{"content": "小克修复了 MCP 记忆卡片\\n\\n三段式关联", '
                '"category": "daily", "subcategory": "daily.note"}',
            )
        ))
        event = client._memory_write_from_update(self._terminal(
            tool_call_id,
            "completed",
            '{"id": "bee5817d", "createdAt": "2026-09-12T05:00:00Z"}',
        ))
        self.assertIsNotNone(event)
        self.assertEqual("memory_write", event["kind"])
        self.assertEqual("write", event["action"])
        self.assertEqual(tool_call_id, event["tool_call_id"])
        self.assertEqual("bee5817d", event["memory_id"])
        self.assertEqual("daily", event["category"])
        self.assertEqual("daily.note", event["subcategory"])
        self.assertEqual("小克修复了 MCP 记忆卡片", event["title"])
        self.assertLessEqual(len(event["snippet"]), 80)

    def test_update_memory_takes_memory_id_from_cached_raw_input(self):
        client = self._client()
        tool_call_id = "0:tool_update1"
        title = "mcp__memory__update_memory"
        client._memory_write_from_update(self._pending(tool_call_id, title))
        client._memory_write_from_update(self._in_progress(
            tool_call_id,
            title,
            '{"id": "7b64ed1e", "content": "追加一行"}',
        ))
        event = client._memory_write_from_update(self._terminal(
            tool_call_id, "completed", '{"ok": true}'
        ))
        self.assertIsNotNone(event)
        self.assertEqual("update", event["action"])
        self.assertEqual("7b64ed1e", event["memory_id"])
        self.assertEqual("追加一行", event["title"])

    def test_failed_terminal_never_projects_and_pops_cache(self):
        client = self._client()
        tool_call_id = "0:tool_failed1"
        title = "mcp__memory__write_memory"
        client._memory_write_from_update(self._pending(tool_call_id, title))
        client._memory_write_from_update(self._in_progress(
            tool_call_id, title, '{"content": "x", "category": "daily"}'
        ))
        self.assertIsNone(client._memory_write_from_update(
            self._terminal(tool_call_id, "failed")
        ))
        self.assertNotIn(tool_call_id, client._memory_write_tool_calls)
        # 缓存已弹出：同 toolCallId 的迟到 completed 也不能凭空产卡片。
        self.assertIsNone(client._memory_write_from_update(
            self._terminal(tool_call_id, "completed", '{"id": "bee5817d"}')
        ))

    def test_terminal_update_with_own_title_still_projects(self):
        client = self._client()
        event = client._memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-legacy",
                "title": "mcp__memory__write_memory",
                "status": "completed",
                "rawInput": {"content": "旧形状", "category": "daily"},
                "output": '{"id": "bee5817d"}',
            },
        })
        self.assertIsNotNone(event)
        self.assertEqual("write", event["action"])
        self.assertEqual("bee5817d", event["memory_id"])

    def test_string_raw_input_json_parsing_and_fallback(self):
        client = self._client()
        # 非法 JSON 字符串按空 dict 处理，不炸、不产字段，但终态仍可凭
        # 缓存的 action + 输出里的 memory_id 产卡片。
        client._memory_write_from_update(self._pending("0:tool_badjson", "mcp__memory__write_memory"))
        client._memory_write_from_update(self._in_progress(
            "0:tool_badjson", "mcp__memory__write_memory", "not-json{"
        ))
        event = client._memory_write_from_update(self._terminal(
            "0:tool_badjson", "completed", '{"id": "bee5817d"}'
        ))
        self.assertIsNotNone(event)
        self.assertEqual("write", event["action"])
        self.assertEqual("bee5817d", event["memory_id"])
        self.assertEqual("", event["category"])
        self.assertEqual("", event["title"])
        # dict 形式的 rawInput（其他 ACP 实现）同样被缓存。
        client._memory_write_from_update(self._in_progress(
            "0:tool_dictinput",
            "mcp__memory__update_memory",
            {"id": "7b64ed1e", "content": "dict 输入"},
        ))
        event = client._memory_write_from_update(self._terminal(
            "0:tool_dictinput", "completed", '{"ok": true}'
        ))
        self.assertIsNotNone(event)
        self.assertEqual("update", event["action"])
        self.assertEqual("7b64ed1e", event["memory_id"])

    def test_pending_cache_is_fifo_bounded(self):
        client = self._client()
        for index in range(70):
            client._memory_write_from_update(self._pending(
                f"0:tool_fifo{index}", "mcp__memory__write_memory"
            ))
        self.assertEqual(64, len(client._memory_write_tool_calls))
        self.assertNotIn("0:tool_fifo0", client._memory_write_tool_calls)
        self.assertIn("0:tool_fifo69", client._memory_write_tool_calls)

    def test_interleaved_calls_project_independent_cards(self):
        client = self._client()
        # A pending → B pending → B completed → A completed：两个写调用交错
        # 完成，卡片必须按各自 toolCallId 的缓存取字段，互不串号。
        client._memory_write_from_update(self._pending(
            "0:tool_interA", "mcp__memory__write_memory"
        ))
        client._memory_write_from_update(self._in_progress(
            "0:tool_interA",
            "mcp__memory__write_memory",
            '{"content": "A 的记忆\\n\\n甲", "category": "daily"}',
        ))
        client._memory_write_from_update(self._pending(
            "0:tool_interB", "mcp__memory__update_memory"
        ))
        client._memory_write_from_update(self._in_progress(
            "0:tool_interB",
            "mcp__memory__update_memory",
            '{"id": "7b64ed1e", "content": "B 的更新"}',
        ))
        event_b = client._memory_write_from_update(self._terminal(
            "0:tool_interB", "completed", '{"ok": true}'
        ))
        event_a = client._memory_write_from_update(self._terminal(
            "0:tool_interA", "completed", '{"id": "bee5817d"}'
        ))
        self.assertIsNotNone(event_b)
        self.assertEqual("update", event_b["action"])
        self.assertEqual("0:tool_interB", event_b["tool_call_id"])
        self.assertEqual("7b64ed1e", event_b["memory_id"])
        self.assertEqual("B 的更新", event_b["title"])
        self.assertEqual("", event_b["category"])
        self.assertIsNotNone(event_a)
        self.assertEqual("write", event_a["action"])
        self.assertEqual("0:tool_interA", event_a["tool_call_id"])
        self.assertEqual("bee5817d", event_a["memory_id"])
        self.assertEqual("A 的记忆", event_a["title"])
        self.assertEqual("daily", event_a["category"])
        self.assertEqual({}, client._memory_write_tool_calls)


class KimiACPBashMemoryWriteTest(unittest.TestCase):
    """Bash 直连记忆 REST API 的写操作也要投 memory_write 事件。

    真实 ACP 流（2026-09-10 实机探测）：命令只出现在 pending/in_progress
    update 的 rawInput.command（title 为 "Running: <cmd>"），终态 completed
    update 不带 title/rawInput，输出在 content 块和 rawOutput 里，必须按
    toolCallId 跨 update 关联。
    """

    def _client(self) -> KimiACPClient:
        return KimiACPClient(state_path="/tmp/unused-kimi-bash-memory-test")

    @staticmethod
    def _in_progress(tool_call_id: str, command: str) -> dict:
        return {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "title": "Running: " + command[:60],
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"command": command},
            },
        }

    @staticmethod
    def _completed(tool_call_id: str, output: str) -> dict:
        return {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": "completed",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": output}},
                ],
                "rawOutput": output,
            },
        }

    def test_curl_post_new_memory_projects_card_event(self):
        client = self._client()
        command = (
            "curl -sS -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-H 'Authorization: Bearer FAKE-TEST-TOKEN' "
            "-d '{\"content\": \"卡拉米工作区规矩\\n\\n只用于卡拉米会话\", "
            "\"category\": \"workflow\", \"subcategory\": \"规矩\"}'"
        )
        self.assertIsNone(
            client._bash_memory_write_from_update(self._in_progress("call-b1", command))
        )
        event = client._bash_memory_write_from_update(self._completed(
            "call-b1",
            '{"id": "bee5817d", "createdAt": "2026-09-10T05:00:00Z", "ok": true}',
        ))
        self.assertIsNotNone(event)
        self.assertEqual("memory_write", event["kind"])
        self.assertEqual("write", event["action"])
        self.assertEqual("call-b1", event["tool_call_id"])
        self.assertEqual("bee5817d", event["memory_id"])
        self.assertEqual("workflow", event["category"])
        self.assertEqual("规矩", event["subcategory"])
        self.assertEqual("卡拉米工作区规矩", event["title"])
        self.assertLessEqual(len(event["snippet"]), 80)

    def test_curl_data_without_explicit_method_is_post(self):
        client = self._client()
        command = (
            "curl -s https://memory.xiaonancaleb.xyz/api/memories "
            "--data '{\"content\": \"隐式 POST\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b2", command))
        event = client._bash_memory_write_from_update(self._completed(
            "call-b2", '{"id": "a1b2c3d4", "createdAt": "2026-09-10"}',
        ))
        self.assertEqual("write", event["action"])
        self.assertEqual("a1b2c3d4", event["memory_id"])

    def test_curl_put_update_takes_id_from_url(self):
        client = self._client()
        command = (
            "curl -sS -X PUT https://memory.xiaonancaleb.xyz/api/memories/7b64ed1e "
            "-H 'Authorization: Bearer FAKE-TEST-TOKEN' "
            "-d '{\"append\": \"补充一行事实\", \"category\": \"daily\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b3", command))
        event = client._bash_memory_write_from_update(self._completed(
            "call-b3",
            '{"id": "7b64ed1e", "updatedAt": "2026-09-10T05:30:00Z"}',
        ))
        self.assertEqual("update", event["action"])
        self.assertEqual("7b64ed1e", event["memory_id"])
        self.assertEqual("补充一行事实", event["title"])
        self.assertEqual("daily", event["category"])

    def test_python_urllib_put_with_fstring_url_falls_back_to_uuid_literal(self):
        client = self._client()
        command = (
            "python3 -c \"\n"
            "import json, urllib.request\n"
            "mid = 'a9e9c390-1234-4abc-8def-0123456789ab'\n"
            "req = urllib.request.Request(\n"
            "    'https://memory.xiaonancaleb.xyz/api/memories/' + mid,\n"
            "    data=json.dumps({'append': 'urllib 追加'}).encode(),\n"
            "    method='PUT')\n"
            "print(urllib.request.urlopen(req).read().decode())\n\""
        )
        client._bash_memory_write_from_update(self._in_progress("call-b4", command))
        event = client._bash_memory_write_from_update(self._completed(
            "call-b4", '{"id": "a9e9c390-1234-4abc-8def-0123456789ab", "updatedAt": "2026-09-10"}',
        ))
        self.assertEqual("update", event["action"])
        self.assertEqual("a9e9c390-1234-4abc-8def-0123456789ab", event["memory_id"])
        self.assertEqual("urllib 追加", event["title"])

    def test_http_400_output_never_projects(self):
        client = self._client()
        command = (
            "curl -sS -i -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-d '{\"content\": \"会失败的写入\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b5", command))
        self.assertIsNone(client._bash_memory_write_from_update(self._completed(
            "call-b5",
            "HTTP/1.1 400 Bad Request\n{\"error\": \"content is required\"}",
        )))

    def test_success_false_output_never_projects(self):
        client = self._client()
        command = (
            "curl -sS -X PUT https://memory.xiaonancaleb.xyz/api/memories/deadbeef "
            "-d '{\"append\": \"x\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b6", command))
        self.assertIsNone(client._bash_memory_write_from_update(self._completed(
            "call-b6", '{"success": false, "message": "记忆不存在"}',
        )))

    def test_unrelated_bash_commands_never_project(self):
        client = self._client()
        for tool_call_id, command in (
            ("call-b7", "ls -la /tmp"),
            ("call-b8", "curl -sS https://memory.xiaonancaleb.xyz/api/memories?limit=1"),
            ("call-b9", "curl -sS -X POST https://example.com/api/memories -d '{}'"),
        ):
            with self.subTest(command=command):
                client._bash_memory_write_from_update(self._in_progress(tool_call_id, command))
                self.assertIsNone(client._bash_memory_write_from_update(self._completed(
                    tool_call_id, '{"id": "bee5817d", "createdAt": "2026-09-10"}',
                )))

    def test_failed_or_pending_terminal_status_never_projects(self):
        client = self._client()
        command = (
            "curl -sS -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-d '{\"content\": \"x\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b10", command))
        for status in ("failed", "in_progress", "pending"):
            with self.subTest(status=status):
                update = self._completed("call-b10", '{"id": "bee5817d", "createdAt": "2026-09-10"}')
                update["update"]["status"] = status
                self.assertIsNone(client._bash_memory_write_from_update(update))

    def test_event_never_contains_authorization_token(self):
        client = self._client()
        secret = "FAKE-TEST-TOKEN-never-leak"
        command = (
            "curl -sS -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-H 'Authorization: Bearer " + secret + "' "
            "-H 'User-Agent: curl/7.81.0' "
            "-d '{\"content\": \"token 泄漏检查\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b11", command))
        event = client._bash_memory_write_from_update(self._completed(
            "call-b11", '{"id": "bee5817d", "createdAt": "2026-09-10"}',
        ))
        self.assertIsNotNone(event)
        self.assertNotIn(secret, json.dumps(event, ensure_ascii=False))
        self.assertEqual(
            {"kind", "action", "tool_call_id", "memory_id", "title",
             "category", "subcategory", "snippet"},
            set(event.keys()),
        )

    def test_get_piped_into_bare_put_literal_never_projects(self):
        # 审核复现的构造性误报：纯 GET 管道后段打印 'PUT'，输出含 createdAt
        # 时曾投出 action="update"、字段全空的假卡片。
        client = self._client()
        command = (
            "curl -s 'https://memory.xiaonancaleb.xyz/api/memories?limit=1' "
            "| python3 -c \"print('PUT')\""
        )
        client._bash_memory_write_from_update(self._in_progress("call-b12", command))
        self.assertIsNone(client._bash_memory_write_from_update(self._completed(
            "call-b12",
            'PUT\n[{"id": "bee5817d", "createdAt": "2026-09-10"}]',
        )))

    def test_update_without_memory_id_never_projects(self):
        # update 类事件必须解析出非空 memory_id（卡片跳转目标），否则不投。
        client = self._client()
        command = (
            "curl -sS -X PUT https://memory.xiaonancaleb.xyz/api/memories "
            "-d '{\"append\": \"x\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b13", command))
        self.assertIsNone(client._bash_memory_write_from_update(self._completed(
            "call-b13", '{"updatedAt": "2026-09-10T06:00:00Z"}',
        )))

    def test_positional_put_literal_inside_urllib_context_still_projects(self):
        client = self._client()
        command = (
            "python3 -c \"import urllib.request; "
            "print(urllib.request.urlopen(urllib.request.Request("
            "'https://memory.xiaonancaleb.xyz/api/memories/7b64ed1e', "
            "data=b'{}', headers={}, origin_req_host=None, method=None) "
            "if False else urllib.request.Request("
            "'https://memory.xiaonancaleb.xyz/api/memories/7b64ed1e', b'{}', {}, None, 'PUT')"
            ").read().decode())\""
        )
        client._bash_memory_write_from_update(self._in_progress("call-b14", command))
        event = client._bash_memory_write_from_update(self._completed(
            "call-b14", '{"id": "7b64ed1e", "updatedAt": "2026-09-10"}',
        ))
        self.assertIsNotNone(event)
        self.assertEqual("update", event["action"])
        self.assertEqual("7b64ed1e", event["memory_id"])

    def test_failed_terminal_status_pops_remembered_command(self):
        client = self._client()
        command = (
            "curl -sS -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-d '{\"content\": \"x\"}'"
        )
        client._bash_memory_write_from_update(self._in_progress("call-b15", command))
        self.assertIn("call-b15", client._bash_tool_commands)
        self.assertIsNone(client._bash_memory_write_from_update({
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-b15",
                "status": "failed",
            },
        }))
        self.assertNotIn("call-b15", client._bash_tool_commands)

    def test_pending_command_state_is_bounded_and_cleared_per_turn(self):
        client = self._client()
        for index in range(40):
            client._bash_memory_write_from_update(
                self._in_progress("call-flood-%d" % index, "echo %d" % index)
            )
        self.assertLessEqual(len(client._bash_tool_commands), 32)
        with client._active_lock:
            client._bash_tool_commands.clear()
        self.assertEqual({}, client._bash_tool_commands)


class KimiMemoryWriteTrackerTest(unittest.TestCase):
    """Web 事件流两帧式（tool.call.started + tool.result）记忆写入投影。

    真实 WS 抓包（2026-09-12，kimi-code v0.42.0 /api/v1/ws）：started 帧
    payload 携带 name+args，result 帧携带 output（失败时另带 isError:
    true，成功时省略该键）。投影与 ACP 三段式共享同一份白名单字段。
    """

    def test_write_memory_started_then_result_projects_card_event(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("tool_w1", "mcp__memory__write_memory", {
            "content": "小克修复了 Web 记忆卡片\n\n两帧式关联",
            "category": "daily",
            "subcategory": "daily.note",
        })
        event = tracker.note_call_result(
            "tool_w1", '{"id": "bee5817d", "createdAt": "2026-09-12T09:00:00Z"}'
        )
        self.assertIsNotNone(event)
        self.assertEqual("memory_write", event["kind"])
        self.assertEqual("write", event["action"])
        self.assertEqual("tool_w1", event["tool_call_id"])
        self.assertEqual("bee5817d", event["memory_id"])
        self.assertEqual("daily", event["category"])
        self.assertEqual("daily.note", event["subcategory"])
        self.assertEqual("小克修复了 Web 记忆卡片", event["title"])
        self.assertLessEqual(len(event["snippet"]), 80)

    def test_update_memory_takes_memory_id_from_args(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("tool_u1", "mcp__memory__update_memory", {
            "id": "7b64ed1e",
            "append": "追加一行",
        })
        event = tracker.note_call_result("tool_u1", '{"success": true}')
        self.assertIsNotNone(event)
        self.assertEqual("update", event["action"])
        self.assertEqual("7b64ed1e", event["memory_id"])
        self.assertEqual("追加一行", event["title"])

    def test_bare_tool_name_without_mcp_prefix_still_projects(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("tool_bare", "write_memory", {
            "content": "裸名兼容", "category": "daily",
        })
        event = tracker.note_call_result("tool_bare", '{"id": "abc123"}')
        self.assertIsNotNone(event)
        self.assertEqual("write", event["action"])

    def test_is_error_result_never_projects(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("tool_e1", "mcp__memory__write_memory", {
            "content": "x", "category": "core",
        })
        self.assertIsNone(tracker.note_call_result(
            "tool_e1", "core 记忆必须选择一个子分类……", is_error=True
        ))
        # 缓存已弹出：同 toolCallId 的迟到成功结果也不能凭空产卡片。
        self.assertIsNone(tracker.note_call_result("tool_e1", '{"id": "bee5817d"}'))

    def test_result_without_remembered_call_never_projects(self):
        tracker = KimiMemoryWriteTracker()
        self.assertIsNone(tracker.note_call_result("tool_unknown", '{"id": "bee5817d"}'))

    def test_non_memory_tool_result_never_projects(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("tool_r1", "mcp__memory__read_memories", {"limit": 1})
        self.assertIsNone(tracker.note_call_result("tool_r1", '[{"id": "x"}]'))

    def test_json_string_args_are_parsed_like_acp_raw_input(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started(
            "tool_js1",
            "mcp__memory__write_memory",
            '{"content": "JSON 字符串 args", "category": "daily"}',
        )
        event = tracker.note_call_result("tool_js1", '{"id": "bee5817d"}')
        self.assertIsNotNone(event)
        self.assertEqual("JSON 字符串 args", event["title"])
        self.assertEqual("daily", event["category"])

    def test_pending_memory_calls_are_bounded(self):
        tracker = KimiMemoryWriteTracker()
        for index in range(80):
            tracker.note_call_started("tool-flood-%d" % index, "write_memory", {"content": "x"})
        self.assertLessEqual(len(tracker._memory_calls), 64)

    def test_empty_tool_call_id_is_ignored(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("", "write_memory", {"content": "x"})
        tracker.note_call_started(None, "write_memory", {"content": "x"})
        self.assertEqual({}, tracker._memory_calls)

    def test_bash_curl_post_memory_api_projects_card_event(self):
        tracker = KimiMemoryWriteTracker()
        command = (
            "curl -sS -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-H 'Authorization: Bearer ***' "
            "-d '{\"content\": \"Web 路径 Bash 直连\", \"category\": \"daily\"}'"
        )
        tracker.note_call_started("tool_bash1", "Bash", {"command": command})
        event = tracker.note_call_result(
            "tool_bash1",
            '{"id": "cafe0123", "content": "Web 路径 Bash 直连", '
            '"createdAt": "2026-09-12T09:00:00Z"}',
        )
        self.assertIsNotNone(event)
        self.assertEqual("write", event["action"])
        self.assertEqual("cafe0123", event["memory_id"])
        self.assertEqual("Web 路径 Bash 直连", event["title"])
        # 命令行里的 Authorization 头绝不进入事件。
        self.assertNotIn("Bearer", repr(event))

    def test_bash_error_result_never_projects(self):
        tracker = KimiMemoryWriteTracker()
        command = (
            "curl -sS -X POST https://memory.xiaonancaleb.xyz/api/memories "
            "-d '{\"content\": \"x\"}'"
        )
        tracker.note_call_started("tool_bash2", "Bash", {"command": command})
        self.assertIsNone(tracker.note_call_result(
            "tool_bash2", "HTTP 422\n{\"error\": \"bad\"}", is_error=True
        ))

    def test_bash_non_memory_command_never_projects(self):
        tracker = KimiMemoryWriteTracker()
        tracker.note_call_started("tool_bash3", "Bash", {"command": "ls /tmp"})
        self.assertIsNone(tracker.note_call_result("tool_bash3", "a\nb"))


if __name__ == "__main__":
    unittest.main()
