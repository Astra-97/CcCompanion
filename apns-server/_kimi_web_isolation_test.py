import inspect
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from kimi_web_client import (
    KimiWebClient,
    KimiWebError,
    KimiWebRecoveryConflict,
    KimiWebRequestRejected,
    KimiWebSessionBusy,
    KimiWebTransportUncertain,
)
from push import PushHandler, ServerState


class FakeKimiWeb:
    def __init__(self, *, start_error=None, status=None, quota=None):
        self.start_error = start_error
        self.status = status or {}
        self.quota = quota or {}
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def get_session_status(self, session_id):
        self.calls.append(("status", session_id))
        return self.status

    def get_quota(self):
        self.calls.append("quota")
        return self.quota


class KimiWebIsolationTest(unittest.TestCase):
    def handler(self, web):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            kimi_web=web,
            kimi_acp=types.SimpleNamespace(load_session_id=lambda: ""),
            kimi_active_turn={},
            kimi_auto_forge_context_threshold=0.75,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    def test_server_state_initialization_does_not_start_kimi_web(self):
        source = inspect.getsource(ServerState.__init__)
        self.assertNotIn("self.kimi_web.start()", source)

    def test_context_query_starts_kimi_lazily(self):
        web = FakeKimiWeb(status={"context_tokens": 25, "max_context_tokens": 100})
        handler = self.handler(web)

        self.assertEqual(0.25, handler._kimi_context_usage("session-1"))
        self.assertEqual(["start", ("status", "session-1")], web.calls)

    def test_context_query_failure_degrades_only_kimi(self):
        web = FakeKimiWeb(start_error=KimiWebError("unavailable"))
        handler = self.handler(web)

        self.assertEqual(0.0, handler._kimi_context_usage("session-1"))
        self.assertEqual(["start"], web.calls)

    def test_quota_query_starts_kimi_lazily_and_falls_back(self):
        available = FakeKimiWeb(quota={"remaining": 3})
        handler = self.handler(available)
        self.assertEqual({"remaining": 3}, handler._kimi_quota_snapshot())
        self.assertEqual(["start", "quota"], available.calls)

        unavailable = FakeKimiWeb(start_error=KimiWebError("unavailable"))
        handler = self.handler(unavailable)
        self.assertEqual({}, handler._kimi_quota_snapshot())
        self.assertEqual(["start"], unavailable.calls)

    def test_status_start_failure_is_confined_to_kimi_endpoint(self):
        web = FakeKimiWeb(start_error=KimiWebError("unavailable"))
        handler = self.handler(web)

        handler._handle_kimi_status()

        self.assertEqual(["start"], web.calls)
        self.assertEqual(
            [(503, {"ok": False, "error": "kimi_web_unavailable"})],
            handler.responses,
        )

    def test_status_request_timeout_is_confined_to_kimi_endpoint(self):
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client.start = Mock()
        client._read_token = lambda: "test-token"
        handler = self.handler(client)

        with patch("kimi_web_client.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            handler._handle_kimi_status()

        # The handler ensures availability before building the response, and
        # the quota helper independently preserves its lazy-start contract.
        self.assertEqual(2, client.start.call_count)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual(
            {
                "text": "配额信息暂不可用",
                "windows": [],
                "billing": {
                    "membership": {"available": False, "tier": "", "level": None},
                    "extra_usage": {"available": False},
                },
            },
            handler.responses[-1][1]["quota"],
        )

    def test_status_exposes_only_bounded_quota_dto(self):
        web = FakeKimiWeb(quota={"private": {"account": "never expose"}, "remaining": 3})
        handler = self.handler(web)

        handler._handle_kimi_status()

        payload = handler.responses[-1][1]
        self.assertIn("quota", payload)
        self.assertNotIn("quota_raw", payload)
        self.assertNotIn("quota_windows", payload)
        self.assertNotIn("private", str(payload["quota"]))

    def test_status_exposes_compact_provider_free_header_display(self):
        web = FakeKimiWeb(status={
            "model": "kimi-code/k3-256k",
            "thinking_level": "high",
            "context_tokens": 12,
            "max_context_tokens": 100,
            "context_usage": 0.12,
        })
        handler = self.handler(web)
        web.load_active_session_id = lambda: "session-1"

        handler._handle_kimi_status()

        payload = handler.responses[-1][1]
        self.assertEqual("K3-256k", payload["model"])
        self.assertEqual("K3-256k · 12%", payload["header_display"]["text"])
        self.assertEqual(1, payload["header_display"]["version"])
        self.assertNotIn("Kimi Code", payload["header_display"]["text"])

    def test_real_client_normalizes_network_os_errors_only(self):
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client._read_token = lambda: "test-token"

        for failure in (TimeoutError("timed out"), OSError("connection reset")):
            with self.subTest(failure=type(failure).__name__), patch(
                "kimi_web_client.urllib.request.urlopen", side_effect=failure
            ):
                with self.assertRaises(KimiWebTransportUncertain):
                    client.get_quota()

        with patch(
            "kimi_web_client.urllib.request.urlopen",
            side_effect=RuntimeError("programming bug"),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming bug"):
                client.get_quota()

    def test_web_pointer_is_private_atomic_and_never_reuses_acp_state(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer = os.path.join(directory, "kimi_web_session.json")
            client = KimiWebClient(command="/unused/kimi", state_path=pointer, cwd="/tmp/kimi-cwd")
            client.save_active_session_id("web_session-1")

            self.assertEqual("web_session-1", client.load_active_session_id())
            self.assertTrue(client.compare_and_swap_active_session_id("web_session-1", "web_session-2"))
            self.assertFalse(client.compare_and_swap_active_session_id("web_session-1", "web_session-3"))
            self.assertEqual("web_session-2", client.load_active_session_id())
            self.assertEqual(0o600, os.stat(pointer).st_mode & 0o777)
            with open(pointer, encoding="utf-8") as saved:
                payload = json.load(saved)
            self.assertEqual(
                {"version": 1, "session_id": "web_session-2", "cwd": "/tmp/kimi-cwd"},
                payload,
            )

    def test_stuck_busy_without_any_prompt_creates_a_new_pointer_without_touching_old_session(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer = os.path.join(directory, "kimi_web_session.json")
            client = KimiWebClient(command="/unused/kimi", state_path=pointer, cwd="/tmp/kimi-cwd")
            client.save_active_session_id("session-old")
            calls = []

            def fake_request(method, path, *, data=None, timeout=10.0):
                calls.append((method, path, data))
                if path.endswith("/status"):
                    return {"busy": path.endswith("session-old/status")}
                if path.endswith("/snapshot"):
                    return {
                        "in_flight_turn": None,
                        "pending_approvals": [],
                        "pending_questions": [],
                    }
                if path.endswith("/prompts"):
                    return {"active": None, "queued": []}
                if method == "POST" and path == "/api/v1/sessions":
                    return {"id": "session-new"}
                if path.endswith("/sessions/session-new"):
                    return {"metadata": {"cwd": "/tmp/kimi-cwd"}}
                raise AssertionError((method, path, data))

            client._request = fake_request
            self.assertEqual(
                "session-new",
                client.replace_stuck_busy_session(
                    "session-old", model="kimi-code/k3", thinking="low",
                ),
            )
            self.assertEqual("session-new", client.load_active_session_id())
            self.assertEqual(1, sum(path == "/api/v1/sessions" for _method, path, _data in calls))
            self.assertFalse(any(":abort" in path for _method, path, _data in calls))

    def test_stuck_busy_replacement_fails_closed_for_prompt_queue_or_matching_lease(self):
        for case, snapshot, prompts, lease in (
            ("active", {"in_flight_turn": None, "pending_approvals": [], "pending_questions": [], "current_prompt_id": "prompt-live"}, {"active": None, "queued": []}, {}),
            ("queued", {"in_flight_turn": None, "pending_approvals": [], "pending_questions": []}, {"active": None, "queued": [{"prompt_id": "prompt-queued"}]}, {}),
            ("missing_snapshot_empty_state", {}, {"active": None, "queued": []}, {}),
            ("missing_active", {"in_flight_turn": None, "pending_approvals": [], "pending_questions": []}, {"queued": []}, {}),
            ("missing_queued", {"in_flight_turn": None, "pending_approvals": [], "pending_questions": []}, {"active": None}, {}),
            ("foreign_lease", {"in_flight_turn": None, "pending_approvals": [], "pending_questions": []}, {"active": None, "queued": []}, {
                "session_id": "session-other", "prompt_id": "prompt-owned", "user_ts": "old", "state": "submitted", "created_at": "1",
            }),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                pointer = os.path.join(directory, "kimi_web_session.json")
                client = KimiWebClient(command="/unused/kimi", state_path=pointer, cwd="/tmp/kimi-cwd")
                client.save_active_session_id("session-old")
                calls = []

                def fake_request(method, path, *, data=None, timeout=10.0):
                    calls.append((method, path, data))
                    if path.endswith("/status"):
                        return {"busy": True}
                    if path.endswith("/snapshot"):
                        return snapshot
                    if path.endswith("/prompts"):
                        return prompts
                    raise AssertionError((method, path, data))

                client._request = fake_request
                client.load_turn_lease = lambda: lease
                with self.assertRaises(KimiWebRecoveryConflict):
                    client.replace_stuck_busy_session("session-old")
                self.assertEqual("session-old", client.load_active_session_id())
                self.assertFalse(any(path == "/api/v1/sessions" for _method, path, _data in calls))

    def test_stuck_busy_replacement_propagates_transport_uncertainty_without_moving_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer = os.path.join(directory, "kimi_web_session.json")
            client = KimiWebClient(command="/unused/kimi", state_path=pointer, cwd="/tmp/kimi-cwd")
            client.save_active_session_id("session-old")
            calls = []

            def fake_request(method, path, *, data=None, timeout=10.0):
                calls.append((method, path, data))
                raise KimiWebTransportUncertain("status response uncertain")

            client._request = fake_request
            with self.assertRaises(KimiWebTransportUncertain):
                client.replace_stuck_busy_session("session-old")
            self.assertEqual("session-old", client.load_active_session_id())
            self.assertFalse(any(path == "/api/v1/sessions" for _method, path, _data in calls))

    def test_stuck_busy_replacement_rejects_unverified_new_session_before_cas(self):
        for case, created_id, session_data, new_status in (
            ("same_id", "session-old", {"metadata": {"cwd": "/tmp/kimi-cwd"}}, {"busy": False}),
            ("wrong_workspace", "session-new", {"metadata": {"cwd": "/tmp/foreign"}}, {"busy": False}),
            ("new_busy", "session-new", {"metadata": {"cwd": "/tmp/kimi-cwd"}}, {"busy": True}),
            ("new_unknown_busy", "session-new", {"metadata": {"cwd": "/tmp/kimi-cwd"}}, {}),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                pointer = os.path.join(directory, "kimi_web_session.json")
                client = KimiWebClient(command="/unused/kimi", state_path=pointer, cwd="/tmp/kimi-cwd")
                client.save_active_session_id("session-old")
                calls = []

                def fake_request(method, path, *, data=None, timeout=10.0):
                    calls.append((method, path, data))
                    if path.endswith("/status"):
                        return {"busy": True} if path.endswith("session-old/status") else new_status
                    if path.endswith("/snapshot"):
                        return {
                            "in_flight_turn": None,
                            "pending_approvals": [],
                            "pending_questions": [],
                        }
                    if path.endswith("/prompts"):
                        return {"active": None, "queued": []}
                    if method == "POST" and path == "/api/v1/sessions":
                        return {"id": created_id}
                    if path.endswith(f"/sessions/{created_id}"):
                        return session_data
                    raise AssertionError((method, path, data))

                client._request = fake_request
                with self.assertRaises(KimiWebError):
                    client.replace_stuck_busy_session("session-old")
                self.assertEqual("session-old", client.load_active_session_id())
                self.assertEqual(1, sum(path == "/api/v1/sessions" for _method, path, _data in calls))

    def test_turn_lease_is_private_atomic_and_clears_only_the_exact_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            pointer = os.path.join(directory, "kimi_web_session.json")
            client = KimiWebClient(command="/unused/kimi", state_path=pointer, cwd="/tmp/kimi-cwd")
            client.save_turn_lease(
                session_id="web_session-1", prompt_id="prompt-1", user_ts="1700000000.1", state="submitted",
            )
            lease_path = os.path.join(directory, "kimi_web_turn_lease.json")
            self.assertEqual(0o600, os.stat(lease_path).st_mode & 0o777)
            self.assertEqual("prompt-1", client.load_turn_lease()["prompt_id"])
            with self.assertRaises(KimiWebRecoveryConflict):
                client.save_turn_lease(
                    session_id="web_session-1", prompt_id="prompt-2", user_ts="1700000000.2", state="submitted",
                )
            self.assertFalse(client.clear_turn_lease(session_id="web_session-1", prompt_id="other"))
            self.assertFalse(client.set_turn_lease_state(session_id="web_session-1", prompt_id="other", state="stopping"))
            self.assertTrue(client.claim_turn_lease(session_id="web_session-1", prompt_id="prompt-1", nonce="process-a"))
            self.assertFalse(client.claim_turn_lease(session_id="web_session-1", prompt_id="prompt-1", nonce="process-b", stale_after=60))
            self.assertTrue(client.release_turn_lease_claim(session_id="web_session-1", prompt_id="prompt-1", nonce="process-a"))
            self.assertEqual("submitted", client.load_turn_lease()["state"])
            client.set_turn_lease_state(session_id="web_session-1", prompt_id="prompt-1", state="stream_lost")
            self.assertEqual("stream_lost", client.load_turn_lease()["state"])
            self.assertTrue(client.clear_turn_lease(session_id="web_session-1", prompt_id="prompt-1"))
            self.assertEqual({}, client.load_turn_lease())

    def test_submit_resolves_exact_abort_prompt_id_from_active_prompt(self):
        client = KimiWebClient(command="/unused/kimi")
        calls = []

        def fake_request(method, path, *, data=None, timeout=10.0):
            calls.append((method, path, data))
            if method == "POST":
                return {"user_message_id": "user-1"}
            return {"active": {"prompt_id": "prompt-1", "user_message_id": "user-1"}}

        client._request = fake_request
        result = client.submit_prompt("session-1", "hello")

        self.assertEqual("prompt-1", result["prompt_id"])
        self.assertEqual(("GET", "/api/v1/sessions/session-1/prompts", None), calls[-1])

    def test_app_web_permission_mode_is_always_auto(self):
        with tempfile.TemporaryDirectory() as directory:
            client = KimiWebClient(
                command="/unused/kimi",
                state_path=os.path.join(directory, "kimi_web_session.json"),
            )
            calls = []

            def fake_request(method, path, *, data=None, timeout=10.0):
                calls.append((method, path, data))
                if path == "/api/v1/sessions":
                    return {"id": "session-1"}
                if path.endswith("/prompts") and method == "POST":
                    return {"prompt_id": "prompt-1"}
                raise AssertionError((method, path, data))

            client._request = fake_request
            client.create_session(permission_mode="unexpected")
            client.submit_prompt("session-1", "hello", permission_mode="unexpected")

            self.assertEqual(
                "auto",
                calls[0][2]["agent_config"]["permission_mode"],
            )
            self.assertEqual("auto", calls[1][2]["permission_mode"])

    def test_submit_uploads_image_and_file_through_kimi_web_content_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "photo.png")
            file_path = os.path.join(directory, "notes.txt")
            with open(image_path, "wb") as handle:
                handle.write(b"png-bytes")
            with open(file_path, "wb") as handle:
                handle.write(b"hello")
            client = KimiWebClient(command="/unused/kimi")
            uploads, calls = [], []

            def upload(path, **kwargs):
                uploads.append((path, kwargs))
                return {"id": f"file-{len(uploads)}"}

            def request(method, path, *, data=None, timeout=10.0):
                calls.append((method, path, data))
                return {"prompt_id": "prompt-1"}

            client._multipart_file_request = upload
            client._request = request
            result = client.submit_prompt("session-1", "look", attachments=[
                {"type": "image", "filename": "photo.png", "media_type": "image/png", "size": 9, "stored_path": image_path},
                {"type": "file", "filename": "notes.txt", "media_type": "text/plain", "size": 5, "stored_path": file_path},
            ])

            self.assertEqual("prompt-1", result["prompt_id"])
            self.assertEqual(["/api/v1/files", "/api/v1/files"], [item[0] for item in uploads])
            self.assertEqual(KimiWebClient.ATTACHMENT_TTL_SECONDS, uploads[0][1]["expires_in_sec"])
            content = calls[-1][2]["content"]
            self.assertEqual({"type": "text", "text": "look"}, content[0])
            self.assertEqual({"type": "image", "source": {"kind": "file", "file_id": "file-1"}}, content[1])
            self.assertEqual({
                "type": "file", "file_id": "file-2", "name": "notes.txt",
                "media_type": "text/plain", "size": 5,
            }, content[2])
            self.assertNotIn(directory, str(content))

    def test_partial_attachment_upload_rolls_back_and_never_submits_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name in ("one.txt", "two.txt"):
                path = os.path.join(directory, name)
                with open(path, "wb") as handle:
                    handle.write(b"x")
                paths.append(path)
            client = KimiWebClient(command="/unused/kimi")
            uploaded, deleted, submitted = [], [], []

            def upload(_path, **_kwargs):
                if uploaded:
                    raise KimiWebError("second upload failed")
                uploaded.append("file-1")
                return {"id": "file-1"}

            client._multipart_file_request = upload
            client.delete_file = lambda file_id: deleted.append(file_id)
            client._request = lambda *_args, **_kwargs: submitted.append(True) or {"prompt_id": "unexpected"}

            with self.assertRaisesRegex(KimiWebError, "second upload failed"):
                client.submit_prompt("session-1", "look", attachments=[
                    {"type": "file", "filename": "one.txt", "size": 1, "stored_path": paths[0]},
                    {"type": "file", "filename": "two.txt", "size": 1, "stored_path": paths[1]},
                ])
            self.assertEqual(["file-1"], deleted)
            self.assertEqual([], submitted)

    def test_rejected_prompt_deletes_uploaded_file_and_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "notes.txt")
            link = os.path.join(directory, "link.txt")
            with open(path, "wb") as handle:
                handle.write(b"hello")
            os.symlink(path, link)
            client = KimiWebClient(command="/unused/kimi")
            deleted = []
            client._multipart_file_request = lambda *_args, **_kwargs: {"id": "file-1"}
            client.delete_file = lambda file_id: deleted.append(file_id)
            client._request = Mock(side_effect=KimiWebRequestRejected("prompt rejected"))

            with self.assertRaisesRegex(KimiWebError, "prompt rejected"):
                client.submit_prompt("session-1", "look", attachments=[
                    {"type": "file", "filename": "notes.txt", "size": 5, "stored_path": path},
                ])
            self.assertEqual(["file-1"], deleted)

            client._request.reset_mock()
            with self.assertRaisesRegex(KimiWebError, "size or file type"):
                client.submit_prompt("session-1", "look", attachments=[
                    {"type": "file", "filename": "link.txt", "size": 5, "stored_path": link},
                ])
            client._request.assert_not_called()

    def test_uncertain_prompt_response_keeps_uploaded_file_for_upstream_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "notes.txt")
            with open(path, "wb") as handle:
                handle.write(b"hello")
            client = KimiWebClient(command="/unused/kimi")
            deleted = []
            client._multipart_file_request = lambda *_args, **_kwargs: {"id": "file-1"}
            client.delete_file = lambda file_id: deleted.append(file_id)
            client._request = Mock(side_effect=KimiWebTransportUncertain("response lost"))

            with self.assertRaisesRegex(KimiWebTransportUncertain, "response lost"):
                client.submit_prompt("session-1", "look", attachments=[
                    {"type": "file", "filename": "notes.txt", "size": 5, "stored_path": path},
                ])

            self.assertEqual([], deleted)
            self.assertEqual(1, client._request.call_count)

    def test_orphaned_pending_approval_is_aborted_by_exact_prompt_and_settles(self):
        client = KimiWebClient(command="/unused/kimi")
        calls, status_checks = [], 0

        def fake_request(method, path, *, data=None, timeout=10.0):
            nonlocal status_checks
            calls.append((method, path, data))
            if path.endswith("/status"):
                status_checks += 1
                return {"busy": status_checks == 1}
            if path.endswith("/snapshot"):
                return {
                    "current_prompt_id": "prompt-approval",
                    "pending_approvals": [{"approval_id": "approval-1"}],
                }
            if path.endswith("/prompts"):
                return {"active": {"prompt_id": "prompt-approval"}}
            if path.endswith(":abort"):
                return {}
            raise AssertionError(path)

        client._request = fake_request
        self.assertTrue(client.recover_owned_orphaned_prompt(
            {"session_id": "session-1", "prompt_id": "prompt-approval"}, settle_timeout=0,
        ))
        self.assertIn(
            ("POST", "/api/v1/sessions/session-1/prompts/prompt-approval:abort", {}), calls,
        )
        self.assertFalse(any("approvals" in path for _, path, _ in calls))

    def test_owned_recovery_accepts_real_in_flight_current_prompt_shape(self):
        client = KimiWebClient(command="/unused/kimi")
        calls, checks = [], 0

        def fake_request(method, path, *, data=None, timeout=10.0):
            nonlocal checks
            calls.append((method, path, data))
            if path.endswith("/status"):
                checks += 1
                return {"busy": checks == 1}
            if path.endswith("/snapshot"):
                return {"in_flight_turn": {"current_prompt_id": "prompt-1"}}
            if path.endswith("/prompts"):
                return {"active": {}}
            if path.endswith(":abort"):
                return {}
            raise AssertionError(path)

        client._request = fake_request
        self.assertTrue(client.recover_owned_orphaned_prompt(
            {"session_id": "session-1", "prompt_id": "prompt-1"}, settle_timeout=0,
        ))
        self.assertTrue(any(path.endswith("/prompts/prompt-1:abort") for _, path, _ in calls))

    def test_orphan_recovery_refuses_a_prompt_that_changes_during_fence_check(self):
        client = KimiWebClient(command="/unused/kimi")
        calls, snapshots = [], iter(("prompt-old", "prompt-new"))

        def fake_request(method, path, *, data=None, timeout=10.0):
            calls.append((method, path, data))
            if path.endswith("/status"):
                return {"busy": True}
            if path.endswith("/snapshot"):
                return {"current_prompt_id": next(snapshots)}
            if path.endswith("/prompts"):
                return {"active": {}}
            raise AssertionError(path)

        client._request = fake_request
        with self.assertRaises(KimiWebRecoveryConflict):
            client.recover_owned_orphaned_prompt({"session_id": "session-1", "prompt_id": "prompt-old"})
        self.assertFalse(any(path.endswith(":abort") for _, path, _ in calls))

    def test_orphan_recovery_propagates_abort_failure_when_still_busy(self):
        client = KimiWebClient(command="/unused/kimi")

        def fake_request(method, path, *, data=None, timeout=10.0):
            if path.endswith("/status"):
                return {"busy": True}
            if path.endswith("/snapshot"):
                return {"current_prompt_id": "prompt-1"}
            if path.endswith("/prompts"):
                return {"active": {"prompt_id": "prompt-1"}}
            if path.endswith(":abort"):
                raise KimiWebError("abort rejected")
            raise AssertionError(path)

        client._request = fake_request
        with self.assertRaises(KimiWebError):
            client.recover_owned_orphaned_prompt(
                {"session_id": "session-1", "prompt_id": "prompt-1"}, settle_timeout=0,
            )

    def test_web_send_route_fails_closed_without_adapter_instead_of_calling_acp(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(kimi_web=None)
        responses = []
        handler._send_json = lambda status, payload: responses.append((status, payload))
        handler._handle_kimi_acp_chat_send = Mock()

        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")

        self.assertEqual([(503, {"ok": False, "error": "kimi_web_unavailable"})], responses)
        handler._handle_kimi_acp_chat_send.assert_not_called()

    def test_stream_client_buffers_top_level_events_until_both_subscription_acks(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.step = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def send(self, raw):
                self.sent.append(json.loads(raw))

            def recv(self, *, timeout):
                self.assertGreaterEqual(len(self.sent), 2)
                hello, subscribe = self.sent[:2]
                frames = (
                    {"type": "assistant.delta", "session_id": "session-1", "payload": {"prompt_id": "p", "delta": "early"}},
                    {"type": "ack", "id": hello["id"], "code": 0},
                    {"type": "turn.ended", "session_id": "session-1", "payload": {"prompt_id": "p", "reason": "completed"}},
                    {"type": "ack", "id": subscribe["id"], "code": 0},
                )
                if self.step >= len(frames):
                    raise TimeoutError()
                frame = frames[self.step]
                self.step += 1
                return json.dumps(frame)

            def assertGreaterEqual(self, actual, expected):
                if actual < expected:
                    raise AssertionError(f"expected {actual} >= {expected}")

        socket = Socket()
        ready = []
        delivered = []
        stop = threading.Event()
        client = KimiWebClient(command="/unused/kimi")
        client._read_token = lambda: "test-token"

        def on_ready():
            # If either pre-ACK event were treated as readiness, this ordering
            # would contain an event before ready and prompt submit could race.
            ready.append("ready")

        def on_event(frame):
            delivered.append(frame["type"])
            if len(delivered) == 2:
                stop.set()

        package = types.ModuleType("websockets")
        sync = types.ModuleType("websockets.sync")
        client_module = types.ModuleType("websockets.sync.client")
        client_module.connect = lambda *_args, **_kwargs: socket
        with patch.dict(sys.modules, {
            "websockets": package,
            "websockets.sync": sync,
            "websockets.sync.client": client_module,
        }):
            client.stream_session("session-1", on_event=on_event, on_ready=on_ready, stop_event=stop)

        self.assertEqual(["ready"], ready)
        self.assertEqual(["assistant.delta", "turn.ended"], delivered)
        self.assertEqual(["client_hello", "subscribe"], [frame["type"] for frame in socket.sent[:2]])

    def test_real_client_start_failure_cleans_up_and_returns(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = 0
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client._try_reuse_existing_server = lambda: False
        client._wait_for_server = Mock(side_effect=KimiWebError("not ready"))
        outcome = []

        def run_start():
            try:
                client.start()
            except Exception as exc:
                outcome.append(exc)

        with patch("kimi_web_client.subprocess.Popen", return_value=process), patch(
            "kimi_web_client.os.killpg"
        ) as killpg:
            thread = threading.Thread(target=run_start)
            thread.start()
            thread.join(timeout=1)

        self.assertFalse(thread.is_alive(), "failed Kimi startup must not deadlock in close()")
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], KimiWebError)
        self.assertIsNone(client._process)
        killpg.assert_called_once_with(process.pid, 15)
        process.wait.assert_called_once_with(timeout=3)

    def test_real_client_concurrent_start_launches_one_process(self):
        process = Mock(pid=23456)
        process.poll.return_value = None
        process.wait.return_value = 0
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client._try_reuse_existing_server = lambda: False
        readiness_entered = threading.Event()
        release_readiness = threading.Event()
        errors = []

        def wait_for_server():
            readiness_entered.set()
            if not release_readiness.wait(timeout=1):
                raise AssertionError("test did not release readiness")

        def run_start():
            try:
                client.start()
            except Exception as exc:
                errors.append(exc)

        client._wait_for_server = wait_for_server
        with patch("kimi_web_client.subprocess.Popen", return_value=process) as popen:
            first = threading.Thread(target=run_start)
            second = threading.Thread(target=run_start)
            first.start()
            self.assertTrue(readiness_entered.wait(timeout=1))
            second.start()
            release_readiness.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, popen.call_count)

    def test_shutdown_kimi_cleanup_failure_does_not_block_shared_cleanup(self):
        calls = []
        state = object.__new__(ServerState)
        state.kairos_terminal = types.SimpleNamespace(release=lambda: calls.append("kairos"))

        def fail_kimi_close():
            calls.append("kimi")
            raise OSError("cleanup failed")

        state.kimi_web = types.SimpleNamespace(close=fail_kimi_close)
        state.codex_app_bridge = types.SimpleNamespace(close=lambda: calls.append("codex"))
        state.client = types.SimpleNamespace(close=lambda: calls.append("apns"))

        state.shutdown()

        self.assertEqual(["kairos", "kimi", "codex", "apns"], calls)


if __name__ == "__main__":
    unittest.main()
