"""Focused security tests for the constrained Co-Reading /reading proxy."""

import io
import json
import os
import tempfile
import types
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from push import PushHandler


class _Response:
    def __init__(self, status=200, payload=None, content_type="application/json; charset=utf-8"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._raw = json.dumps({"ok": True} if payload is None else payload).encode("utf-8")

    def read(self, size=-1):
        return self._raw if size < 0 else self._raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ReadingProxyTest(unittest.TestCase):
    def setUp(self):
        PushHandler._reading_token_cache = None

    def tearDown(self):
        PushHandler._reading_token_cache = None

    def handler(self, path, *, body=None, token="native-secret"):
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
        handler.state = types.SimpleNamespace(shared_secret=token, strict_auth=True)
        handler.responses = []
        handler._send_json = lambda status, payload, **_kwargs: handler.responses.append((status, payload))
        handler._check_ip_allowed = lambda: True
        headers = Message()
        if token:
            headers["X-Auth-Token"] = token
        if body is not None:
            raw = json.dumps(body, separators=(",", ":")).encode("utf-8") if not isinstance(body, bytes) else body
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(raw))
            handler.rfile = io.BytesIO(raw)
        else:
            handler.rfile = io.BytesIO()
        handler.headers = headers
        return handler

    def test_missing_native_auth_never_reaches_proxy(self):
        handler = self.handler("/reading/books", token="")
        handler.state.shared_secret = "native-secret"
        with patch.object(PushHandler, "_handle_reading_proxy") as proxy:
            handler.do_GET()
        proxy.assert_not_called()
        self.assertEqual(handler.responses, [(401, {"error": "unauthorized"})])

    def test_ai_continue_post_dispatches_before_generic_reading_proxy(self):
        handler = self.handler("/reading/ai/continue", body={"requestedChars": 1, "requestId": "one"})
        handler.command = "POST"
        handler._reading_ai_bridge_contact = lambda: "kairos"
        handler._handle_reading_ai_continue = lambda contact: handler.responses.append((299, {"contact": contact}))
        with patch.object(PushHandler, "_handle_reading_proxy") as proxy:
            handler.do_POST()
        proxy.assert_not_called()
        self.assertEqual(handler.responses, [(299, {"contact": "kairos"})])

        denied = self.handler("/reading/ai/continue", body={"requestedChars": 1, "requestId": "one"})
        denied.command = "POST"
        denied._reading_ai_bridge_contact = lambda: None
        with patch.object(PushHandler, "_handle_reading_proxy") as proxy:
            denied.do_POST()
        proxy.assert_not_called()
        self.assertEqual(denied.responses, [(401, {"error": "unauthorized"})])

    def test_only_whitelisted_routes_are_forwarded(self):
        cases = {
            "/reading/books": "/api/books",
            "/reading/books/book_1/chunks": "/api/books/book_1/chunks",
            "/reading/books/book_1/chunks/ch_2": "/api/books/book_1/chunks/ch_2",
            "/reading/books/%E4%B9%A6.epub/chunks": "/api/books/%E4%B9%A6.epub/chunks",
            "/reading/progress?bookId=book_1": "/api/progress?bookId=book_1",
        }
        for path, upstream_path in cases.items():
            with self.subTest(path=path):
                handler = self.handler(path)
                captured = {}

                def open_request(request, timeout):
                    captured.update(url=request.full_url, method=request.get_method(), timeout=timeout)
                    return _Response(payload={"path": upstream_path})

                with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                        patch.object(PushHandler, "_reading_open", staticmethod(open_request)):
                    handler._handle_reading_proxy("GET")
                self.assertEqual(captured["url"], "https://reading.xiaonancaleb.xyz" + upstream_path)
                self.assertEqual(captured["method"], "GET")
                self.assertEqual(captured["timeout"], PushHandler._READING_TIMEOUT_SEC)
                self.assertEqual(handler.responses[0][0], 200)

        rejected = self.handler("/reading/cards")
        with patch.object(PushHandler, "_reading_open") as open_request:
            rejected._handle_reading_proxy("GET")
        open_request.assert_not_called()
        self.assertEqual(rejected.responses[0][0], 404)

    def test_ai_bridge_is_fixed_contact_bounded_and_idempotent(self):
        class Chat:
            def __init__(self): self.rows = []
            def tail(self, _limit): return list(self.rows)
            def append(self, **row): self.rows.append(row); return row

        chat = Chat()
        handler = self.handler("/reading/ai/continue")
        handler.state = types.SimpleNamespace(contact_routes={}, contact_chats={"kairos": chat})
        handler._chat_for_contact = lambda _contact: chat
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            PushHandler._READING_AI_STATE_PATH_ENV: os.path.join(root, "anchors.json"),
        }, clear=False), patch.object(PushHandler, "_reading_ai_allowed_contacts", classmethod(lambda _cls, _state: {"kairos"})), \
                patch.object(PushHandler, "_reading_token", classmethod(lambda _cls: "upstream-token")):
            def upstream(_method, path, _token, _body=None):
                if path.endswith("/chunks"):
                    return 200, {"chunks": [{"id": "ch_1", "title": "第一章"}, {"id": "ch_2", "title": "第二章"}]}
                if path.endswith("ch_1"):
                    return 200, {"text": "a😀bcdef"}
                return 200, {"text": "第二章正文"}

            handler._reading_request = upstream
            handler._reading_ai_grant_from_metadata("kairos", {
                "reading_share": {"schemaVersion": 2, "bookId": "book_1", "chunkId": "ch_1", "anchorOffset": 1, "bookTitle": "星河", "chapterTitle": "第一章"},
            })
            status, first = handler._reading_ai_continue("kairos", 3, "request-1")
            self.assertEqual(status, 200)
            self.assertLessEqual(len(first["text"].encode("utf-16-le")) // 2, 3)
            self.assertEqual(len(chat.rows), 1)
            status, repeated = handler._reading_ai_continue("kairos", 3, "request-1")
            self.assertEqual(status, 200)
            self.assertEqual(repeated, first)
            self.assertEqual(len(chat.rows), 1)
            self.assertNotIn("mark-read", " ".join(row.get("text", "") for row in chat.rows))
            self.assertEqual(chat.rows[0]["role"], "system")
            event = chat.rows[0]["metadata"]["ai_reading_event"]
            self.assertEqual(event["returnedChars"], len(first["text"].encode("utf-16-le")) // 2)
            self.assertIn("from", event)
            self.assertIn("to", event)

    def test_ai_grant_validates_upstream_and_never_rewinds_same_book(self):
        handler = self.handler("/reading/ai/continue")
        handler.state = types.SimpleNamespace(contact_routes={}, contact_chats={"kairos": object()})
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            PushHandler._READING_AI_STATE_PATH_ENV: os.path.join(root, "anchors.json"),
        }, clear=False), patch.object(PushHandler, "_reading_ai_allowed_contacts", classmethod(lambda _cls, _state: {"kairos"})), \
                patch.object(PushHandler, "_reading_token", classmethod(lambda _cls: "upstream-token")):
            handler._reading_request = lambda _method, path, _token, _body=None: (
                (200, {"chunks": [{"id": "ch_1"}, {"id": "ch_2"}]}) if path.endswith("/chunks")
                else (200, {"text": "abcdef"})
            )
            handler._reading_ai_grant_from_metadata("kairos", {"reading_share": {"schemaVersion": 2, "bookId": "book_1", "chunkId": "ch_2", "anchorOffset": 4, "bookTitle": "星河", "chapterTitle": "二"}})
            handler._reading_ai_grant_from_metadata("kairos", {"reading_share": {"schemaVersion": 2, "bookId": "book_1", "chunkId": "ch_1", "anchorOffset": 1, "bookTitle": "星河", "chapterTitle": "一"}})
            self.assertEqual(PushHandler._reading_ai_store_load()["anchors"]["kairos"]["chunkId"], "ch_2")

    def test_ai_grant_hook_only_accepts_a_successfully_appended_user_record(self):
        handler = self.handler("/reading/ai/continue")
        handler._reading_ai_grant_from_metadata = Mock()
        metadata = {"reading_share": {"schemaVersion": 2}}
        handler._reading_ai_grant_after_user_append("kairos", {"role": "user", "metadata": metadata})
        handler._reading_ai_grant_after_user_append("kairos", {"role": "assistant", "metadata": metadata})
        # A rejected append has no record, so it has no grant hook to invoke.
        handler._reading_ai_grant_after_user_append("kairos", None)
        handler._reading_ai_grant_from_metadata.assert_called_once_with("kairos", metadata)

    def test_rejected_chat_route_cannot_grant_a_reading_anchor(self):
        handler = self.handler("/chat/send")
        handler.state.contact_chats = {}
        handler._chat_contact_directory = lambda: []
        handler._reading_ai_grant_after_user_append = Mock()
        handler._handle_chat_send({
            "text": "share", "contact_id": "not-registered",
            "metadata": {"reading_share": {"schemaVersion": 2}},
        })
        self.assertEqual(handler.responses[0][0], 501)
        handler._reading_ai_grant_after_user_append.assert_not_called()

    def test_ai_bridge_rejects_non_ai_contact_and_invalid_tool_shape(self):
        handler = self.handler("/reading/ai/continue")
        handler.state = types.SimpleNamespace(contact_routes={}, contact_chats={})
        with patch.object(PushHandler, "_reading_ai_allowed_contacts", classmethod(lambda _cls, _state: {"kairos"})):
            self.assertEqual(handler._reading_ai_continue("hajiki", 1, "request-1")[0], 403)
        handler = self.handler("/reading/ai/continue", body={"requestedChars": 1001, "requestId": "request-1"})
        handler._handle_reading_ai_continue("kairos")
        self.assertEqual(handler.responses[0][0], 400)

    def test_ai_bridge_allowlist_comes_from_server_contact_capability(self):
        state = types.SimpleNamespace(
            contact_routes={
                "kairos": {"send_handler": "kairos", "capabilities": ["chat", "ai_reading_continue"]},
                "kimi": {"send_handler": "kimi", "capabilities": ["chat"]},
                "xiaoke": {"send_handler": "xiaoke", "capabilities": ["chat", "ai_reading_continue"]},
                "apples": {"send_handler": "apples", "capabilities": ["chat", "group_chat"]},
            },
            contact_chats={"kairos": object(), "kimi": object(), "xiaoke": object(), "apples": object()},
        )
        self.assertEqual(PushHandler._reading_ai_allowed_contacts(state), {"xiaoke", "kairos"})

    def test_ai_request_ledger_is_sharded_past_one_megabyte_and_replays_after_restart(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            PushHandler._READING_AI_STATE_PATH_ENV: os.path.join(root, "anchors.json"),
            PushHandler._READING_AI_RESULTS_DIR_ENV: os.path.join(root, "results"),
        }, clear=False):
            PushHandler._reading_ai_store_save({"anchors": {"kairos": {
                "bookId": "book_1", "chunkId": "ch_1", "anchorOffset": 2000,
                "bookTitle": "星河", "chapterTitle": "第一章",
            }}})
            for index in range(400):
                request_id = f"request-{index}"
                result = {
                    "requestId": request_id, "requestedChars": 1000, "text": "续" * 1000,
                    "bookTitle": "星河", "chapterTitle": "第一章",
                    "eventId": f"reading-ai:kairos:{request_id}",
                    "from": {"bookId": "book_1", "chunkId": "ch_1", "anchorOffset": 0},
                    "to": {"bookId": "book_1", "chunkId": "ch_1", "anchorOffset": 1000},
                    "returnedChars": 1000, "completed": False,
                }
                PushHandler._reading_ai_result_save("kairos", request_id, result)
            result_bytes = sum(path.stat().st_size for path in (Path(root) / "results").rglob("*.json"))
            self.assertGreater(result_bytes, 1024 * 1024)
            # Simulate a restart: compact state still loads and an old ID is
            # answered from its own durable result without fetching or moving.
            restarted = self.handler("/reading/ai/continue")
            restarted.state = types.SimpleNamespace(contact_routes={}, contact_chats={})
            restarted._reading_request = Mock(side_effect=AssertionError("old id must not re-fetch upstream"))
            with patch.object(PushHandler, "_reading_ai_allowed_contacts", classmethod(lambda _cls, _state: {"kairos"})):
                status, replay = restarted._reading_ai_continue("kairos", 1000, "request-0")
            self.assertEqual(status, 200)
            self.assertEqual(replay["requestId"], "request-0")
            self.assertEqual(PushHandler._reading_ai_store_load()["anchors"]["kairos"]["anchorOffset"], 2000)

    def test_ai_event_idempotency_is_not_tail_limited(self):
        with tempfile.TemporaryDirectory() as root:
            history = os.path.join(root, "history.jsonl")
            with open(history, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"metadata": {"ai_reading_event": {"requestId": "old-request"}}}) + "\n")
                for _ in range(300):
                    handle.write(json.dumps({"role": "assistant"}) + "\n")
            self.assertTrue(PushHandler._reading_ai_existing_event(types.SimpleNamespace(path=history), "old-request"))

    def test_path_traversal_and_duplicate_query_are_rejected_before_upstream(self):
        for path in (
            "/reading/books/%2e%2e/chunks",
            "/reading/books/book%2Fother/chunks",
            "/reading/progress?bookId=one&bookId=two",
            "/reading/progress?bookId=one&redirect=https%3A%2F%2Fevil.example",
            "/reading/books?bookId=one",
        ):
            with self.subTest(path=path):
                handler = self.handler(path)
                with patch.object(PushHandler, "_reading_open") as open_request:
                    handler._handle_reading_proxy("GET")
                open_request.assert_not_called()
                self.assertEqual(handler.responses[0][0], 400)

    def test_post_schema_and_size_limits_are_enforced(self):
        malformed = self.handler("/reading/import", body={"filename": "a.txt", "dataBase64": "YQ==", "url": "https://evil"})
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open") as open_request:
            malformed._handle_reading_proxy("POST")
        open_request.assert_not_called()
        self.assertEqual(malformed.responses[0][0], 400)

        too_big = self.handler("/reading/import", body={"filename": "a.txt", "dataBase64": "YQ=="})
        too_big.headers.replace_header("Content-Length", str(PushHandler._READING_IMPORT_REQUEST_LIMIT + 1))
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open") as open_request:
            too_big._handle_reading_proxy("POST")
        open_request.assert_not_called()
        self.assertEqual(too_big.responses[0][0], 413)

        mark = self.handler("/reading/mark-read", body={"bookId": "book_1", "chunkId": "ch_2"})
        captured = {}

        def open_request(request, timeout):
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response(payload={"bookId": "book_1", "lastChunkId": "ch_2"})

        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", staticmethod(open_request)):
            mark._handle_reading_proxy("POST")
        self.assertEqual(captured, {"method": "POST", "body": {"bookId": "book_1", "chunkId": "ch_2"}})
        self.assertEqual(mark.responses[0][0], 200)

    def test_only_import_gets_the_extended_upstream_timeout(self):
        handler = self.handler("/reading/import", body={"filename": "a.txt", "dataBase64": "YQ=="})
        captured = {}

        def open_request(_request, timeout):
            captured["timeout"] = timeout
            return _Response()

        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", staticmethod(open_request)):
            handler._handle_reading_proxy("POST")
        self.assertEqual(captured["timeout"], PushHandler._READING_IMPORT_TIMEOUT_SEC)
        self.assertEqual(PushHandler._READING_TIMEOUT_SEC, 20)

    def test_import_heading_profile_is_mapped_and_untrusted_options_are_rejected(self):
        handler = self.handler("/reading/import", body={
            "filename": "book.txt", "format": "txt", "dataBase64": "YQ==",
            "headingProfile": "auto-v1",
        })
        captured = {}

        def open_request(request, _timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response()

        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", staticmethod(open_request)):
            handler._handle_reading_proxy("POST")
        self.assertEqual(captured["body"]["headingRegex"], PushHandler._READING_AUTO_HEADING_REGEX)
        self.assertEqual(captured["body"]["minSectionChars"], 1)
        for malicious in (
            {"filename": "book.txt", "dataBase64": "YQ==", "headingRegex": ".*"},
            {"filename": "book.txt", "dataBase64": "YQ==", "minSectionChars": 999999},
            {"filename": "book.txt", "dataBase64": "YQ==", "headingProfile": "unknown"},
            {"filename": "book.epub", "format": "epub", "dataBase64": "YQ==", "headingProfile": "auto-v1"},
        ):
            with self.subTest(malicious=malicious):
                rejected = self.handler("/reading/import", body=malicious)
                with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                        patch.object(PushHandler, "_reading_open") as open_request:
                    rejected._handle_reading_proxy("POST")
                open_request.assert_not_called()
                self.assertEqual(rejected.responses[0][0], 400)

    def test_mark_read_keeps_the_normal_timeout(self):
        handler = self.handler("/reading/mark-read", body={"bookId": "book_1", "chunkId": "ch_2"})
        captured = {}

        def open_request(_request, timeout):
            captured["timeout"] = timeout
            return _Response()

        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", staticmethod(open_request)):
            handler._handle_reading_proxy("POST")
        self.assertEqual(captured["timeout"], 20)

    def test_redirect_and_upstream_errors_do_not_leak_response_data(self):
        handler = self.handler("/reading/books")
        redirect = urllib.error.HTTPError("https://reading.xiaonancaleb.xyz/api/books", 302, "Found", {}, None)
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", side_effect=redirect):
            handler._handle_reading_proxy("GET")
        self.assertEqual(handler.responses, [(502, {"error": "reading upstream unavailable"})])

        handler = self.handler("/reading/books/missing/chunks")
        error = urllib.error.HTTPError("https://reading.xiaonancaleb.xyz/api/books/missing/chunks", 404, "Not Found", {"X-Private": "no"}, io.BytesIO(b"<html>secret</html>"))
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", side_effect=error):
            handler._handle_reading_proxy("GET")
        self.assertEqual(handler.responses, [(404, {"error": "reading upstream rejected request"})])

    def test_method_boundaries_and_delete_success(self):
        wrong_method = self.handler("/reading/import")
        with patch.object(PushHandler, "_reading_open") as open_request:
            wrong_method._handle_reading_proxy("GET")
        open_request.assert_not_called()
        self.assertEqual(wrong_method.responses[0][0], 405)

        delete = self.handler("/reading/books/book_1")
        captured = {}

        def open_request(request, timeout):
            captured.update(method=request.get_method(), url=request.full_url)
            return _Response(payload={"deleted": "book_1"})

        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open", staticmethod(open_request)):
            delete._handle_reading_proxy("DELETE")
        self.assertEqual(captured, {"method": "DELETE", "url": "https://reading.xiaonancaleb.xyz/api/books/book_1"})
        self.assertEqual(delete.responses, [(200, {"deleted": "book_1"})])

    def test_rejected_or_unread_request_bodies_close_keep_alive(self):
        get_with_body = self.handler("/reading/books", body=b"x")
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open") as open_request:
            get_with_body._handle_reading_proxy("GET")
        open_request.assert_not_called()
        self.assertTrue(get_with_body.close_connection)
        self.assertEqual(get_with_body.responses[0][0], 400)

        delete = self.handler("/reading/books/book_1", body=b"x")
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open") as open_request:
            delete._handle_reading_proxy("DELETE")
        open_request.assert_not_called()
        self.assertTrue(delete.close_connection)
        self.assertEqual(delete.responses[0][0], 400)

        missing_token = self.handler("/reading/import", body={"filename": "a.txt", "dataBase64": "YQ=="})
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: None)), \
                patch.object(PushHandler, "_reading_open") as open_request:
            missing_token._handle_reading_proxy("POST")
        open_request.assert_not_called()
        self.assertTrue(missing_token.close_connection)
        self.assertEqual(missing_token.responses[0][0], 502)

        bad_type = self.handler("/reading/import", body={"filename": "a.txt", "dataBase64": "YQ=="})
        bad_type.headers.replace_header("Content-Type", "text/plain")
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open") as open_request:
            bad_type._handle_reading_proxy("POST")
        open_request.assert_not_called()
        self.assertTrue(bad_type.close_connection)
        self.assertEqual(bad_type.responses[0][0], 415)

        duplicate_length = self.handler("/reading/mark-read", body={"bookId": "book_1", "chunkId": "ch_2"})
        duplicate_length.headers["Content-Length"] = duplicate_length.headers["Content-Length"]
        with patch.object(PushHandler, "_reading_token", classmethod(lambda cls: "upstream-token")), \
                patch.object(PushHandler, "_reading_open") as open_request:
            duplicate_length._handle_reading_proxy("POST")
        open_request.assert_not_called()
        self.assertTrue(duplicate_length.close_connection)
        self.assertEqual(duplicate_length.responses[0][0], 413)


if __name__ == "__main__":
    unittest.main()
