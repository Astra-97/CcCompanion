import threading
import types
import unittest
from unittest.mock import Mock, patch

from kimi_acp import KimiACPCancelled, KimiACPError
from link_preview import LinkPreviewBundle
from kimi_web_client import KimiWebError, KimiWebSessionBusy
from push import KimiTerminalBusy, PushHandler, _should_generate_chat_append_tts


class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item

    def tail(self, limit):
        return list(self.records[-limit:])


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
            while not stop_event.wait(0.01):
                pass
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

    def test_private_web_turn_never_calls_acp_and_keeps_kimi_history_isolated(self):
        handler, chat, web = self.make_handler()
        handler.state.kimi_acp.prepare_session = Mock(side_effect=AssertionError("ACP fallback"))
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.wait_idle(handler)
        self.assertEqual(["hello", "reply"], [row["text"] for row in chat.records])
        self.assertEqual("kimi-web", handler.responses[-1][1]["turn"]["transport"])
        self.assertEqual(["ensure", "stream", "submit"], [row[0] for row in web.calls[:3]])
        self.assertEqual("auto", next(row[3]["permission_mode"] for row in web.calls if row[0] == "submit"))

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

    def test_web_chat_never_appends_or_submits_while_kimi_tui_is_uncertain(self):
        handler, chat, web = self.make_handler()
        handler.state.kimi_terminal = types.SimpleNamespace(
            input_transaction=lambda: __import__("contextlib").nullcontext(),
            release_for_acp=lambda: (_ for _ in ()).throw(
                KimiTerminalBusy("Kimi 终端可能仍在生成，请先离开终端后再发送")
            ),
        )

        handler._handle_kimi_web_chat_send({"text": "must not submit"}, "kimi", web)

        self.assertEqual([], chat.records)
        self.assertEqual([], web.calls)
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_terminal_busy", handler.responses[-1][1]["error"])
        self.assertEqual("", handler.state.kimi_prepare_token)

    def test_stream_subscription_is_ready_before_submit_and_exact_stop_is_fenced(self):
        handler, chat, web = self.make_handler(web=FakeWebChat(wait_for_stop=True))
        handler._handle_kimi_chat_send({"text": "hello"}, "kimi")
        self.assertEqual(["ensure", "stream", "submit"], [row[0] for row in web.calls[:3]])
        user_ts = handler.responses[-1][1]["turn"]["user_ts"]
        handler._handle_kimi_chat_stop(user_ts)
        self.wait_idle(handler)
        self.assertIn(("abort", "web-session-1", "prompt-1"), web.calls)
        self.assertTrue(any(row["role"] == "assistant" for row in chat.records))

    def test_second_turn_and_history_failure_fail_before_web_submit(self):
        handler, chat, web = self.make_handler()
        handler.state.kimi_active_turn = {"user_ts": "old", "cancel_event": threading.Event(), "session_id": "s"}
        handler._handle_kimi_chat_send({"text": "second"}, "kimi")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertFalse(web.calls)

        class BrokenChat(FakeChat):
            def append(self, **_record): raise OSError("disk")
        handler, _chat, web = self.make_handler(chat=BrokenChat())
        handler._handle_kimi_chat_send({"text": "first"}, "kimi")
        self.assertEqual(500, handler.responses[-1][0])
        self.assertEqual(["ensure"], [row[0] for row in web.calls])

    def test_orphaned_web_busy_prompt_is_recovered_then_client_retry_appends_once(self):
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
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_web_orphan_recovered_retry", handler.responses[-1][1]["error"])
        self.assertNotIn("retry after orphan", [row["text"] for row in chat.records])
        self.assertEqual(1, sum(bool(row.get("metadata", {}).get("orphan_recovery")) for row in chat.records))
        handler._handle_kimi_chat_send({"text": "retry after orphan"}, "kimi")
        self.wait_idle(handler)

        self.assertEqual(["ensure", "recover", "ensure", "stream", "submit"], [row[0] for row in web.calls])
        self.assertIn(("recover", "web-session-1"), web.calls)
        self.assertEqual(1, [row["text"] for row in chat.records].count("retry after orphan"))
        self.assertEqual(200, handler.responses[-1][0])

    def test_idle_stream_lost_lease_is_reconciled_before_new_history_or_submit(self):
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
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_web_orphan_recovered_retry", handler.responses[-1][1]["error"])
        self.assertFalse(any(row[0] == "submit" for row in web.calls))
        self.assertNotIn("new after idle", [row["text"] for row in chat.records])
        handler._handle_kimi_chat_send({"text": "new after idle"}, "kimi")
        self.wait_idle(handler)
        self.assertEqual(1, [row["text"] for row in chat.records].count("new after idle"))

    def test_upstream_busy_does_not_recover_while_a_legitimate_local_turn_owns_control(self):
        class ShouldNotRecover(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise AssertionError("local live turn must reject before Web access")
            def recover_owned_orphaned_prompt(self, _lease):
                raise AssertionError("must not abort a legitimate local turn")

        handler, chat, web = self.make_handler(web=ShouldNotRecover())
        handler.state.kimi_active_turn = {"user_ts": "live", "session_id": "web-session-1"}
        handler._handle_kimi_chat_send({"text": "do not interrupt"}, "kimi")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual([], chat.records)

    def test_ambiguous_orphan_busy_reports_conflict_without_history_append(self):
        class AmbiguousBusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise KimiWebSessionBusy("web-session-1")
            def load_turn_lease(self):
                return {"session_id": "web-session-1", "prompt_id": "prompt-old", "user_ts": "old", "state": "stream_lost", "created_at": "1"}
            def recover_owned_orphaned_prompt(self, _lease):
                from kimi_web_client import KimiWebRecoveryConflict
                raise KimiWebRecoveryConflict("prompt moved")

        handler, chat, web = self.make_handler(web=AmbiguousBusyWeb())
        handler._handle_kimi_chat_send({"text": "safe conflict"}, "kimi")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_web_busy_recovery_conflict", handler.responses[-1][1]["error"])
        self.assertEqual([], chat.records)
        self.assertEqual("", handler.state.kimi_prepare_token)

    def test_external_busy_without_durable_app_lease_never_aborts_or_appends(self):
        class ExternalBusyWeb(FakeWebChat):
            def ensure_active_session(self, *, model, thinking, **_kwargs):
                raise KimiWebSessionBusy("web-session-1")
            def recover_owned_orphaned_prompt(self, _lease):
                raise AssertionError("unknown provider prompt must never be aborted")

        handler, chat, _web = self.make_handler(web=ExternalBusyWeb())
        handler._handle_kimi_chat_send({"text": "external busy"}, "kimi")
        self.assertEqual(409, handler.responses[-1][0])
        self.assertEqual("kimi_web_busy_recovery_conflict", handler.responses[-1][1]["error"])
        self.assertEqual([], chat.records)

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
        self.assertEqual(1, sum(bool(row.get("metadata", {}).get("orphan_recovery")) for row in chat.records))
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
        self.assertTrue(event.is_set())
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
        self.assertTrue(event.is_set())
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
        self.assertEqual(["hello", "partial"], [r["text"] for r in chat.records])
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
        self.assertEqual("auto", next(row[3]["permission_mode"] for row in web.calls if row[0] == "submit"))
        self.assertEqual([1], routed)
        self.assertTrue(typing and typing[-1][1]["is_typing"] is False)

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


if __name__ == "__main__":
    unittest.main()
