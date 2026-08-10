"""Regression tests for authenticated all-contact background chat SSE."""
from __future__ import annotations

import threading
import time
import types
import unittest

from chat_history import ChatStreamBus
from push import PushHandler


class _CaptureWriter:
    def __init__(self, stop_when: bytes | None = None):
        self._lock = threading.Lock()
        self.data = bytearray()
        self.stop_when = stop_when
        self.seen = threading.Event()

    def write(self, value: bytes) -> int:
        with self._lock:
            self.data.extend(value)
            if self.stop_when and self.stop_when in self.data:
                self.seen.set()
                # Model a client disconnect immediately after receiving the
                # target frame.  The handler must still unsubscribe in finally.
                raise BrokenPipeError("test disconnect")
        return len(value)

    def flush(self) -> None:
        return None

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self.data)


class BackgroundChatStreamTest(unittest.TestCase):
    CONTACTS = {
        "xiaoke": object(),
        "kairos": object(),
        "kimi": object(),
        "hajiki": object(),
        "apples": object(),
        "toolbot": object(),
        "internal-admin": object(),
    }

    def handler(self, path: str, *, token: str = "") -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
        handler.headers = {"X-Auth-Token": token} if token else {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.state = types.SimpleNamespace(
            shared_secret="test-secret",
            strict_auth=True,
            web_session_enabled=False,
            contact_chats=dict(self.CONTACTS),
            chat_stream_bus=ChatStreamBus(),
        )
        handler.responses = []
        handler.response_headers = []
        handler._send_json = lambda status, payload, **_kwargs: handler.responses.append((status, payload))
        handler.send_response = lambda status: handler.responses.append((status, None))
        handler.send_header = lambda name, value: handler.response_headers.append((name, value))
        handler.end_headers = lambda: None
        return handler

    def test_single_contact_contract_remains_compatible(self):
        handler = self.handler("/chat/stream?contact_id=kairos")
        subscription = handler._chat_stream_subscription()
        self.assertEqual(
            subscription,
            (
                "kairos",
                frozenset({"kairos"}),
                PushHandler._CHAT_STREAM_FOREGROUND_HEARTBEAT_SECONDS,
            ),
        )
        # Legacy clean/fallback semantics stay unchanged for unknown ids.
        handler.path = "/chat/stream?contact_id=not-a-contact"
        self.assertEqual(handler._chat_stream_subscription()[0], "xiaoke")

    def test_wildcard_requires_explicit_auth_and_rejects_ambiguous_target(self):
        missing = self.handler("/chat/stream?contacts=all&heartbeat=background")
        self.assertIsNone(missing._chat_stream_subscription())
        self.assertEqual(missing.responses, [(401, {"error": "unauthorized"})])

        ambiguous = self.handler(
            "/chat/stream?contacts=all&contact_id=kairos",
            token="test-secret",
        )
        self.assertIsNone(ambiguous._chat_stream_subscription())
        self.assertEqual(ambiguous.responses[0][0], 400)

        valid = self.handler(
            "/chat/stream?contacts=all&heartbeat=background",
            token="test-secret",
        )
        target, allowed, heartbeat = valid._chat_stream_subscription()
        self.assertEqual(target, "all")
        self.assertEqual(allowed, PushHandler._CHAT_STREAM_APP_CONTACTS)
        self.assertEqual(allowed, frozenset({"xiaoke", "apples", "kairos", "kimi"}))
        for excluded in ("hajiki", "toolbot", "internal-admin"):
            self.assertNotIn(excluded, allowed)
        self.assertEqual(heartbeat, 25.0)

    def test_heartbeat_profile_is_fixed_not_caller_controlled(self):
        invalid = self.handler(
            "/chat/stream?contacts=all&heartbeat=3600",
            token="test-secret",
        )
        self.assertIsNone(invalid._chat_stream_subscription())
        self.assertEqual(invalid.responses[0][0], 400)

        foreground = self.handler(
            "/chat/stream?contacts=all&heartbeat=foreground",
            token="test-secret",
        )
        self.assertEqual(foreground._chat_stream_subscription()[2], 1.0)

    def test_background_wait_wakes_immediately_and_filters_allowlist_and_private_fields(self):
        handler = self.handler(
            "/chat/stream?contacts=all&heartbeat=background",
            token="test-secret",
        )
        writer = _CaptureWriter(stop_when=b'"text": "visible"')
        handler.wfile = writer
        thread = threading.Thread(target=handler._handle_chat_stream, daemon=True)
        started = time.monotonic()
        thread.start()
        deadline = time.monotonic() + 1.0
        while handler.state.chat_stream_bus.subscriber_count() != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(handler.state.chat_stream_bus.subscriber_count(), 1)

        # A future internal contact and unknown event kind are never public.
        handler.state.chat_stream_bus.publish({
            "event": "draft",
            "contact_id": "internal-admin",
            "text": "sensitive-internal",
        })
        handler.state.chat_stream_bus.publish({
            "event": "draft",
            "contact_id": "hajiki",
            "text": "hajiki-background-excluded",
        })
        handler.state.chat_stream_bus.publish({
            "event": "draft",
            "contact_id": "toolbot",
            "text": "toolbot-background-excluded",
        })
        handler.state.chat_stream_bus.publish({
            "event": "runner_debug",
            "contact_id": "kairos",
            "prompt": "sensitive-prompt",
        })
        handler.state.chat_stream_bus.publish({
            "event": "draft",
            "contact_id": "kairos",
            "turn_id": "turn-1",
            "reply_state": "generating",
            "revision": 2,
            "updated_at": "now",
            "text": "visible",
            "activity_text": "run /private/cmd --token=sensitive-activity",
            "source": "private-source",
            "session_id": "/private/session",
            "prompt": "sensitive-prompt",
            "worker_activity_items": [{
                "worker_id": "reviewer",
                "name": "reviewer",
                "status": "running",
                "count": 1,
                "task": "sensitive-worker-task",
            }],
        })

        self.assertTrue(writer.seen.wait(0.5), "event waited for the 25s heartbeat")
        self.assertLess(time.monotonic() - started, 1.0)
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(handler.state.chat_stream_bus.subscriber_count(), 0)

        wire = writer.snapshot().decode("utf-8")
        self.assertIn('"contacts": "all"', wire)
        self.assertIn('"text": "visible"', wire)
        self.assertIn("正在处理（详情已隐藏）", wire)
        self.assertIn('"worker_id": "reviewer"', wire)
        self.assertNotIn('"task"', wire)
        for forbidden in (
            "sensitive-internal", "hajiki-background-excluded",
            "toolbot-background-excluded", "sensitive-prompt", "sensitive-worker-task",
            "sensitive-activity", "private-source", "/private/session", "/private/cmd",
        ):
            self.assertNotIn(forbidden, wire)

    def test_single_contact_stream_does_not_receive_other_contact(self):
        handler = self.handler("/chat/stream?contact_id=kairos", token="test-secret")
        writer = _CaptureWriter(stop_when=b'"text": "mine"')
        handler.wfile = writer
        thread = threading.Thread(target=handler._handle_chat_stream, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1.0
        while handler.state.chat_stream_bus.subscriber_count() != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        handler.state.chat_stream_bus.publish({
            "event": "chunk", "stream_id": "other", "contact_id": "xiaoke",
            "text": "not-mine", "ts": "now",
        })
        handler.state.chat_stream_bus.publish({
            "event": "chunk", "stream_id": "own", "contact_id": "kairos",
            "text": "mine", "ts": "now",
        })
        self.assertTrue(writer.seen.wait(0.5))
        thread.join(1.0)
        wire = writer.snapshot().decode("utf-8")
        self.assertIn('"contact_id": "kairos"', wire)
        self.assertNotIn("not-mine", wire)
        self.assertIn(("X-Accel-Buffering", "no"), handler.response_headers)
        self.assertEqual(handler.state.chat_stream_bus.subscriber_count(), 0)

    def test_bus_queue_is_bounded_for_slow_clients(self):
        bus = ChatStreamBus()
        q = bus.subscribe()
        for seq in range(80):
            bus.publish({"seq": seq})
        self.assertEqual(len(q), 50)
        self.assertEqual(q[0]["seq"], 30)
        self.assertEqual(q[-1]["seq"], 79)
        bus.unsubscribe(q)
        self.assertEqual(bus.subscriber_count(), 0)


if __name__ == "__main__":
    unittest.main()
