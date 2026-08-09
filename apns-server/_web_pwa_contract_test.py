"""Regression tests for the Windows PWA server contract.

These tests deliberately exercise only the server boundary: browser sessions
are opaque cookies, no shared-secret handoff is required, and the UI sees one
contact contract for XiaoKe (Claude Code) and Kairos (Codex app-server).
"""

from __future__ import annotations

import io
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from push import (
    KAIROS_TERMINAL_ALIAS,
    PushHandler,
    StagedAttachmentStore,
    WEB_SESSION_CONTRACT_VERSION,
    WebSessionStore,
)


class WebPwaContractTest(unittest.TestCase):
    def handler(self, path: str = "/chat/contacts", method: str = "GET") -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = method
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 1)
        handler.responses = []
        handler._send_json = lambda status, payload, **kwargs: handler.responses.append(
            (status, payload, kwargs)
        )
        return handler

    def test_opaque_session_expires_and_revokes(self):
        store = WebSessionStore(300)
        token, _expires_at = store.create()
        self.assertTrue(store.valid(token))
        self.assertFalse(store.valid("not-a-session"))
        store.revoke(token)
        self.assertFalse(store.valid(token))

    def test_web_session_route_is_scoped_not_admin_or_token_access(self):
        handler = self.handler("/chat/send", "POST")
        self.assertTrue(handler._web_session_route_allowed())
        handler.path = "/memory/taxonomy"
        handler.command = "GET"
        self.assertTrue(handler._web_session_route_allowed())
        handler.path = "/admin/rotate-secret"
        self.assertFalse(handler._web_session_route_allowed())
        handler.path = "/tokens"
        self.assertFalse(handler._web_session_route_allowed())
        handler.path = "/push"
        handler.command = "POST"
        self.assertFalse(handler._web_session_route_allowed())

    def test_create_session_never_returns_shared_secret(self):
        handler = self.handler("/web/session", "POST")
        handler.state = types.SimpleNamespace(
            web_session_enabled=True,
            login_username="astra",
            login_password="correct horse battery staple",
            web_sessions=WebSessionStore(300),
            web_session_secure_cookie=True,
            shared_secret="must-not-leave-server",
        )
        PushHandler._login_fail_counts.clear()
        PushHandler._login_locked_ips.clear()
        handler._handle_web_session_create({
            "username": "astra",
            "password": "correct horse battery staple",
        })
        status, payload, kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["contract_version"], WEB_SESSION_CONTRACT_VERSION)
        self.assertTrue(payload["csrf_token"])
        self.assertNotIn("auth_token", payload)
        self.assertNotIn("must-not-leave-server", str(payload))
        cookie = kwargs["extra_headers"]["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        self.assertNotIn("must-not-leave-server", cookie)

    def test_web_cookie_only_auth_accepts_contact_query_paths_and_live_sse(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        handler = self.handler("/chat/status?contact_id=kairos")
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        handler.state = types.SimpleNamespace(web_session_enabled=True, web_sessions=store)
        self.assertTrue(handler._web_session_matches())
        handler.path = "/chat/stream?contact_id=xiaoke"
        self.assertTrue(handler._web_session_matches())
        handler.path = "/admin/rotate-secret"
        self.assertFalse(handler._web_session_matches())

    def test_web_cookie_allowlist_rejects_terminal_codex_admin_and_uploads(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        handler = self.handler("/tmux/capture?session=kairos")
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        handler.state = types.SimpleNamespace(web_session_enabled=True, web_sessions=store)
        self.assertFalse(handler._web_session_matches())
        for path in (
            "/tmux/sessions", "/uploads/private.bin", "/codex/status",
            "/admin/rotate-secret", "/tokens",
        ):
            handler.path = path
            handler.command = "GET"
            self.assertFalse(handler._web_session_matches(), path)
        for path in ("/terminal/key", "/tmux/send", "/codex/forge", "/codex/switch"):
            handler.path = path
            handler.command = "POST"
            self.assertFalse(handler._web_session_matches(), path)

    def test_cookie_write_needs_exact_origin_and_memory_only_csrf(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        csrf = store.csrf_token(token)
        handler = self.handler("/chat/send", "POST")
        handler.state = types.SimpleNamespace(
            web_session_enabled=True,
            web_sessions=store,
            public_server_url="https://desk.example",
            shared_secret="native-only",
            strict_auth=True,
        )
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        self.assertFalse(handler._web_session_write_matches())  # Origin absent
        handler.headers["Origin"] = "https://sibling.desk.example"
        handler.headers["X-CC-Web-CSRF"] = csrf
        self.assertFalse(handler._web_session_write_matches())
        handler.headers["Origin"] = "https://desk.example"
        handler.headers["X-CC-Web-CSRF"] = "wrong"
        self.assertFalse(handler._web_session_write_matches())
        handler.headers["X-CC-Web-CSRF"] = csrf
        self.assertTrue(handler._web_session_write_matches())
        self.assertTrue(handler._require_write_auth())

    def test_do_get_routes_status_query_to_contact_handler(self):
        handler = self.handler("/chat/status?contact_id=kairos")
        called = []
        handler._check_ip_allowed = lambda: True
        handler._require_auth = lambda: True
        handler._handle_chat_status = lambda: called.append("status")
        handler.do_GET()
        self.assertEqual(called, ["status"])

    def test_contact_manifest_exposes_both_primary_providers_with_common_endpoints(self):
        handler = self.handler()
        handler.state = types.SimpleNamespace(contact_chats={
            "xiaoke": object(), "kairos": object(), "kimi": object(), "toolbot": object(),
        })
        handler._handle_chat_contacts()
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["contract_version"], WEB_SESSION_CONTRACT_VERSION)
        contacts = {item["id"]: item for item in payload["contacts"]}
        self.assertEqual(contacts["xiaoke"]["provider"], "claude-code")
        self.assertEqual(contacts["kairos"]["provider"], "codex-app-server")
        self.assertEqual(contacts["kairos"]["terminal_target"], KAIROS_TERMINAL_ALIAS)
        self.assertEqual(contacts["xiaoke"]["stop"]["endpoint"], "/chat/stop")
        self.assertEqual(contacts["kairos"]["stop"]["endpoint"], "/chat/stop")
        self.assertEqual(payload["chat_endpoints"]["send"], "/chat/send")
        self.assertEqual(payload["chat_endpoints"]["status"], "/chat/status?contact_id={contact_id}")
        self.assertNotIn("typing", payload["chat_endpoints"])

    def test_generic_stop_routes_kairos_to_fenced_codex_abort(self):
        handler = self.handler("/chat/stop", "POST")
        captured = []
        handler._handle_codex_abort = lambda body: captured.append(body)
        handler._handle_chat_stop({"contact_id": "kairos", "user_ts": "turn-1"})
        self.assertEqual(captured, [{
            "contact_id": "kairos", "user_ts": "turn-1", "cancel_pending": True,
        }])

    def test_kairos_stop_requires_exact_turn_before_abort(self):
        handler = self.handler("/chat/stop", "POST")
        captured = []
        handler._handle_codex_abort = lambda body: captured.append(body)
        handler._handle_chat_stop({"contact_id": "kairos"})
        self.assertEqual(captured, [])
        self.assertEqual(handler.responses[-1][0], 400)

    def test_do_post_allows_fenced_kairos_stop_without_tmux_switch(self):
        handler = self.handler("/chat/stop", "POST")
        handler.state = types.SimpleNamespace(allow_remote_control=False)
        handler._check_ip_allowed = lambda: True
        handler._require_write_auth = lambda: True
        handler._read_body = lambda: {"contact_id": "kairos", "user_ts": "turn-1"}
        called = []
        handler._handle_chat_stop = lambda body: called.append(body)
        handler.do_POST()
        self.assertEqual(called, [{"contact_id": "kairos", "user_ts": "turn-1"}])

    def test_do_post_keeps_xiaoke_stop_behind_remote_control_gate(self):
        handler = self.handler("/chat/stop", "POST")
        handler.state = types.SimpleNamespace(allow_remote_control=False)
        handler._check_ip_allowed = lambda: True
        handler._require_write_auth = lambda: True
        handler._read_body = lambda: {"contact_id": "xiaoke", "user_ts": "turn-1", "session": "cc"}
        handler.do_POST()
        self.assertEqual(handler.responses[-1][0], 403)

    def test_upload_is_cookie_scope_and_dispatches_before_json_decode(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        handler = self.handler("/chat/upload?contact_id=kairos", "POST")
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        handler.state = types.SimpleNamespace(web_session_enabled=True, web_sessions=store)
        self.assertTrue(handler._web_session_route_allowed())
        handler._check_ip_allowed = lambda: True
        handler._require_write_auth = lambda: True
        called = []
        handler._handle_chat_upload = lambda: called.append("upload")
        handler.do_POST()
        self.assertEqual(called, ["upload"])

    def test_staged_attachment_is_owned_ttl_bound_and_single_consume(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = StagedAttachmentStore(root / "staging", ttl_seconds=60)
            staged = store.stage_stream(
                owner="session-a",
                contact_id="kairos",
                filename="reference.png",
                attachment_type="image",
                extension=".png",
                length=3,
                stream=io.BytesIO(b"png"),
            )
            self.assertNotIn("stored_path", staged)
            attachment_id = staged["attachment_id"]
            with self.assertRaisesRegex(ValueError, "owner_or_contact"):
                store.consume(
                    owner="session-b", contact_id="kairos",
                    attachment_ids=[attachment_id], destination=root,
                )
            consumed = store.consume(
                owner="session-a", contact_id="kairos",
                attachment_ids=[attachment_id], destination=root,
            )
            self.assertTrue(Path(consumed[0]["stored_path"]).is_file())
            self.assertTrue(consumed[0]["attachment_url"].startswith("/attachments/"))
            with self.assertRaisesRegex(ValueError, "missing_or_expired"):
                store.consume(
                    owner="session-a", contact_id="kairos",
                    attachment_ids=[attachment_id], destination=root,
                )

    def test_staged_attachment_expiry_and_logout_style_cancel_remove_bytes(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = StagedAttachmentStore(root / "staging", ttl_seconds=60)
            staged = store.stage_stream(
                owner="session-a", contact_id="xiaoke", filename="note.txt",
                attachment_type="file", extension=".txt", length=2, stream=io.BytesIO(b"ok"),
            )
            attachment_id = staged["attachment_id"]
            stage_path = store._items[attachment_id]["path"]
            self.assertEqual(store.cancel(owner="session-b", attachment_ids=[attachment_id]), 0)
            self.assertTrue(stage_path.exists())
            self.assertEqual(store.cancel(owner="session-a", attachment_ids=[attachment_id]), 1)
            self.assertFalse(stage_path.exists())
            staged = store.stage_stream(
                owner="session-a", contact_id="xiaoke", filename="old.txt",
                attachment_type="file", extension=".txt", length=2, stream=io.BytesIO(b"ok"),
            )
            store._items[staged["attachment_id"]]["created_at"] = 0
            with self.assertRaisesRegex(ValueError, "missing_or_expired"):
                store.consume(
                    owner="session-a", contact_id="xiaoke",
                    attachment_ids=[staged["attachment_id"]], destination=root,
                )

    def test_staged_limits_include_concurrent_stream_reservations(self):
        class BlockingStream:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.sent = False

            def read(self, _size):
                self.entered.set()
                self.release.wait(2)
                if self.sent:
                    return b""
                self.sent = True
                return b"abcd"

        with tempfile.TemporaryDirectory() as raw_tmp:
            store = StagedAttachmentStore(Path(raw_tmp) / "staging", max_pending_files=2, max_pending_bytes=6)
            stream = BlockingStream()
            result = []
            worker = threading.Thread(target=lambda: result.append(store.stage_stream(
                owner="session-a", contact_id="kairos", filename="a.txt", attachment_type="file",
                extension=".txt", length=4, stream=stream,
            )))
            worker.start()
            self.assertTrue(stream.entered.wait(1))
            with self.assertRaisesRegex(ValueError, "byte_limit"):
                store.stage_stream(
                    owner="session-a", contact_id="kairos", filename="b.txt", attachment_type="file",
                    extension=".txt", length=4, stream=io.BytesIO(b"bbbb"),
                )
            stream.release.set()
            worker.join(2)
            self.assertEqual(len(result), 1)
            with self.assertRaisesRegex(ValueError, "file_limit"):
                store = StagedAttachmentStore(Path(raw_tmp) / "other", max_pending_files=1, max_pending_bytes=10)
                store.stage_stream(owner="s", contact_id="kairos", filename="a.txt", attachment_type="file", extension=".txt", length=1, stream=io.BytesIO(b"a"))
                store.stage_stream(owner="s", contact_id="kairos", filename="b.txt", attachment_type="file", extension=".txt", length=1, stream=io.BytesIO(b"b"))

    def test_inflight_stage_cancel_logout_and_ttl_release_capacity_immediately(self):
        class BlockingStream:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def read(self, _size):
                self.entered.set()
                self.release.wait(2)
                return b"abcd"

        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for action in ("cancel", "logout", "ttl"):
                with self.subTest(action=action):
                    store = StagedAttachmentStore(root / action, max_pending_files=1, max_pending_bytes=4)
                    stream = BlockingStream()
                    errors = []

                    def stage() -> None:
                        try:
                            store.stage_stream(
                                owner="session-a", contact_id="kairos", filename="a.txt",
                                attachment_type="file", extension=".txt", length=4, stream=stream,
                            )
                        except Exception as exc:  # Expected after cancellation.
                            errors.append(exc)

                    worker = threading.Thread(target=stage)
                    worker.start()
                    self.assertTrue(stream.entered.wait(1))
                    self.assertEqual(len(store._reservations), 1)
                    self.assertTrue(list((root / action).glob("*.part")))
                    if action == "ttl":
                        next(iter(store._reservations.values()))["created_at"] = 0
                        store.cleanup_expired()
                    else:
                        # A logout has the same exact-owner cancellation path.
                        store.cancel(owner="session-a")
                    self.assertEqual(store._reservations, {})
                    self.assertEqual(store._items, {})
                    self.assertFalse(list((root / action).glob("*.part")))
                    # Capacity is free before the blocked request returns.
                    replacement = store.stage_stream(
                        owner="session-a", contact_id="kairos", filename="b.txt",
                        attachment_type="file", extension=".txt", length=1, stream=io.BytesIO(b"b"),
                    )
                    self.assertTrue(replacement["attachment_id"])
                    stream.release.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], ValueError)
                    self.assertEqual(len(store._items), 1)  # Only replacement; no resurrection.

    def test_staged_read_timeout_releases_reservation_and_pwa_socket_timeout_is_bounded(self):
        class TimeoutStream:
            def read(self, _size):
                raise TimeoutError("simulated socket timeout")

        class SocketProbe:
            def __init__(self):
                self.calls = []

            def gettimeout(self):
                return 17

            def settimeout(self, value):
                self.calls.append(value)

        with tempfile.TemporaryDirectory() as raw_tmp:
            store = StagedAttachmentStore(Path(raw_tmp) / "staging", read_timeout_seconds=9999)
            self.assertEqual(store.read_timeout_seconds, StagedAttachmentStore.MAX_READ_TIMEOUT_SECONDS)
            with self.assertRaisesRegex(TimeoutError, "simulated"):
                store.stage_stream(
                    owner="session-a", contact_id="kairos", filename="a.txt",
                    attachment_type="file", extension=".txt", length=1, stream=TimeoutStream(),
                )
            self.assertEqual(store._reservations, {})
            self.assertEqual(store._items, {})
            self.assertFalse(list((Path(raw_tmp) / "staging").glob("*.part")))
            handler = self.handler("/chat/upload", "POST")
            handler.state = types.SimpleNamespace(staged_attachments=store)
            handler.connection = SocketProbe()
            with handler._pwa_upload_read_timeout():
                pass
            self.assertEqual(handler.connection.calls, [300, 17])

    def test_staged_multi_consume_rolls_back_every_moved_destination(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            store = StagedAttachmentStore(root / "staging")
            first = store.stage_stream(owner="s", contact_id="kairos", filename="a.txt", attachment_type="file", extension=".txt", length=1, stream=io.BytesIO(b"a"))
            second = store.stage_stream(owner="s", contact_id="kairos", filename="b.txt", attachment_type="file", extension=".txt", length=1, stream=io.BytesIO(b"b"))
            original_replace = __import__("push").os.replace
            calls = []

            def fail_second(source, destination):
                calls.append((source, destination))
                if len(calls) == 2:
                    raise OSError("injected move failure")
                return original_replace(source, destination)

            with patch("push.os.replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "injected move failure"):
                    store.consume(owner="s", contact_id="kairos", attachment_ids=[first["attachment_id"], second["attachment_id"]], destination=root)
            self.assertFalse(list(root.glob("*.txt")))
            self.assertFalse(list((root / "staging").glob("*.part")))
            self.assertEqual(store._items, {})

    def test_staging_startup_sweeps_only_safe_orphan_parts(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "staging"
            root.mkdir()
            orphan = root / ("A" * 24 + ".png.part")
            orphan.write_bytes(b"orphan")
            unrelated = root / "keep-me.part"
            unrelated.write_bytes(b"keep")
            target = Path(raw_tmp) / "outside.txt"
            target.write_bytes(b"outside")
            link = root / ("B" * 24 + ".txt.part")
            link.symlink_to(target)
            StagedAttachmentStore(root)
            self.assertFalse(orphan.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(target.exists())
            self.assertTrue(link.is_symlink())

    def test_private_json_response_is_no_store_and_nosniff(self):
        handler = self.handler("/chat/status")
        handler._send_json = PushHandler._send_json.__get__(handler, PushHandler)
        headers = {}
        handler.send_response = lambda _status: None
        handler.send_header = lambda name, value: headers.__setitem__(name, value)
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler._send_json(200, {"ok": True})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Pragma"], "no-cache")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_cookie_can_read_only_safe_flat_attachment_get_or_head(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        handler = self.handler("/attachments/a1b2c3.png", "GET")
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        handler.state = types.SimpleNamespace(web_session_enabled=True, web_sessions=store)
        self.assertTrue(handler._web_session_matches())
        handler.command = "HEAD"
        self.assertTrue(handler._web_session_matches())
        handler.command = "POST"
        self.assertFalse(handler._web_session_matches())

    def test_cookie_attachment_rejects_query_encoding_staging_and_unsafe_names(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        handler = self.handler("/attachments/a.png?download=1", "GET")
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        handler.state = types.SimpleNamespace(web_session_enabled=True, web_sessions=store)
        for path in (
            "/attachments/a.png?download=1", "/attachments/a%2fb.png",
            "/attachments/%2e%2e%2fsecret", "/attachments/.pwa-staging",
            "/attachments/token.part", "/attachments/a/b.png",
        ):
            handler.path = path
            self.assertFalse(handler._web_session_matches(), path)
        handler.headers = {}
        handler.path = "/attachments/a1b2c3.png"
        self.assertFalse(handler._web_session_matches())

    def test_attachment_response_is_private_and_symlink_safe(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            safe = root / "a1b2c3.png"
            safe.write_bytes(b"png")
            outside = root.parent / "outside.png"
            outside.write_bytes(b"outside")
            symlink = root / "link.png"
            symlink.symlink_to(outside)
            handler = self.handler("/attachments/a1b2c3.png", "GET")
            handler.state = types.SimpleNamespace(attachments_dir=root)
            sent = {}
            handler.send_response = lambda status: sent.__setitem__("status", status)
            handler.send_header = lambda name, value: sent.__setitem__(name, value)
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler._handle_attachment_get()
            self.assertEqual(sent["status"], 200)
            self.assertEqual(sent["Cache-Control"], "private, no-store")
            self.assertEqual(sent["X-Content-Type-Options"], "nosniff")
            self.assertEqual(sent["Referrer-Policy"], "no-referrer")
            self.assertIn("inline", sent["Content-Disposition"])
            handler.path = "/attachments/link.png"
            handler.responses = []
            handler._send_json = lambda status, payload, **_kwargs: handler.responses.append((status, payload))
            handler._handle_attachment_get()
            self.assertEqual(handler.responses[-1][0], 400)

    def test_public_pwa_shell_is_static_only_and_rejects_traversal(self):
        handler = self.handler("/web/pwa/")
        self.assertTrue(handler._is_public_get())
        handler.send_response = lambda _status: None
        handler.send_header = lambda _name, _value: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "index.html").write_text("<main>PWA</main>", encoding="utf-8")
            with patch("push.WINDOWS_PWA_ROOT", root):
                handler._handle_windows_pwa_asset("/web/pwa/")
            self.assertEqual(handler.wfile.getvalue(), b"<main>PWA</main>")
        handler.responses.clear()
        handler._handle_windows_pwa_asset("/web/pwa/%2e%2e/secret.txt")
        self.assertEqual(handler.responses[-1][0], 404)


if __name__ == "__main__":
    unittest.main()
