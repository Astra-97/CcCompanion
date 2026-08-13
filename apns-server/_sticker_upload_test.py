import io
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from push import PushHandler


class _Catalog:
    def __init__(self, categories=None, stickers=None):
        self.invalidated = False; self.categories = categories or [{"id": "cats", "name": "猫猫"}]; self.stickers = stickers or []
    def invalidate(self): self.invalidated = True
    def snapshot(self):
        if not self.invalidated:
            return {"categories": self.categories, "stickers": self.stickers}
        return {"categories": self.categories, "stickers": [{"name": "团团", "category_id": "cats", "url": "https://assets.example/user-stickers/%E5%9B%A2%E5%9B%A2.png"}]}


class StickerUploadRouteTests(unittest.TestCase):
    def handler(self, headers, body=b""):
        item = object.__new__(PushHandler)
        item.headers = headers; item.path = "/stickers/upload?name=%E5%9B%A2%E5%9B%A2&filename=in.png&category_id=cats"
        item.rfile = io.BytesIO(body); item.state = SimpleNamespace(shared_secret="native", sticker_catalog=_Catalog(), sticker_upload_command=["fixed", "helper"])
        item.close_connection = False; item._send_json = MagicMock()
        return item

    def test_rejects_before_reading_body_without_native_token(self):
        handler = self.handler({"Content-Length": "3", "Content-Type": "image/png"})
        guarded = MagicMock(side_effect=AssertionError("body must not be read")); handler.rfile = guarded
        PushHandler._handle_sticker_upload(handler)
        handler._send_json.assert_called_once_with(401, {"ok": False, "error": "unauthorized"})

    def test_chunked_and_oversized_are_rejected_before_reading_body(self):
        for headers, status in (({"X-Auth-Token": "native", "Transfer-Encoding": "chunked"}, 400), ({"X-Auth-Token": "native", "Content-Length": str(8 * 1024 * 1024 + 1)}, 413)):
            handler = self.handler(headers); handler.rfile = MagicMock(side_effect=AssertionError("body must not be read"))
            PushHandler._handle_sticker_upload(handler)
            self.assertEqual(status, handler._send_json.call_args.args[0])
            self.assertTrue(handler.close_connection)

    def test_all_prebody_rejections_close_keepalive(self):
        cases = [
            ({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "text/plain"}, 415),
            ({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"}, 503),
        ]
        for headers, status in cases:
            handler = self.handler(headers); handler.rfile = MagicMock(side_effect=AssertionError("body must not be read"))
            if status == 503: handler.state.sticker_upload_command = None
            PushHandler._handle_sticker_upload(handler)
            self.assertEqual(status, handler._send_json.call_args.args[0]); self.assertTrue(handler.close_connection)

    def test_global_duplicate_and_category_name_conflict_do_not_read_body(self):
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"})
        handler.state.sticker_catalog = _Catalog(stickers=[{"name": "团团"}]); handler.rfile = MagicMock(side_effect=AssertionError("body must not be read"))
        PushHandler._handle_sticker_upload(handler)
        self.assertEqual(409, handler._send_json.call_args.args[0]); self.assertTrue(handler.close_connection)
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"})
        handler.path = "/stickers/upload?name=%E5%9B%A2%E5%9B%A2&filename=in.png&new_category_name=%E7%8C%AB%E7%8C%AB"; handler.rfile = MagicMock(side_effect=AssertionError("body must not be read"))
        PushHandler._handle_sticker_upload(handler)
        self.assertEqual(409, handler._send_json.call_args.args[0]); self.assertTrue(handler.close_connection)

    def test_filename_limits_and_controls_are_prebody_rejections(self):
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"})
        handler.path = "/stickers/upload?name=%E5%9B%A2%E5%9B%A2&filename=" + ("a" * 241) + ".png"; handler.rfile = MagicMock(side_effect=AssertionError("body must not be read"))
        PushHandler._handle_sticker_upload(handler)
        self.assertEqual(400, handler._send_json.call_args.args[0]); self.assertTrue(handler.close_connection)

    def test_fixed_argv_stdin_frame_and_catalog_url_response(self):
        body = b"abc"
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"}, body)
        with patch.object(PushHandler, "_run_sticker_import_bounded", return_value=(0, b'{"ok":true}')) as run:
            PushHandler._handle_sticker_upload(handler)
        self.assertEqual(["fixed", "helper"], run.call_args.args[0])
        self.assertIn(body, run.call_args.args[1])
        self.assertEqual(200, handler._send_json.call_args.args[0])
        self.assertEqual("https://assets.example/user-stickers/%E5%9B%A2%E5%9B%A2.png", handler._send_json.call_args.args[1]["sticker"]["url"])

    def test_oversize_helper_output_is_bounded_and_rejected(self):
        handler = self.handler({})
        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "survived"
            command = [sys.executable, "-c", "import pathlib,sys,time;sys.stdout.buffer.write(b'x'*20000);sys.stdout.flush();time.sleep(1);pathlib.Path(sys.argv[1]).write_text('alive')", str(marker)]
            started = time.monotonic()
            with self.assertRaises(ValueError): handler._run_sticker_import_bounded(command, b"frame")
            self.assertLess(time.monotonic() - started, 1.0)
            time.sleep(0.1)
            self.assertFalse(marker.exists())

    def test_helper_duplicate_race_maps_to_conflict(self):
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"}, b"abc")
        with patch.object(PushHandler, "_run_sticker_import_bounded", return_value=(2, b'{"ok":false,"error":"sticker already exists"}')):
            PushHandler._handle_sticker_upload(handler)
        self.assertEqual(409, handler._send_json.call_args.args[0])

    def test_body_socket_timeout_closes_and_restores_connection_timeout(self):
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"})
        handler.rfile = MagicMock(); handler.rfile.read1.side_effect = TimeoutError("slow")
        connection = MagicMock(); connection.gettimeout.return_value = 42.0; handler.connection = connection
        PushHandler._handle_sticker_upload(handler)
        self.assertEqual(408, handler._send_json.call_args.args[0]); self.assertTrue(handler.close_connection)
        self.assertEqual(42.0, connection.settimeout.call_args_list[-1].args[0])

    def test_slow_drip_cannot_extend_total_deadline(self):
        handler = self.handler({"X-Auth-Token": "native", "Content-Length": "3", "Content-Type": "image/png"})
        handler.rfile = MagicMock(); handler.rfile.read1.return_value = b"x"
        connection = MagicMock(); connection.gettimeout.return_value = None; handler.connection = connection
        with patch("push.time.monotonic", side_effect=[0.0, 1.0, 31.0]):
            PushHandler._handle_sticker_upload(handler)
        self.assertEqual(408, handler._send_json.call_args.args[0])
        self.assertEqual(1, handler.rfile.read1.call_count)

    def test_real_socket_body_read_uses_remaining_deadline(self):
        receiver, sender = socket.socketpair()
        try:
            sender.sendall(b"x")
            handler = self.handler({"X-Auth-Token": "native", "Content-Length": "2", "Content-Type": "image/png"})
            handler.connection = receiver; handler.rfile = receiver.makefile("rb")
            handler._STICKER_UPLOAD_READ_TIMEOUT_SECONDS = 0.05
            handler._STICKER_UPLOAD_READ_DEADLINE_SECONDS = 0.05
            PushHandler._handle_sticker_upload(handler)
            self.assertEqual(408, handler._send_json.call_args.args[0])
        finally:
            try: handler.rfile.close()
            except Exception: pass
            receiver.close(); sender.close()

    def test_real_socket_continuous_drip_is_cut_off_by_total_deadline(self):
        receiver, sender = socket.socketpair()
        stop = threading.Event()
        sent = []
        def drip():
            while not stop.wait(0.01):
                try:
                    sender.sendall(b"x"); sent.append(1)
                except OSError:
                    return
        worker = threading.Thread(target=drip, daemon=True); worker.start()
        try:
            handler = self.handler({"X-Auth-Token": "native", "Content-Length": "100", "Content-Type": "image/png"})
            handler.connection = receiver; handler.rfile = receiver.makefile("rb")
            handler._STICKER_UPLOAD_READ_TIMEOUT_SECONDS = 0.04
            handler._STICKER_UPLOAD_READ_DEADLINE_SECONDS = 0.09
            started = time.monotonic(); PushHandler._handle_sticker_upload(handler); elapsed = time.monotonic() - started
            self.assertEqual(408, handler._send_json.call_args.args[0])
            self.assertGreaterEqual(len(sent), 3)
            self.assertLess(elapsed, 0.3)
        finally:
            stop.set(); worker.join(1)
            try: handler.rfile.close()
            except Exception: pass
            receiver.close(); sender.close()


if __name__ == "__main__":
    unittest.main()
