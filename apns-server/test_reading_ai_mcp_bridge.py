import json
import os
import tempfile
import unittest
from unittest.mock import patch

import reading_ai_mcp_bridge as bridge


class ReadingAiMcpBridgeTest(unittest.TestCase):
    def test_tool_has_no_contact_or_book_parameters(self):
        schema = bridge._tool()["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"requestedChars", "requestId"})
        self.assertEqual(schema["properties"]["requestedChars"]["maximum"], 1000)

    def test_call_rejects_unbounded_or_extra_arguments_before_http(self):
        credential = {"endpoint": "https://example.invalid", "token": "a" * 16}
        for arguments in (
            {"requestedChars": 0, "requestId": "one"},
            {"requestedChars": 1001, "requestId": "one"},
            {"requestedChars": 1, "requestId": "one", "bookId": "no"},
        ):
            with self.subTest(arguments=arguments), patch("reading_ai_mcp_bridge.urllib.request.build_opener") as opener:
                ok, _message = bridge._call(credential, "kairos", arguments)
                self.assertFalse(ok)
                opener.assert_not_called()

    def test_call_sends_fixed_contact_header_and_never_more_than_1000_utf16_units(self):
        captured = {}
        class Response:
            def read(self, _limit): return json.dumps({"text": "😀" * 500}).encode()
            def __enter__(self): return self
            def __exit__(self, *_args): return False
        class Opener:
            def open(self, request, timeout):
                captured["headers"] = dict(request.header_items())
                captured["body"] = json.loads(request.data)
                captured["timeout"] = timeout
                return Response()
        credential = {"endpoint": "https://example.invalid", "token": "a" * 16}
        with patch("reading_ai_mcp_bridge.urllib.request.build_opener", return_value=Opener()):
            ok, text = bridge._call(credential, "kairos", {"requestedChars": 1000, "requestId": "one"})
        self.assertTrue(ok)
        self.assertEqual(len(text.encode("utf-16-le")) // 2, 1000)
        self.assertEqual(captured["body"], {"requestedChars": 1000, "requestId": "one"})
        self.assertEqual(captured["headers"]["X-cc-reading-ai-contact"], "kairos")

    def test_credential_read_rejects_relaxed_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "CC_COMPANION_READING_AI_MCP_CREDENTIAL_ROOT": root,
        }, clear=False):
            path = os.path.join(root, "kairos.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"contactId": "kairos", "endpoint": "https://example.invalid", "token": "a" * 16}, handle)
            os.chmod(path, 0o600)
            self.assertIsNotNone(bridge._credential("kairos"))
            os.chmod(path, 0o644)
            self.assertIsNone(bridge._credential("kairos"))
            os.unlink(path)
            target = os.path.join(root, "target.json")
            with open(target, "w", encoding="utf-8") as handle:
                json.dump({"contactId": "kairos", "endpoint": "https://example.invalid", "token": "a" * 16}, handle)
            os.chmod(target, 0o600)
            os.symlink(target, path)
            self.assertIsNone(bridge._credential("kairos"))
