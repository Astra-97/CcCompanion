import threading
import types
import unittest
from unittest.mock import Mock, patch

from kimi_acp import KimiACPCancelled, KimiACPError
from link_preview import LinkPreviewBundle
from push import KimiTerminalBusy, PushHandler, _should_generate_chat_append_tts


class FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item


class KimiChatRoutingTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
