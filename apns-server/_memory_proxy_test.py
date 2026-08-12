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


_TAXONOMY = {
    "version": 1,
    "categories": [
        {
            "key": "core",
            "label": "深层",
            "subcategories": [
                {"key": "profile.preference", "label": "偏好", "count": 1},
                {"key": "operations.tooling", "label": "工具使用", "count": 0},
                {"key": "legacy", "label": "历史兼容", "count": 0},
            ],
        },
        {
            "key": "diary",
            "label": "日记",
            "subcategories": [{"key": "diary.worklog", "label": "牛马日志", "count": 1}],
        },
        {
            "key": "xiayizhou",
            "label": "夏以昼",
            "subcategories": [
                {"key": "xiayizhou.astra_fanfic", "label": "Astra 写的同人文", "count": 16},
            ],
        },
    ],
}


def _sync_payload(status="running", **overrides):
    payload = {
        "ok": status != "failed",
        "status": status,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "last_sync": None,
        "modules": [
            {"key": "diary.general", "label": "日记"},
            {"key": "diary.worklog", "label": "牛马日志"},
            {"key": "diary.health", "label": "运动健康"},
        ],
        "module_count": 3,
        "started_at": "2026-08-12T00:00:00Z",
        "finished_at": None,
        "errors": [],
        "ambiguous": 0,
        "orphaned": 0,
        "ambiguities": [],
    }
    payload.update(overrides)
    return payload


class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        return self._raw if size < 0 else self._raw[:size]

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

    def test_all_read_routes_mapped(self):
        self.assertEqual(
            PushHandler._MEMORY_ROUTES,
            {
                "/memory/stats": "/api/stats",
                "/memory/taxonomy": "/api/taxonomy",
                "/memory/categories": "/api/categories",
                "/memory/list": "/api/memories",
                "/memory/semantic-search": "/api/semantic-search",
                "/memory/board": "/api/board",
                "/memory/calendar": "/api/calendar",
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

    def run_forward(self, path: str, upstream_payload, upstream_status: int = 200, taxonomy_payload=_TAXONOMY):
        handler = self.handler(path)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured.setdefault("urls", []).append(req.full_url)
            captured["headers"] = dict(req.headers)
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            payload = taxonomy_payload if req.full_url.endswith("/api/taxonomy") else upstream_payload
            return _FakeResponse(upstream_status, payload)

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

    def test_calendar_requires_one_strict_month_and_forwards_only_it(self):
        payload = {"2026-08-12": {"entries": [], "mood": None}}
        handler, captured = self.run_forward("/memory/calendar?month=2026-08", payload)
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/calendar?month=2026-08")
        self.assertEqual(handler.responses[0], (200, payload))

        for path in (
            "/memory/calendar",
            "/memory/calendar?month=2026-8",
            "/memory/calendar?month=2026-13",
            "/memory/calendar?month=2026-08&month=2026-09",
            "/memory/calendar?month=2026-08&category=diary",
        ):
            rejected = self.handler(path)
            with patch("urllib.request.urlopen") as urlopen:
                rejected._handle_memory_get()
            urlopen.assert_not_called()
            self.assertEqual(rejected.responses[0][0], 400)

    def test_memory_get_rejects_oversized_or_invalid_json_response(self):
        oversized = self.handler("/memory/stats")
        invalid = self.handler("/memory/stats")

        with patch.object(PushHandler, "_memory_token", classmethod(lambda cls: "tok-abc")), \
                patch("urllib.request.urlopen", return_value=_FakeResponse(200, "x" * (PushHandler._MEMORY_RESPONSE_LIMIT + 1))):
            oversized._handle_memory_get()
        self.assertEqual(oversized.responses[0], (502, {"error": "memory upstream response too large"}))

        class InvalidResponse(_FakeResponse):
            def __init__(self):
                self.status = 200
                self._raw = b"not-json"

        with patch.object(PushHandler, "_memory_token", classmethod(lambda cls: "tok-abc")), \
                patch("urllib.request.urlopen", return_value=InvalidResponse()):
            invalid._handle_memory_get()
        self.assertEqual(invalid.responses[0], (502, {"error": "memory upstream returned invalid json"}))

    def test_list_passes_whitelisted_params_only(self):
        handler, captured = self.run_forward(
            "/memory/list?category=core&subcategory=profile.preference&limit=50&evil=1&redirect=http://x", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?category=core&subcategory=profile.preference&limit=50",
        )
        self.assertEqual(handler.responses[0], (200, []))

    def test_invalid_subcategory_scopes_are_rejected_against_upstream_taxonomy(self):
        for path, error in (
            ("/memory/list?category=core&subcategory=__unclassified__&per_page=50", "无效"),
            ("/memory/semantic-search?query=abc&category=core&subcategory=unknown", "无效"),
            ("/memory/list?category=diary&subcategory=diary.unknown", "无效"),
            ("/memory/list?category=diary&subcategory=profile.preference", "无效"),
            ("/memory/list?category=xiayizhou&subcategory=unknown", "无效"),
            ("/memory/list?category=xiayizhou&subcategory=profile.preference", "无效"),
            ("/memory/list?category=core&subcategory=diary.general", "无效"),
        ):
            handler, captured = self.run_forward(path, [])
            self.assertEqual(handler.responses[0][0], 400)
            self.assertIn(error, handler.responses[0][1]["error"])
            self.assertEqual(captured["urls"], ["https://memory.xiaonancaleb.xyz/api/taxonomy"])

    def test_malformed_subcategory_scope_is_rejected_before_taxonomy_lookup(self):
        for path, error in (
            ("/memory/list?category=core&subcategory=", "不能为空"),
            ("/memory/list?category=diary&category=core&subcategory=diary.general", "只能提供一次"),
            ("/memory/list?subcategory=profile.preference", "必须同时提供"),
        ):
            handler = self.handler(path)
            with patch("urllib.request.urlopen") as urlopen:
                handler._handle_memory_get()
            urlopen.assert_not_called()
            self.assertEqual(handler.responses[0][0], 400)
            self.assertIn(error, handler.responses[0][1]["error"])

    def test_valid_core_subcategory_scope_is_forwarded(self):
        _, captured = self.run_forward(
            "/memory/list?category=core&subcategory=legacy&per_page=50", {}
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?category=core&subcategory=legacy&per_page=50",
        )

    def test_valid_diary_subcategory_scope_is_forwarded(self):
        _, captured = self.run_forward(
            "/memory/semantic-search?query=abc&category=diary&subcategory=diary.worklog&limit=5", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/semantic-search?query=abc&category=diary&subcategory=diary.worklog&limit=5",
        )

    def test_valid_xiayizhou_subcategory_scope_is_forwarded(self):
        _, captured = self.run_forward(
            "/memory/list?category=xiayizhou&subcategory=xiayizhou.astra_fanfic&per_page=50", {}
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?category=xiayizhou&subcategory=xiayizhou.astra_fanfic&per_page=50",
        )

    def test_dynamic_taxonomy_allows_a_new_backend_category_without_proxy_changes(self):
        taxonomy = {
            "version": 1,
            "categories": [{
                "key": "future",
                "label": "未来",
                "subcategories": [{"key": "future.experimental", "label": "实验", "count": 3}],
            }],
        }
        _, captured = self.run_forward(
            "/memory/list?category=future&subcategory=future.experimental",
            {},
            taxonomy_payload=taxonomy,
        )
        self.assertEqual(
            captured["urls"],
            [
                "https://memory.xiaonancaleb.xyz/api/taxonomy",
                "https://memory.xiaonancaleb.xyz/api/memories?category=future&subcategory=future.experimental",
            ],
        )

    def test_taxonomy_route_is_forwarded_unchanged(self):
        handler, captured = self.run_forward("/memory/taxonomy", _TAXONOMY)
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/taxonomy")
        self.assertEqual(handler.responses[0], (200, _TAXONOMY))

    def test_limit_clamped_and_bad_limit_dropped(self):
        _, captured = self.run_forward("/memory/list?limit=99999", [])
        self.assertIn("limit=200", captured["url"])
        _, captured = self.run_forward("/memory/list?limit=abc", [])
        self.assertNotIn("limit", captured["url"])

    def test_paginated_list_params_are_forwarded_and_validated(self):
        _, captured = self.run_forward(
            "/memory/list?page=3&per_page=50&sort_by=updatedAt&sort_order=asc", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/memories?page=3&per_page=50&sort_by=updatedAt&sort_order=asc",
        )

        _, captured = self.run_forward(
            "/memory/list?page=oops&per_page=7&sort_by=editedAt&sort_order=sideways", []
        )
        self.assertNotIn("page=", captured["url"])
        self.assertNotIn("per_page=", captured["url"])
        self.assertNotIn("sort_by=", captured["url"])
        self.assertNotIn("sort_order=", captured["url"])

    def test_list_sort_by_accepts_only_one_known_value(self):
        for sort_by in ("createdAt", "updatedAt"):
            _, captured = self.run_forward(f"/memory/list?sort_by={sort_by}", [])
            self.assertEqual(
                captured["url"],
                f"https://memory.xiaonancaleb.xyz/api/memories?sort_by={sort_by}",
            )

        for query in (
            "sort_by=created_at",
            "sort_by=",
            "sort_by=createdAt&sort_by=updatedAt",
        ):
            _, captured = self.run_forward(f"/memory/list?{query}", [])
            self.assertNotIn("sort_by=", captured["url"])

    def test_sort_by_is_not_forwarded_to_search_or_board(self):
        _, captured = self.run_forward(
            "/memory/semantic-search?query=abc&sort_by=updatedAt", [],
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/semantic-search?query=abc",
        )
        _, captured = self.run_forward("/memory/board?sort_by=updatedAt", {})
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/board")

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
            "/memory/semantic-search?query=abc&category=core&subcategory=operations.tooling", []
        )
        self.assertEqual(
            captured["url"],
            "https://memory.xiaonancaleb.xyz/api/semantic-search?query=abc&category=core&subcategory=operations.tooling",
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

    # ---------- Notion sync trigger ----------

    def test_notion_sync_forwards_fixed_empty_post_with_bounded_timeout(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["headers"] = dict(req.headers)
            captured["timeout"] = timeout
            return _FakeResponse(202, _sync_payload())

        with patch.object(PushHandler, "_memory_sync_open", side_effect=fake_open):
            status, payload = PushHandler._memory_sync_request("tok-abc")

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(captured["url"], "https://memory.xiaonancaleb.xyz/api/sync/notion")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["data"], b"{}")
        self.assertEqual(captured["timeout"], 15)
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer tok-abc")

    def test_notion_sync_get_polls_same_fixed_path_without_a_body(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            return _FakeResponse(200, _sync_payload(
                status="completed", created=2, updated=1, skipped=206,
                finished_at="2026-08-12T00:01:00Z",
            ))

        with patch.object(PushHandler, "_memory_sync_open", side_effect=fake_open):
            status, payload = PushHandler._memory_sync_request("tok-abc", method="GET")

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["created"], 2)
        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])

    def test_notion_sync_post_route_reads_only_a_small_json_object(self):
        handler = self.handler("/memory/sync/notion")
        handler.command = "POST"
        handler.state = types.SimpleNamespace(shared_secret="app-secret", strict_auth=False)
        handler.headers = {"Content-Length": "2", "X-Auth-Token": "app-secret"}
        handler.rfile = io.BytesIO(b"{}")
        handler._check_ip_allowed = lambda: True
        handler._require_write_auth = lambda: True
        received = []
        handler._handle_memory_sync_post = lambda body: received.append(body)

        handler.do_POST()

        self.assertEqual(received, [{}])
        self.assertEqual(handler.responses, [])

    def test_notion_sync_post_rejects_oversize_or_chunked_body(self):
        for headers in (
            {"Content-Length": str(PushHandler._MEMORY_SYNC_REQUEST_LIMIT + 1)},
            {"Content-Length": "2", "Transfer-Encoding": "chunked"},
        ):
            handler = self.handler("/memory/sync/notion")
            handler.command = "POST"
            handler.state = types.SimpleNamespace(shared_secret="app-secret", strict_auth=False)
            handler.headers = {**headers, "X-Auth-Token": "app-secret"}
            handler.rfile = io.BytesIO(b"{}")
            handler._check_ip_allowed = lambda: True
            handler._require_write_auth = lambda: True
            with patch.object(PushHandler, "_handle_memory_sync_post") as sync:
                handler.do_POST()
            sync.assert_not_called()
            self.assertTrue(handler.close_connection)
            self.assertIn(handler.responses[0][0], (400, 413))

    def test_notion_sync_rejects_client_parameters_and_does_not_call_upstream(self):
        handler = self.handler("/memory/sync/notion")
        with patch.object(PushHandler, "_memory_sync_request") as request:
            handler._handle_memory_sync_post({"database_id": "attacker-controlled"})
        request.assert_not_called()
        self.assertEqual(handler.responses[0][0], 400)

    def test_notion_sync_http_error_does_not_forward_upstream_text_or_token(self):
        import urllib.error

        error = urllib.error.HTTPError(
            "https://memory.xiaonancaleb.xyz/api/sync/notion",
            422,
            "invalid",
            {},
            io.BytesIO(b'{"ok":false,"error":"bad tok-abc"}'),
        )
        with patch.object(PushHandler, "_memory_sync_open", side_effect=error):
            status, payload = PushHandler._memory_sync_request("tok-abc")
        self.assertEqual(status, 422)
        self.assertEqual(payload, {"ok": False, "error": "memory upstream http 422"})
        self.assertNotIn("tok-abc", json.dumps(payload))

    def test_notion_sync_response_size_is_bounded(self):
        class OversizedResponse(_FakeResponse):
            def __init__(self):
                self.status = 200
                self._raw = b"x" * (PushHandler._MEMORY_SYNC_RESPONSE_LIMIT + 1)

        with patch.object(PushHandler, "_memory_sync_open", return_value=OversizedResponse()):
            status, payload = PushHandler._memory_sync_request("tok-abc")
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"error": "memory upstream response too large"})

    def test_notion_sync_success_with_invalid_json_is_not_reported_as_success(self):
        response = _FakeResponse(200, {})
        response._raw = b"not-json"
        with patch.object(PushHandler, "_memory_sync_open", return_value=response):
            status, payload = PushHandler._memory_sync_request("tok-abc")
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"error": "memory upstream returned invalid json"})

    def test_notion_sync_whitelists_contract_and_rejects_missing_status(self):
        payload = _sync_payload(status="completed", debug={"notion_token": "secret"})
        public = PushHandler._memory_sync_public_payload(payload)
        self.assertIsNotNone(public)
        self.assertNotIn("debug", public)
        self.assertEqual(public["modules"][0], {"key": "diary.general", "label": "日记"})
        self.assertEqual(public["ambiguous"], 0)
        self.assertEqual(public["orphaned"], 0)
        self.assertNotIn("ambiguities", public)
        self.assertIsNone(PushHandler._memory_sync_public_payload({"ok": True}))
        missing_count = _sync_payload()
        missing_count.pop("created")
        self.assertIsNone(PushHandler._memory_sync_public_payload(missing_count))
        self.assertIsNone(PushHandler._memory_sync_public_payload(_sync_payload(created="1")))
        self.assertIsNone(PushHandler._memory_sync_public_payload(_sync_payload(ambiguous=True)))
        self.assertIsNone(PushHandler._memory_sync_public_payload(_sync_payload(orphaned="1")))
        self.assertIsNone(PushHandler._memory_sync_public_payload(_sync_payload(ambiguities=[{}] * 21)))

        failed = PushHandler._memory_sync_public_payload(_sync_payload(
            status="failed", ok=False, failed=1,
            errors=[{"page_id": "private-page", "error": "private Notion debug details"}],
        ))
        self.assertEqual(failed["error_count"], 1)
        self.assertNotIn("errors", failed)
        self.assertNotIn("private Notion", json.dumps(failed))

    def test_notion_sync_redirect_is_rejected_without_following_location(self):
        import urllib.error

        redirect = urllib.error.HTTPError(
            "https://memory.xiaonancaleb.xyz/api/sync/notion",
            301,
            "moved",
            {"Location": "https://evil.example/steal"},
            io.BytesIO(b""),
        )
        with patch.object(PushHandler, "_memory_sync_open", side_effect=redirect) as opened:
            status, payload = PushHandler._memory_sync_request("tok-abc")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(status, 502)
        self.assertEqual(payload, {"ok": False, "error": "memory upstream redirect rejected"})
        self.assertNotIn("Location", json.dumps(payload))

    def test_memory_sync_transport_installs_a_no_redirect_handler(self):
        sentinel = object()

        class FakeOpener:
            def open(self, request, timeout=None):
                self.request = request
                self.timeout = timeout
                return sentinel

        opener = FakeOpener()

        def fake_build(handler):
            self.assertIsNone(
                handler.redirect_request(None, None, 301, "moved", {}, "https://evil.example")
            )
            return opener

        with patch("urllib.request.build_opener", side_effect=fake_build):
            result = PushHandler._memory_sync_open("request", 15)
        self.assertIs(result, sentinel)
        self.assertEqual(opener.request, "request")
        self.assertEqual(opener.timeout, 15)

    def test_memory_sync_routes_fail_closed_even_when_legacy_strict_auth_is_off(self):
        for method, secret, supplied in (
            ("POST", "app-secret", ""),
            ("POST", "", ""),
            ("GET", "app-secret", "wrong"),
            ("GET", "", ""),
        ):
            handler = self.handler("/memory/sync/notion")
            handler.command = method
            handler.client_address = ("127.0.0.1", 1234)
            handler.state = types.SimpleNamespace(shared_secret=secret, strict_auth=False)
            handler.headers = {
                "Content-Length": "2",
                "X-Auth-Token": supplied,
            }
            handler.rfile = io.BytesIO(b"{}")
            handler._check_ip_allowed = lambda: True
            with patch.object(PushHandler, "_memory_sync_request") as upstream:
                if method == "POST":
                    handler.do_POST()
                else:
                    handler.do_GET()
            upstream.assert_not_called()
            self.assertEqual(handler.responses[0][0], 401)

    def test_memory_sync_does_not_accept_legacy_x_auth_alias(self):
        handler = self.handler("/memory/sync/notion")
        handler.state = types.SimpleNamespace(shared_secret="app-secret", strict_auth=False)
        handler.headers = {"X-Auth": "app-secret"}
        self.assertFalse(handler._memory_sync_auth_matches())


if __name__ == "__main__":
    unittest.main()
