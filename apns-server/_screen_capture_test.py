#!/usr/bin/env python3
"""Call screen-share mailbox, WS routing and CLI (2026-10-21)."""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import call_screenshot
import push
import screen_capture as sc
import voice_call_ws as vws


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class MailboxTest(unittest.TestCase):
    def test_request_is_consumed_once_and_expires(self) -> None:
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            box = sc.Mailbox(tmp, clock=clock)
            rid = box.new_request()
            self.assertTrue(sc.is_valid_request_id(rid))
            self.assertEqual(box.take_requests(), [rid])
            self.assertEqual(box.take_requests(), [])
            box.new_request()
            clock.now += sc.REQUEST_TTL_SEC + 1
            self.assertEqual(box.take_requests(), [])

    def test_pending_claim_is_one_shot_and_fresh_only(self) -> None:
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            box = sc.Mailbox(tmp, clock=clock)
            rid = box.new_request()
            self.assertFalse(box.claim_pending(rid))  # never dispatched
            box.mark_pending(rid)
            self.assertTrue(box.claim_pending(rid))
            self.assertFalse(box.claim_pending(rid))
            box.mark_pending(rid)
            clock.now += sc.PENDING_TTL_SEC + 1
            self.assertFalse(box.claim_pending(rid))
            self.assertFalse(box.claim_pending("../../etc/passwd"))

    def test_result_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            box = sc.Mailbox(tmp)
            rid = box.new_request()
            self.assertIsNone(box.poll_result(rid))
            box.write_error(rid, "not_authorized")
            self.assertEqual(box.poll_result(rid)["error"], "not_authorized")
            self.assertIsNone(box.poll_result(rid))


def fake_session(*, opened: float, caps=(sc.CAPABILITY,), share=True, started=True, closed=False):
    return SimpleNamespace(
        started=started,
        capabilities=frozenset(caps),
        screen_share_active=share,
        opened_at=opened,
        session_id=f"s{opened}",
        ws=SimpleNamespace(close_code=1000 if closed else None),
    )


class RoutingTest(unittest.TestCase):
    def test_pick_session(self) -> None:
        self.assertEqual(vws.pick_screen_capture_session([]), (None, "no_active_call"))
        old_client = fake_session(opened=1, caps=())
        self.assertEqual(vws.pick_screen_capture_session([old_client]), (None, "no_active_call"))
        no_grant = fake_session(opened=2, share=False)
        self.assertEqual(vws.pick_screen_capture_session([no_grant]), (None, "not_authorized"))
        closed = fake_session(opened=5, closed=True)
        good = fake_session(opened=3)
        newer = fake_session(opened=4)
        self.assertIs(vws.pick_screen_capture_session([good, newer, closed, no_grant])[0], newer)

    def test_dispatch_sends_frame_and_marks_pending(self) -> None:
        sent = []

        async def fake_send(ws, payload):
            sent.append(payload)

        with tempfile.TemporaryDirectory() as tmp:
            box = sc.Mailbox(tmp)
            rid = box.new_request()
            sess = fake_session(opened=1)
            with patch.object(vws, "ACTIVE_SESSIONS", [sess]), patch.object(vws, "send_json", fake_send):
                asyncio.run(vws.dispatch_screen_capture_requests(box))
            self.assertEqual(sent, [{"type": "screen_capture_request", "request_id": rid}])
            self.assertTrue(box.claim_pending(rid))

            rid2 = box.new_request()
            with patch.object(vws, "ACTIVE_SESSIONS", []):
                asyncio.run(vws.dispatch_screen_capture_requests(box))
            self.assertEqual(box.poll_result(rid2)["error"], "no_active_call")

    def test_capability_is_advertised(self) -> None:
        self.assertIn(sc.CAPABILITY, vws.SERVER_CAPABILITIES)


class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class UploadEndpointTest(unittest.TestCase):
    def _upload(self, root: str, attachments: str, rid: str, body: bytes, authed=True):
        handler = object.__new__(push.PushHandler)
        handler.path = f"/voice-call/screen-capture?request_id={rid}"
        handler.headers = _Headers({"Content-Length": str(len(body))})
        handler.rfile = io.BytesIO(body)
        handler.state = SimpleNamespace(attachments_dir=attachments)
        responses = []
        handler._send_json = lambda status, payload: responses.append((status, payload))
        handler._native_pairing_auth_matches = lambda: authed
        with patch.object(push.PushHandler, "_SCREEN_CAPTURE_ROOT", Path(root)):
            handler._handle_voice_screen_capture_upload()
        return responses[-1]

    def test_only_dispatched_requests_are_accepted_once(self) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as att:
            box = sc.Mailbox(root)
            rid = box.new_request()
            self.assertEqual(self._upload(root, att, rid, jpeg)[0], 409)
            box.mark_pending(rid)
            self.assertEqual(self._upload(root, att, rid, jpeg, authed=False)[0], 401)
            self.assertEqual(self._upload(root, att, rid, jpeg)[0], 200)
            result = box.poll_result(rid)
            self.assertTrue(result["ok"])
            saved = Path(result["path"])
            self.assertEqual(saved.read_bytes(), jpeg)
            self.assertEqual(saved.parent, Path(att).resolve())
            self.assertEqual(self._upload(root, att, rid, jpeg)[0], 409)


class CliTest(unittest.TestCase):
    def test_success_error_and_timeout(self) -> None:
        clock = Clock()

        def sleep(sec: float) -> None:
            clock.now += sec

        with tempfile.TemporaryDirectory() as tmp:
            box = sc.Mailbox(tmp)
            real_new = box.new_request

            def new_with_result(payload):
                def _new():
                    rid = real_new()
                    box.write_result(rid, payload)
                    return rid
                return _new

            box.new_request = new_with_result({"ok": True, "path": "/x/y.jpg"})
            self.assertEqual(call_screenshot.run(box, 10, sleep=sleep, clock=clock), (0, "/x/y.jpg"))
            box.new_request = new_with_result({"ok": False, "error": "not_authorized"})
            code, text = call_screenshot.run(box, 10, sleep=sleep, clock=clock)
            self.assertEqual(code, 3)
            self.assertIn("not_authorized", text)
            box.new_request = real_new
            code, text = call_screenshot.run(box, 2, sleep=sleep, clock=clock)
            self.assertEqual(code, 4)
            self.assertEqual(list(Path(tmp, "requests").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
