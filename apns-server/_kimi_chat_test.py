import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from chat_history import ChatStreamBus
from kimi_acp import KimiACPCancelled, KimiACPError
from link_preview import LinkPreviewBundle
from kimi_web_client import KimiWebError, KimiWebSessionBusy
from push import KimiTerminalBusy, PushHandler, _should_expire_chat_typing, _should_generate_chat_append_tts


class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item

    def tail(self, limit):
        return list(self.records[-limit:])


class FakeGroupChat(FakeChat):
    """群历史 append 的前两个参数是位置参数 (role, text)。"""

    def append(self, role=None, text=None, **record):
        if role is not None:
            record.setdefault("role", role)
        if text is not None:
            record.setdefault("text", text)
        return super().append(**record)


class _LegacyKimiACPFixtures:
    def handler(self):
        kimi = FakeChat()
        xiaoke = FakeChat()
        state = types.SimpleNamespace(
            contact_chats={"kimi": kimi, "xiaoke": xiaoke},
            kimi_turn_lock=threading.RLock(),
            kimi_active_turn={},
            kimi_prepare_token="",
            chat_draft_lock=threading.Lock(),
            chat_drafts={},
            chat_reply_states={},
            kimi_acp=types.SimpleNamespace(
                prepare_session=lambda: "session-1",
                prompt_existing=lambda *_args, **_kwargs: None,
                cancel=lambda _turn, _session: True,
                close=lambda: None,
            ),
        )
        handler = object.__new__(PushHandler)
        handler.state = state
        handler.headers = {}
        handler.responses = []
        handler.completed = []
        handler.interrupted = []
        handler.typing = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._set_typing_for_contact = (
            lambda *args, **kwargs: handler.typing.append((args, kwargs))
        )
        handler._set_chat_generating = (
            lambda *args, **kwargs: PushHandler._set_chat_generating(handler, *args, **kwargs)
        )

        def set_completed(*args, **kwargs):
            handler.completed.append((args, kwargs))
            return PushHandler._set_chat_completed(handler, *args, **kwargs)

        def set_interrupted(*args, **kwargs):
            handler.interrupted.append((args, kwargs))
            return PushHandler._set_chat_interrupted(handler, *args, **kwargs)

        handler._set_chat_completed = set_completed
        handler._set_chat_interrupted = set_interrupted
        handler._set_chat_draft = (
            lambda *args, **kwargs: PushHandler._set_chat_draft(handler, *args, **kwargs)
        )
        handler._clear_chat_draft = (
            lambda *args, **kwargs: PushHandler._clear_chat_draft(handler, *args, **kwargs)
        )
        handler._chat_for_contact = lambda contact_id: state.contact_chats[contact_id]
        handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
        return handler, kimi, xiaoke

    @staticmethod
    def _immediate_thread(target, **_kwargs):
        class ImmediateThread:
            def start(self):
                target()

        return ImmediateThread()

    def _run_kimi_reply(self, handler, *, user_text, assistant_text, bundle, failure=None):
        """Run the Kimi worker inline and return the prompt it received."""
        prompts = []
        handler._kimi_link_bundle = lambda _text: bundle

        def prompt_existing(prompt, *, on_update, **_kwargs):
            prompts.append(prompt)
            if assistant_text:
                on_update(assistant_text)
            if failure is not None:
                raise failure

        handler.state.kimi_acp = types.SimpleNamespace(
            prepare_session=lambda: "session-1",
            prompt_existing=prompt_existing,
            cancel=lambda _turn, _session: True,
            close=lambda: None,
        )
        with patch("push.threading.Thread", self._immediate_thread):
            handler._handle_kimi_chat_send({"text": user_text}, "kimi")
        return prompts

    @staticmethod
    def _xhs_login_bundle():
        return LinkPreviewBundle(
            previews=({"comments_status": "login_required"},),
            prompt_context="[链接全文资料]\n小红书登录已失效。",
        )

    def test_kimi_send_uses_only_kimi_history_and_worker(self):
        handler, kimi, xiaoke = self.handler()

        class DeferredThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                pass

        with patch("push.threading.Thread", DeferredThread):
            handler._handle_chat_send({"contact_id": "kimi", "text": "hello"})

        self.assertEqual(["hello"], [item["text"] for item in kimi.records])
        self.assertEqual([], xiaoke.records)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual("kimi-acp", handler.responses[-1][1]["turn"]["transport"])
        self.assertEqual("session-1", handler.state.kimi_active_turn["session_id"])

    def test_chat_releases_idle_terminal_before_preparing_acp(self):
        handler, _kimi, _xiaoke = self.handler()
        order = []
        handler.state.kimi_terminal = types.SimpleNamespace(
            input_transaction=lambda: __import__("contextlib").nullcontext(),
            release_for_acp=lambda: order.append("terminal-release") or True,
        )
        handler.state.kimi_acp.prepare_session = lambda: order.append("acp-prepare") or "session-1"

        class DeferredThread:
            def __init__(self, target, **_kwargs): self.target = target
            def start(self): pass

        with patch("push.threading.Thread", DeferredThread):
            handler._handle_kimi_chat_send({"text": "hello"}, "kimi")

        self.assertEqual(["terminal-release", "acp-prepare"], order)
        self.assertEqual(200, handler.responses[-1][0])

    def test_busy_terminal_rejects_chat_before_user_history_or_acp_prepare(self):
        handler, kimi, _xiaoke = self.handler()
        prepare = Mock(return_value="session-1")
        handler.state.kimi_acp.prepare_session = prepare
        handler.state.kimi_terminal = types.SimpleNamespace(
            input_transaction=lambda: __import__("contextlib").nullcontext(),
            release_for_acp=lambda: (_ for _ in ()).throw(
                KimiTerminalBusy("Kimi 终端可能仍在生成，请先离开终端后再发送")
            ),
        )

        handler._handle_kimi_chat_send({"text": "must not persist"}, "kimi")

        self.assertEqual([], kimi.records)
        prepare.assert_not_called()
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_terminal_busy", handler.responses[-1][1]["error"])
        self.assertEqual("", handler.state.kimi_prepare_token)

    def test_second_kimi_turn_is_rejected_before_it_can_enter_history(self):
        handler, kimi, xiaoke = self.handler()
        handler.state.kimi_active_turn = {
            "user_ts": "active-turn",
            "cancel_event": threading.Event(),
            "session_id": "session-active",
        }

        handler._handle_kimi_chat_send({"text": "must not enqueue"}, "kimi")

        self.assertEqual([], kimi.records)
        self.assertEqual([], xiaoke.records)
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_turn_active", handler.responses[-1][1]["error"])

    def test_prepare_runs_outside_turn_lock_and_reserves_one_history_write(self):
        handler, kimi, _xiaoke = self.handler()
        prepare_started = threading.Event()
        release_prepare = threading.Event()
        first_finished = threading.Event()

        def prepare_session():
            prepare_started.set()
            self.assertTrue(release_prepare.wait(timeout=2))
            return "session-1"

        handler.state.kimi_acp.prepare_session = prepare_session

        def send_first():
            try:
                handler._handle_kimi_chat_send({"text": "first"}, "kimi")
            finally:
                first_finished.set()

        first_thread = threading.Thread(target=send_first)
        first_thread.start()
        self.assertTrue(prepare_started.wait(timeout=2))

        acquired = handler.state.kimi_turn_lock.acquire(blocking=False)
        self.assertTrue(acquired, "prepare_session must not hold kimi_turn_lock")
        if acquired:
            handler.state.kimi_turn_lock.release()

        handler._handle_kimi_chat_send({"text": "second"}, "kimi")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual([], kimi.records)

        release_prepare.set()
        self.assertTrue(first_finished.wait(timeout=2))
        first_thread.join(timeout=2)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(
            ["first"],
            [record["text"] for record in kimi.records if record["role"] == "user"],
        )

    def test_stale_stop_cannot_cancel_newer_kimi_turn(self):
        handler, _kimi, _xiaoke = self.handler()
        event = threading.Event()
        handler.state.kimi_active_turn = {
            "user_ts": "new",
            "cancel_event": event,
            "session_id": "session-new",
        }

        handler._handle_kimi_chat_stop("old")

        self.assertFalse(event.is_set())
        self.assertEqual(409, handler.responses[-1][0])

    def test_stop_without_exact_user_timestamp_fails_closed(self):
        handler, _kimi, _xiaoke = self.handler()
        event = threading.Event()
        handler.state.kimi_active_turn = {
            "user_ts": "turn-1",
            "cancel_event": event,
            "session_id": "session-1",
        }

        handler._handle_kimi_chat_stop("")

        self.assertFalse(event.is_set())
        self.assertEqual(400, handler.responses[-1][0])
        self.assertEqual("missing_turn_identity", handler.responses[-1][1]["error"])

    def test_stop_refuses_active_turn_without_real_session_id(self):
        handler, _kimi, _xiaoke = self.handler()
        event = threading.Event()
        handler.state.kimi_active_turn = {
            "user_ts": "turn-1",
            "cancel_event": event,
            "session_id": "",
        }

        handler._handle_kimi_chat_stop("turn-1")

        self.assertFalse(event.is_set())
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("invalid_turn_identity", handler.responses[-1][1]["error"])

    def test_exact_stop_only_signals_worker_and_never_double_sends_cancel(self):
        handler, _kimi, _xiaoke = self.handler()
        event = threading.Event()
        cancelled = []
        handler.state.kimi_active_turn = {
            "user_ts": "turn-1",
            "cancel_event": event,
            "session_id": "session-1",
        }
        handler.state.kimi_acp = types.SimpleNamespace(
            cancel=lambda turn_id, session_id: cancelled.append((turn_id, session_id)) or True,
        )

        handler._handle_kimi_chat_stop("turn-1")

        self.assertTrue(event.is_set())
        self.assertEqual([], cancelled)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["stopped"])

    def test_history_failure_never_exposes_internal_exception(self):
        handler, kimi, _xiaoke = self.handler()

        def fail_append(**_record):
            raise RuntimeError("/private/path secret detail")

        kimi.append = fail_append
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")

        status, payload = handler.responses[-1]
        self.assertEqual(500, status)
        self.assertEqual("kimi_history_unavailable", payload["error"])
        self.assertNotIn("private", str(payload))
        self.assertNotIn("secret", str(payload))

    def test_acp_failure_is_not_retried_or_routed_to_another_contact(self):
        handler, kimi, xiaoke = self.handler()
        prompt_calls = []

        def prompt(*_args, **_kwargs):
            prompt_calls.append(True)
            raise KimiACPError("internal transport detail")

        handler.state.kimi_acp = types.SimpleNamespace(
            prepare_session=lambda: "session-1",
            prompt_existing=prompt,
            cancel=lambda _turn, _session: True,
            close=lambda: None,
        )

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch("push.threading.Thread", ImmediateThread):
            handler._handle_kimi_chat_send({"text": "hello"}, "kimi")

        self.assertEqual(1, len(prompt_calls))
        self.assertEqual([], xiaoke.records)
        self.assertEqual(["hello", "Kimi 这次没有成功回复。请稍后重试；原消息已经保留。"], [
            item["text"] for item in kimi.records
        ])
        self.assertNotIn("internal transport detail", str(kimi.records))
        self.assertEqual(1, len(handler.completed))
        self.assertFalse(handler.typing[-1][0][1]["is_typing"])

    def test_kimi_xhs_login_marker_is_stripped_only_for_trusted_login_required_reply(self):
        handler, kimi, _xiaoke = self.handler()
        marker = "[[CCC_XHS_LOGIN_CARD:v1]]"
        prompts = self._run_kimi_reply(
            handler,
            user_text="https://www.xiaohongshu.com/explore/example",
            assistant_text=f"小红书需要重新登录。\n{marker}",
            bundle=self._xhs_login_bundle(),
        )

        assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
        self.assertEqual("小红书需要重新登录。", assistant["text"])
        self.assertTrue(assistant["metadata"]["xhs_login_card"])
        self.assertNotIn(marker, assistant["text"])
        self.assertIn(marker, prompts[0])

    def test_normal_kimi_final_reply_never_gets_an_xhs_login_card(self):
        handler, kimi, _xiaoke = self.handler()
        marker = "[[CCC_XHS_LOGIN_CARD:v1]]"
        prompts = self._run_kimi_reply(
            handler,
            user_text="普通问题",
            assistant_text="这是正常回复。",
            bundle=LinkPreviewBundle(),
        )

        assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
        self.assertEqual("这是正常回复。", assistant["text"])
        self.assertNotIn("xhs_login_card", assistant["metadata"])
        self.assertNotIn(marker, prompts[0])

    def test_kimi_xhs_marker_is_not_a_client_or_inline_card_trigger(self):
        marker = "[[CCC_XHS_LOGIN_CARD:v1]]"
        with self.subTest("user-marker-cannot-create-card"):
            handler, kimi, _xiaoke = self.handler()
            self._run_kimi_reply(
                handler,
                user_text=marker,
                assistant_text=marker,
                bundle=LinkPreviewBundle(),
            )
            assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
            self.assertEqual(marker, assistant["text"])
            self.assertNotIn("xhs_login_card", assistant["metadata"])

        with self.subTest("inline-marker-is-ordinary-text"):
            handler, kimi, _xiaoke = self.handler()
            inline = f"示例：{marker}"
            self._run_kimi_reply(
                handler,
                user_text="https://www.xiaohongshu.com/explore/example",
                assistant_text=inline,
                bundle=self._xhs_login_bundle(),
            )
            assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
            self.assertEqual(inline, assistant["text"])
            self.assertNotIn("xhs_login_card", assistant["metadata"])

        with self.subTest("repeated-marker-is-ordinary-text"):
            handler, kimi, _xiaoke = self.handler()
            repeated = f"{marker}\n{marker}"
            self._run_kimi_reply(
                handler,
                user_text="https://www.xiaohongshu.com/explore/example",
                assistant_text=repeated,
                bundle=self._xhs_login_bundle(),
            )
            assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
            self.assertEqual(repeated, assistant["text"])
            self.assertNotIn("xhs_login_card", assistant["metadata"])

        with self.subTest("marker-must-be-last-nonempty-line"):
            handler, kimi, _xiaoke = self.handler()
            trailing = f"{marker}\n这句在标记后面"
            self._run_kimi_reply(
                handler,
                user_text="https://www.xiaohongshu.com/explore/example",
                assistant_text=trailing,
                bundle=self._xhs_login_bundle(),
            )
            assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
            self.assertEqual(trailing, assistant["text"])
            self.assertNotIn("xhs_login_card", assistant["metadata"])

    def test_kimi_xhs_marker_never_survives_as_a_card_on_failure_or_interrupt(self):
        marker = "[[CCC_XHS_LOGIN_CARD:v1]]"
        with self.subTest("failure"):
            handler, kimi, _xiaoke = self.handler()
            self._run_kimi_reply(
                handler,
                user_text="https://www.xiaohongshu.com/explore/example",
                assistant_text="",
                bundle=self._xhs_login_bundle(),
                failure=KimiACPError("transport failed"),
            )
            assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
            self.assertNotIn("xhs_login_card", assistant["metadata"])
            self.assertNotIn(marker, assistant["text"])

        with self.subTest("interrupted"):
            handler, kimi, _xiaoke = self.handler()
            self._run_kimi_reply(
                handler,
                user_text="https://www.xiaohongshu.com/explore/example",
                assistant_text=marker,
                bundle=self._xhs_login_bundle(),
                failure=KimiACPCancelled("stopped"),
            )
            assistant = [record for record in kimi.records if record["role"] == "assistant"][-1]
            self.assertNotIn("xhs_login_card", assistant["metadata"])
            self.assertIn(marker, assistant["text"])

    def test_assistant_history_failure_still_finishes_every_lifecycle_state(self):
        handler, kimi, _xiaoke = self.handler()
        original_append = kimi.append

        def append(**record):
            if record.get("role") == "assistant":
                raise RuntimeError("/private/assistant-history")
            return original_append(**record)

        kimi.append = append

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with patch("push.threading.Thread", ImmediateThread):
            handler._handle_kimi_chat_send({"text": "hello"}, "kimi")

        self.assertEqual(["hello"], [record["text"] for record in kimi.records])
        self.assertEqual(1, len(handler.completed))
        self.assertEqual({}, handler.state.kimi_active_turn)
        self.assertFalse(handler.typing[-1][0][1]["is_typing"])
        self.assertNotIn("kimi", handler.state.chat_drafts)
        self.assertEqual(
            "completed",
            handler.state.chat_reply_states["kimi"]["reply_state"],
        )
        self.assertNotIn("private", str(handler.responses))

    def test_kimi_card_action_append_attachment_and_voice_are_text_only(self):
        handler, kimi, _xiaoke = self.handler()
        handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
        handler._check_auth = lambda: True
        tts_calls = []
        handler._run_stackchan_voice_helper = lambda *_args, **_kwargs: tts_calls.append(True)

        handler._handle_chat_card_action({"contact_id": "kimi", "text": "action"})
        self.assertEqual(415, handler.responses[-1][0])

        handler._handle_chat_append({
            "contact_id": "kimi",
            "text": "caption",
            "attachment_url": "/attachments/file.png",
        })
        self.assertEqual(415, handler.responses[-1][0])

        handler._handle_chat_append({
            "contact_id": "kimi",
            "text": "card",
            "metadata": {"card_title": "Interactive"},
        })
        self.assertEqual(415, handler.responses[-1][0])

        handler._handle_chat_send({
            "contact_id": "kimi",
            "text": "not a login card",
            "metadata": {"xhs_login_card": True},
        })
        self.assertEqual(415, handler.responses[-1][0])
        self.assertEqual("kimi_text_only", handler.responses[-1][1]["error"])

        for body in (
            {"contact_id": "kimi", "text": "metadata", "metadata": {}},
            {"contact_id": "kimi", "text": "thinking", "thinking": "secret"},
            {"contact_id": "kimi", "text": "tools", "tools": "call"},
            {"contact_id": "kimi", "text": "task", "role": "task"},
            {"contact_id": "kimi", "text": "", "role": "assistant"},
        ):
            handler._handle_chat_append(body)
            self.assertEqual(415, handler.responses[-1][0])
            self.assertEqual("kimi_text_only", handler.responses[-1][1]["error"])

        handler._handle_voice_push({"contact_id": "kimi", "text": "voice"})
        self.assertEqual(415, handler.responses[-1][0])
        self.assertFalse(
            _should_generate_chat_append_tts(
                "kimi",
                "assistant",
                "plain text",
                None,
                True,
            )
        )
        self.assertTrue(
            _should_generate_chat_append_tts(
                "xiaoke",
                "assistant",
                "plain text",
                None,
                True,
            )
        )
        self.assertEqual([], tts_calls)
        self.assertEqual([], kimi.records)


class FakeWebChat:
    def __init__(self, *, text="reply", wait_for_stop=False):
        self.text = text
        self.wait_for_stop = wait_for_stop
        self.calls = []
        self.stop = threading.Event()
        self.message_count = 0

    def ensure_active_session(self, *, model, thinking, **_kwargs):
        self.calls.append(("ensure", model, thinking))
        return "web-session-1"

    def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
        self.calls.append(("stream", session_id))
        on_ready()
        if self.wait_for_stop:
            self.stop.wait(1)
            on_event({"type": "prompt.aborted", "session_id": session_id, "payload": {"turnId": "turn-1"}})
            return
        on_event({"type": "assistant.delta", "session_id": session_id, "payload": {"turnId": "turn-1", "delta": self.text}})
        on_event({"type": "turn.ended", "session_id": session_id, "payload": {"turnId": "turn-1", "reason": "completed"}})

    def submit_prompt(self, session_id, prompt, **_kwargs):
        self.calls.append(("submit", session_id, prompt, dict(_kwargs)))
        return {"prompt_id": "prompt-1"}

    def get_snapshot(self, _session_id):
        return {
            # Real Kimi Web snapshots can nest the submitted identity rather
            # than exposing a legacy flat current_prompt_id field.
            "current_prompt": {"id": "prompt-1"}, "epoch": "test-epoch", "as_of_seq": 0,
            "in_flight_turn": {"turnId": "turn-1"},
        }
    def get_session(self, _session_id): return {"message_count": self.message_count}
    def load_turn_lease(self): return {}
    def save_turn_lease(self, **_kwargs): return None
    def set_turn_lease_state(self, **_kwargs): return True
    def clear_turn_lease(self, **_kwargs): return True
    def claim_turn_lease(self, **_kwargs): return True
    def release_turn_lease_claim(self, **_kwargs): return True
    # A normal snapshot carries no interaction decision; the worker must not
    # invent one.  Tests that exercise approvals override this explicitly.
    def list_approvals(self, _session_id): raise KimiWebError("no pending approval")
    def approve_once(self, *_args): raise AssertionError("unexpected approval")
    def abort_prompt(self, session_id, prompt_id, **_kwargs):
        self.calls.append(("abort", session_id, prompt_id))
        self.stop.set()


class KimiWebChatRoutingTest(unittest.TestCase):
    def make_handler(self, *, web=None, chat=None):
        web = web or FakeWebChat()
        chat = chat or FakeChat()
        handler = object.__new__(PushHandler)
        state = types.SimpleNamespace(
            contact_chats={"kimi": chat}, kimi_web=web, kimi_acp=types.SimpleNamespace(),
            kimi_turn_lock=threading.RLock(), kimi_active_turn={}, kimi_prepare_token="",
            kimi_web_permission_mode="auto",
            kimi_preferences=types.SimpleNamespace(snapshot=lambda: ("kimi-code/k3-256k", "low")),
            kimi_terminal_observer=types.SimpleNamespace(begin=lambda *_: 1, record=lambda *_: True, finish=lambda *_: True, record_assistant_text=lambda *_: True),
            kimi_semantic_memory_recall_enabled=False, chat_draft_lock=threading.Lock(), chat_drafts={},
            chat_reply_states={}, chat_stream_revisions={}, sticker_catalog=types.SimpleNamespace(snapshot=lambda: {"stickers": []}),
        )
        handler.state = state; handler.headers = {}; handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._chat_for_contact = lambda _contact: chat
        handler._source_for_request = lambda suffix="": f"test:{suffix}"
        handler._set_typing_for_contact = lambda *_args, **_kwargs: None
        handler._kimi_semantic_recall = lambda *_args, **_kwargs: None
        handler._kimi_link_bundle = lambda _text: LinkPreviewBundle(previews=(), prompt_context="")
        return handler, chat, web

    @staticmethod
    def wait_idle(handler):
        for _ in range(100):
            if not handler.state.kimi_active_turn: return
            __import__("time").sleep(0.01)
        raise AssertionError("Kimi Web worker did not finish")

    @staticmethod
    def wait_queue_drained(handler):
        """等排队 worker 把队列投递完：队列空、worker 退出（含投递/退避中）且无活跃轮/预约。"""
        for _ in range(600):
            with handler.state.kimi_turn_lock:
                idle = not handler.state.kimi_active_turn and not handler.state.kimi_prepare_token
                pending = bool(getattr(handler.state, "kimi_chat_queue", None))
                running = bool(getattr(handler.state, "kimi_chat_queue_worker_running", False))
            if idle and not pending and not running:
                return
            __import__("time").sleep(0.02)
        raise AssertionError("Kimi chat queue did not drain")

    def test_private_web_turn_never_calls_acp_and_keeps_kimi_history_isolated(self):
        handler, chat, web = self.make_handler()
        handler.state.kimi_acp.prepare_session = Mock(side_effect=AssertionError("ACP fallback"))
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.wait_idle(handler)
        self.assertEqual(["hello", "正在思考", "reply"], [row["text"] for row in chat.records])
        activity = chat.records[-2]
        self.assertEqual("task", activity["role"])
        self.assertEqual("kimi-web:activity", activity["source"])
        self.assertEqual("completed", activity["metadata"]["status"])
        self.assertEqual(1, activity["metadata"]["activity_count"])
        self.assertEqual("auxiliary_activity", activity["metadata"]["turn_message_kind"])
        self.assertFalse(activity["metadata"]["turn_terminal"])
        self.assertEqual(chat.records[0]["ts"], activity["metadata"]["kimi_user_ts"])
        self.assertEqual("kimi-web", handler.responses[-1][1]["turn"]["transport"])
        self.assertEqual(["ensure", "stream", "submit"], [row[0] for row in web.calls[:3]])
        self.assertEqual("auto", next(row[3]["permission_mode"] for row in web.calls if row[0] == "submit"))
        self.assertTrue(chat.records[-1]["metadata"]["turn_terminal"])
        self.assertEqual("terminal_answer", chat.records[-1]["metadata"]["turn_message_kind"])

    def test_private_web_turn_passes_consumed_attachment_records_once(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = os.path.join(directory, "stored.png")
            with open(image_path, "wb") as handle:
                handle.write(b"image")
            attachment = {
                "attachment_id": "opaque-1",
                "attachment_url": "/attachments/stored.png",
                "filename": "photo.png",
                "type": "image",
                "media_type": "image/png",
                "size": 5,
                "stored_path": image_path,
            }
            handler, chat, web = self.make_handler()
            handler._handle_kimi_chat_send({
                "text": "看看",
                "metadata": {"attachments": [{"attachment_id": "opaque-1"}]},
                "_pwa_staged_attachments": [attachment],
            }, "kimi")
            self.wait_idle(handler)

            submit = next(call for call in web.calls if call[0] == "submit")
            self.assertEqual([attachment], submit[3]["attachments"])
            self.assertEqual("/attachments/stored.png", chat.records[0]["attachment_url"])
            self.assertEqual("image", chat.records[0]["attachment_type"])
            self.assertEqual("photo.png", chat.records[0]["attachment_filename"])
            self.assertEqual(1, len([call for call in web.calls if call[0] == "submit"]))

    def test_private_attachment_only_turn_uses_safe_caption_without_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "stored.pdf")
            with open(file_path, "wb") as handle:
                handle.write(b"pdf")
            attachment = {
                "attachment_id": "opaque-2",
                "attachment_url": "/attachments/stored.pdf",
                "filename": "report.pdf",
                "type": "file",
                "media_type": "application/pdf",
                "size": 3,
                "stored_path": file_path,
            }
            handler, _chat, web = self.make_handler()
            handler._handle_kimi_chat_send({"text": "", "_pwa_staged_attachments": [attachment]}, "kimi")
            self.wait_idle(handler)

            prompt = next(call[2] for call in web.calls if call[0] == "submit")
            self.assertIn("[用户发送了附件]", prompt)
            self.assertNotIn(directory, prompt)

    def test_web_commits_recall_only_after_prompt_and_lease_are_accepted(self):
        order = []

        class OrderedWeb(FakeWebChat):
            def submit_prompt(self, session_id, prompt, **kwargs):
                order.append("submit")
                return super().submit_prompt(session_id, prompt, **kwargs)

            def save_turn_lease(self, **kwargs):
                order.append("lease")
                return super().save_turn_lease(**kwargs)

        recall = types.SimpleNamespace(
            context="safe recall",
            items=({"date": "2026-08-20", "title": "memory", "snippet": "safe"},),
            memory_keys=("v1:" + "c" * 64,),
        )
        handler, _chat, _web = self.make_handler(web=OrderedWeb())
        handler._kimi_semantic_recall = Mock(return_value=recall)
        handler._append_kimi_recall_card = Mock(return_value=True)
        handler._commit_kimi_recall = Mock(
            side_effect=lambda *_args: order.append("commit") or True,
        )

        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.wait_idle(handler)

        handler._commit_kimi_recall.assert_called_once_with(recall, "web-session-1")
        self.assertEqual(["submit", "lease", "commit"], order)

    def test_web_does_not_commit_recall_when_turn_lease_cannot_be_saved(self):
        class UnleasedWeb(FakeWebChat):
            def save_turn_lease(self, **_kwargs):
                raise KimiWebError("lease unavailable")

        recall = types.SimpleNamespace(
            context="safe recall",
            items=({"date": "2026-08-20", "title": "memory", "snippet": "safe"},),
            memory_keys=("v1:" + "e" * 64,),
        )
        handler, _chat, _web = self.make_handler(web=UnleasedWeb())
        handler._kimi_semantic_recall = Mock(return_value=recall)
        handler._append_kimi_recall_card = Mock(return_value=True)
        handler._commit_kimi_recall = Mock(return_value=True)

        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.wait_idle(handler)

        handler._commit_kimi_recall.assert_not_called()
        self.assertEqual(503, handler.responses[-1][0])

    def test_web_does_not_commit_recall_when_prompt_is_not_accepted(self):
        class RejectingWeb(FakeWebChat):
            def submit_prompt(self, session_id, prompt, **_kwargs):
                self.calls.append(("submit", session_id, prompt, dict(_kwargs)))
                raise KimiWebError("rejected")

        recall = types.SimpleNamespace(
            context="safe recall",
            items=({"date": "2026-08-20", "title": "memory", "snippet": "safe"},),
            memory_keys=("v1:" + "d" * 64,),
        )
        handler, _chat, _web = self.make_handler(web=RejectingWeb())
        handler._kimi_semantic_recall = Mock(return_value=recall)
        handler._append_kimi_recall_card = Mock(return_value=True)
        handler._commit_kimi_recall = Mock(return_value=True)

        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.wait_idle(handler)

        handler._commit_kimi_recall.assert_not_called()
        self.assertEqual(503, handler.responses[-1][0])

    def test_busy_send_commits_attachment_and_queues_for_later(self):
        """忙时不再 409 丢附件：消息与附件入库入队，等当前轮结束自动发送。"""
        with tempfile.TemporaryDirectory() as directory:
            stored_path = os.path.join(directory, "consumed.pdf")
            with open(stored_path, "wb") as handle:
                handle.write(b"pdf")
            handler, chat, _web = self.make_handler()
            handler.state.attachments_dir = directory
            handler.state.kimi_active_turn = {"user_ts": "old-turn"}
            with patch("push.threading.Thread"):  # 本用例只断言入队，不跑 worker
                handler._handle_kimi_chat_send({
                    "text": "new",
                    "_pwa_staged_attachments": [{
                        "attachment_id": "consumed", "attachment_url": "/attachments/consumed.pdf",
                        "filename": "report.pdf", "type": "file", "media_type": "application/pdf",
                        "size": 3, "stored_path": stored_path,
                    }],
                }, "kimi")

            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["queued"])
            self.assertEqual(1, len(chat.records))
            self.assertEqual("/attachments/consumed.pdf", chat.records[0]["attachment_url"])
            # 附件已随历史提交，忙时绝不删除。
            self.assertTrue(os.path.exists(stored_path))
            self.assertEqual(1, len(handler.state.kimi_chat_queue))

    def test_prompt_rejection_preserves_attachment_already_committed_to_history(self):
        class RejectingWeb(FakeWebChat):
            def submit_prompt(self, session_id, prompt, **_kwargs):
                self.calls.append(("submit", session_id, prompt, dict(_kwargs)))
                raise KimiWebError("rejected")

        with tempfile.TemporaryDirectory() as directory:
            stored_path = os.path.join(directory, "committed.pdf")
            with open(stored_path, "wb") as handle:
                handle.write(b"pdf")
            handler, chat, _web = self.make_handler(web=RejectingWeb())
            handler.state.attachments_dir = directory
            handler._handle_kimi_chat_send({
                "text": "read",
                "_pwa_staged_attachments": [{
                    "attachment_id": "committed", "attachment_url": "/attachments/committed.pdf",
                    "filename": "report.pdf", "type": "file", "media_type": "application/pdf",
                    "size": 3, "stored_path": stored_path,
                }],
            }, "kimi")
            self.wait_idle(handler)

            self.assertEqual(503, handler.responses[-1][0])
            self.assertEqual("/attachments/committed.pdf", chat.records[0]["attachment_url"])
            self.assertTrue(os.path.isfile(stored_path))

    def test_web_chat_releases_an_idle_kimi_tui_before_session_prepare(self):
        handler, _chat, web = self.make_handler()
        order = []
        handler.state.kimi_terminal = types.SimpleNamespace(
            input_transaction=lambda: __import__("contextlib").nullcontext(),
            release_for_acp=lambda: order.append("terminal-release") or True,
        )
        original_ensure = web.ensure_active_session
        web.ensure_active_session = lambda **kwargs: order.append("web-prepare") or original_ensure(**kwargs)

        handler._handle_kimi_web_chat_send({"text": "hello"}, "kimi", web)
        self.wait_idle(handler)

        self.assertEqual(["terminal-release", "web-prepare"], order)

    def test_web_chat_queues_without_submitting_while_kimi_tui_is_uncertain(self):
        """TUI 不确定期间绝不提交 prompt；消息入库入队，等就绪后自动发出。"""
        handler, chat, web = self.make_handler()
        handler.state.kimi_terminal = types.SimpleNamespace(
            input_transaction=lambda: __import__("contextlib").nullcontext(),
            release_for_acp=lambda: (_ for _ in ()).throw(
                KimiTerminalBusy("Kimi 终端可能仍在生成，请先离开终端后再发送")
            ),
        )

        with patch("push.threading.Thread"):  # 本用例只断言入队，不跑 worker
            handler._handle_kimi_web_chat_send({"text": "must not submit"}, "kimi", web)

        self.assertEqual(["must not submit"], [row["text"] for row in chat.records])
        self.assertEqual([], web.calls)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["queued"])
        self.assertEqual("", handler.state.kimi_prepare_token)
        self.assertEqual(1, len(handler.state.kimi_chat_queue))

    def test_stream_subscription_is_ready_before_submit_and_exact_stop_is_fenced(self):
        handler, chat, web = self.make_handler(web=FakeWebChat(wait_for_stop=True))
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.assertEqual(["ensure", "stream", "submit"], [row[0] for row in web.calls[:3]])
        user_ts = handler.responses[-1][1]["turn"]["user_ts"]
        handler._handle_kimi_chat_stop(user_ts)
        self.wait_idle(handler)
        self.assertIn(("abort", "web-session-1", "prompt-1"), web.calls)
        self.assertTrue(any(row["role"] == "assistant" for row in chat.records))
        activity = next(row for row in chat.records if (row.get("metadata") or {}).get("activity_summary"))
        self.assertEqual("interrupted", activity["metadata"]["status"])
        self.assertLess(chat.records.index(activity), len(chat.records) - 1)

    def test_web_interleaved_activity_keeps_typing_until_exact_terminal(self):
        class InterleavedWeb(FakeWebChat):
            def __init__(self):
                super().__init__()
                self.activity_ready = threading.Event()
                self.release_terminal = threading.Event()

            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id))
                on_ready()
                for event_type, payload in (
                    ("assistant.delta", {"turnId": "turn-1", "delta": "draft"}),
                    ("tool.started", {"turnId": "turn-1", "command": "rm -rf /", "output": "secret"}),
                    ("subagent.started", {"turnId": "turn-1", "name": "private child", "arguments": {"token": "secret"}}),
                ):
                    on_event({"type": event_type, "session_id": session_id, "payload": payload})
                self.activity_ready.set()
                self.release_terminal.wait(1)
                on_event({"type": "turn.ended", "session_id": session_id,
                          "payload": {"turnId": "turn-1", "reason": "completed"}})

        handler, _chat, web = self.make_handler(web=InterleavedWeb())
        typing, activity_updates = [], []
        handler._set_typing_for_contact = lambda contact_id, value: typing.append((contact_id, dict(value)))
        original_set_activity = handler._set_chat_activity

        def record_activity(*args, **kwargs):
            activity_updates.append(dict(kwargs))
            return original_set_activity(*args, **kwargs)

        handler._set_chat_activity = record_activity
        handler._handle_kimi_chat_send({"text": "interleaved"}, "kimi")
        self.assertTrue(web.activity_ready.wait(1))
        self.assertTrue(handler.state.kimi_active_turn)
        self.assertTrue(typing and typing[0][1]["is_typing"])
        self.assertFalse(any(not value["is_typing"] for _contact, value in typing))
        self.assertEqual([1, 2, 3], [update["activity_count"] for update in activity_updates])
        draft = handler.state.chat_drafts["kimi"]
        self.assertEqual(3, draft["activity_count"])
        self.assertEqual(["正在思考", "正在使用工具", "正在协作"], draft["activity_items"])
        self.assertNotIn("rm -rf", str({"draft": draft, "updates": activity_updates}))
        self.assertNotIn("secret", str({"draft": draft, "updates": activity_updates}))

        web.release_terminal.set()
        self.wait_idle(handler)
        for _ in range(100):
            if typing and typing[-1][1].get("is_typing") is False:
                break
            __import__("time").sleep(0.01)
        self.assertFalse(typing[-1][1]["is_typing"])
        persisted = next(
            row for row in _chat.records
            if (row.get("metadata") or {}).get("activity_summary")
        )
        self.assertEqual(3, persisted["metadata"]["activity_count"])
        self.assertEqual(
            ["正在思考", "正在使用工具", "正在协作"],
            persisted["metadata"]["activity_items"],
        )

    def test_exact_kimi_web_typing_does_not_expire_during_long_tool_turn(self):
        exact = {"transport": "kimi-web", "exact_turn": True}
        self.assertFalse(_should_expire_chat_typing("kimi", exact, 3600))
        self.assertFalse(_should_expire_chat_typing("apples", exact, 3600))
        self.assertTrue(_should_expire_chat_typing("kimi", {"transport": "kimi-web"}, 121))

    def test_private_abort_without_lifecycle_settles_only_after_exact_idle_proof(self):
        class LostTerminalWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.status_calls = 0
            def stream_session(self, session_id, *, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id)); on_ready(); stop_event.wait(1)
            def get_session_status(self, _session_id, **_kwargs):
                self.status_calls += 1; return {"busy": False}
            def get_snapshot(self, _session_id, **_kwargs):
                return {}

        handler, _chat, web = self.make_handler(web=LostTerminalWeb())
        handler.state.contact_typing_states = {"kimi": {"is_typing": False, "since": None}}
        handler._set_typing_for_contact = PushHandler._set_typing_for_contact.__get__(handler)
        with patch("push.KIMI_WEB_ABORT_SETTLE_SECONDS", 0.01):
            handler._handle_kimi_chat_send({"text": "settle"}, "kimi")
            user_ts = handler.responses[-1][1]["turn"]["user_ts"]
            self.assertTrue(handler.state.contact_typing_states["kimi"]["exact_turn"])
            self.assertFalse(_should_expire_chat_typing("kimi", handler.state.contact_typing_states["kimi"], 3600))
            handler._handle_kimi_chat_stop(user_ts)
            self.assertTrue(handler.state.kimi_active_turn)
            self.wait_idle(handler)
        self.assertEqual(1, web.status_calls)
        self.assertFalse(handler.state.contact_typing_states["kimi"]["is_typing"])

    def test_abort_settlement_busy_or_stale_turn_never_clears_foreground(self):
        handler, _chat, _web = self.make_handler()
        handler.state.contact_typing_states = {"kimi": {"is_typing": True, "since": "new", "exact_turn": True}}
        handler._set_typing_for_contact = PushHandler._set_typing_for_contact.__get__(handler)
        cancel = threading.Event()
        handler.state.kimi_active_turn = {"user_ts": "old", "session_id": "s", "prompt_id": "p"}
        busy_web = types.SimpleNamespace(get_session_status=lambda *_args, **_kwargs: {"busy": True})
        with patch("push.KIMI_WEB_ABORT_SETTLE_SECONDS", 0.01):
            handler._schedule_kimi_web_abort_settlement(web=busy_web, session_id="s", prompt_id="p", user_ts="old", cancel_event=cancel)
            __import__("time").sleep(0.05)
        self.assertFalse(cancel.is_set())
        self.assertTrue(handler.state.contact_typing_states["kimi"]["is_typing"])
        self.assertFalse(handler._clear_typing_for_contact_if_turn("kimi", "old"))
        handler.state.kimi_active_turn.pop("abort_settlement_scheduled", None)
        foreign_web = types.SimpleNamespace(
            get_session_status=lambda *_args, **_kwargs: {"busy": False},
            get_snapshot=lambda *_args, **_kwargs: {"current_prompt_id": "p-new"},
        )
        with patch("push.KIMI_WEB_ABORT_SETTLE_SECONDS", 0.01):
            handler._schedule_kimi_web_abort_settlement(web=foreign_web, session_id="s", prompt_id="p", user_ts="old", cancel_event=cancel)
            __import__("time").sleep(0.05)
        self.assertFalse(cancel.is_set())
        self.assertTrue(handler.state.contact_typing_states["kimi"]["is_typing"])

    def test_group_blocked_abort_without_lifecycle_settles_after_idle_proof(self):
        class GroupLostTerminalWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.status_calls = 0
            def stream_session(self, session_id, *, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id)); on_ready(); stop_event.wait(1)
            def get_session_status(self, _session_id, **_kwargs):
                self.status_calls += 1; return {"busy": False}
            def get_snapshot(self, _session_id, **_kwargs):
                # The first snapshot drives the bounded blocked-abort path;
                # the watchdog's later idle snapshot proves it is gone.
                if self.status_calls:
                    return {}
                return {"current_prompt_id": "prompt-1", "in_flight_turn": {"turnId": "turn-1"}, "pending_questions": [{"id": "q"}]}

        handler, chat, web = self.make_handler(web=GroupLostTerminalWeb())
        handler.state.contact_typing_states = {"apples": {"is_typing": False, "since": None}}
        handler._set_typing_for_contact = PushHandler._set_typing_for_contact.__get__(handler)
        handler._release_kimi_control = lambda _token: None
        handler._has_pending_group_reply = lambda: False
        user = chat.append(role="user", text="@Kimi settle", source="android-app:apples")
        with patch("push.KIMI_WEB_ABORT_SETTLE_SECONDS", 0.01):
            handler._start_group_kimi_reply(chat, user["text"], sender_name="Astra", user_ts=user["ts"], hop_count=0)
            self.wait_idle(handler)
        self.assertEqual(1, web.status_calls)
        self.assertFalse(handler.state.contact_typing_states["apples"]["is_typing"])

    def test_second_turn_queues_and_history_failure_fail_before_web_submit(self):
        handler, chat, web = self.make_handler()
        handler.state.kimi_active_turn = {"user_ts": "old", "cancel_event": threading.Event(), "session_id": "s"}
        with patch("push.threading.Thread"):  # 本用例只断言入队，不跑 worker
            handler._handle_kimi_chat_send({"text": "second"}, "kimi")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["queued"])
        self.assertFalse(web.calls)
        self.assertEqual(["second"], [row["text"] for row in chat.records])

        class BrokenChat(FakeChat):
            def append(self, **_record): raise OSError("disk")
        handler, _chat, web = self.make_handler(chat=BrokenChat())
        handler._handle_kimi_chat_send({"text": "first"}, "kimi")
        self.assertEqual(500, handler.responses[-1][0])
        self.assertEqual(["ensure"], [row[0] for row in web.calls])

    def test_queued_messages_auto_send_in_order_after_active_turn_ends(self):
        """活跃轮期间的消息按序排队，轮结束后 worker 依次自动发出。"""
        handler, chat, web = self.make_handler()
        handler.state.kimi_active_turn = {
            "user_ts": "busy-turn", "cancel_event": threading.Event(), "session_id": "web-session-1",
        }
        handler._handle_kimi_chat_send({"text": "第一条"}, "kimi")
        handler._handle_kimi_chat_send({"text": "第二条"}, "kimi")
        self.assertEqual([200, 200], [status for status, _ in handler.responses[-2:]])
        self.assertEqual([1, 2], [payload["queue_position"] for _, payload in handler.responses[-2:]])
        # 轮进行中绝不碰 Web。
        self.assertFalse(web.calls)

        handler.state.kimi_active_turn = {}
        self.wait_queue_drained(handler)
        self.wait_idle(handler)

        prompts = [row[2] for row in web.calls if row[0] == "submit"]
        self.assertEqual(2, len(prompts))
        self.assertIn("第一条", prompts[0])
        self.assertIn("第二条", prompts[1])
        user_texts = [row["text"] for row in chat.records if row["role"] == "user"]
        self.assertEqual(["第一条", "第二条"], user_texts)

    def test_chat_queue_full_returns_429_and_marks_failed(self):
        """队列有界：满了才报错（429，不是 409），消息仍入库；429 同步告知 App。"""
        handler, chat, web = self.make_handler()
        handler.state.kimi_active_turn = {
            "user_ts": "busy", "cancel_event": threading.Event(), "session_id": "s",
        }
        with patch("push.KIMI_CHAT_QUEUE_MAX", 1), patch("push.threading.Thread"):
            handler._handle_kimi_chat_send({"text": "queued"}, "kimi")
            handler._handle_kimi_chat_send({"text": "overflow"}, "kimi")
        self.assertEqual(200, handler.responses[-2][0])
        status, payload = handler.responses[-1]
        self.assertEqual(429, status)
        self.assertEqual("kimi_queue_full", payload["error"])
        self.assertEqual(["queued", "overflow"], [row["text"] for row in chat.records])
        # 第一条仍在排队，reply_state 属于它；溢出消息由 429 同步告知，不覆盖queued 态。
        self.assertEqual("queued", handler.state.chat_reply_states["kimi"]["reply_state"])
        self.assertFalse(web.calls)

    def test_stop_clear_queued_empties_queue_and_marks_interrupted(self):
        """Stop 链路的清空途径：clear_queued 清掉排队消息并标记 interrupted。"""
        handler, chat, web = self.make_handler()
        handler.state.kimi_active_turn = {
            "user_ts": "busy", "cancel_event": threading.Event(), "session_id": "s",
        }
        with patch("push.threading.Thread"):
            handler._handle_kimi_chat_send({"text": "排队一"}, "kimi")
            handler._handle_kimi_chat_send({"text": "排队二"}, "kimi")
        self.assertEqual(2, len(handler.state.kimi_chat_queue))

        handler.state.kimi_active_turn = {}
        handler._handle_kimi_chat_stop("", body={"clear_queued": True})
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual(2, payload["cleared_queued"])
        self.assertEqual(0, len(handler.state.kimi_chat_queue))
        self.assertEqual("interrupted", handler.state.chat_reply_states["kimi"]["reply_state"])
        self.assertFalse(web.calls)

    def test_orphaned_web_busy_prompt_is_recovered_then_queued_message_auto_sends(self):
        class OrphanedBusyWeb(FakeWebChat):
            def __init__(self):
                super().__init__()
                self.recovered = False
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                self.calls.append(("ensure", model, thinking))
                if not self.recovered:
                    raise KimiWebSessionBusy("web-session-1")
                return "web-session-1"
            def load_turn_lease(self):
                return {"session_id": "web-session-1", "prompt_id": "prompt-old", "user_ts": "old", "state": "stream_lost", "created_at": "1"}
            def recover_owned_orphaned_prompt(self, lease):
                session_id = lease["session_id"]
                self.calls.append(("recover", session_id))
                self.recovered = True
                return True

        handler, chat, web = self.make_handler(web=OrphanedBusyWeb())
        handler._handle_kimi_chat_send({"text": "retry after orphan"}, "kimi")
        # 不再 409 要求用户重发：消息入库入队，回收完成后 worker 自动重投。
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["queued"])
        self.assertEqual(1, [row["text"] for row in chat.records].count("retry after orphan"))
        self.assertEqual(1, sum(bool((row.get("metadata") or {}).get("orphan_recovery")) for row in chat.records))
        self.wait_queue_drained(handler)
        self.wait_idle(handler)

        self.assertEqual(["ensure", "recover", "ensure", "stream", "submit"], [row[0] for row in web.calls])
        self.assertIn(("recover", "web-session-1"), web.calls)
        # 重投绝不重复入库。
        self.assertEqual(1, [row["text"] for row in chat.records].count("retry after orphan"))

    def test_idle_stream_lost_lease_is_reconciled_then_queued_message_auto_sends(self):
        class IdleLeaseWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.reconciled = False
            def reconcile_owned_idle_lease(self, **_kwargs):
                if not self.reconciled:
                    self.reconciled = True
                    return {"session_id": "web-session-1", "prompt_id": "old", "user_ts": "old-ts", "state": "stream_lost", "created_at": "1"}
                return {}

        handler, chat, web = self.make_handler(web=IdleLeaseWeb())
        handler._handle_kimi_chat_send({"text": "new after idle"}, "kimi")
        # 残留 lease 整理期间不再 409：消息入库入队，整理完自动重投。
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["queued"])
        self.assertFalse(any(row[0] == "submit" for row in web.calls))
        self.assertEqual(1, [row["text"] for row in chat.records].count("new after idle"))
        self.wait_queue_drained(handler)
        self.wait_idle(handler)
        self.assertTrue(any(row[0] == "submit" for row in web.calls))
        self.assertEqual(1, [row["text"] for row in chat.records].count("new after idle"))

    def test_upstream_busy_does_not_recover_while_a_legitimate_local_turn_owns_control(self):
        class ShouldNotRecover(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise AssertionError("local live turn must reject before Web access")
            def recover_owned_orphaned_prompt(self, _lease):
                raise AssertionError("must not abort a legitimate local turn")

        handler, chat, web = self.make_handler(web=ShouldNotRecover())
        handler.state.kimi_active_turn = {"user_ts": "live", "session_id": "web-session-1"}
        with patch("push.threading.Thread"):  # 活跃轮期间 worker 不应启动投递
            handler._handle_kimi_chat_send({"text": "do not interrupt"}, "kimi")
        # 不再 409：入库入队，等本地轮结束自动发送；全程不碰 Web。
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["queued"])
        self.assertEqual(["do not interrupt"], [row["text"] for row in chat.records])
        self.assertEqual(1, len(handler.state.kimi_chat_queue))

    def test_ambiguous_orphan_busy_queues_then_fails_visibly_after_bounded_retries(self):
        class AmbiguousBusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise KimiWebSessionBusy("web-session-1")
            def load_turn_lease(self):
                return {"session_id": "web-session-1", "prompt_id": "prompt-old", "user_ts": "old", "state": "stream_lost", "created_at": "1"}
            def recover_owned_orphaned_prompt(self, _lease):
                from kimi_web_client import KimiWebRecoveryConflict
                raise KimiWebRecoveryConflict("prompt moved")

        handler, chat, web = self.make_handler(web=AmbiguousBusyWeb())
        # 忙类重投不消耗 attempts（见 KIMI_CHAT_QUEUE_BUSY_REQUEUE_REASONS），
        # 判死走入队时长兜底；这里直接压死兜底判定让用例立即落到失败终态。
        with patch("push.KIMI_CHAT_QUEUE_MAX_ATTEMPTS", 0), patch(
            "push.PushHandler._kimi_chat_queue_wait_expired", return_value=True,
        ):
            handler._handle_kimi_chat_send({"text": "safe conflict"}, "kimi")
            # 归属不明的忙态不再 409：入库入队；等待兜底超时后落可见失败说明。
            self.assertEqual(200, handler.responses[-1][0])
            self.assertTrue(handler.responses[-1][1]["queued"])
            self.assertEqual(["safe conflict"], [row["text"] for row in chat.records])
            self.assertEqual("", handler.state.kimi_prepare_token)
            self.wait_queue_drained(handler)
        notes = [row["text"] for row in chat.records if row["role"] == "assistant"]
        self.assertTrue(any("未能送出" in note for note in notes))

    def test_external_busy_without_durable_app_lease_never_aborts_and_queues(self):
        class ExternalBusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise KimiWebSessionBusy("web-session-1")
            def recover_owned_orphaned_prompt(self, _lease):
                raise AssertionError("unknown provider prompt must never be aborted")

        handler, chat, _web = self.make_handler(web=ExternalBusyWeb())
        with patch("push.KIMI_CHAT_QUEUE_MAX_ATTEMPTS", 0), patch(
            "push.PushHandler._kimi_chat_queue_wait_expired", return_value=True,
        ):
            handler._handle_kimi_chat_send({"text": "external busy"}, "kimi")
            self.assertEqual(200, handler.responses[-1][0])
            self.assertTrue(handler.responses[-1][1]["queued"])
            self.assertEqual(["external busy"], [row["text"] for row in chat.records])
            self.wait_queue_drained(handler)
        notes = [row["text"] for row in chat.records if row["role"] == "assistant"]
        self.assertTrue(any("未能送出" in note for note in notes))

    def test_proven_stale_busy_after_restart_reuses_session_without_notice(self):
        class StaleBusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                self.calls.append(("ensure", model, thinking))
                raise KimiWebSessionBusy("web-session-old")
            def recover_owned_orphaned_prompt(self, _lease):
                raise AssertionError("no durable lease means the old session must not be aborted")
            def stuck_busy_predates_process(self, session_id):
                self.calls.append(("predates", session_id))
                return True
            def reset_stuck_busy_session(self, session_id, **kwargs):
                self.calls.append(("reset-stuck", session_id, dict(kwargs)))
                self.assert_prepare_reservation()
                return session_id

        handler, chat, web = self.make_handler(web=StaleBusyWeb())
        web.assert_prepare_reservation = lambda: self.assertTrue(
            bool(handler.state.kimi_prepare_token), "reset must remain inside prepare reservation",
        )
        pushed = []
        handler._send_chat_notification = lambda title, body: pushed.append((title, body))
        handler._handle_kimi_chat_send({"text": "continue after false busy"}, "kimi")
        self.wait_idle(handler)

        self.assertEqual(200, handler.responses[-1][0])
        # 假忙被证明早于本进程启动：清标记复用旧会话，不开新会话。
        self.assertEqual("web-session-old", handler.responses[-1][1]["turn"]["session_id"])
        self.assertEqual(1, [row["text"] for row in chat.records].count("continue after false busy"))
        self.assertEqual(
            [("reset-stuck", "web-session-old")],
            [(row[0], row[1]) for row in web.calls if row[0] == "reset-stuck"],
        )
        # 没有会话切换，因此没有恢复通知、没有推送。
        self.assertFalse(any(row.get("source") == "system:kimi-session-recovery" for row in chat.records))
        self.assertEqual([], pushed)

    def test_unproven_busy_after_restart_stays_fail_closed_without_new_session(self):
        class UnprovenBusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise KimiWebSessionBusy("web-session-old")
            def recover_owned_orphaned_prompt(self, _lease):
                raise AssertionError("no durable lease means the old session must not be aborted")
            def stuck_busy_predates_process(self, _session_id):
                return False
            def reset_stuck_busy_session(self, *_args, **_kwargs):
                raise AssertionError("unproven busy must never be cleared")
            def replace_stuck_busy_session(self, *_args, **_kwargs):
                raise AssertionError("unproven busy must never switch sessions")

        handler, chat, web = self.make_handler(web=UnprovenBusyWeb())
        pushed = []
        handler._send_chat_notification = lambda title, body: pushed.append((title, body))
        with patch("push.KIMI_CHAT_QUEUE_MAX_ATTEMPTS", 0), patch(
            "push.PushHandler._kimi_chat_queue_wait_expired", return_value=True,
        ):
            handler._handle_kimi_chat_send({"text": "maybe live external work"}, "kimi")
            # 无法证明是重启遗留的忙：不开新会话、不清标记，入库排队自动重投。
            self.assertEqual(200, handler.responses[-1][0])
            self.assertTrue(handler.responses[-1][1]["queued"])
            self.assertEqual(["maybe live external work"], [row["text"] for row in chat.records])
            self.wait_queue_drained(handler)
        self.assertFalse(any(row.get("source") == "system:kimi-session-recovery" for row in chat.records))
        self.assertEqual([], pushed)
        notes = [row["text"] for row in chat.records if row["role"] == "assistant"]
        self.assertTrue(any("未能送出" in note for note in notes))

    def test_stale_pointer_swap_after_restart_appends_recovery_notice(self):
        class StalePointerWeb(FakeWebChat):
            def load_active_session_id(self):
                return "web-session-dead"
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                self.calls.append(("ensure", model, thinking))
                return "web-session-fresh"

        handler, chat, web = self.make_handler(web=StalePointerWeb())
        pushed = []
        handler._send_chat_notification = lambda title, body: pushed.append((title, body))
        handler._handle_kimi_chat_send({"text": "after restart"}, "kimi")
        self.wait_idle(handler)

        self.assertEqual(200, handler.responses[-1][0])
        notices = [row for row in chat.records if row.get("source") == "system:kimi-session-recovery"]
        self.assertEqual(1, len(notices))
        self.assertEqual("assistant", notices[0]["role"])
        self.assertIn("web-session-dead", notices[0]["text"])
        self.assertIn("web-session-fresh", notices[0]["text"])
        self.assertIn("无法继续", notices[0]["text"])
        self.assertEqual(1, len(pushed))

    def test_stable_pointer_and_first_session_stay_silent(self):
        class StableWeb(FakeWebChat):
            def load_active_session_id(self):
                return "web-session-1"

        handler, chat, _web = self.make_handler(web=StableWeb())
        handler._handle_kimi_chat_send({"text": "ordinary"}, "kimi")
        self.wait_idle(handler)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertFalse(any(row.get("source") == "system:kimi-session-recovery" for row in chat.records))

        # No pointer at all (first-ever session) is not a swap either.
        handler, chat, _web = self.make_handler(web=FakeWebChat())
        handler._handle_kimi_chat_send({"text": "first ever"}, "kimi")
        self.wait_idle(handler)
        self.assertFalse(any(row.get("source") == "system:kimi-session-recovery" for row in chat.records))

    def test_recovery_network_wait_does_not_hold_kimi_turn_lock(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingRecoveryWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.recovered = False
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                if not self.recovered:
                    raise KimiWebSessionBusy("web-session-1")
                return "web-session-1"
            def load_turn_lease(self):
                return {"session_id": "web-session-1", "prompt_id": "prompt-1", "user_ts": "old", "state": "stream_lost", "created_at": "1"}
            def recover_owned_orphaned_prompt(self, _lease):
                entered.set(); release.wait(1); self.recovered = True; return True

        handler, _chat, _web = self.make_handler(web=BlockingRecoveryWeb())
        thread = threading.Thread(target=lambda: handler._handle_kimi_chat_send({"text": "wait"}, "kimi"))
        thread.start(); self.assertTrue(entered.wait(1))
        acquired = handler.state.kimi_turn_lock.acquire(blocking=False)
        self.assertTrue(acquired, "Web recovery network I/O must not hold kimi_turn_lock")
        if acquired:
            handler.state.kimi_turn_lock.release()
        release.set(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.wait_idle(handler)

    def test_stop_can_recover_exact_durable_orphan_but_respects_recovery_reservation(self):
        class DurableStopWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.cleared = []
            def load_turn_lease(self):
                return {"session_id": "web-session-1", "prompt_id": "prompt-1", "user_ts": "orphan-ts", "state": "stream_lost", "created_at": "1"}
            def recover_owned_orphaned_prompt(self, lease):
                self.calls.append(("recover", lease["session_id"], lease["prompt_id"])); return True
            def clear_turn_lease(self, **kwargs): self.cleared.append(kwargs); return True

        handler, chat, web = self.make_handler(web=DurableStopWeb())
        handler._handle_kimi_chat_stop("orphan-ts")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertTrue(handler.responses[-1][1]["stopped"])
        self.assertEqual([{"session_id": "web-session-1", "prompt_id": "prompt-1"}], web.cleared)
        self.assertEqual(1, sum(bool((row.get("metadata") or {}).get("orphan_recovery")) for row in chat.records))
        handler.state.kimi_recovery_token = "other"
        handler._handle_kimi_chat_stop("orphan-ts")
        self.assertEqual(409, handler.responses[-1][0])

    def test_xhs_marker_is_only_a_server_rendered_assistant_card(self):
        handler, _chat, _web = self.make_handler()
        visible, card = handler._kimi_extract_xhs_login_card("请先登录\n[[CCC_XHS_LOGIN_CARD:v1]]", allowed=True)
        self.assertTrue(card)
        self.assertNotIn("[[CCC_XHS_LOGIN_CARD:v1]]", visible)
        visible, card = handler._kimi_extract_xhs_login_card("[[CCC_XHS_LOGIN_CARD:v1]]", allowed=False)
        self.assertFalse(card)
        self.assertIn("[[CCC_XHS_LOGIN_CARD:v1]]", visible)

    def test_xhs_card_is_emitted_only_for_trusted_web_reply_marker(self):
        marker = "[[CCC_XHS_LOGIN_CARD:v1]]"
        handler, chat, _web = self.make_handler(web=FakeWebChat(text=f"请重新登录\n{marker}"))
        handler._kimi_link_bundle = lambda _text: LinkPreviewBundle(
            previews=({"comments_status": "login_required"},), prompt_context="xhs"
        )
        handler._handle_kimi_chat_send({"text": "https://www.xiaohongshu.com/explore/x"}, "kimi")
        self.wait_idle(handler)
        assistant = chat.records[-1]
        self.assertTrue(assistant["metadata"]["xhs_login_card"])
        self.assertNotIn(marker, assistant["text"])

        handler, chat, _web = self.make_handler(web=FakeWebChat(text=marker))
        handler._handle_kimi_chat_send({"text": "ordinary"}, "kimi")
        self.wait_idle(handler)
        self.assertNotIn("xhs_login_card", chat.records[-1]["metadata"])
        self.assertEqual(marker, chat.records[-1]["text"])

    def test_stop_after_completed_turn_is_idempotent_and_never_aborts_a_new_turn(self):
        handler, _chat, web = self.make_handler()
        handler._handle_kimi_chat_send({"text": "done"}, "kimi")
        user_ts = handler.responses[-1][1]["turn"]["user_ts"]
        self.wait_idle(handler)
        handler._handle_kimi_chat_stop(user_ts)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertFalse(handler.responses[-1][1]["stopped"])
        self.assertFalse(any(row[0] == "abort" for row in web.calls))

    def test_web_stop_identity_is_exact_and_abort_is_prompt_fenced(self):
        handler, _chat, web = self.make_handler()
        event = threading.Event()
        handler.state.kimi_active_turn = {
            "user_ts": "new", "cancel_event": event, "session_id": "web-session-1",
            "prompt_id": "prompt-1", "transport": "kimi-web",
        }
        handler._handle_kimi_chat_stop("old")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertFalse(event.is_set())
        self.assertFalse(any(row[0] == "abort" for row in web.calls))

        handler._handle_kimi_chat_stop("")
        self.assertEqual(400, handler.responses[-1][0])
        handler.state.kimi_active_turn["session_id"] = ""
        handler._handle_kimi_chat_stop("new")
        self.assertEqual(409, handler.responses[-1][0])

        handler.state.kimi_active_turn["session_id"] = "web-session-1"
        handler._handle_kimi_chat_stop("new")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertFalse(event.is_set())
        self.assertIn(("abort", "web-session-1", "prompt-1"), web.calls)

    def test_stop_http_acceptance_keeps_lease_until_provider_terminal_event(self):
        class BusyAfterAbortWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.states = []; self.clears = []
            def set_turn_lease_state(self, **kwargs): self.states.append(kwargs); return True
            def clear_turn_lease(self, **kwargs): self.clears.append(kwargs); return True

        handler, _chat, web = self.make_handler(web=BusyAfterAbortWeb())
        event = threading.Event()
        handler.state.kimi_active_turn = {
            "user_ts": "turn-1", "cancel_event": event, "session_id": "web-session-1",
            "prompt_id": "prompt-1", "transport": "kimi-web",
        }
        handler._handle_kimi_chat_stop("turn-1")
        self.assertFalse(event.is_set())
        self.assertEqual([], web.clears)
        self.assertEqual(
            [{"session_id": "web-session-1", "prompt_id": "prompt-1", "state": "stopping"}],
            web.states,
        )

    def test_web_kimi_is_text_only_across_append_card_and_voice_paths(self):
        handler, kimi, _web = self.make_handler()
        handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
        handler._check_auth = lambda: True
        tts_calls = []
        handler._run_stackchan_voice_helper = lambda *_args, **_kwargs: tts_calls.append(True)
        for invoke, body in (
            (handler._handle_chat_card_action, {"contact_id": "kimi", "text": "action"}),
            (handler._handle_chat_append, {"contact_id": "kimi", "text": "caption", "attachment_url": "/attachments/file.png"}),
            (handler._handle_chat_append, {"contact_id": "kimi", "text": "card", "metadata": {"card_title": "Interactive"}}),
            (handler._handle_chat_send, {"contact_id": "kimi", "text": "no", "metadata": {"xhs_login_card": True}}),
            (handler._handle_voice_push, {"contact_id": "kimi", "text": "voice"}),
        ):
            invoke(body)
            self.assertEqual(415, handler.responses[-1][0])
        self.assertEqual([], kimi.records)
        self.assertEqual([], tts_calls)
        self.assertFalse(_should_generate_chat_append_tts("kimi", "assistant", "plain", None, True))

    def _make_kimi_append_handler(self, attachments_dir):
        """Minimal handler state for exercising _handle_chat_append success paths."""
        handler, chat, _web = self.make_handler()
        handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
        handler._check_auth = lambda: True
        handler._has_pending_group_reply = lambda: False
        handler._send_chat_notification = lambda *_args, **_kwargs: None
        handler.state.settings = {"tts_enabled": False}
        handler.state.tokens = types.SimpleNamespace(all_active=lambda: [])
        handler.state.attachments_dir = Path(attachments_dir)
        return handler, chat

    def test_kimi_append_accepts_image_attachment_by_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, chat = self._make_kimi_append_handler(tmpdir)
            handler._handle_chat_append({
                "contact_id": "kimi",
                "text": "文生图结果",
                "role": "assistant",
                "attachment_url": "/attachments/gen-img.png",
                "attachment_type": "image",
                "attachment_filename": "gen-img.png",
            })
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["ok"])
            rec = chat.records[-1]
            self.assertEqual("assistant", rec["role"])
            self.assertEqual("文生图结果", rec["text"])
            self.assertEqual("/attachments/gen-img.png", rec["attachment_url"])
            self.assertEqual("image", rec["attachment_type"])
            self.assertEqual("gen-img.png", rec["attachment_filename"])

    def test_apples_assistant_append_publishes_one_persisted_completion_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, _chat = self._make_kimi_append_handler(tmpdir)
            bus = ChatStreamBus()
            queue = bus.subscribe()
            handler.state.chat_stream_bus = bus
            handler._apples_source_member = lambda _source: "xiaoke"
            handler._apples_member_name = lambda member_id: member_id
            handler._normalize_mentioned_member_ids = lambda _value: []
            handler._detect_apples_mentions = lambda _text: set()
            handler._maybe_route_apples_assistant_mention = lambda *_args, **_kwargs: []
            handler.state.task_buffer = types.SimpleNamespace(
                append=lambda **_kwargs: {"ok": True},
            )

            handler._handle_chat_append({
                "contact_id": "apples",
                "role": "assistant",
                "text": "群聊完成回复",
            })

            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual(1, len(queue))
            event = queue.popleft()
            self.assertEqual("done", event["event"])
            self.assertEqual("apples", event["contact_id"])
            self.assertTrue(event["persisted"])
            self.assertTrue(event["stream_id"].startswith("persisted:apples:"))

            handler._handle_chat_append({
                "contact_id": "apples", "role": "assistant", "text": "[op] private status",
            })
            handler._handle_chat_append({
                "contact_id": "apples", "role": "task", "text": "internal task",
            })
            self.assertEqual([], list(queue))
            handler._publish_persisted_assistant_completion("apples", {
                "role": "assistant", "text": "heartbeat", "source": "heartbeat", "ts": "heartbeat-ts",
            })
            self.assertEqual([], list(queue))
            bus.unsubscribe(queue)

            class FailingBus:
                def publish(self, _event):
                    raise OSError("subscriber unavailable")

            handler.state.chat_stream_bus = FailingBus()
            handler._handle_chat_append({
                "contact_id": "apples", "role": "assistant", "text": "durable despite SSE failure",
            })
            self.assertEqual(200, handler.responses[-1][0])

    def test_kimi_append_copies_image_attachment_path_into_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, chat = self._make_kimi_append_handler(tmpdir)
            src = Path(tmpdir) / "source.jpg"
            src.write_bytes(b"\xff\xd8\xff test jpeg")
            # 无 caption 的纯图片消息: text 可整个省略
            handler._handle_chat_append({
                "contact_id": "kimi",
                "role": "assistant",
                "attachment_path": str(src),
            })
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["ok"])
            rec = chat.records[-1]
            self.assertEqual("", rec["text"])
            self.assertEqual("image", rec["attachment_type"])
            self.assertEqual("source.jpg", rec["attachment_filename"])
            stored_url = rec["attachment_url"]
            self.assertTrue(stored_url.startswith("/attachments/"))
            stored_name = stored_url.rsplit("/", 1)[-1]
            self.assertTrue(stored_name.endswith(".jpg"))
            self.assertEqual(
                b"\xff\xd8\xff test jpeg",
                (Path(tmpdir) / stored_name).read_bytes(),
            )

    def test_kimi_append_rejects_non_image_and_untyped_attachments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, chat = self._make_kimi_append_handler(tmpdir)
            txt = Path(tmpdir) / "notes.txt"
            txt.write_text("not an image")
            before = set(os.listdir(tmpdir))
            for body in (
                # 显式非图片类型
                {"contact_id": "kimi", "text": "file", "attachment_url": "/attachments/a.zip", "attachment_type": "file"},
                # 纯 attachment_url 未显式声明 image
                {"contact_id": "kimi", "text": "untyped", "attachment_url": "/attachments/a.png"},
                # attachment_path 扩展名不是图片
                {"contact_id": "kimi", "text": "txt", "attachment_path": str(txt)},
                # 只有 type/filename 没有实际附件
                {"contact_id": "kimi", "text": "type only", "attachment_type": "image"},
                # 互动卡片 metadata 仍然拒绝
                {"contact_id": "kimi", "text": "card", "metadata": {"card_title": "Interactive"}},
            ):
                handler._handle_chat_append(body)
                self.assertEqual(415, handler.responses[-1][0])
                self.assertEqual("kimi_text_only", handler.responses[-1][1]["error"])
            self.assertEqual([], chat.records)
            # 被拒的 attachment_path 不得复制进附件库 (不留孤儿文件)
            self.assertEqual(before, set(os.listdir(tmpdir)))

    def test_kimi_append_image_attachment_requires_auth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, chat = self._make_kimi_append_handler(tmpdir)
            handler._check_auth = lambda: False
            handler._handle_chat_append({
                "contact_id": "kimi",
                "text": "no auth",
                "attachment_url": "/attachments/gen.png",
                "attachment_type": "image",
            })
            self.assertEqual(401, handler.responses[-1][0])
            self.assertEqual([], chat.records)

    def test_web_turn_failure_cleans_lock_typing_and_draft_without_acp_fallback(self):
        class FailingWeb(FakeWebChat):
            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id))
                on_ready()
                on_event({"type": "assistant.delta", "session_id": session_id,
                          "payload": {"turnId": "turn-1", "delta": "partial"}})
                raise KimiWebError("stream lost")

        handler, chat, web = self.make_handler(web=FailingWeb())
        typing = []
        handler._set_typing_for_contact = lambda *args, **_kwargs: typing.append(args)
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.wait_idle(handler)
        self.assertEqual(["hello", "正在思考", "partial"], [r["text"] for r in chat.records])
        self.assertEqual("failed", chat.records[-2]["metadata"]["status"])
        self.assertEqual("kimi-web:failed", chat.records[-1]["source"])
        self.assertEqual({}, handler.state.kimi_active_turn)
        self.assertNotIn("kimi", handler.state.chat_drafts)
        self.assertTrue(typing and typing[-1][1].get("is_typing") is False)
        self.assertFalse(hasattr(handler.state.kimi_acp, "prepare_session"))
        self.assertEqual(["ensure", "stream", "submit"], [row[0] for row in web.calls[:3]])

    def test_stream_loss_preserves_exact_durable_lease_for_restart_recovery(self):
        class LeaseFailingWeb(FakeWebChat):
            def __init__(self):
                super().__init__(); self.lease = {}; self.lease_states = []
            def save_turn_lease(self, **kwargs): self.lease = dict(kwargs)
            def set_turn_lease_state(self, **kwargs): self.lease_states.append(dict(kwargs)); return True
            def clear_turn_lease(self, **_kwargs): raise AssertionError("stream loss must preserve ownership lease")
            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id)); on_ready()
                __import__("time").sleep(0.03)
                raise KimiWebError("stream lost")

        handler, _chat, web = self.make_handler(web=LeaseFailingWeb())
        handler._handle_kimi_chat_send({"text": "keep lease"}, "kimi")
        self.wait_idle(handler)
        self.assertEqual("prompt-1", web.lease["prompt_id"])
        self.assertIn(
            {"session_id": "web-session-1", "prompt_id": "prompt-1", "state": "stream_lost"},
            web.lease_states,
        )

    def test_private_web_rejects_wrong_current_prompt_and_unbound_events(self):
        class ForeignPromptWeb(FakeWebChat):
            def get_snapshot(self, _session_id):
                return {
                    "current_prompt": {"id": "old-prompt"}, "epoch": "test-epoch", "as_of_seq": 7,
                    "in_flight_turn": {"turnId": "old-turn", "assistant_text": "SNAPSHOT LEAK"},
                }

            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id))
                on_ready()
                # A live session can emit old or unlabelled frames around a
                # fast submit. Neither may append text or end this turn.
                for payload, seq in (
                    ({"delta": "MISSING LEAK"}, 8),
                    ({"turnId": "old-turn", "delta": "OLD LEAK"}, 9),
                    ({"turnId": "old-turn", "reason": "completed"}, 10),
                ):
                    on_event({
                        "type": "assistant.delta" if "delta" in payload else "turn.ended",
                        "session_id": session_id, "epoch": "test-epoch", "seq": seq,
                        "payload": payload,
                    })

        handler, chat, _web = self.make_handler(web=ForeignPromptWeb())
        handler._handle_kimi_chat_send({"text": "current question"}, "kimi")
        self.wait_idle(handler)

        assistant = chat.records[-1]
        self.assertEqual("kimi-web:failed", assistant["source"])
        self.assertEqual("Kimi 这次没有成功回复。请稍后重试；原消息已经保留。", assistant["text"])
        self.assertNotIn("LEAK", assistant["text"])

    def test_private_web_snapshot_floor_rejects_buffered_frames_then_allows_new_unbound_frame(self):
        class SnapshotFloorWeb(FakeWebChat):
            def get_snapshot(self, _session_id):
                # Kimi has confirmed this submitted prompt, but has not yet
                # assigned a turnId. Only frames newer than this exact
                # snapshot's epoch/sequence floor may fill the response.
                return {
                    "current_prompt_id": "prompt-1", "epoch": "epoch-a", "as_of_seq": 10,
                    "in_flight_turn": {"assistant_text": "snapshot "},
                }

            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id)); on_ready()
                for event_type, payload, seq in (
                    ("assistant.delta", {"delta": "OLD BUFFER"}, 10),
                    ("assistant.delta", {"delta": "new"}, 11),
                    ("turn.ended", {"reason": "completed"}, 12),
                ):
                    on_event({"type": event_type, "session_id": session_id,
                              "epoch": "epoch-a", "seq": seq, "payload": payload})

        handler, chat, _web = self.make_handler(web=SnapshotFloorWeb())
        handler._handle_kimi_chat_send({"text": "current question"}, "kimi")
        self.wait_idle(handler)

        assistant = chat.records[-1]
        self.assertEqual("kimi-web", assistant["source"])
        self.assertEqual("snapshot new", assistant["text"])
        self.assertNotIn("OLD BUFFER", assistant["text"])

    def test_private_web_bound_turn_id_rejects_missing_and_wrong_turn_events(self):
        class BoundTurnWeb(FakeWebChat):
            def get_snapshot(self, _session_id):
                return {
                    "current_prompt_id": "prompt-1", "epoch": "epoch-b", "as_of_seq": 4,
                    "in_flight_turn": {"turn_id": "turn-current", "assistant_text": "snapshot "},
                }

            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id)); on_ready()
                for event_type, payload, seq in (
                    ("assistant.delta", {"delta": "MISSING LEAK"}, 5),
                    ("assistant.delta", {"turnId": "turn-old", "delta": "OLD LEAK"}, 6),
                    ("turn.ended", {"turnId": "turn-old", "reason": "completed"}, 7),
                    ("assistant.delta", {"turnId": "turn-current", "delta": "new"}, 8),
                    ("turn.ended", {"turnId": "turn-current", "reason": "completed"}, 9),
                ):
                    on_event({"type": event_type, "session_id": session_id,
                              "epoch": "epoch-b", "seq": seq, "payload": payload})

        handler, chat, _web = self.make_handler(web=BoundTurnWeb())
        handler._handle_kimi_chat_send({"text": "current question"}, "kimi")
        self.wait_idle(handler)

        assistant = chat.records[-1]
        self.assertEqual("kimi-web", assistant["source"])
        self.assertEqual("snapshot new", assistant["text"])
        self.assertNotIn("LEAK", assistant["text"])

    def test_private_web_failure_finally_clears_turn_and_typing_when_terminal_state_write_raises(self):
        class ErrorWeb(FakeWebChat):
            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id))
                on_ready()
                raise KimiWebError("stream lost")

        handler, _chat, _web = self.make_handler(web=ErrorWeb())
        typing = []
        handler._set_typing_for_contact = lambda *args, **kwargs: typing.append((args, kwargs))

        def fail_terminal_state(*_args, **_kwargs):
            raise OSError("reply-state storage failed")

        handler._set_chat_failed = fail_terminal_state
        # The failure is deliberately allowed to escape the terminal-state
        # write; suppress only the test process's default thread traceback so
        # this assertion focuses on the worker's outer-finally cleanup.
        with patch.object(threading, "excepthook") as thread_crash:
            handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
            self.wait_idle(handler)
            for _ in range(100):
                if typing and len(typing[-1][0]) > 1 and typing[-1][0][1].get("is_typing") is False:
                    break
                __import__("time").sleep(0.01)

        self.assertEqual({}, handler.state.kimi_active_turn)
        self.assertNotIn("kimi", handler.state.chat_drafts)
        self.assertTrue(typing and typing[-1][0][1].get("is_typing") is False)
        thread_crash.assert_called_once()

    def test_one_approval_is_allowed_once_but_questions_or_multiple_approvals_abort(self):
        class ApprovalWeb(FakeWebChat):
            def __init__(self, snapshot):
                super().__init__()
                self.snapshot = snapshot
                self.approved = []
            def get_snapshot(self, _session_id): return self.snapshot
            def list_approvals(self, _session_id): return []
            def approve_once(self, session_id, approval_id): self.approved.append((session_id, approval_id))
            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id)); on_ready()
                on_event({"type": "turn.ended", "session_id": session_id, "payload": {"turnId": "turn-1", "reason": "completed"}})

        for snapshot, expected_approvals, expect_abort in (
            ({"current_prompt_id": "prompt-1", "in_flight_turn": {"turnId": "turn-1"}, "pending_approvals": [{"approval_id": "a1", "tool_name": "read_file", "tool_input_display": "README.md"}], "pending_questions": []}, [("web-session-1", "a1")], False),
            ({"current_prompt_id": "prompt-1", "in_flight_turn": {"turnId": "turn-1"}, "pending_approvals": [{"approval_id": "a1"}, {"approval_id": "a2"}], "pending_questions": []}, [], True),
            ({"current_prompt_id": "prompt-1", "in_flight_turn": {"turnId": "turn-1"}, "pending_approvals": [], "pending_questions": [{"id": "q1"}]}, [], True),
        ):
            with self.subTest(snapshot=snapshot):
                handler, _chat, web = self.make_handler(web=ApprovalWeb(snapshot))
                handler._handle_kimi_chat_send({"text": "approval"}, "kimi")
                self.wait_idle(handler)
                self.assertEqual(expected_approvals, web.approved)
                self.assertEqual(expect_abort, any(row[0] == "abort" for row in web.calls))

    def test_only_known_readonly_single_approval_is_auto_allowed(self):
        self.assertTrue(PushHandler._kimi_is_routine_readonly_approval({
            "approval_id": "a", "tool_name": "read_file", "tool_input_display": "notes.txt",
        }))
        for approval in (
            {"approval_id": "a", "tool_name": "write_file", "tool_input_display": "notes.txt"},
            {"approval_id": "a", "tool_name": "read_file", "tool_input_display": "curl https://example.com"},
            {"approval_id": "a", "tool_name": "web_fetch", "tool_input_display": "https://example.com"},
            {"approval_id": "a", "tool_name": "unknown_tool", "tool_input_display": "x"},
        ):
            self.assertFalse(PushHandler._kimi_is_routine_readonly_approval(approval))

    def test_empty_web_session_is_seeded_once_from_app_history(self):
        handler, chat, web = self.make_handler()
        chat.append(role="assistant", text="以前的 Kimi 回复", source="kimi-web")
        handler._handle_kimi_chat_send({"text": "现在的问题"}, "kimi")
        self.wait_idle(handler)
        first_prompt = next(row[2] for row in web.calls if row[0] == "submit")
        self.assertIn("以前的 Kimi 回复", first_prompt)
        web.message_count = 2
        handler._handle_kimi_chat_send({"text": "第二个问题"}, "kimi")
        self.wait_idle(handler)
        second_prompt = [row[2] for row in web.calls if row[0] == "submit"][-1]
        self.assertNotIn("历史参考：仅首次接续", second_prompt)

    def test_group_web_reply_does_not_duplicate_user_and_preserves_group_identity(self):
        handler, chat, web = self.make_handler(web=FakeWebChat(text="@Kairos 群里好"))
        bus = ChatStreamBus()
        queue = bus.subscribe()
        handler.state.chat_stream_bus = bus
        user = chat.append(role="user", text="@Kimi 你好", source="android-app:apples", sender_id="astra", sender_name="方小南")
        routed = []
        typing = []
        handler._kimi_selection = lambda: ("kimi-code/k3", "low")
        handler._release_kimi_control = lambda _token: None
        handler._detect_apples_mentions = lambda text: {"kairos"} if "@Kairos" in text else set()
        handler._maybe_route_apples_assistant_mention = lambda *_args, **kwargs: routed.append(kwargs.get("hop_count"))
        handler._has_pending_group_reply = lambda: False
        handler._set_typing_for_contact = lambda *args, **_kwargs: typing.append(args)
        handler._start_group_kimi_reply(chat, user["text"], sender_name="方小南", user_ts=user["ts"], hop_count=1)
        self.wait_idle(handler)
        self.assertEqual(1, len([row for row in chat.records if row["role"] == "user"]))
        assistant = chat.records[-1]
        self.assertEqual("assistant", assistant["role"])
        self.assertEqual("kimi", assistant["sender_id"])
        self.assertEqual("Kimi", assistant["sender_name"])
        self.assertEqual("group:kimi-web", assistant["source"])
        self.assertEqual(["kairos"], assistant["mentions"])
        activity = chat.records[-2]
        self.assertEqual("task", activity["role"])
        self.assertEqual("group:kimi-web:activity", activity["source"])
        self.assertEqual("kimi", activity["sender_id"])
        self.assertEqual(user["ts"], activity["metadata"]["kimi_user_ts"])
        self.assertEqual("completed", activity["metadata"]["status"])
        self.assertEqual("auto", next(row[3]["permission_mode"] for row in web.calls if row[0] == "submit"))
        self.assertEqual([1], routed)
        self.assertTrue(typing and typing[-1][1]["is_typing"] is False)
        events = list(queue)
        completion = next(event for event in events if event["text"] == "@Kairos 群里好")
        self.assertEqual("apples", completion["contact_id"])
        bus.unsubscribe(queue)

    def test_group_kairos_final_reply_publishes_persisted_completion(self):
        class FakeRunner:
            def __init__(self, **_kwargs): pass

            def run_prompt(self, **kwargs):
                kwargs["on_update"]("Kairos draft")
                return "thread-group", "Kairos 群最终回复", "", 0

        fake_runs = types.SimpleNamespace(
            latest=lambda: None,
            start=lambda **_kwargs: ("run-group", threading.Event()),
            set_observer_phase=lambda *_args: None,
            publish_runner_activity=lambda *_args: None,
            finish=lambda *_args: None,
        )
        handler, chat, _web = self.make_handler()
        bus = ChatStreamBus()
        queue = bus.subscribe()
        handler.state.chat_stream_bus = bus
        handler.state.codex_bin = "codex"
        handler.state.codex_home = "/tmp/codex"
        handler._clear_chat_draft = lambda _contact: None
        handler._load_codex_target = lambda: ("session-group", "/tmp")
        handler._codex_preference_snapshot = lambda: ("model", "low")
        handler._codex_session_busy = lambda _session: False
        handler._save_codex_target = lambda *_args: None
        handler._apples_member_name = lambda member_id: member_id
        handler._detect_apples_mentions = lambda _text: set()
        handler._maybe_route_apples_assistant_mention = lambda *_args, **_kwargs: []
        handler._has_pending_group_reply = lambda: False
        handler._set_typing_for_contact = lambda *_args, **_kwargs: None
        with patch("push.CODEX_RUNS", fake_runs), patch.dict(
            sys.modules,
            {"codex_common": types.SimpleNamespace(CodexRunner=FakeRunner)},
        ):
            handler._start_group_kairos_reply(chat, "@Kairos 你好", sender_name="Astra")
            deadline = __import__("time").monotonic() + 1
            while not queue and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)

        event = next(event for event in queue if event["text"] == "Kairos 群最终回复")
        self.assertEqual("apples", event["contact_id"])
        self.assertEqual("Kairos 群最终回复", event["text"])
        self.assertTrue(event["stream_id"].startswith("persisted:apples:"))
        bus.unsubscribe(queue)

    def test_group_web_reply_passes_attachment_batch_only_in_prompt_content(self):
        with tempfile.TemporaryDirectory() as directory:
            stored_path = os.path.join(directory, "stored.pdf")
            with open(stored_path, "wb") as handle:
                handle.write(b"pdf")
            attachment = {
                "attachment_id": "opaque-group",
                "attachment_url": "/attachments/stored.pdf",
                "filename": "report.pdf",
                "type": "file",
                "media_type": "application/pdf",
                "size": 3,
                "stored_path": stored_path,
            }
            handler, chat, web = self.make_handler()
            user = chat.append(
                role="user", text="", source="android-app:apples",
                sender_id="astra", sender_name="方小南",
            )
            handler._release_kimi_control = lambda _token: None
            handler._has_pending_group_reply = lambda: False
            handler._start_group_kimi_reply(
                chat, "", sender_name="方小南", user_ts=user["ts"], hop_count=0,
                attachments=[attachment],
            )
            self.wait_idle(handler)

            submit = next(row for row in web.calls if row[0] == "submit")
            self.assertEqual([attachment], submit[3]["attachments"])
            self.assertIn("[用户发送了附件]", submit[2])
            self.assertNotIn(directory, submit[2])
            self.assertEqual(1, len([row for row in web.calls if row[0] == "submit"]))

    def test_group_web_bound_turn_id_rejects_missing_and_foreign_turn_events(self):
        class ForeignPromptWeb(FakeWebChat):
            def get_snapshot(self, _session_id):
                return {
                    # The camel spelling is also emitted by Kimi Web; it
                    # binds exactly like current_prompt, never by turnId alone.
                    "currentPrompt": {"id": "prompt-1"}, "epoch": "group-epoch", "as_of_seq": 3,
                    "in_flight_turn": {"turnId": "group-current", "assistant_text": "snapshot "},
                }

            def stream_session(self, session_id, *, on_event, on_ready, stop_event, **_kwargs):
                self.calls.append(("stream", session_id))
                on_ready()
                for event_type, payload, seq in (
                    ("assistant.delta", {"delta": "GROUP MISSING LEAK"}, 4),
                    ("assistant.delta", {"turnId": "group-old", "delta": "GROUP OLD LEAK"}, 5),
                    ("turn.ended", {"turnId": "group-old", "reason": "completed"}, 6),
                    ("assistant.delta", {"turnId": "group-current", "delta": "new"}, 7),
                    ("turn.ended", {"turnId": "group-current", "reason": "completed"}, 8),
                ):
                    on_event({
                        "type": event_type, "session_id": session_id,
                        "epoch": "group-epoch", "seq": seq,
                        "payload": payload,
                    })

        handler, chat, _web = self.make_handler(web=ForeignPromptWeb())
        handler._kimi_selection = lambda: ("kimi-code/k3", "low")
        handler._release_kimi_control = lambda _token: None
        handler._detect_apples_mentions = lambda _text: set()
        handler._maybe_route_apples_assistant_mention = lambda *_args, **_kwargs: None
        handler._has_pending_group_reply = lambda: False
        handler._set_typing_for_contact = lambda *_args, **_kwargs: None
        user = chat.append(role="user", text="@Kimi current", source="android-app:apples")
        handler._start_group_kimi_reply(
            chat, user["text"], sender_name="Astra", user_ts=user["ts"], hop_count=0,
        )
        self.wait_idle(handler)

        assistant = chat.records[-1]
        self.assertEqual("group:kimi-web", assistant["source"])
        self.assertEqual("snapshot new", assistant["text"])
        self.assertNotIn("LEAK", assistant["text"])

    def test_group_web_finally_clears_turn_and_typing_when_draft_cleanup_raises(self):
        handler, chat, _web = self.make_handler(web=FakeWebChat())
        handler._kimi_selection = lambda: ("kimi-code/k3", "low")
        handler._release_kimi_control = lambda _token: None
        handler._detect_apples_mentions = lambda _text: set()
        handler._maybe_route_apples_assistant_mention = lambda *_args, **_kwargs: None
        handler._has_pending_group_reply = lambda: False
        typing = []
        handler._set_typing_for_contact = lambda *args, **kwargs: typing.append((args, kwargs))

        def fail_group_draft(contact_id):
            self.assertEqual("apples", contact_id)
            raise OSError("group draft storage failed")

        handler._clear_chat_draft = fail_group_draft
        user = chat.append(role="user", text="@Kimi current", source="android-app:apples")
        with patch.object(threading, "excepthook") as thread_crash:
            handler._start_group_kimi_reply(
                chat, user["text"], sender_name="Astra", user_ts=user["ts"], hop_count=0,
            )
            self.wait_idle(handler)
            for _ in range(100):
                if typing and len(typing[-1][0]) > 1 and typing[-1][0][1].get("is_typing") is False:
                    break
                __import__("time").sleep(0.01)

        self.assertEqual({}, handler.state.kimi_active_turn)
        self.assertTrue(typing and typing[-1][0][1].get("is_typing") is False)
        thread_crash.assert_not_called()

    def test_idle_web_send_with_pending_queue_enqueues_at_tail(self):
        """队列非空时（worker 轮询窗口）新 Web 消息入队跟尾，不插队直达。"""
        handler, chat, web = self.make_handler()
        handler._kimi_chat_queue().append({
            "kind": "web", "contact_id": "kimi", "text": "先到",
            "record": {"ts": "ts-old"}, "staged_attachments": [], "attempts": 0,
        })
        with patch("push.threading.Thread"):  # 本用例只断言入队，不跑 worker
            handler._handle_kimi_web_chat_send({"text": "后到"}, "kimi", web)

        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertTrue(payload["queued"])
        self.assertEqual("kimi_queue_ahead", payload["reason"])
        queue = handler._kimi_chat_queue()
        self.assertEqual(["先到", "后到"], [item["text"] for item in queue])
        self.assertEqual("", handler.state.kimi_prepare_token)
        self.assertFalse(web.calls)
        self.assertEqual(["后到"], [row["text"] for row in chat.records])

    def test_idle_acp_send_with_pending_queue_enqueues_at_tail(self):
        """ACP 回滚通道与 Web 入口一致：队列非空时新消息入队跟尾。"""
        handler, chat, _web = self.make_handler()
        handler._kimi_chat_queue().append({
            "kind": "acp", "contact_id": "kimi", "text": "先到",
            "record": {"ts": "ts-old"}, "attempts": 0,
        })
        with patch("push.threading.Thread"):  # 本用例只断言入队，不跑 worker
            handler._handle_kimi_acp_chat_send({"text": "后到"}, "kimi")

        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertTrue(payload["queued"])
        self.assertEqual("kimi_queue_ahead", payload["reason"])
        self.assertEqual(["先到", "后到"], [item["text"] for item in handler._kimi_chat_queue()])
        self.assertEqual("", handler.state.kimi_prepare_token)

    def test_idle_group_reply_with_pending_queue_enqueues_at_tail(self):
        """群入口同样保序：队列非空时新群消息入队跟尾并留一条排队提示。"""
        handler, chat, web = self.make_handler()
        handler._kimi_chat_queue().append({
            "kind": "group", "contact_id": "apples", "text": "先到",
            "sender_name": "Astra", "user_ts": "ts-old", "hop_count": 0,
            "attachments": [], "attempts": 0,
        })
        with patch("push.threading.Thread"):  # 本用例只断言入队，不跑 worker
            outcome = handler._start_group_kimi_reply(
                chat, "后到", sender_name="Astra", user_ts="ts-new", hop_count=0,
            )

        self.assertEqual("started", outcome)
        queue = handler._kimi_chat_queue()
        self.assertEqual(["先到", "后到"], [item["text"] for item in queue])
        self.assertEqual("group", queue[-1]["kind"])
        notes = [row for row in chat.records if row.get("source") == "system:group:kimi"]
        self.assertEqual(1, len(notes))
        self.assertIn("已排队", notes[0]["text"])
        self.assertEqual("", handler.state.kimi_prepare_token)
        self.assertFalse(web.calls)

    def test_chat_queue_worker_survives_a_crashed_iteration(self):
        """worker 单次迭代崩溃只丢该条、记日志继续跑，running 标志不卡死。"""
        handler, chat, web = self.make_handler()
        original_dispatch = handler._dispatch_queued_kimi_chat
        dispatched = []

        def flaky_dispatch(item):
            dispatched.append(item.get("text"))
            if len(dispatched) == 1:
                raise RuntimeError("transient worker crash")
            return original_dispatch(item)

        handler._dispatch_queued_kimi_chat = flaky_dispatch
        with patch("push.threading.Thread"):  # 先入队两条，worker 由本用例手动启动
            for text in ("崩溃条", "存活条"):
                rec = chat.append(role="user", text=text, source="test:kimi-web")
                handler._enqueue_kimi_chat_turn({
                    "kind": "web", "contact_id": "kimi", "text": text,
                    "record": rec, "staged_attachments": [], "attempts": 0,
                })
        self.assertTrue(handler.state.kimi_chat_queue_worker_running)

        worker = threading.Thread(target=handler._kimi_chat_queue_worker, daemon=True)
        worker.start()
        self.wait_queue_drained(handler)
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertFalse(handler.state.kimi_chat_queue_worker_running)
        self.assertEqual(["崩溃条", "存活条"], dispatched)
        prompts = [row[2] for row in web.calls if row[0] == "submit"]
        self.assertEqual(1, len(prompts))
        self.assertIn("存活条", prompts[0])
        self.wait_idle(handler)

    def test_group_reentry_requeues_without_note_spam_and_keeps_attempts(self):
        """worker 重入再遇忙：重投保序、不重复写排队提示；kimi_turn_active 是
        「合法忙」类重投，不消耗 attempts 判死配额（时间兜底见专门用例）。"""
        handler, chat, web = self.make_handler()
        handler.state.kimi_active_turn = {
            "user_ts": "busy", "cancel_event": threading.Event(), "session_id": "s",
        }
        item = {
            "kind": "group", "contact_id": "apples", "text": "@Kimi 群消息",
            "sender_name": "Astra", "user_ts": "ts-1", "hop_count": 0,
            "attachments": [], "attempts": 2,
        }
        records_before = len(chat.records)
        with patch("push.threading.Thread"):  # 本用例只断言重投，不跑 worker
            outcome = handler._start_group_kimi_reply(
                chat, item["text"], sender_name="Astra", user_ts="ts-1",
                hop_count=0, queue_item=item,
            )

        self.assertEqual("requeued", outcome)
        self.assertEqual(2, item["attempts"])
        queue = handler._kimi_chat_queue()
        self.assertEqual(1, len(queue))
        self.assertIs(item, queue[0])
        self.assertEqual(records_before, len(chat.records))
        self.assertFalse(web.calls)

    def test_requeue_puts_item_back_at_front_in_order(self):
        """过渡态重投放回队首：本就先到的消息不被新到消息插队。"""
        handler, _chat, _web = self.make_handler()
        with patch("push.threading.Thread"):  # 本用例只断言队序，不跑 worker
            for text in ("新到一", "新到二"):
                handler._enqueue_kimi_chat_turn({
                    "kind": "web", "contact_id": "kimi", "text": text,
                    "record": {"ts": f"ts-{text}"}, "staged_attachments": [], "attempts": 0,
                })
            older = {
                "kind": "web", "contact_id": "kimi", "text": "先到",
                "record": {"ts": "ts-older"}, "staged_attachments": [], "attempts": 1,
            }
            # 用真故障类 reason：忙类 reason 不再消耗 attempts（见专门用例）。
            kept = handler._requeue_kimi_chat_turn(older, reason="dispatch_crashed")

        self.assertTrue(kept)
        self.assertEqual(2, older["attempts"])
        self.assertEqual(
            ["先到", "新到一", "新到二"],
            [item["text"] for item in handler._kimi_chat_queue()],
        )

    def test_busy_requeue_never_consumes_attempts_quota(self):
        """「合法忙」类重投（上游会话在跑长任务等，等忙完就能成）反复重投也
        不消耗 attempts 判死配额：消息一直留在队里等忙完，不落失败卡片。"""
        from push import KIMI_CHAT_QUEUE_BUSY_REQUEUE_REASONS, KIMI_CHAT_QUEUE_MAX_ATTEMPTS
        handler, chat, _web = self.make_handler()
        rec = chat.append(role="user", text="等忙完", source="test:kimi-web")
        for reason in sorted(KIMI_CHAT_QUEUE_BUSY_REQUEUE_REASONS):
            with self.subTest(reason=reason):
                item = {
                    "kind": "web", "contact_id": "kimi", "text": "等忙完",
                    "record": rec, "staged_attachments": [], "attempts": 0,
                }
                with patch("push.threading.Thread"):  # 只断言重投入队，不跑 worker
                    for _ in range(KIMI_CHAT_QUEUE_MAX_ATTEMPTS + 3):
                        kept = handler._requeue_kimi_chat_turn(item, reason=reason)
                        self.assertTrue(kept)
                        handler._kimi_chat_queue().clear()
                self.assertEqual(0, item["attempts"])
        self.assertFalse(any(
            row.get("source") == "kimi-web:queue-failed" for row in chat.records
        ))

    def test_busy_requeue_fails_visibly_after_wait_expired(self):
        """时间兜底：入队超过 KIMI_CHAT_QUEUE_MAX_WAIT_SECONDS 的忙类重投仍
        判死并落可见失败卡片——绝不无限重投，也绝不静默吞消息。"""
        from push import KIMI_CHAT_QUEUE_MAX_WAIT_SECONDS
        handler, chat, _web = self.make_handler()
        rec = chat.append(role="user", text="排太久了", source="test:kimi-web")
        item = {
            "kind": "web", "contact_id": "kimi", "text": "排太久了",
            "record": rec, "staged_attachments": [], "attempts": 0,
            "queued_at": (
                datetime.now(timezone.utc)
                - timedelta(seconds=KIMI_CHAT_QUEUE_MAX_WAIT_SECONDS + 60)
            ).isoformat(),
        }
        kept = handler._requeue_kimi_chat_turn(
            item, reason="kimi_web_busy_recovery_upstream_conflict",
        )

        self.assertFalse(kept)
        self.assertEqual(0, len(handler._kimi_chat_queue()))
        notes = [row for row in chat.records if row.get("source") == "kimi-web:queue-failed"]
        self.assertEqual(1, len(notes))
        self.assertIn("未能送出", notes[0]["text"])
        self.assertEqual("failed", handler.state.chat_reply_states["kimi"]["reply_state"])

    def test_real_fault_requeue_still_fails_at_attempts_cap(self):
        """真故障（崩溃/503/网络错误）照旧按 attempts 计数：超限判死落卡片。"""
        from push import KIMI_CHAT_QUEUE_MAX_ATTEMPTS
        handler, chat, _web = self.make_handler()
        rec = chat.append(role="user", text="真故障", source="test:kimi-web")
        item = {
            "kind": "web", "contact_id": "kimi", "text": "真故障",
            "record": rec, "staged_attachments": [], "attempts": 0,
        }
        with patch("push.threading.Thread"):  # 只断言重投入队，不跑 worker
            for expected in range(1, KIMI_CHAT_QUEUE_MAX_ATTEMPTS + 1):
                self.assertTrue(handler._requeue_kimi_chat_turn(item, reason="kimi_web_unavailable"))
                self.assertEqual(expected, item["attempts"])
                handler._kimi_chat_queue().clear()
            kept = handler._requeue_kimi_chat_turn(item, reason="kimi_web_unavailable")

        self.assertFalse(kept)
        self.assertEqual(0, len(handler._kimi_chat_queue()))
        notes = [row for row in chat.records if row.get("source") == "kimi-web:queue-failed"]
        self.assertEqual(1, len(notes))

    def test_failed_queued_web_message_discards_staged_attachment_files(self):
        """排队消息永久失败：随队 staged 附件文件一并清理，不留孤儿文件。"""
        with tempfile.TemporaryDirectory() as directory:
            stored_path = os.path.join(directory, "staged.png")
            with open(stored_path, "wb") as handle:
                handle.write(b"png")
            handler, chat, _web = self.make_handler()
            handler.state.attachments_dir = directory
            item = {
                "kind": "web", "contact_id": "kimi", "text": "带附件",
                "record": {"ts": "ts-1"},
                "staged_attachments": [{"stored_path": stored_path}],
                "attempts": 3,
            }
            handler._fail_queued_kimi_chat(item, "kimi_turn_active")

            self.assertFalse(os.path.exists(stored_path))
            notes = [row for row in chat.records if row.get("source") == "kimi-web:queue-failed"]
            self.assertEqual(1, len(notes))

    def test_failed_queued_group_message_discards_attachment_files(self):
        """群队列项的附件存在 attachments 键：永久失败时同样要清理文件。"""
        with tempfile.TemporaryDirectory() as directory:
            stored_path = os.path.join(directory, "group-staged.png")
            with open(stored_path, "wb") as handle:
                handle.write(b"png")
            handler, _chat, _web = self.make_handler()
            handler.state.attachments_dir = directory
            handler.state.group_chat = FakeGroupChat()
            item = {
                "kind": "group", "contact_id": "apples", "text": "@Kimi 带附件",
                "sender_name": "Astra", "user_ts": "ts-1", "hop_count": 0,
                "attachments": [{"stored_path": stored_path}],
                "attempts": 3,
            }
            handler._fail_queued_kimi_chat(item, "kimi_turn_active")

            self.assertFalse(os.path.exists(stored_path))
            notes = [
                row for row in handler.state.group_chat.records
                if row.get("source") == "system:group:kimi-queue-failed"
            ]
            self.assertEqual(1, len(notes))

    def test_queued_web_dispatch_survives_dead_worker_handler_socket(self):
        """worker 挂在已结束的请求 handler 上：其 socket 关闭后真实 _send_json
        吃 OSError Errno 9。响应必须被 proxy 捕获，投递本身照常完成。"""
        handler, chat, web = self.make_handler()

        def dead_socket_send(status, payload):
            raise OSError(9, "Bad file descriptor")

        handler._send_json = dead_socket_send
        rec = chat.append(role="user", text="排队消息", source="test:kimi-web")
        item = {
            "kind": "web", "contact_id": "kimi", "text": "排队消息",
            "record": rec, "staged_attachments": [], "attempts": 0,
        }
        outcome = handler._dispatch_queued_kimi_chat(item)
        self.wait_idle(handler)

        self.assertEqual("started", outcome)
        prompts = [row[2] for row in web.calls if row[0] == "submit"]
        self.assertEqual(1, len(prompts))
        self.assertIn("排队消息", prompts[0])
        # 真实 handler 的 _send_json（会写死 socket 的那条）全程未被触发。
        self.assertEqual([], handler.responses)
        self.assertFalse(any(
            row.get("source") == "kimi-web:queue-failed" for row in chat.records
        ))

    def test_queued_web_busy_reentry_with_dead_socket_requeues(self):
        """事故复现：busy 重入 + worker handler 连接已死。修复前 enqueue_busy
        的 200 queued 写 socket 炸出 Errno 9，被外层当成 dispatch_crashed 永久
        失败；现在响应被捕获，消息重投回队首等下次投递。"""
        class BusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                self.calls.append(("ensure", model, thinking))
                raise KimiWebSessionBusy("web-session-1")

        handler, chat, web = self.make_handler(web=BusyWeb())

        def dead_socket_send(status, payload):
            raise OSError(9, "Bad file descriptor")

        handler._send_json = dead_socket_send
        rec = chat.append(role="user", text="排队消息", source="test:kimi-web")
        item = {
            "kind": "web", "contact_id": "kimi", "text": "排队消息",
            "record": rec, "staged_attachments": [], "attempts": 0,
        }
        with patch("push.threading.Thread"):  # 只断言重投入队，不跑真实 worker
            outcome = handler._dispatch_queued_kimi_chat(item)

        self.assertEqual("requeued", outcome)
        # 本路径的重投 reason 是真故障类 kimi_web_recovery_failed（FakeWebChat
        # 无 recover_owned_orphaned_prompt），照旧消耗 attempts 配额。
        self.assertEqual(1, item["attempts"])
        queue = handler._kimi_chat_queue()
        self.assertEqual(1, len(queue))
        self.assertIs(item, queue[0])
        # 既无失败卡片也无重复入库：消息仍在队列里等下次空闲投递。
        self.assertFalse(any(
            row.get("source") == "kimi-web:queue-failed" for row in chat.records
        ))
        self.assertEqual(1, [row["text"] for row in chat.records].count("排队消息"))

    def test_queued_dispatch_crash_requeues_then_fails_visibly_at_cap(self):
        """投递崩溃（瞬时 fd 失效/上游刚重启）不再一次判死刑：先重投，
        attempts 超限才落可见失败卡片并标记 failed，消息绝不静默丢失。"""
        handler, chat, _web = self.make_handler()
        handler._handle_kimi_web_chat_send = Mock(side_effect=RuntimeError("transient crash"))
        rec = chat.append(role="user", text="崩溃消息", source="test:kimi-web")
        item = {
            "kind": "web", "contact_id": "kimi", "text": "崩溃消息",
            "record": rec, "staged_attachments": [], "attempts": 0,
        }
        with patch("push.threading.Thread"):  # 只断言重投入队，不跑真实 worker
            outcome = handler._dispatch_queued_kimi_chat(item)

        self.assertEqual("requeued", outcome)
        self.assertEqual(1, item["attempts"])
        self.assertIs(item, handler._kimi_chat_queue()[0])
        self.assertFalse(any(
            row.get("source") == "kimi-web:queue-failed" for row in chat.records
        ))

        handler._kimi_chat_queue().clear()
        with patch("push.KIMI_CHAT_QUEUE_MAX_ATTEMPTS", 0), patch("push.threading.Thread"):
            outcome = handler._dispatch_queued_kimi_chat(item)

        self.assertEqual("failed", outcome)
        self.assertEqual(0, len(handler._kimi_chat_queue()))
        notes = [row for row in chat.records if row.get("source") == "kimi-web:queue-failed"]
        self.assertEqual(1, len(notes))
        self.assertIn("未能送出", notes[0]["text"])
        self.assertEqual("failed", handler.state.chat_reply_states["kimi"]["reply_state"])

    def test_queued_group_dispatch_crash_requeues_then_fails_visibly(self):
        """群投递崩溃同样不静默丢：先重投，超限后群里落可见失败说明。"""
        handler, _chat, _web = self.make_handler()
        handler.state.group_chat = FakeGroupChat()
        handler._start_group_kimi_reply = Mock(side_effect=RuntimeError("transient crash"))
        item = {
            "kind": "group", "contact_id": "apples", "text": "@Kimi 群消息",
            "sender_name": "Astra", "user_ts": "ts-1", "hop_count": 0,
            "attachments": [], "attempts": 0,
        }
        with patch("push.threading.Thread"):
            first = handler._dispatch_queued_kimi_chat(item)

        self.assertEqual("requeued", first)
        self.assertIs(item, handler._kimi_chat_queue()[0])
        self.assertFalse(any(
            row.get("source") == "system:group:kimi-queue-failed"
            for row in handler.state.group_chat.records
        ))

        handler._kimi_chat_queue().clear()
        with patch("push.KIMI_CHAT_QUEUE_MAX_ATTEMPTS", 0), patch("push.threading.Thread"):
            second = handler._dispatch_queued_kimi_chat(item)

        self.assertEqual("failed", second)
        notes = [
            row for row in handler.state.group_chat.records
            if row.get("source") == "system:group:kimi-queue-failed"
        ]
        self.assertEqual(1, len(notes))


if __name__ == "__main__":
    unittest.main()
