#!/usr/bin/env python3
"""Regression tests for token-bound XiaoKe voice-call replies."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import push
from link_preview import LinkPreviewBundle
from push import PushHandler, TmuxInjectionResult
import voice_call_ws as voice_ws
import voice_protocol
from voice_protocol import (
    VOICE_CALL_SOURCE,
    VOICE_INTERNAL_HEADER,
    VOICE_REPLY_SOURCE,
    VOICE_REPLY_TOKEN_FIELD,
    PendingVoiceReplies,
    VoiceReplyNotPending,
    build_voice_reply_instruction,
    load_or_create_voice_internal_token,
    normalize_voice_reply_token,
    parse_voice_reply,
)


TOKEN = "1a" * 16
OLD_TOKEN = "2b" * 16
MARKER = f"[[CCC_VOICE_REPLY:{TOKEN}]]"


class VoiceProtocolUnitTest(unittest.TestCase):
    def test_token_shape_is_strict(self) -> None:
        self.assertEqual(normalize_voice_reply_token(TOKEN), TOKEN)
        for invalid in (
            "",
            TOKEN.upper(),
            TOKEN[:-1],
            TOKEN + "0",
            "g" * 32,
            None,
            123,
        ):
            self.assertEqual(normalize_voice_reply_token(invalid), "")

    def test_marker_must_be_first_and_clean_body_is_returned(self) -> None:
        self.assertEqual(parse_voice_reply(f"{MARKER}\n你好"), ("你好", TOKEN))
        self.assertEqual(parse_voice_reply(f"{MARKER}  你好  "), ("你好", TOKEN))
        embedded = f"前言 {MARKER}\n你好"
        self.assertEqual(parse_voice_reply(embedded), (embedded, ""))
        leading_space = f" \n{MARKER}\n你好"
        self.assertEqual(parse_voice_reply(leading_space), (leading_space, ""))

    def test_empty_formal_body_stays_empty(self) -> None:
        self.assertEqual(parse_voice_reply(f"{MARKER}\n  "), ("", TOKEN))

    def test_instruction_contains_only_valid_exact_marker(self) -> None:
        instruction = build_voice_reply_instruction(TOKEN)
        self.assertIn(MARKER, instruction)
        with self.assertRaises(ValueError):
            build_voice_reply_instruction("not-a-token")

    def test_internal_credential_is_stable_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice-token"
            first = load_or_create_voice_internal_token(path)
            second = load_or_create_voice_internal_token(path)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_voice_ws_uses_internal_header_path_only_for_xiaoke_token_turn(self) -> None:
        with patch.object(
            voice_ws,
            "_request_json",
            return_value={"ok": True, "record": {"ts": "turn"}},
        ) as request:
            self.assertEqual(
                voice_ws._send_live_contact_message("xiaoke", "hello", TOKEN),
                "turn",
            )
        kwargs = request.call_args.kwargs
        self.assertTrue(kwargs["internal_voice"])
        self.assertEqual(kwargs["payload"][VOICE_REPLY_TOKEN_FIELD], TOKEN)
        self.assertNotIn("source", kwargs["payload"])

        with patch.object(
            voice_ws,
            "_request_json",
            return_value={"ok": True, "record": {"ts": "turn-2"}},
        ) as request:
            voice_ws._send_live_contact_message("kairos", "hello", "")
        self.assertFalse(request.call_args.kwargs["internal_voice"])
        self.assertNotIn(VOICE_REPLY_TOKEN_FIELD, request.call_args.kwargs["payload"])

    def test_pending_token_expires_at_ttl(self) -> None:
        pending = PendingVoiceReplies(ttl_seconds=1)
        with patch.object(voice_protocol.time, "monotonic", return_value=10.0):
            pending.register(TOKEN)
            self.assertTrue(pending.is_pending(TOKEN))
        with patch.object(voice_protocol.time, "monotonic", return_value=11.001):
            self.assertFalse(pending.is_pending(TOKEN))
            with self.assertRaises(VoiceReplyNotPending):
                pending.claim_and_run(TOKEN, lambda: None)

    def test_concurrent_double_claim_has_exactly_one_winner(self) -> None:
        pending = PendingVoiceReplies()
        pending.register(TOKEN)
        ready = threading.Barrier(3)
        operations: list[str] = []
        outcomes: list[str] = []

        def contender(name: str) -> None:
            ready.wait()
            try:
                pending.claim_and_run(TOKEN, lambda: operations.append(name))
                outcomes.append("claimed")
            except VoiceReplyNotPending:
                outcomes.append("rejected")

        first = threading.Thread(target=contender, args=("first",))
        second = threading.Thread(target=contender, args=("second",))
        first.start()
        second.start()
        ready.wait()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(outcomes), ["claimed", "rejected"])
        self.assertEqual(len(operations), 1)


class _FakeChat:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    def append(self, **record):
        saved = {**record, "ts": f"ts-{len(self.appended) + 1}"}
        self.appended.append(saved)
        return saved

    def merge_thinking_to_last_assistant(self, *_args) -> bool:
        return False


class PushVoiceProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        PushHandler._chat_append_dedupe_cache = {}

    def _base_handler(self) -> tuple[PushHandler, _FakeChat]:
        handler = object.__new__(PushHandler)
        chat = _FakeChat()
        typing = {"is_typing": False, "since": None}
        handler.state = types.SimpleNamespace(
            settings={},
            default_session="cctg",
            active_session="cctg",
            xiaoke_stop_lock=threading.RLock(),
            xiaoke_stopping_claim={},
            xiaoke_send_reservation={},
            typing_state=typing,
            contact_typing_states={"xiaoke": typing},
            contact_chats={"xiaoke": chat, "kairos": chat},
            tokens=types.SimpleNamespace(all_active=lambda: []),
            tasks=types.SimpleNamespace(snapshot=lambda: {}),
            apns_enabled=False,
            gomoku_msg_cache=OrderedDict(),
            voice_internal_token="internal-only-secret",
            pending_voice_replies=PendingVoiceReplies(),
            chat_stream_bus=types.SimpleNamespace(publish=lambda record: None),
        )
        handler.headers = {}
        handler.responses = []
        handler.injected = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._check_auth = lambda: True
        handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
        handler._source_for_request = lambda *_args: "android-app"
        handler._chat_for_contact = lambda _contact: chat
        handler._enrich_user_links = lambda _text: LinkPreviewBundle()
        handler._channel_transport_enabled_for = lambda _contact: False
        handler._inject_to_session = lambda session, text, **kwargs: (
            handler.injected.append((session, text, kwargs))
            or TmuxInjectionResult(True, "")
        )
        handler._complete_xiaoke_turn_if_match = lambda *_args, **_kwargs: False
        handler._has_pending_group_reply = lambda: False
        handler._send_chat_notification = lambda *_args, **_kwargs: None
        return handler, chat

    def test_send_adds_private_instruction_but_keeps_user_history_clean(self) -> None:
        handler, chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler._handle_chat_send({
            "contact_id": "xiaoke",
            "text": "我想睡觉",
            VOICE_REPLY_TOKEN_FIELD: TOKEN,
            "metadata": {
                VOICE_REPLY_TOKEN_FIELD: "forged",
                "voice_reply_v": 9,
                "voice_reply_text": "forged",
                "kept": "yes",
            },
        })

        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(chat.appended[0]["text"], "我想睡觉")
        self.assertEqual(chat.appended[0]["metadata"], {"kept": "yes"})
        self.assertEqual(chat.appended[0]["source"], VOICE_CALL_SOURCE)
        self.assertTrue(handler.state.pending_voice_replies.is_pending(TOKEN))
        injected = handler.injected[0][1]
        self.assertIn(MARKER, injected)
        self.assertIn("cc-companion channel", injected)

    def test_forged_source_or_malformed_token_never_enables_protocol(self) -> None:
        cases = (
            {"source": "android-app", VOICE_REPLY_TOKEN_FIELD: TOKEN},
            {"source": VOICE_CALL_SOURCE + ":forged", VOICE_REPLY_TOKEN_FIELD: TOKEN},
            {"source": f" {VOICE_CALL_SOURCE} ", VOICE_REPLY_TOKEN_FIELD: TOKEN},
            {"source": VOICE_CALL_SOURCE, VOICE_REPLY_TOKEN_FIELD: TOKEN.upper()},
            {"source": VOICE_CALL_SOURCE, VOICE_REPLY_TOKEN_FIELD: TOKEN[:-1]},
        )
        for body_fields in cases:
            with self.subTest(body_fields=body_fields):
                handler, chat = self._base_handler()
                handler._handle_chat_send({
                    "contact_id": "xiaoke",
                    "text": "普通消息",
                    **body_fields,
                })
                self.assertEqual(handler.responses[-1][0], 200)
                self.assertNotIn("CCC_VOICE_REPLY", handler.injected[0][1])
                self.assertFalse(handler.state.pending_voice_replies.has_pending())
                self.assertEqual(chat.appended[0]["source"], "android-app")

        handler, _chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler._handle_chat_send({
            "contact_id": "xiaoke",
            "text": "bad internal token shape",
            VOICE_REPLY_TOKEN_FIELD: TOKEN.upper(),
        })
        self.assertNotIn("CCC_VOICE_REPLY", handler.injected[0][1])

    @patch("push.threading.Thread")
    def test_append_strips_marker_and_persists_clean_token_binding(self, thread_cls) -> None:
        handler, chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler.state.pending_voice_replies.register(TOKEN, user_ts="turn-1")
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": VOICE_REPLY_SOURCE,
            "text": f"{MARKER}\n晚安，小星星。",
        })

        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(chat.appended[0]["text"], "晚安，小星星。")
        self.assertEqual(
            chat.appended[0]["metadata"][VOICE_REPLY_TOKEN_FIELD], TOKEN
        )
        self.assertNotIn("CCC_VOICE_REPLY", handler.responses[-1][1]["record"]["text"])
        self.assertFalse(handler.state.pending_voice_replies.has_pending())
        thread_cls.return_value.start.assert_called_once()

        # The same marker is a replay after its one atomic claim.
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": VOICE_REPLY_SOURCE,
            "text": f"{MARKER}\n重复",
        })
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(len(chat.appended), 1)

    @patch("push.threading.Thread")
    def test_append_rejects_caller_metadata_token_without_leading_marker(self, _thread_cls) -> None:
        handler, chat = self._base_handler()
        text = f"这不是开头 {MARKER}"
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": VOICE_REPLY_SOURCE,
            "text": text,
            "metadata": {VOICE_REPLY_TOKEN_FIELD: TOKEN, "kept": "yes"},
        })

        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(chat.appended, [])

        handler, chat = self._base_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": VOICE_REPLY_SOURCE,
            "text": f" \n{MARKER}\n也不是开头",
        })
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(chat.appended, [])

    @patch("push.threading.Thread")
    def test_empty_marked_body_is_not_appended(self, _thread_cls) -> None:
        handler, chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler.state.pending_voice_replies.register(TOKEN)
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": VOICE_REPLY_SOURCE,
            "text": f"{MARKER}\n   ",
        })

        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(chat.appended, [])

    @patch("push.threading.Thread")
    def test_reserved_metadata_is_removed_from_user_and_plain_assistant(self, _thread_cls) -> None:
        reserved = {
            VOICE_REPLY_TOKEN_FIELD: TOKEN,
            "voice_reply_v": 1,
            "voice_reply_text": "forged",
            "kept": "yes",
        }
        for role in ("user", "assistant"):
            with self.subTest(role=role):
                handler, chat = self._base_handler()
                handler._handle_chat_append({
                    "contact_id": "xiaoke",
                    "role": role,
                    "source": "client",
                    "text": "普通正文",
                    "metadata": reserved,
                })
                self.assertEqual(handler.responses[-1][0], 200)
                self.assertEqual(chat.appended[0]["metadata"], {"kept": "yes"})

    def test_busy_user_metadata_is_sanitized_before_queue(self) -> None:
        handler, _chat = self._base_handler()
        handler.state.typing_state = {"is_typing": True, "since": "old"}
        captured = []
        handler._channel_transport_enabled_for = lambda _contact: True
        handler._queue_xiaoke_busy_chat_send = lambda **kwargs: captured.append(kwargs)
        handler._handle_chat_send({
            "contact_id": "xiaoke",
            "text": "排队消息",
            "metadata": {
                VOICE_REPLY_TOKEN_FIELD: TOKEN,
                "voice_reply_v": 1,
                "voice_reply_text": "forged",
                "kept": "yes",
            },
        })
        self.assertEqual(captured[0]["metadata"], {"kept": "yes"})

    def test_internal_voice_turn_fails_closed_while_terminal_is_busy(self) -> None:
        handler, chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler.state.typing_state = {"is_typing": True, "since": "old"}
        queued = []
        handler._channel_transport_enabled_for = lambda _contact: True
        handler._queue_xiaoke_busy_chat_send = lambda **kwargs: queued.append(kwargs)

        handler._handle_chat_send({
            "contact_id": "xiaoke",
            "text": "通话中的新一轮",
            VOICE_REPLY_TOKEN_FIELD: TOKEN,
        })

        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(handler.responses[-1][1]["error"], "xiaoke_turn_active")
        self.assertNotIn(TOKEN, str(handler.responses[-1][1]))
        self.assertEqual(queued, [])
        self.assertEqual(chat.appended, [])
        self.assertFalse(handler.state.pending_voice_replies.has_pending())

    @patch("push.threading.Thread")
    def test_wrong_channel_or_old_marker_is_rejected_without_consuming_pending(self, _thread_cls) -> None:
        handler, chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler.state.pending_voice_replies.register(TOKEN)
        for source, token in (("ccc-stop-hook", TOKEN), (VOICE_REPLY_SOURCE, OLD_TOKEN)):
            marker = f"[[CCC_VOICE_REPLY:{token}]]"
            handler._handle_chat_append({
                "contact_id": "xiaoke",
                "role": "assistant",
                "source": source,
                "text": f"{marker}\n不能入库",
            })
            self.assertEqual(handler.responses[-1][0], 409)
            self.assertNotIn(token, str(handler.responses[-1][1]))
        self.assertEqual(chat.appended, [])
        self.assertTrue(handler.state.pending_voice_replies.is_pending(TOKEN))

    @patch("push.threading.Thread")
    def test_captured_marker_and_forged_channel_source_need_internal_header(self, _thread_cls) -> None:
        for header in (None, "wrong-internal-secret"):
            with self.subTest(header=header):
                handler, chat = self._base_handler()
                handler.state.pending_voice_replies.register(TOKEN)
                if header is not None:
                    handler.headers[VOICE_INTERNAL_HEADER] = header
                handler._handle_chat_append({
                    "contact_id": "xiaoke",
                    "role": "assistant",
                    "source": VOICE_REPLY_SOURCE,
                    "text": f"{MARKER}\n抢先伪造",
                })
                self.assertEqual(handler.responses[-1][0], 403)
                self.assertEqual(chat.appended, [])
                self.assertTrue(handler.state.pending_voice_replies.is_pending(TOKEN))
                self.assertNotIn(TOKEN, str(handler.responses[-1][1]))

    def test_internal_cancel_consumes_pending_and_stops_exact_turn(self) -> None:
        handler, _chat = self._base_handler()
        handler.headers[VOICE_INTERNAL_HEADER] = "internal-only-secret"
        handler.state.pending_voice_replies.register(TOKEN, user_ts="voice-user-ts")
        handler.state.typing_state = {
            "is_typing": True,
            "since": "voice-user-ts",
            "session": "cctg",
            "transport": "tmux",
        }
        stopped = []
        handler._handle_chat_stop = lambda body: (
            stopped.append(body),
            handler._send_json(200, {"ok": True, "stopped": True}),
        )

        handler._handle_voice_call_cancel({VOICE_REPLY_TOKEN_FIELD: TOKEN})

        self.assertEqual(handler.responses[-1][0], 200)
        self.assertEqual(stopped[0]["user_ts"], "voice-user-ts")
        self.assertFalse(handler.state.pending_voice_replies.has_pending())
        handler.state.pending_voice_replies.register(OLD_TOKEN, user_ts="next")
        self.assertTrue(handler.state.pending_voice_replies.is_pending(OLD_TOKEN))

    def test_xiaoke_stream_is_suppressed_for_pending_turn_and_late_done(self) -> None:
        handler, _chat = self._base_handler()
        published = []
        handler.state.chat_stream_bus = types.SimpleNamespace(publish=published.append)
        handler.state.pending_voice_replies.register(TOKEN)

        handler._handle_chat_stream_chunk({
            "event": "chunk",
            "stream_id": "voice-stream",
            "contact_id": "xiaoke",
            "text": "private partial",
        })
        self.assertTrue(handler.responses[-1][1]["suppressed"])
        self.assertEqual(published, [])

        handler.state.pending_voice_replies.claim_and_run(TOKEN, lambda: None)
        handler._handle_chat_stream_chunk({
            "event": "done",
            "stream_id": "voice-stream",
            "contact_id": "xiaoke",
            "text": f"{MARKER}\nprivate final",
        })
        self.assertTrue(handler.responses[-1][1]["suppressed"])
        self.assertEqual(published, [])

        handler._handle_chat_stream_chunk({
            "event": "done",
            "stream_id": "kairos-stream",
            "contact_id": "kairos",
            "text": "Kairos normal stream",
        })
        self.assertEqual(published[-1]["contact_id"], "kairos")


class VoiceWaiterRaceTest(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_count(self, values: list[str], expected: int) -> None:
        for _ in range(100):
            if len(values) >= expected:
                return
            await asyncio.sleep(0.01)
        self.fail(f"expected {expected} cleanup calls, got {len(values)}")

    async def test_old_stop_hook_and_stale_or_forged_records_are_ignored(self) -> None:
        sent: list[tuple[str, str, str]] = []
        since_values: list[str] = []
        polls = 0

        def fake_send(contact_id: str, text: str, token: str) -> str:
            sent.append((contact_id, text, token))
            return "user-turn-ts"

        def fake_read(_contact_id: str, since_ts: str) -> list[dict]:
            nonlocal polls
            since_values.append(since_ts)
            polls += 1
            noise = [
                {"role": "assistant", "source": "ccc-stop-hook", "text": "终端自言自语"},
                {
                    "role": "assistant",
                    "source": VOICE_REPLY_SOURCE,
                    "text": "旧轮正式回复",
                    "metadata": {VOICE_REPLY_TOKEN_FIELD: OLD_TOKEN},
                },
                {
                    "role": "assistant",
                    "source": "claude-code",
                    "text": "伪造来源",
                    "metadata": {VOICE_REPLY_TOKEN_FIELD: TOKEN},
                },
                {
                    "role": "assistant",
                    "source": VOICE_REPLY_SOURCE,
                    "text": "",
                    "metadata": {VOICE_REPLY_TOKEN_FIELD: TOKEN},
                },
            ]
            if polls >= 2:
                noise.append({
                    "role": "assistant",
                    "source": VOICE_REPLY_SOURCE,
                    "text": "真正的正式回复",
                    "metadata": {VOICE_REPLY_TOKEN_FIELD: TOKEN},
                })
            return noise

        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", side_effect=fake_send),
            patch.object(voice_ws, "_read_live_contact_records", side_effect=fake_read),
            patch.object(voice_ws, "LIVE_REPLY_POLL_INTERVAL_SEC", 0),
        ):
            reply = await voice_ws.send_live_contact_and_wait_reply("xiaoke", "我想睡觉")

        self.assertEqual(reply, "真正的正式回复")
        self.assertEqual(sent, [("xiaoke", "我想睡觉", TOKEN)])
        self.assertGreaterEqual(polls, 2)
        self.assertEqual(set(since_values), {""})

    async def test_successful_formal_reply_never_cancels_token(self) -> None:
        canceled: list[str] = []
        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", return_value="turn"),
            patch.object(
                voice_ws,
                "_read_live_contact_records",
                return_value=[{
                    "role": "assistant",
                    "source": VOICE_REPLY_SOURCE,
                    "text": "已经正式回复",
                    "metadata": {VOICE_REPLY_TOKEN_FIELD: TOKEN},
                }],
            ),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda token: canceled.append(token),
            ),
        ):
            reply = await voice_ws.send_live_contact_and_wait_reply("xiaoke", "你好")
            await asyncio.sleep(0.05)

        self.assertEqual(reply, "已经正式回复")
        self.assertEqual(canceled, [])

    async def test_websocket_end_current_task_cancel_cleans_exact_token_once(self) -> None:
        interrupt = asyncio.Event()
        read_started = threading.Event()
        release_read = threading.Event()
        canceled: list[str] = []

        def blocked_read(_contact: str, _since: str) -> list[dict]:
            read_started.set()
            release_read.wait(2)
            return []

        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", return_value="turn"),
            patch.object(voice_ws, "_read_live_contact_records", side_effect=blocked_read),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda token: canceled.append(token),
            ),
        ):
            task = asyncio.create_task(
                voice_ws.send_live_contact_and_wait_reply(
                    "xiaoke", "断线测试", interrupt
                )
            )
            self.assertTrue(await asyncio.to_thread(read_started.wait, 0.5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.2)
            await self._wait_for_count(canceled, 1)
            release_read.set()
            await asyncio.sleep(0.05)

        self.assertEqual(canceled, [TOKEN])

    async def test_timeout_cleans_exact_token_once(self) -> None:
        canceled: list[str] = []
        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", return_value="turn"),
            patch.object(voice_ws, "LIVE_REPLY_TIMEOUT_SEC", 0),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda token: canceled.append(token),
            ),
        ):
            with self.assertRaises(TimeoutError):
                await voice_ws.send_live_contact_and_wait_reply("xiaoke", "超时测试")
            await self._wait_for_count(canceled, 1)
            await asyncio.sleep(0.05)

        self.assertEqual(canceled, [TOKEN])

    async def test_history_exception_cleans_exact_token_once(self) -> None:
        canceled: list[str] = []
        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", return_value="turn"),
            patch.object(
                voice_ws,
                "_read_live_contact_records",
                side_effect=RuntimeError("history unavailable"),
            ),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda token: canceled.append(token),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "history unavailable"):
                await voice_ws.send_live_contact_and_wait_reply("xiaoke", "异常测试")
            await self._wait_for_count(canceled, 1)
            await asyncio.sleep(0.05)

        self.assertEqual(canceled, [TOKEN])

    async def test_kairos_keeps_legacy_reply_path_without_voice_token(self) -> None:
        sent = []

        def fake_send(contact_id: str, text: str, token: str) -> str:
            sent.append((contact_id, text, token))
            return "kairos-turn"

        with (
            patch.object(voice_ws, "generate_voice_reply_token") as generate,
            patch.object(voice_ws, "_send_live_contact_message", side_effect=fake_send),
            patch.object(
                voice_ws,
                "_read_live_contact_records",
                return_value=[{
                    "role": "assistant",
                    "source": "codex:kairos",
                    "text": "Kairos 的旧链路回复",
                }],
            ),
        ):
            reply = await voice_ws.send_live_contact_and_wait_reply("kairos", "你好")

        self.assertEqual(reply, "Kairos 的旧链路回复")
        self.assertEqual(sent, [("kairos", "你好", "")])
        generate.assert_not_called()

    async def test_waiter_exits_immediately_when_interrupted_between_polls(self) -> None:
        interrupt = asyncio.Event()
        canceled = threading.Event()

        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", return_value="turn"),
            patch.object(voice_ws, "_read_live_contact_records", return_value=[]),
            patch.object(voice_ws, "LIVE_REPLY_POLL_INTERVAL_SEC", 30),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda _token: canceled.set(),
            ),
        ):
            task = asyncio.create_task(
                voice_ws.send_live_contact_and_wait_reply(
                    "xiaoke", "要打断", interrupt
                )
            )
            await asyncio.sleep(0.02)
            interrupt.set()
            reply = await asyncio.wait_for(task, timeout=0.2)
            self.assertTrue(await asyncio.to_thread(canceled.wait, 0.5))

        self.assertEqual(reply, "")

    async def test_interrupt_does_not_wait_for_blocked_send_http(self) -> None:
        interrupt = asyncio.Event()
        send_started = threading.Event()
        release_send = threading.Event()
        canceled = threading.Event()

        def blocked_send(_contact: str, _text: str, _token: str) -> str:
            send_started.set()
            if not release_send.wait(2):
                raise RuntimeError("test send was not released")
            return "turn"

        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", side_effect=blocked_send),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda _token: canceled.set(),
            ),
        ):
            task = asyncio.create_task(
                voice_ws.send_live_contact_and_wait_reply("xiaoke", "阻塞发送", interrupt)
            )
            self.assertTrue(await asyncio.to_thread(send_started.wait, 0.5))
            interrupt.set()
            reply = await asyncio.wait_for(task, timeout=0.2)
            self.assertEqual(reply, "")
            release_send.set()
            self.assertTrue(await asyncio.to_thread(canceled.wait, 0.5))

    async def test_interrupt_does_not_wait_for_blocked_history_http(self) -> None:
        interrupt = asyncio.Event()
        read_started = threading.Event()
        release_read = threading.Event()
        canceled = threading.Event()

        def blocked_read(_contact: str, _since: str) -> list[dict]:
            read_started.set()
            if not release_read.wait(2):
                raise RuntimeError("test read was not released")
            return []

        with (
            patch.object(voice_ws, "generate_voice_reply_token", return_value=TOKEN),
            patch.object(voice_ws, "_send_live_contact_message", return_value="turn"),
            patch.object(voice_ws, "_read_live_contact_records", side_effect=blocked_read),
            patch.object(
                voice_ws,
                "_cancel_live_voice_reply",
                side_effect=lambda _token: canceled.set(),
            ),
        ):
            task = asyncio.create_task(
                voice_ws.send_live_contact_and_wait_reply("xiaoke", "阻塞读取", interrupt)
            )
            self.assertTrue(await asyncio.to_thread(read_started.wait, 0.5))
            interrupt.set()
            reply = await asyncio.wait_for(task, timeout=0.2)
            self.assertEqual(reply, "")
            self.assertTrue(await asyncio.to_thread(canceled.wait, 0.5))
            release_read.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
