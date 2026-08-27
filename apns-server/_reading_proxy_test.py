"""Focused security tests for the constrained Co-Reading /reading proxy."""

import io
import json
import types
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

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
