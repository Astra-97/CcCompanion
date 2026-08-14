"""Regression tests for the Windows PWA server contract.

These tests deliberately exercise only the server boundary: browser sessions
are opaque cookies, no shared-secret handoff is required, and the UI sees one
contact contract for XiaoKe (Claude Code) and Kairos (Codex app-server).
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import types
import unittest
from http.client import parse_headers
from pathlib import Path
from unittest.mock import patch

from push import (
    KAIROS_TERMINAL_ALIAS,
    PushHandler,
    StagedAttachmentStore,
    WebPairingStore,
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

    def test_native_pairing_create_requires_shared_secret_not_web_cookie(self):
        pairings = WebPairingStore()
        handler = self.handler("/web/pairing/create", "POST")
        handler.state = types.SimpleNamespace(
            web_session_enabled=True,
            web_pairings=pairings,
            web_sessions=WebSessionStore(300),
            shared_secret="native-only",
        )
        handler._check_ip_allowed = lambda: True
        handler.headers = {
            "X-Auth-Token": "native-only",
            "Content-Type": "application/json",
            "Content-Length": "26",
        }
        handler.rfile = io.BytesIO(b'{"display_name":"ignored"}')
        handler.do_POST()
        status, payload, kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        code = payload["pairing_code"]
        self.assertEqual(len(code), WebPairingStore.CODE_LENGTH)
        self.assertTrue(set(code).issubset(set(WebPairingStore.CODE_ALPHABET)))
        self.assertEqual(payload["expires_in_seconds"], 300)
        self.assertEqual(payload["display_name"], "Astra")
        self.assertEqual(kwargs["extra_headers"]["Cache-Control"], "no-store")
        self.assertNotIn(code, repr(pairings._codes))

        cookie_only = self.handler("/web/pairing/create", "POST")
        cookie_only.state = handler.state
        cookie_only._check_ip_allowed = lambda: True
        web_token, _ = handler.state.web_sessions.create()
        cookie_only.headers = {
            "Cookie": f"__Host-cccompanion={web_token}",
            "Content-Type": "application/json",
            "Content-Length": "2",
        }
        cookie_only.rfile = io.BytesIO(b"{}")
        cookie_only.do_POST()
        self.assertEqual(cookie_only.responses[-1][0], 401)
        self.assertEqual(len(pairings._codes), 1)

    def test_pairing_request_body_is_bounded_drained_and_fail_closed(self):
        pairings = WebPairingStore()
        handler = self.handler("/web/pairing/create", "POST")
        handler.state = types.SimpleNamespace(
            web_session_enabled=True,
            web_pairings=pairings,
            shared_secret="native-only",
        )
        handler._check_ip_allowed = lambda: True
        handler.headers = {
            "X-Auth-Token": "native-only",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": "2",
        }
        handler.rfile = io.BytesIO(b"{}")
        handler.do_POST()
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(handler.rfile.read(), b"")

        no_secret = self.handler("/web/pairing/create", "POST")
        no_secret.state = types.SimpleNamespace(
            web_session_enabled=True,
            web_pairings=WebPairingStore(),
            shared_secret="",
        )
        no_secret._check_ip_allowed = lambda: True
        no_secret.headers = {
            "X-Auth-Token": "anything",
            "Content-Type": "application/json",
            "Content-Length": "2",
        }
        no_secret.rfile = io.BytesIO(b"{}")
        no_secret.do_POST()
        self.assertEqual(no_secret.responses[-1][0], 401)

        invalid = self.handler("/web/session/pair", "POST")
        invalid.state = types.SimpleNamespace(allowed_ips=[])
        invalid.headers = {"Content-Type": "application/json"}  # Missing length.
        invalid.rfile = io.BytesIO(b'{"code":"ABCDEFGH"}')
        invalid.do_POST()
        self.assertEqual(invalid.responses[-1][0], 400)
        self.assertTrue(invalid.close_connection)

        oversized = self.handler("/web/session/pair", "POST")
        oversized.state = types.SimpleNamespace(allowed_ips=[])
        oversized.headers = {
            "Content-Type": "application/json",
            "Content-Length": "1025",
        }
        oversized.rfile = io.BytesIO(b"")
        oversized.do_POST()
        self.assertEqual(oversized.responses[-1][0], 400)
        self.assertTrue(oversized.close_connection)

        pairing_code, _ = pairings.create()
        malformed_code_body = b'{"code":"O0I1O0I1"}'
        malformed_code = self.handler("/web/session/pair", "POST")
        malformed_code.state = types.SimpleNamespace(
            allowed_ips=[],
            web_session_enabled=True,
            web_pairings=pairings,
            web_sessions=WebSessionStore(300),
            public_server_url="https://desk.example",
        )
        malformed_code.headers = {
            "Origin": "https://desk.example",
            "Content-Type": "application/json",
            "Content-Length": str(len(malformed_code_body)),
        }
        malformed_code.rfile = io.BytesIO(malformed_code_body)
        malformed_code.do_POST()
        self.assertEqual(malformed_code.responses[-1][0:2], (401, {
            "ok": False, "error": "invalid_pairing_code",
        }))
        self.assertIn(pairings._digest(pairing_code), pairings._codes)

    def test_pairing_reader_uses_short_socket_timeout(self):
        class SocketProbe:
            def __init__(self):
                self.calls = []

            def gettimeout(self):
                return 17

            def settimeout(self, value):
                self.calls.append(value)

        handler = self.handler("/web/session/pair", "POST")
        handler.headers = {"Content-Type": "application/json", "Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        handler.connection = SocketProbe()
        self.assertEqual(handler._read_pairing_json_object(), {})
        self.assertEqual(handler.connection.calls, [5, 17])

    def test_pairing_requires_exact_origin_and_issues_same_web_session_response(self):
        pairings = WebPairingStore()
        code, _ = pairings.create()
        state = types.SimpleNamespace(
            web_session_enabled=True,
            web_pairings=pairings,
            web_sessions=WebSessionStore(300),
            public_server_url="https://desk.example",
        )
        handler = self.handler("/web/session/pair", "POST")
        handler.state = state
        handler.headers = {"Origin": "https://other.example"}
        handler._handle_web_session_pair({"code": code})
        self.assertEqual(handler.responses[-1][0], 403)

        handler.headers = {"Origin": "https://desk.example"}
        handler._handle_web_session_pair({"code": code})
        status, payload, kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        self.assertTrue(payload["csrf_token"])
        self.assertIn("upload_limits", payload)
        self.assertEqual(kwargs["extra_headers"]["Cache-Control"], "no-store")
        cookie = kwargs["extra_headers"]["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)

    def test_pairing_is_one_time_expires_and_fails_indistinguishably(self):
        pairings = WebPairingStore()
        code, _ = pairings.create()
        self.assertTrue(pairings.consume(code, client_ip="10.0.0.1"))
        self.assertFalse(pairings.consume(code, client_ip="10.0.0.2"))
        self.assertFalse(pairings.consume("NOTACODE", client_ip="10.0.0.3"))
        expiring_code, _ = pairings.create()
        pairings._codes[pairings._digest(expiring_code)] = 0
        self.assertFalse(pairings.consume(expiring_code, client_ip="10.0.0.4"))
        capped = WebPairingStore()
        capped.MAX_PENDING = 1
        capped.create()
        with self.assertRaisesRegex(RuntimeError, "pairing_capacity"):
            capped.create()

        state = types.SimpleNamespace(
            web_session_enabled=True,
            web_pairings=pairings,
            web_sessions=WebSessionStore(300),
            public_server_url="https://desk.example",
        )
        handler = self.handler("/web/session/pair", "POST")
        handler.state = state
        handler.headers = {"Origin": "https://desk.example"}
        handler._handle_web_session_pair({"code": "NOTACODE"})
        missing_response = handler.responses[-1]
        handler._handle_web_session_pair({"code": expiring_code})
        expired_response = handler.responses[-1]
        self.assertEqual(missing_response[0:2], expired_response[0:2])
        self.assertEqual(missing_response[2]["extra_headers"]["Cache-Control"], "no-store")

    def test_pairing_consume_is_atomic_and_failure_lock_recovers(self):
        pairings = WebPairingStore()
        code, _ = pairings.create()
        start = threading.Barrier(2)
        results = []

        def consume(index: int) -> None:
            start.wait()
            results.append(pairings.consume(code, client_ip=f"10.0.1.{index}"))

        first = threading.Thread(target=consume, args=(1,))
        second = threading.Thread(target=consume, args=(2,))
        first.start()
        second.start()
        first.join(2)
        second.join(2)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)

        recovery_code, _ = pairings.create()
        client_ip = "10.0.2.1"
        with patch("push.time.time", return_value=1_000):
            for _ in range(WebPairingStore.MAX_FAILURES):
                self.assertFalse(pairings.consume("WRONGCODE", client_ip=client_ip))
            self.assertFalse(pairings.consume(recovery_code, client_ip=client_ip))
        with patch("push.time.time", return_value=1_061):
            self.assertTrue(pairings.consume(recovery_code, client_ip=client_ip))

    def test_pairing_rate_key_trusts_cloudflare_then_nginx_only_from_loopback(self):
        handler = self.handler("/web/session/pair", "POST")
        handler.headers = {"X-Real-IP": "203.0.113.10"}
        self.assertEqual(handler._trusted_client_ip(), "203.0.113.10")
        handler.headers = {
            "CF-Connecting-IP": "203.0.113.11",
            "X-Real-IP": "203.0.113.10",
        }
        self.assertEqual(handler._trusted_client_ip(), "203.0.113.11")
        handler.headers = {
            "CF-Connecting-IP": "203.0.113.11, 198.51.100.1",
            "X-Real-IP": "203.0.113.10",
        }
        self.assertEqual(handler._trusted_client_ip(), "127.0.0.1")
        handler.headers = {
            "CF-Connecting-IP": "not-an-ip",
            "X-Real-IP": "203.0.113.10",
        }
        self.assertEqual(handler._trusted_client_ip(), "127.0.0.1")
        handler.client_address = ("198.51.100.9", 1)
        handler.headers = {
            "CF-Connecting-IP": "203.0.113.11",
            "X-Real-IP": "203.0.113.10",
        }
        self.assertEqual(handler._trusted_client_ip(), "198.51.100.9")

        pairings = WebPairingStore()
        state = types.SimpleNamespace(
            web_session_enabled=True,
            web_pairings=pairings,
            web_sessions=WebSessionStore(300),
            public_server_url="https://desk.example",
        )
        first = self.handler("/web/session/pair", "POST")
        first.state = state
        first.headers = {"Origin": "https://desk.example", "CF-Connecting-IP": "203.0.113.10"}
        first._handle_web_session_pair({"code": "WRONGCODE"})
        second = self.handler("/web/session/pair", "POST")
        second.state = state
        second.headers = {"Origin": "https://desk.example", "CF-Connecting-IP": "203.0.113.11"}
        second._handle_web_session_pair({"code": "WRONGCODE"})
        self.assertEqual(set(pairings._failures), {"203.0.113.10", "203.0.113.11"})

    def test_cloudflare_present_empty_or_duplicate_fails_safe_without_xreal_fallback(self):
        handler = self.handler("/web/session/pair", "POST")
        handler.headers = parse_headers(io.BytesIO(
            b"CF-Connecting-IP:\r\nX-Real-IP: 203.0.113.10\r\n\r\n"
        ))
        self.assertEqual(handler.headers.get_all("CF-Connecting-IP"), [""])
        self.assertEqual(handler._trusted_client_ip(), "127.0.0.1")
        handler.headers = parse_headers(io.BytesIO(
            b"CF-Connecting-IP: 203.0.113.10\r\n"
            b"CF-Connecting-IP: 203.0.113.11\r\n"
            b"X-Real-IP: 203.0.113.12\r\n\r\n"
        ))
        self.assertEqual(handler.headers.get_all("CF-Connecting-IP"), [
            "203.0.113.10", "203.0.113.11",
        ])
        self.assertEqual(handler._trusted_client_ip(), "127.0.0.1")

    def test_legacy_login_rate_key_uses_trusted_client_ip(self):
        PushHandler._login_fail_counts.clear()
        PushHandler._login_locked_ips.clear()
        self.addCleanup(PushHandler._login_fail_counts.clear)
        self.addCleanup(PushHandler._login_locked_ips.clear)
        state = types.SimpleNamespace(login_username="astra", login_password="correct")

        first = self.handler("/login", "POST")
        first.state = state
        first.headers = {"CF-Connecting-IP": "203.0.113.20"}
        first._handle_login({"username": "astra", "password": "wrong"})
        second = self.handler("/login", "POST")
        second.state = state
        second.headers = {"CF-Connecting-IP": "203.0.113.21"}
        second._handle_login({"username": "astra", "password": "wrong"})
        self.assertEqual(set(PushHandler._login_fail_counts), {"203.0.113.20", "203.0.113.21"})

        invalid_cf = self.handler("/login", "POST")
        invalid_cf.state = state
        invalid_cf.headers = {
            "CF-Connecting-IP": "not-an-ip",
            "X-Real-IP": "203.0.113.22",
        }
        invalid_cf._handle_login({"username": "astra", "password": "wrong"})
        self.assertIn("127.0.0.1", PushHandler._login_fail_counts)

        spoofed_direct = self.handler("/login", "POST")
        spoofed_direct.state = state
        spoofed_direct.client_address = ("198.51.100.23", 1)
        spoofed_direct.headers = {
            "CF-Connecting-IP": "203.0.113.24",
            "X-Real-IP": "203.0.113.25",
        }
        spoofed_direct._handle_login({"username": "astra", "password": "wrong"})
        self.assertIn("198.51.100.23", PushHandler._login_fail_counts)

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

    def test_kairos_chat_status_has_only_bounded_read_only_panels_when_idle_or_busy(self):
        handler = self.handler("/chat/status?contact_id=kairos")
        handler._contact_id_from_query = lambda: "kairos"
        handler._typing_for_contact = lambda _contact: {"is_typing": False, "since": None}
        handler._chat_draft_snapshot = lambda _contact: {"is_active": False, "reply_state": "idle", "text": ""}
        handler._contact_stop_request = lambda *_args, **_kwargs: {"supported": False, "body": None}
        handler._codex_busy_snapshot = lambda **_kwargs: {"busy": False}
        handler._pwa_kairos_instrument_snapshot = lambda: {"available": True, "model": "gpt-5.5", "effort": "high", "context": {"available": False, "used_percent": None, "used_tokens": None, "window_tokens": None}, "quota": {"plan": "", "windows": []}}
        handler._pwa_kairos_terminal_snapshot = lambda: {"available": True, "busy": False, "phase": "idle", "events": []}
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(tail=lambda _limit: [])
        handler._handle_chat_status()
        idle = handler.responses[-1][1]
        self.assertFalse(idle["busy"])
        self.assertEqual(idle["terminal"], {"available": True, "busy": False, "phase": "idle", "events": []})
        self.assertEqual(idle["instrument"]["model"], "gpt-5.5")

        handler._codex_busy_snapshot = lambda **_kwargs: {"busy": True}
        handler._handle_chat_status()
        busy = handler.responses[-1][1]
        self.assertTrue(busy["busy"])
        self.assertIn("instrument", busy)
        self.assertIn("terminal", busy)

        handler._codex_busy_snapshot = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("probe failed"))
        handler._pwa_kairos_instrument_snapshot = lambda: {"available": False, "model": "", "effort": "", "context": {"available": False, "used_percent": None, "used_tokens": None, "window_tokens": None}, "quota": {"plan": "", "windows": []}}
        handler._pwa_kairos_terminal_snapshot = lambda: {"available": False, "busy": False, "phase": "unavailable", "events": []}
        handler._handle_chat_status()
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertFalse(handler.responses[-1][1]["busy"])

    def test_pwa_instrument_contract_bounds_and_redacts_runtime_details(self):
        handler = self.handler()
        handler.state = types.SimpleNamespace()
        handler._codex_preference_snapshot = lambda: ("gpt-5.5", "high")
        handler._load_codex_target = lambda: (None, Path("/not-returned"))
        handler._codex_context_snapshot = lambda _meta: {
            "available": True, "input_tokens": 1200, "window_tokens": 10000,
            "context_text": "SECRET runtime line", "last_turn_text": "/private/path",
        }
        handler._codex_quota_lines_cached = lambda: [
            "额度: Plus / private@example.test",
            "5h: 剩余 42%（2 小时后）",
            "weekly: 剩余 88%（周一）",
            "third: 剩余 1%（never）",
        ]
        payload = handler._pwa_kairos_instrument_snapshot()
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["context"], {"available": True, "used_percent": 12.0, "used_tokens": 1200, "window_tokens": 10000})
        self.assertEqual(len(payload["quota"]["windows"]), 2)
        encoded = str(payload)
        for forbidden in ("private@example.test", "SECRET runtime", "/private/path", "session", "cwd", "email"):
            self.assertNotIn(forbidden, encoded)

    def test_pwa_instrument_context_import_path_is_idempotent_across_polls(self):
        handler = self.handler()
        handler.state = types.SimpleNamespace()
        handler._codex_preference_snapshot = lambda: ("gpt-5.5", "high")
        handler._load_codex_target = lambda: ("session-is-never-returned", Path("/not-returned"))
        handler._codex_quota_lines_cached = lambda: []
        fake_store = types.SimpleNamespace(find_by_id=lambda _session_id: None)
        fake_common = types.SimpleNamespace(SessionStore=lambda _root: fake_store)
        fake_service = types.SimpleNamespace(_latest_context_usage=lambda _meta: None)
        fake_tg = types.SimpleNamespace(TgCodexService=fake_service)
        with patch("push.sys.path", []), patch.dict("sys.modules", {
            "codex_common": fake_common,
            "tg_codex_bot": fake_tg,
        }):
            handler._pwa_kairos_instrument_snapshot()
            handler._pwa_kairos_instrument_snapshot()
            self.assertEqual([entry for entry in sys.path if entry == "/root/Windows-Codex-TG"], ["/root/Windows-Codex-TG"])

    def test_expired_pwa_quota_cache_import_path_is_idempotent_without_real_quota_probe(self):
        handler = self.handler()
        handler.state = types.SimpleNamespace(codex_home="/not-read")
        calls = []
        fake_service = types.SimpleNamespace(_quota_status_lines=lambda root: calls.append(root) or ["额度: Plus"])
        fake_tg = types.SimpleNamespace(TgCodexService=fake_service)
        with patch("push.sys.path", []), patch.dict("sys.modules", {"tg_codex_bot": fake_tg}), patch.dict("push.CODEX_QUOTA_CACHE", {"ts": 0.0, "lines": []}, clear=True):
            self.assertEqual(handler._codex_quota_lines_cached(), ["额度: Plus"])
            # Force a second expired-cache read; the fake service confirms no
            # filesystem/network quota provider ran during this contract test.
            import push
            push.CODEX_QUOTA_CACHE["ts"] = 0.0
            self.assertEqual(handler._codex_quota_lines_cached(), ["额度: Plus"])
            self.assertEqual(len(calls), 2)
            self.assertEqual([entry for entry in sys.path if entry == "/root/Windows-Codex-TG"], ["/root/Windows-Codex-TG"])

    def test_pwa_terminal_uses_observer_precedence_fallback_and_safe_labels_only(self):
        raw = {"busy": True, "phase": "raw phase", "events": [
            {"elapsed_seconds": 4, "label": "运行命令（参数与输出已隐藏）"},
            {"elapsed_seconds": 5, "label": "SUPER SECRET --token"},
            {"elapsed_seconds": -1, "label": "开始处理"},
        ]}
        bridge = types.SimpleNamespace(observer_snapshot=lambda: raw)
        fallback = types.SimpleNamespace(observer_snapshot=lambda: {"busy": True, "phase": "处理完成", "events": []})
        handler = self.handler()
        handler.state = types.SimpleNamespace(codex_app_bridge=bridge)
        with patch("push.CODEX_RUNS", fallback):
            payload = handler._pwa_kairos_terminal_snapshot()
        self.assertEqual(payload["phase"], "正在处理")
        self.assertEqual(payload["events"], [{"elapsed_seconds": 4, "label": "运行命令（参数与输出已隐藏）"}])
        self.assertNotIn("SUPER SECRET", str(payload))

        handler.state = types.SimpleNamespace(codex_app_bridge=types.SimpleNamespace(observer_snapshot=lambda: {"busy": False, "phase": None, "events": []}))
        with patch("push.CODEX_RUNS", fallback):
            self.assertEqual(handler._pwa_kairos_terminal_snapshot()["phase"], "处理完成")
        handler.state = types.SimpleNamespace(codex_app_bridge=types.SimpleNamespace(observer_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("nope"))))
        with patch("push.CODEX_RUNS", types.SimpleNamespace(observer_snapshot=lambda: (_ for _ in ()).throw(RuntimeError("nope")))):
            self.assertEqual(handler._pwa_kairos_terminal_snapshot(), {"available": False, "busy": False, "phase": "unavailable", "events": []})

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

    def test_sticker_catalog_is_a_cookie_read_route_and_has_no_model_url_input(self):
        store = WebSessionStore(300)
        token, _ = store.create()
        handler = self.handler("/stickers/catalog", "GET")
        handler.state = types.SimpleNamespace(
            web_session_enabled=True,
            web_sessions=store,
            sticker_catalog=types.SimpleNamespace(snapshot=lambda: {
                "ok": True, "version": "v1", "stickers": [{"name": "爱", "url": "https://assets.example/%E7%88%B1.gif"}],
            }),
        )
        handler.headers = {"Cookie": f"__Host-cccompanion={token}"}
        self.assertTrue(handler._web_session_matches())
        handler._handle_sticker_catalog()
        status, payload, kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual("爱", payload["stickers"][0]["name"])
        self.assertEqual("no-store", kwargs["extra_headers"]["Cache-Control"])

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
