import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

import mcp_bridge


class McpBridgeTest(unittest.TestCase):
    def setUp(self):
        mcp_bridge._session_id = None

    def tearDown(self):
        mcp_bridge._session_id = None

    def test_fixed_provider_forwards_jsonrpc_with_fresh_token_and_never_follows_url_input(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("first-token\n", encoding="utf-8")
            captured = {}
            class Response:
                headers = {"Content-Type": "application/json"}
                def read(self, size): return b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
                def __enter__(self): return self
                def __exit__(self, *args): return False
            class Opener:
                def open(self, request, timeout):
                    captured["url"] = request.full_url
                    captured["auth"] = request.get_header("Authorization")
                    return Response()
            with patch.dict(os.environ, {"LUCKIN_MCP_TOKEN_PATH": str(token_path)}), \
                    patch("mcp_bridge.urllib.request.build_opener", return_value=Opener()):
                response = mcp_bridge.forward("luckin", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(response["result"], {"tools": []})
            self.assertEqual(captured["url"], "https://gwmcp.lkcoffee.com/order/user/mcp")
            self.assertEqual(captured["auth"], "Bearer first-token")

    def test_large_official_catalog_is_forwarded_but_true_oversize_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("token\n", encoding="utf-8")
            large_catalog = (
                b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"tool","description":"'
                + (b"x" * (85 * 1024))
                + b'"}]}}'
            )

            class Response:
                headers = {"Content-Type": "application/json"}
                def __init__(self, body): self.body = body
                def read(self, size): return self.body[:size]
                def __enter__(self): return self
                def __exit__(self, *args): return False

            class Opener:
                def __init__(self, body): self.body = body
                def open(self, request, timeout): return Response(self.body)

            with patch.dict(os.environ, {"MCDONALDS_MCP_TOKEN_PATH": str(token_path)}), patch(
                "mcp_bridge.urllib.request.build_opener", return_value=Opener(large_catalog)
            ):
                answer = mcp_bridge.forward("mcdonalds", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(len(answer["result"]["tools"]), 1)

            oversized = b"x" * (mcp_bridge.MCP_TEST_RESPONSE_LIMIT + 1)
            with patch.dict(os.environ, {"MCDONALDS_MCP_TOKEN_PATH": str(token_path)}), patch(
                "mcp_bridge.urllib.request.build_opener", return_value=Opener(oversized)
            ):
                answer = mcp_bridge.forward("mcdonalds", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(answer["error"]["code"], -32001)

    def test_missing_token_is_safe_jsonrpc_error(self):
        with patch.dict(os.environ, {"MCDONALDS_MCP_TOKEN_PATH": "/does/not/exist"}):
            answer = mcp_bridge.forward("mcdonalds", {"jsonrpc": "2.0", "id": "a", "method": "tools/list"})
        self.assertEqual(answer["error"]["code"], -32000)

    def test_initialize_captures_session_and_sse_multiline_response_then_forwards_it(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("token\n", encoding="utf-8")
            requests = []
            class Response:
                def __init__(self, body, headers): self._body, self.headers = body, headers
                def read(self, size): return self._body
                def __enter__(self): return self
                def __exit__(self, *args): return False
            class Opener:
                def open(self, request, timeout):
                    requests.append(request)
                    if len(requests) == 1:
                        return Response(b'{"jsonrpc":"2.0","id":1,"result":{}}', {"Content-Type": "application/json", "Mcp-Session-Id": "session-1"})
                    return Response(b'data: {"jsonrpc":"2.0",\ndata: "id":2,"result":{"tools":[]}}\n\n', {"Content-Type": "text/event-stream"})
            mcp_bridge._session_id = None
            with patch.dict(os.environ, {"LUCKIN_MCP_TOKEN_PATH": str(token_path)}), \
                    patch("mcp_bridge.urllib.request.build_opener", return_value=Opener()):
                self.assertIn("result", mcp_bridge.forward("luckin", {"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
                answer = mcp_bridge.forward("luckin", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(answer["result"], {"tools": []})
            self.assertEqual(requests[1].get_header("Mcp-session-id"), "session-1")

    def test_not_found_clears_stale_session(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("token\n", encoding="utf-8")
            class Opener:
                def open(self, request, timeout):
                    raise urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)
            mcp_bridge._session_id = "expired"
            with patch.dict(os.environ, {"MCDONALDS_MCP_TOKEN_PATH": str(token_path)}), \
                    patch("mcp_bridge.urllib.request.build_opener", return_value=Opener()):
                answer = mcp_bridge.forward("mcdonalds", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            self.assertEqual(answer["error"]["code"], -32003)
            self.assertIsNone(mcp_bridge._session_id)


if __name__ == "__main__":
    unittest.main()
