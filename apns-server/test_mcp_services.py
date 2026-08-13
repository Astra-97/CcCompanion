import json
import os
import stat
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_services import MCP_TEST_RESPONSE_LIMIT, McpServiceError, McpServiceStore, PROVIDERS
from push import PushHandler
from provision_mcp_runtime import _append_codex_config, _install_xia_runtime_templates


class McpServiceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.luckin = self.root / "luckin.token"
        self.mcd = self.root / "mcd.token"
        self.store = McpServiceStore(self.root / "state.json")
        self.env = patch.dict(os.environ, {
            "LUCKIN_MCP_TOKEN_PATH": str(self.luckin),
            "MCDONALDS_MCP_TOKEN_PATH": str(self.mcd),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_status_redacts_tokens_and_recognizes_existing_luckin_file(self):
        self.luckin.write_text("very-secret-token\n", encoding="utf-8")
        payload = self.store.status()
        encoded = json.dumps(payload)
        self.assertTrue(payload["providers"][0]["configured"])
        self.assertNotIn("very-secret-token", encoded)
        self.assertEqual(payload["providers"][0]["endpoint"], "https://gwmcp.lkcoffee.com/order/user/mcp")

    def test_legacy_luckin_is_visible_but_truthfully_pending_shared_migration(self):
        legacy = self.root / "legacy-luckin"
        canonical = self.root / "canonical-luckin"
        legacy.write_text("old-secret\n", encoding="utf-8")
        patched = {**PROVIDERS["luckin"], "canonical_token_path": str(canonical), "legacy_token_path": str(legacy)}
        with patch.dict(os.environ, {}, clear=True), patch.dict(PROVIDERS, {"luckin": patched}):
            item = self.store.status()["providers"][0]
            runtime = self.store.status()["runtime"]
        self.assertTrue(item["configured"])
        self.assertEqual(item["configuration_source"], "legacy_migration_pending")
        self.assertNotIn("old-secret", json.dumps(item))
        self.assertTrue(runtime["migration_pending"])

    def test_save_is_atomic_mode_600_and_blank_does_not_clear(self):
        self.store.update({"provider_id": "luckin", "action": "save", "token": "fresh-token"})
        self.assertEqual(self.luckin.read_text(encoding="utf-8"), "fresh-token\n")
        self.assertEqual(stat.S_IMODE(self.luckin.stat().st_mode), 0o600)
        self.assertEqual(self.luckin.stat().st_uid, os.geteuid())
        self.store.update({"provider_id": "luckin", "action": "save", "token": ""})
        self.assertEqual(self.luckin.read_text(encoding="utf-8"), "fresh-token\n")

    def test_canonical_save_is_relay_owned_but_env_override_is_not_chowned(self):
        account = __import__("pwd").getpwnam("cc-xia-relay")
        canonical_root = self.root / "canonical"
        canonical_root.mkdir(mode=0o700)
        os.chown(canonical_root, account.pw_uid, account.pw_gid)
        canonical = canonical_root / "luckin.token"
        provider = {**PROVIDERS["luckin"], "canonical_token_path": str(canonical)}
        # Clear the test override for this one call so it exercises exactly the
        # fixed-path branch used after provisioning.
        with patch.dict(os.environ, {}, clear=True), patch.dict(PROVIDERS, {"luckin": provider}):
            self.store.update({"provider_id": "luckin", "action": "save", "token": "canonical-token"})
        info = canonical.stat()
        self.assertEqual((info.st_uid, info.st_gid), (account.pw_uid, account.pw_gid))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    def test_clear_requires_explicit_confirmation(self):
        self.store.update({"provider_id": "mcdonalds", "action": "save", "token": "secret"})
        with self.assertRaises(McpServiceError):
            self.store.update({"provider_id": "mcdonalds", "action": "clear"})
        self.assertTrue(self.mcd.exists())
        self.store.update({"provider_id": "mcdonalds", "action": "clear", "confirm_clear": True})
        self.assertFalse(self.mcd.exists())

    def test_validation_and_mcp_json_or_sse_parser(self):
        with self.assertRaises(McpServiceError):
            self.store.update({"provider_id": "evil", "action": "save", "token": "x"})
        with self.assertRaises(McpServiceError):
            self.store.update({"provider_id": "luckin", "action": "save", "token": "x", "url": "https://evil"})
        good = b'{"jsonrpc":"2.0","result":{"tools":[]}}'
        self.assertEqual(self.store._parse_test_response(good, "application/json")[0], "connected")
        self.assertEqual(self.store._parse_test_response(b'data: ' + good, "text/event-stream")[0], "connected")

    def test_connection_check_uses_only_the_official_endpoint_and_mcp_headers(self):
        captured = {}
        class Response:
            headers = {"Content-Type": "application/json"}
            def read(self, size): return b'{"result":{"tools":[]}}'
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["auth"] = request.get_header("Authorization")
                captured["protocol"] = request.get_header("Mcp-protocol-version")
                captured["timeout"] = timeout
                return Response()
        with patch("mcp_services.urllib.request.build_opener", return_value=Opener()):
            self.assertEqual(self.store._test_provider("luckin", "test-value")[0], "connected")
        self.assertEqual(captured["url"], "https://gwmcp.lkcoffee.com/order/user/mcp")
        self.assertEqual(captured["auth"], "Bearer test-value")
        self.assertEqual(captured["protocol"], "2025-03-26")

    def test_connection_check_accepts_large_official_catalog_but_rejects_true_oversize(self):
        large_catalog = json.dumps({
            "result": {"tools": [{"name": "tool", "description": "x" * (85 * 1024)}]},
        }).encode("utf-8")

        class Response:
            headers = {"Content-Type": "application/json"}
            def __init__(self, body): self.body = body
            def read(self, size): return self.body[:size]
            def __enter__(self): return self
            def __exit__(self, *args): return False

        class Opener:
            def __init__(self, body): self.body = body
            def open(self, request, timeout): return Response(self.body)

        with patch("mcp_services.urllib.request.build_opener", return_value=Opener(large_catalog)):
            self.assertEqual(self.store._test_provider("mcdonalds", "test-value")[0], "connected")
        oversized = b"x" * (MCP_TEST_RESPONSE_LIMIT + 1)
        with patch("mcp_services.urllib.request.build_opener", return_value=Opener(oversized)):
            self.assertEqual(self.store._test_provider("mcdonalds", "test-value"), ("failed", "服务响应过大"))

    def test_xiaoke_runtime_ready_requires_authenticated_live_health(self):
        channel_token = self.root / "channel.token"
        channel_token.write_text("private-channel-token\n", encoding="utf-8")
        with patch.dict(os.environ, {"CC_XIA_CHANNEL_TOKEN_PATH": str(channel_token)}), patch.object(
            McpServiceStore, "_read_xiaoke_health", return_value=b'{"ready":true}'
        ) as read_health:
            self.assertTrue(self.store._xiaoke_channel_ready())
        read_health.assert_called_once_with("private-channel-token")

        channel_token.unlink()
        with patch.dict(os.environ, {"CC_XIA_CHANNEL_TOKEN_PATH": str(channel_token)}):
            self.assertFalse(self.store._xiaoke_channel_ready())

    def test_xiaoke_health_failures_are_pending_not_status_errors(self):
        channel_token = self.root / "channel.token"
        cases = [b"[]", b"null", b"not-json", b"[" * 1100 + b"0" + b"]" * 1100]
        with patch.dict(os.environ, {"CC_XIA_CHANNEL_TOKEN_PATH": str(channel_token)}):
            for raw in cases:
                channel_token.write_text("valid-token\n", encoding="utf-8")
                with patch.object(McpServiceStore, "_read_xiaoke_health", return_value=raw):
                    self.assertFalse(self.store._xiaoke_channel_ready())
            for error in (TimeoutError(), OSError(), ValueError(), TypeError()):
                with patch.object(McpServiceStore, "_read_xiaoke_health", side_effect=error):
                    self.assertFalse(self.store._xiaoke_channel_ready())
            channel_token.write_text("bad\rvalue\n", encoding="utf-8")
            with patch.object(McpServiceStore, "_read_xiaoke_health") as read_health:
                self.assertFalse(self.store._xiaoke_channel_ready())
                read_health.assert_not_called()

    def test_xiaoke_health_http_parser_rejects_redirect_truncation_and_oversize(self):
        good = b'{"ready":true}'
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 14\r\nConnection: close\r\n\r\n" + good
        self.assertEqual(self.store._parse_xiaoke_health_http(response), good)
        invalid = [
            b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 500 Error\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\n\r\n" + (b"x" * (16 * 1024 + 1)),
        ]
        for raw in invalid:
            with self.assertRaises(ValueError):
                self.store._parse_xiaoke_health_http(raw)

    def test_routes_fail_closed_before_status_or_body_is_handled(self):
        get_handler = object.__new__(PushHandler)
        get_handler.path = "/mcp-services"
        get_handler._is_public_get = lambda: False
        get_handler._check_ip_allowed = lambda: True
        get_handler._require_auth = lambda: True
        get_handler._native_pairing_auth_matches = lambda: False
        get_handler._handle_mcp_services_get = lambda: self.fail("must not expose status")
        get_response = []
        get_handler._send_json = lambda code, body, **kwargs: get_response.append((code, body))
        PushHandler.do_GET(get_handler)
        self.assertEqual(get_response[0][0], 401)

        post_handler = object.__new__(PushHandler)
        post_handler.path = "/mcp-services"
        post_handler._check_ip_allowed = lambda: True
        post_handler._require_write_auth = lambda: True
        post_handler._native_pairing_auth_matches = lambda: False
        post_handler._read_body = lambda: self.fail("must not read a token before auth")
        post_handler._handle_mcp_services_post = lambda body: self.fail("must not update")
        post_response = []
        post_handler._send_json = lambda code, body, **kwargs: post_response.append((code, body))
        PushHandler.do_POST(post_handler)
        self.assertEqual(post_response[0][0], 401)

    def test_codex_and_xiaoke_discovery_contracts_use_the_same_fixed_bridge(self):
        codex = self.root / "codex.toml"
        _append_codex_config(codex)
        template = Path(__file__).parent / "xia_claude_channel" / ".mcp.json.in"
        active = self.root / "xia-active.json"
        active.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        bridge = self.root / "cc-companion-mcp-bridge"
        bridge.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        bridge.chmod(0o755)
        with patch.dict(os.environ, {
            "CODEX_HOME": str(self.root),
            "CC_MCP_BRIDGE_PATH": str(bridge),
            "CC_XIA_MCP_TEMPLATE": str(template),
            "CC_XIA_ACTIVE_MCP_CONFIG": str(active),
            "CC_MCP_TOKEN_ROOT": str(self.root),
        }), patch("mcp_services.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.geteuid())), patch.object(
            McpServiceStore, "_xiaoke_channel_ready", return_value=True
        ):
            # status expects CODEX_HOME/config.toml, exactly what Codex uses.
            (self.root / "config.toml").write_text(codex.read_text(encoding="utf-8"), encoding="utf-8")
            runtime = self.store.status()["runtime"]
        self.assertTrue(runtime["codex_registered"])
        self.assertTrue(runtime["xiaoke_template_registered"])
        self.assertTrue(runtime["xiaoke_active_registered"])
        self.assertTrue(runtime["xiaoke_channel_ready"])
        self.assertEqual(runtime["activation"], "ready")
        with patch.dict(os.environ, {
            "CODEX_HOME": str(self.root),
            "CC_MCP_BRIDGE_PATH": str(bridge),
            "CC_XIA_MCP_TEMPLATE": str(template),
            "CC_XIA_ACTIVE_MCP_CONFIG": str(active),
            "CC_MCP_TOKEN_ROOT": str(self.root),
        }), patch("mcp_services.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.geteuid())), patch.object(
            McpServiceStore, "_xiaoke_channel_ready", return_value=False
        ):
            pending = self.store.status()["runtime"]
        self.assertFalse(pending["xiaoke_channel_ready"])
        self.assertEqual(pending["activation"], "pending_activation")
        codex_text = codex.read_text(encoding="utf-8")
        xia = json.loads(template.read_text(encoding="utf-8"))["mcpServers"]
        xia_settings = json.loads((Path(__file__).parent / "xia_claude_channel" / "settings.json").read_text(encoding="utf-8"))
        for provider in ("luckin", "mcdonalds"):
            self.assertIn(f"[mcp_servers.{provider}]", codex_text)
            self.assertEqual(xia[provider]["args"], [provider])
            self.assertIn("cc-companion-mcp-bridge", xia[provider]["command"])
            self.assertNotIn("sudo", json.dumps(xia[provider]))
            self.assertIn(f"mcp__{provider}__*", xia_settings["permissions"]["allow"])

    def test_provision_refuses_conflicting_codex_stanza_and_deploys_actual_xia_files(self):
        codex = self.root / "config.toml"
        codex.write_text('[mcp_servers.luckin]\ncommand = "other"\nargs = ["luckin"]\n', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            _append_codex_config(codex)
        deployed = self.root / "opt-xia"
        source = Path(__file__).parent / "xia_claude_channel"
        _install_xia_runtime_templates(source, deployed)
        self.assertEqual((deployed / ".mcp.json.in").read_bytes(), (source / ".mcp.json.in").read_bytes())
        self.assertEqual((deployed / "settings.json").read_bytes(), (source / "settings.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
