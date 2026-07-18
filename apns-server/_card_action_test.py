"""互动卡片地基测试 (2026-07-18, app v1.9.79 前置).

覆盖:
  1. GET /attachments/<file>.html|.htm|.json|.csv 的 MIME 推断 + nosniff header
  2. /chat/append metadata.card_title 原样入库 + poll (read_since) 透传 + 轻量归一
  3. POST /chat/card_action 与 /chat/send 完全同管线 (入库/turn_token/注入/busy 排队)
  4. /chat/card_action 无鉴权拒绝 (401, 语义同 /chat/append) + 带 token 正常路由

全部直接调 handler, 不碰线上 8291 服务。
"""
import io
import json
import re
import tempfile
import threading
import types
import unittest
from pathlib import Path

from chat_history import ChatHistory
from push import PushHandler, TmuxInjectionResult, _ATTACHMENT_MIME_MAP


class AttachmentMimeTest(unittest.TestCase):
    def serve(self, filename: str, content: bytes = b"<h1>hi</h1>") -> "types.SimpleNamespace":
        handler = object.__new__(PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        attachments = Path(tmp.name)
        (attachments / filename).write_bytes(content)
        handler.state = types.SimpleNamespace(attachments_dir=attachments)
        handler.path = f"/attachments/{filename}"
        result = types.SimpleNamespace(status=None, headers={}, body=io.BytesIO(), json=None)
        handler.send_response = lambda status: setattr(result, "status", status)
        handler.send_header = lambda k, v: result.headers.__setitem__(k, v)
        handler.end_headers = lambda: None
        handler.wfile = result.body
        handler._send_json = lambda status, payload: (
            setattr(result, "status", status), setattr(result, "json", payload)
        )
        handler._handle_attachment_get()
        return result

    def test_html_served_as_text_html_utf8(self) -> None:
        result = self.serve("abc123.html")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(result.body.getvalue(), b"<h1>hi</h1>")

    def test_htm_served_as_text_html_utf8(self) -> None:
        result = self.serve("abc123.htm")
        self.assertEqual(result.headers["Content-Type"], "text/html; charset=utf-8")

    def test_json_and_csv_mimes(self) -> None:
        self.assertEqual(self.serve("a.json", b"{}").headers["Content-Type"], "application/json")
        self.assertEqual(self.serve("a.csv", b"x,y").headers["Content-Type"], "text/csv")

    def test_existing_types_unchanged_and_unknown_still_octet_stream(self) -> None:
        self.assertEqual(self.serve("a.png", b"p").headers["Content-Type"], "image/png")
        self.assertEqual(self.serve("a.md", b"m").headers["Content-Type"], "text/markdown")
        self.assertEqual(
            self.serve("a.bin", b"b").headers["Content-Type"], "application/octet-stream"
        )

    def test_nosniff_header_present(self) -> None:
        result = self.serve("abc123.html")
        self.assertEqual(result.headers.get("X-Content-Type-Options"), "nosniff")

    def test_head_shares_same_mime_map(self) -> None:
        # HEAD handler 和 GET handler 共用 _ATTACHMENT_MIME_MAP — 防止两张表再漂移
        self.assertEqual(_ATTACHMENT_MIME_MAP[".html"], "text/html; charset=utf-8")
        self.assertEqual(_ATTACHMENT_MIME_MAP[".htm"], "text/html; charset=utf-8")
        self.assertEqual(_ATTACHMENT_MIME_MAP[".json"], "application/json")
        self.assertEqual(_ATTACHMENT_MIME_MAP[".csv"], "text/csv")


class CardTitleAppendTest(unittest.TestCase):
    """metadata.card_title 原样入库 + read_since (poll/history 数据源) 透传."""

    def append_handler(self) -> PushHandler:
        handler = object.__new__(PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = ChatHistory(Path(tmp.name) / "history.jsonl")
        handler.state = types.SimpleNamespace(
            shared_secret="",  # _auth_matches → True, 独立于外层 do_POST 鉴权
            strict_auth=True,
            contact_chats={"xiaoke": chat},
            attachments_dir=Path(tmp.name),
            settings={},
            tokens=types.SimpleNamespace(all_active=lambda: []),
            apns_enabled=False,
        )
        handler.headers = {}
        handler._chat_for_contact = lambda _contact: chat
        handler._source_for_request = lambda *a: "claude-code"
        handler._has_pending_group_reply = lambda: False
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.chat = chat
        return handler

    def test_card_title_stored_verbatim_and_round_trips_via_read_since(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": "claude-code",
            "text": "",
            "attachment_url": "/attachments/deadbeef.html",
            "attachment_type": "file",
            "attachment_filename": "点餐卡.html",
            "metadata": {"card_title": "今晚吃什么", "extra": "kept"},
        })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        rec = payload["record"]
        self.assertEqual(rec["metadata"]["card_title"], "今晚吃什么")
        self.assertEqual(rec["metadata"]["extra"], "kept")
        # poll/history 都从 read_since 出 record 原文 — 透传证明
        stored = handler.chat.read_since()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["metadata"], {"card_title": "今晚吃什么", "extra": "kept"})
        self.assertEqual(stored[0]["attachment_url"], "/attachments/deadbeef.html")

    def test_card_title_truncated_to_200_chars(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "text": "",
            "attachment_url": "/attachments/longtitle.html",
            "metadata": {"card_title": "  " + "标" * 500},
        })
        rec = handler.responses[-1][1]["record"]
        self.assertEqual(rec["metadata"]["card_title"], "标" * 200)

    def test_non_string_card_title_dropped_other_metadata_kept(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "text": "",
            "attachment_url": "/attachments/badtitle.html",
            "metadata": {"card_title": 123, "other": "x"},
        })
        rec = handler.responses[-1][1]["record"]
        self.assertEqual(rec["metadata"], {"other": "x"})

    def test_only_invalid_card_title_leaves_no_metadata(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "text": "",
            "attachment_url": "/attachments/onlybad.html",
            "metadata": {"card_title": "   "},
        })
        rec = handler.responses[-1][1]["record"]
        self.assertNotIn("metadata", rec)

    def test_plain_attachment_without_metadata_unchanged(self) -> None:
        handler = self.append_handler()
        handler._handle_chat_append({
            "contact_id": "xiaoke",
            "role": "assistant",
            "text": "",
            "attachment_url": "/attachments/plainfile.md",
        })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertNotIn("metadata", payload["record"])


class CardActionTest(unittest.TestCase):
    """/chat/card_action 委托 /chat/send: 同入库、同 turn_token 登记、同注入管线."""

    INJECTED_RE = r"^\[CCC_APP_TURN:[0-9a-f]{32}:cctg\]\n\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "

    def send_handler(self, *, active: bool = False) -> PushHandler:
        handler = object.__new__(PushHandler)
        typing = {
            "is_typing": active,
            "since": "turn-0" if active else None,
            "session": "cctg" if active else "",
            "transport": "tmux" if active else "",
            "turn_token": "a" * 32 if active else "",
        }
        handler.append_calls = []
        handler.injection_calls = []
        handler.channel_calls = []
        handler.responses = []
        handler.state = types.SimpleNamespace(
            allow_remote_control=True,
            active_session="cctg",
            default_session="cctg",
            typing_state=typing,
            contact_typing_states={"xiaoke": typing},
            xiaoke_stop_lock=threading.RLock(),
            xiaoke_stop_tombstone={},
            xiaoke_stopping_claim={},
            xiaoke_send_reservation={},
            contact_chats={"xiaoke": object()},
            channel_transport_enabled=False,
            channel_transport_contacts=["xiaoke"],
            settings={},
        )
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(
            append=lambda **record: handler.append_calls.append(record) or {**record, "ts": "turn-1"}
        )
        handler._source_for_request = lambda *a: ("android-app:" + a[0]) if a else "android-app"
        handler._channel_transport_enabled_for = lambda _contact: False
        handler._send_to_channel_transport = lambda **kwargs: (
            handler.channel_calls.append(kwargs) or (True, "", {"queued": True})
        )
        handler._inject_to_session = lambda *args, **kwargs: (
            handler.injection_calls.append((args, kwargs)) or TmuxInjectionResult(True)
        )
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    def test_card_action_appends_user_record_with_via_card_metadata(self) -> None:
        handler = self.send_handler()
        handler._handle_chat_card_action({
            "contact_id": "xiaoke", "text": "RESULT:gomoku:3,4", "card": "gomoku.html",
        })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(handler.append_calls), 1)
        appended = handler.append_calls[0]
        self.assertEqual(appended["role"], "user")
        self.assertEqual(appended["text"], "RESULT:gomoku:3,4")
        self.assertEqual(appended["metadata"], {"via": "card", "card": "gomoku.html"})
        self.assertEqual(appended["source"], "android-app:card")

    def test_card_action_injects_and_registers_turn_like_chat_send(self) -> None:
        handler = self.send_handler()
        handler._handle_chat_card_action({
            "contact_id": "xiaoke", "text": "RESULT:42", "card": "quiz.html",
        })
        # 注入了 tmux, 文本带 CCC_APP_TURN marker (与 /chat/send 完全同格式)
        self.assertEqual(len(handler.injection_calls), 1)
        args, kwargs = handler.injection_calls[0]
        self.assertEqual(args[0], "cctg")
        self.assertRegex(args[1], self.INJECTED_RE + r"RESULT:42$")
        self.assertTrue(kwargs["force_direct_tmux"])
        # turn_token 登记: typing_state 里的 token == 注入 marker 里的 token
        token = re.match(r"^\[CCC_APP_TURN:([0-9a-f]{32}):cctg\]", args[1]).group(1)
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertEqual(handler.state.typing_state["turn_token"], token)
        # 响应带 turn 对象 (Stop 按钮 / typing 状态机可用)
        turn = handler.responses[-1][1]["turn"]
        self.assertEqual(turn, {
            "contact_id": "xiaoke",
            "user_ts": "turn-1",
            "session": "cctg",
            "transport": "tmux",
        })

    def test_card_action_response_shape_matches_chat_send(self) -> None:
        card = self.send_handler()
        card._handle_chat_card_action({"contact_id": "xiaoke", "text": "hello", "card": "c.html"})
        plain = self.send_handler()
        plain._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})
        card_status, card_payload = card.responses[-1]
        plain_status, plain_payload = plain.responses[-1]
        self.assertEqual(card_status, plain_status)
        self.assertEqual(set(card_payload), set(plain_payload))
        self.assertEqual(card_payload["turn"], plain_payload["turn"])
        # append 除 metadata/source 标记外与 /chat/send 完全一致
        card_append = dict(card.append_calls[0])
        plain_append = dict(plain.append_calls[0])
        card_append.pop("metadata"); card_append.pop("source")
        plain_append.pop("metadata"); plain_append.pop("source")
        self.assertEqual(card_append, plain_append)
        self.assertIsNone(plain.append_calls[0]["metadata"])  # 普通 send 不带 metadata, 行为不变
        # 注入文本除随机 token/时间戳外一致
        strip = lambda s: re.sub(self.INJECTED_RE, "", s)
        self.assertEqual(
            strip(card.injection_calls[0][0][1]), strip(plain.injection_calls[0][0][1])
        )

    def test_card_action_empty_text_400_without_append_or_injection(self) -> None:
        handler = self.send_handler()
        handler._handle_chat_card_action({"contact_id": "xiaoke", "text": "  ", "card": "c.html"})
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(handler.append_calls, [])
        self.assertEqual(handler.injection_calls, [])
        self.assertEqual(handler.state.xiaoke_send_reservation, {})

    def test_card_action_busy_turn_queues_via_channel_with_metadata(self) -> None:
        handler = self.send_handler(active=True)
        handler.state.channel_transport_enabled = True
        handler._channel_transport_enabled_for = lambda _contact: True
        typing_before = dict(handler.state.typing_state)
        handler._handle_chat_card_action({
            "contact_id": "xiaoke", "text": "RESULT:late", "card": "quiz.html",
        })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["transport"], "channel")
        self.assertEqual(len(handler.append_calls), 1)
        self.assertEqual(
            handler.append_calls[0]["metadata"], {"via": "card", "card": "quiz.html"}
        )
        self.assertEqual(len(handler.channel_calls), 1)
        self.assertEqual(handler.channel_calls[0]["text"], "RESULT:late")
        # busy 排队不接管 turn (与 /chat/send 一致)
        self.assertEqual(handler.injection_calls, [])
        self.assertEqual(handler.state.typing_state, typing_before)

    def test_card_name_truncated_and_optional(self) -> None:
        handler = self.send_handler()
        handler._handle_chat_card_action({
            "contact_id": "xiaoke", "text": "ok", "card": "x" * 500,
        })
        self.assertEqual(handler.append_calls[0]["metadata"]["card"], "x" * 200)
        handler2 = self.send_handler()
        handler2._handle_chat_card_action({"contact_id": "xiaoke", "text": "ok"})
        self.assertEqual(handler2.append_calls[0]["metadata"], {"via": "card"})


class CardActionAuthRoutingTest(unittest.TestCase):
    """do_POST 层: 无 token 401 (语义同 /chat/append), 有 token 正常路由到 handler."""

    def post_handler(self, headers: dict[str, str]) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            allowed_ips=[],
            shared_secret="test-secret",
            strict_auth=True,
        )
        handler.path = "/chat/card_action"
        handler.headers = headers
        handler.responses = []
        handler.routed = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._read_body = lambda: {"contact_id": "xiaoke", "text": "ok", "card": "c.html"}
        handler._handle_chat_card_action = lambda body: handler.routed.append(body)
        return handler

    def test_missing_token_is_rejected_before_handler(self) -> None:
        handler = self.post_handler({})
        handler.do_POST()
        self.assertEqual(handler.responses[-1][0], 401)
        self.assertEqual(handler.routed, [])

    def test_wrong_token_is_rejected_before_handler(self) -> None:
        handler = self.post_handler({"X-Auth-Token": "wrong"})
        handler.do_POST()
        self.assertEqual(handler.responses[-1][0], 401)
        self.assertEqual(handler.routed, [])

    def test_valid_token_routes_to_card_action_handler(self) -> None:
        handler = self.post_handler({"X-Auth-Token": "test-secret"})
        handler.do_POST()
        self.assertEqual(len(handler.routed), 1)
        self.assertEqual(handler.routed[0]["text"], "ok")
        self.assertEqual(handler.routed[0]["card"], "c.html")


if __name__ == "__main__":
    unittest.main()
