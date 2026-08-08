"""Tests for the read-only memory library proxy (/memory/*).

仿照 _xiaoke_stop_test.py 的风格：object.__new__ 构造 handler，
stub _send_json 收集响应，mock urllib 不打真实上游。
"""

import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from push import PushHandler


class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MemoryProxyTest(unittest.TestCase):
    def setUp(self):
        PushHandler._memory_token_cache = None

    def tearDown(self):
        PushHandler._memory_token_cache = None

    def handler(self, path: str) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.state = types.SimpleNamespace()
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    # ---------- token extraction ----------

    def test_memory_token_extracted_from_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[mcp_servers.memory]\nurl = "https://memory.example/mcp?token=sekrit-123"\n')
            cfg = Path(f.name)
        try:
            with patch.object(PushHandler, "_MEMORY_CONFIG_PATH", cfg):
                self.assertEqual(PushHandler._memory_token(), "sekrit-123")
                # second call hits the cache
                self.assertEqual(PushHandler._memory_token_cache, "sekrit-123")
        finally:
            cfg.unlink()

    def test_memory_token_missing_config_returns_none(self):
        with patch.object(PushHandler, "_MEMORY_CONFIG_PATH", Path("/nonexistent/config.toml")):
            self.assertIsNone(PushHandler._memory_token())

    def test_missing_token_yields_502_and_no_upstream_call(self):
        handler = self.handler("/memory/stats")
        with patch.object(PushHandler, "_memory_token", classmethod(lambda cls: None)), \
                patch("urllib.request.urlopen") as urlopen:
            handler._handle_memory_get()
        urlopen.assert_not_called()
        status, payload = handler.responses[0]
        self.assertEqual(status, 502)
        self.assertIn("error", payload)

    # ---------- route whitelist ----------

    def test_all_five_routes_mapped(self):
        self.assertEqual(
            PushHandler._MEMORY_ROUTES,
            {
                "/memory/stats": "/api/stats",
                "/memory/categories": "/api/categories",
                "/memory/list": "/api/memories",
                "/memory/semantic-search": "/api/semantic-search",
                "/memory/board": "/api/board",
            },
        )

    def test_unknown_memory_path_404_without_upstream(self):
        handler = self.handler("/memory/delete-everything")
        with patch("urllib.request.urlopen") as urlopen:
            handler._handle_memory_get()
        urlopen.assert_not_called()
        self.assertEqual(handler.responses[0][0], 404)

    def test_path_traversal_not_forwarded(self):
        handler = self.handler("/memory/../admin")
        with patch("urllib.request.urlopen") as urlopen:
            handler._handle_memory_get()
        urlopen.assert_not_called()
        self.assertEqual(handler.responses[0][0], 404)

    # ---------- forwarding ----------

    def run_forward(self, path: str, upstream_payload, upstream_status: int = 200):
        handler = self.handler(path)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            return _FakeResponse(upstream_status, upstream_payload)

        with patch.object(PushHandler, "_memory_token", classmethod(lambda cls: "tok-abc")), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen):
            handler._handle_memory_get()
        return handler, captured

    def test_stats_forwarded_with_auth_and_curl_ua(self):
        handler, captured = self.run_forward("/memory/stats", {"total": 549})
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/stats")
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["timeout"], 10)
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer tok-abc")
        self.assertEqual(captured["headers"].get("User-agent"), "curl/7.81.0")
        self.assertEqual(handler.responses[0], (200, {"total": 549}))

    def test_list_passes_whitelisted_params_only(self):
        handler, captured = self.run_forward(
            "/memory/list?category=core&subcategory=profile.preference&limit=50&evil=1&redirect=http://x", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?category=core&subcategory=profile.preference&limit=50",
        )
        self.assertEqual(handler.responses[0], (200, []))

    def test_unclassified_core_sentinel_is_forwarded_without_rewriting(self):
        _, captured = self.run_forward(
            "/memory/list?category=core&subcategory=__unclassified__&per_page=50", {}
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?category=core&subcategory=__unclassified__&per_page=50",
        )

    def test_limit_clamped_and_bad_limit_dropped(self):
        _, captured = self.run_forward("/memory/list?limit=99999", [])
        self.assertIn("limit=200", captured["url"])
        _, captured = self.run_forward("/memory/list?limit=abc", [])
        self.assertNotIn("limit", captured["url"])

    def test_paginated_list_params_are_forwarded_and_validated(self):
        _, captured = self.run_forward(
            "/memory/list?page=3&per_page=50&sort_order=asc", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?page=3&per_page=50&sort_order=asc",
        )

        _, captured = self.run_forward(
            "/memory/list?page=oops&per_page=7&sort_order=sideways", []
        )
        self.assertNotIn("page=", captured["url"])
        self.assertNotIn("per_page=", captured["url"])
        self.assertNotIn("sort_order=", captured["url"])

    def test_paginated_board_params_are_forwarded(self):
        _, captured = self.run_forward(
            "/memory/board?page=2&per_page=20&sort_order=desc", {}
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/board?page=2&per_page=20&sort_order=desc",
        )

    def test_keyset_cursor_is_forwarded_but_malformed_or_oversized_values_are_not(self):
        cursor = "eyJ2IjoxLCJpZCI6Im0tNTAifQ"
        _, captured = self.run_forward(
            f"/memory/list?per_page=50&sort_order=desc&cursor={cursor}", {}
        )
        self.assertIn(f"cursor={cursor}", captured["url"])

        _, captured = self.run_forward("/memory/list?cursor=bad%2Fcursor", {})
        self.assertNotIn("cursor=", captured["url"])
        _, captured = self.run_forward("/memory/list?cursor=" + "a" * 1025, {})
        self.assertNotIn("cursor=", captured["url"])

    def test_semantic_query_url_encoded(self):
        _, captured = self.run_forward(
            "/memory/semantic-search?query=%E5%A4%8F%E4%BB%A5%E6%98%BC&limit=5", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/semantic-search?query=%E5%A4%8F%E4%BB%A5%E6%98%BC&limit=5",
        )

    def test_keyword_search_route_removed(self):
        # 方小南改单：关键词搜索不暴露，只留语义搜索
        handler = self.handler("/memory/search?query=abc")
        with patch("urllib.request.urlopen") as urlopen:
            handler._handle_memory_get()
        urlopen.assert_not_called()
        self.assertEqual(handler.responses[0][0], 404)

    def test_semantic_search_route(self):
        _, captured = self.run_forward(
            "/memory/semantic-search?query=abc&category=core&subcategory=__unclassified__", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/semantic-search?query=abc&category=core&subcategory=__unclassified__",
        )

    def test_board_and_categories_list_passthrough(self):
        handler, captured = self.run_forward("/memory/board", [{"from": "小克", "content": "hi"}])
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/board")
        self.assertEqual(handler.responses[0][1], [{"from": "小克", "content": "hi"}])
        handler, captured = self.run_forward("/memory/categories", ["core", "diary"])
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/categories")
        self.assertEqual(handler.responses[0][1], ["core", "diary"])

    # ---------- upstream errors ----------

    def test_upstream_http_error_becomes_502_without_token_leak(self):
        import urllib.error

        handler = self.handler("/memory/stats")
        err = urllib.error.HTTPError(
            "https://memory.xiaonancaleb.xyz/api/stats", 403, "forbidden", {}, io.BytesIO(b"")
        )
        with patch.object(PushHandler, "_memory_token", classmethod(lambda cls: "tok-abc")), \
                patch("urllib.request.urlopen", side_effect=err):
            handler._handle_memory_get()
        status, payload = handler.responses[0]
        self.assertEqual(status, 502)
        self.assertNotIn("tok-abc", json.dumps(payload))

    def test_upstream_unreachable_becomes_502(self):
        handler = self.handler("/memory/board")
        with patch.object(PushHandler, "_memory_token", classmethod(lambda cls: "tok-abc")), \
                patch("urllib.request.urlopen", side_effect=OSError("timed out")):
            handler._handle_memory_get()
        status, payload = handler.responses[0]
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"error": "memory upstream unreachable"})


if __name__ == "__main__":
    unittest.main()
