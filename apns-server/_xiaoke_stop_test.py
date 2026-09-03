import json
import os
from pathlib import Path
import subprocess
import threading
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from push import (
    PushHandler,
    TmuxInjectionResult,
    _direct_tmux_injection,
    _inject_to_tmux_session,
    _xiaoke_attachment_paste_settle_seconds,
    _should_expire_chat_typing,
)


class XiaokeStopTest(unittest.TestCase):
    def handler(
        self,
        *,
        active: bool = True,
        user_ts: str = "turn-1",
        session: str = "cctg",
        transport: str = "tmux",
    ) -> PushHandler:
        handler = object.__new__(PushHandler)
        typing = {
            "is_typing": active,
            "since": user_ts if active else None,
            "session": session if active else "",
            "transport": transport if active else "",
            "turn_token": "a" * 32 if active else "",
        }
        handler.state = types.SimpleNamespace(
            allow_remote_control=True,
            active_session=session,
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
        )
        handler.responses = []
        handler.interrupted = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._set_chat_interrupted = lambda contact_id, **kwargs: handler.interrupted.append(
            (contact_id, kwargs)
        )
        return handler

    @staticmethod
    def request(user_ts: str = "turn-1", session: str = "cctg") -> dict[str, str]:
        return {"contact_id": "xiaoke", "user_ts": user_ts, "session": session}

    def send_handler(self) -> PushHandler:
        handler = self.handler(active=False)
        handler.state.settings = {}
        handler.state.bus_send_path = "/tmp/bus_send.py"
        handler.state.contact_chats = {"xiaoke": types.SimpleNamespace(
            append=lambda **record: {**record, "ts": "turn-1"}
        )}
        handler._chat_for_contact = lambda _contact: handler.state.contact_chats["xiaoke"]
        handler._source_for_request = lambda *_args: "android-app"
        handler._channel_transport_enabled_for = lambda _contact: False
        return handler

    def run_repository_stop_hook(self, records: list[dict], direct_last: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "turn.jsonl"
            transcript.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            payload_path = root / "payload.json"
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "out=''\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    -o) out=$2; shift 2 ;;\n"
                "    --data) printf '%s' \"$2\" > \"$FAKE_CURL_PAYLOAD\"; shift 2 ;;\n"
                "    *) shift ;;\n"
                "  esac\n"
                "done\n"
                "[ -n \"$out\" ] && printf '{}' > \"$out\"\n"
                "printf '200'\n",
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            hook_input = json.dumps({
                "transcript_path": str(transcript),
                "last_assistant_message": direct_last,
            })
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ.get('PATH', '')}",
                "CCC_AUTH_TOKEN": "test-token",
                "FAKE_CURL_PAYLOAD": str(payload_path),
            }
            result = subprocess.run(
                ["bash", str(Path(__file__).parent / "claude_hooks" / "ccc_stop_hook.sh")],
                input=hook_input,
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(payload_path.read_text(encoding="utf-8"))

    @staticmethod
    def user_record(text: str) -> dict:
        return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}

    @staticmethod
    def assistant_record(text: str) -> dict:
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    @patch("push.subprocess.run")
    def test_exact_active_turn_sends_one_literal_ctrl_c(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        handler = self.handler()

        handler._handle_chat_stop(self.request())

        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["tmux", "send-keys", "-t", "cctg", "C-c"])
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertTrue(handler.responses[-1][1]["stopped"])
        self.assertFalse(handler.state.typing_state["is_typing"])
        self.assertEqual(handler.interrupted[-1][1]["user_ts"], "turn-1")

    @patch("push.subprocess.run")
    def test_retry_is_idempotent_and_never_sends_a_second_key(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        handler = self.handler()

        handler._handle_chat_stop(self.request())
        handler._handle_chat_stop(self.request())

        self.assertEqual(run.call_count, 1)
        self.assertTrue(handler.responses[-1][1]["duplicate"])

    @patch("push.subprocess.run")
    def test_stale_turn_conflicts_without_touching_tmux(self, run) -> None:
        handler = self.handler(user_ts="new-turn")

        handler._handle_chat_stop(self.request(user_ts="old-turn"))

        run.assert_not_called()
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(handler.responses[-1][1]["error"], "stale_turn")

    @patch("push.subprocess.run")
    def test_stale_session_conflicts_without_touching_tmux(self, run) -> None:
        handler = self.handler(session="cctg-new")

        handler._handle_chat_stop(self.request(session="cctg-old"))

        run.assert_not_called()
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(handler.responses[-1][1]["error"], "stale_session")

    @patch("push.subprocess.run")
    def test_completion_race_is_benign_noop(self, run) -> None:
        handler = self.handler(active=False)

        handler._handle_chat_stop(self.request())

        run.assert_not_called()
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertTrue(handler.responses[-1][1]["already_finished"])

    @patch("push.subprocess.run")
    def test_non_tmux_active_turn_conflicts_without_touching_tmux(self, run) -> None:
        handler = self.handler(transport="channel")

        handler._handle_chat_stop(self.request())

        run.assert_not_called()
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(handler.responses[-1][1]["error"], "stale_session")

    @patch("push.subprocess.run")
    def test_tmux_failure_restores_exact_active_claim_for_retry(self, run) -> None:
        run.return_value = subprocess.CompletedProcess([], 1, "", "no session")
        handler = self.handler()

        handler._handle_chat_stop(self.request())

        self.assertEqual(run.call_count, 1)
        self.assertEqual(handler.responses[-1][0], 500)
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertEqual(handler.state.typing_state["since"], "turn-1")
        self.assertEqual(handler.state.xiaoke_stop_tombstone, {})

    @patch("push.subprocess.run")
    def test_blocked_ctrl_c_keeps_send_barrier_until_subprocess_returns(self, run) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_run(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return subprocess.CompletedProcess([], 0, "", "")

        run.side_effect = blocked_run
        handler = self.handler()
        append_calls = []
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(
            append=lambda **record: append_calls.append(record)
        )
        stop_thread = threading.Thread(target=lambda: handler._handle_chat_stop(self.request()))
        stop_thread.start()
        self.assertTrue(entered.wait(2))

        self.assertTrue(handler.state.typing_state["stopping"])
        self.assertIsNot(handler.state.typing_state.get("completed"), True)

        handler._handle_chat_send({"contact_id": "xiaoke", "text": "new turn"})

        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(handler.responses[-1][1]["error"], "xiaoke_turn_stopping")
        self.assertEqual(append_calls, [])
        release.set()
        stop_thread.join(2)
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(run.call_count, 1)

    def test_busy_send_queues_via_channel_instead_of_409(self) -> None:
        """转发/生成中发消息：turn active 时经 channel transport 排队而不是 409。"""
        handler = self.handler()  # active turn
        handler.state.channel_transport_enabled = True
        append_calls = []
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(
            append=lambda **record: append_calls.append(record) or {**record, "ts": "queued-1"}
        )
        channel_calls = []
        handler._send_to_channel_transport = lambda **kwargs: (
            channel_calls.append(kwargs) or (True, "", {"queued": True})
        )
        injection_calls = []
        handler._inject_to_session = lambda *args, **kwargs: (
            injection_calls.append((args, kwargs)) or TmuxInjectionResult(True)
        )
        typing_before = dict(handler.state.typing_state)

        handler._handle_chat_send({"contact_id": "xiaoke", "text": "[转发自Kairos]\nhello"})

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["transport"], "channel")
        self.assertNotIn("turn", payload)
        self.assertEqual(len(append_calls), 1)
        self.assertEqual(append_calls[0]["text"], "[转发自Kairos]\nhello")
        self.assertEqual(len(channel_calls), 1)
        self.assertEqual(channel_calls[0]["text"], "[转发自Kairos]\nhello")
        self.assertEqual(channel_calls[0]["contact_id"], "xiaoke")
        # 排队不接管 turn：不注入 tmux、不动 typing_state、不占 reservation
        self.assertEqual(injection_calls, [])
        self.assertEqual(handler.state.typing_state, typing_before)
        self.assertEqual(handler.state.xiaoke_send_reservation, {})

    def test_busy_send_channel_failure_surfaces_502_without_tmux_fallback(self) -> None:
        handler = self.handler()  # active turn
        handler.state.channel_transport_enabled = True
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(
            append=lambda **record: {**record, "ts": "queued-1"}
        )
        handler._send_to_channel_transport = lambda **kwargs: (False, "boom", None)
        injection_calls = []
        handler._inject_to_session = lambda *args, **kwargs: (
            injection_calls.append((args, kwargs)) or TmuxInjectionResult(True)
        )

        handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        status, payload = handler.responses[-1]
        self.assertEqual(status, 502)
        self.assertFalse(payload["ok"])
        self.assertEqual(injection_calls, [])
        self.assertTrue(handler.state.typing_state["is_typing"])

    @patch("push.subprocess.run")
    def test_completion_during_failed_ctrl_c_is_not_resurrected(self, run) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_failure(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return subprocess.CompletedProcess([], 1, "", "failed")

        run.side_effect = blocked_failure
        handler = self.handler()
        thread = threading.Thread(target=lambda: handler._handle_chat_stop(self.request()))
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(handler._complete_xiaoke_turn_if_match("a" * 32, "cctg"))
        release.set()
        thread.join(2)

        self.assertFalse(handler.state.typing_state["is_typing"])
        self.assertEqual(handler.state.xiaoke_stopping_claim, {})
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertTrue(handler.responses[-1][1]["already_finished"])

    @patch("push.subprocess.run")
    def test_strict_contact_rejects_unknown_without_ctrl_c(self, run) -> None:
        handler = self.handler()
        handler._handle_chat_stop({"contact_id": "not-xiaoke", "user_ts": "turn-1", "session": "cctg"})
        run.assert_not_called()
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(
            handler.responses[-1][1]["error"],
            "Stop only supports the XiaoKe private chat",
        )

        handler._handle_chat_stop({"contact_id": "XIAOKE", "user_ts": "turn-1", "session": "cctg"})
        run.assert_not_called()
        self.assertEqual(handler.responses[-1][0], 400)

    def test_late_old_hook_cannot_clear_new_turn(self) -> None:
        handler = self.handler(user_ts="new-turn")
        handler.state.typing_state["turn_token"] = "b" * 32
        handler.state.contact_typing_states["xiaoke"] = handler.state.typing_state

        matched = handler._complete_xiaoke_turn_if_match("a" * 32, "cctg")

        self.assertFalse(matched)
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertEqual(handler.state.typing_state["since"], "new-turn")

    def test_exact_natural_completion_retains_terminal_turn_identity(self) -> None:
        handler = self.handler(user_ts="turn-1", session="cctg", transport="tmux")

        matched = handler._complete_xiaoke_turn_if_match("a" * 32, "cctg")

        self.assertTrue(matched)
        self.assertEqual(handler.state.typing_state, {
            "is_typing": False,
            "since": "turn-1",
            "session": "cctg",
            "transport": "tmux",
            "turn_token": "a" * 32,
            "completed": True,
        })
        self.assertEqual(
            handler.state.contact_typing_states["xiaoke"],
            handler.state.typing_state,
        )

    def test_exact_long_tmux_turn_does_not_expire_after_120_seconds(self) -> None:
        state = {
            "is_typing": True,
            "transport": "tmux",
            "session": "cctg",
            "turn_token": "a" * 32,
        }
        self.assertFalse(_should_expire_chat_typing("xiaoke", state, 121))
        self.assertFalse(_should_expire_chat_typing("xiaoke", state, 3600))
        self.assertTrue(_should_expire_chat_typing("kairos", state, 121))

    def test_same_session_stale_completion_stamps_grace_then_expires(self) -> None:
        handler = self.handler(user_ts="new-turn")
        handler.state.typing_state["turn_token"] = "b" * 32
        handler.state.contact_typing_states["xiaoke"] = handler.state.typing_state

        matched = handler._complete_xiaoke_turn_if_match("a" * 32, "cctg")

        self.assertFalse(matched)
        state = handler.state.typing_state
        self.assertTrue(state["is_typing"])
        self.assertIsInstance(state.get("stale_completion_at"), float)
        # Inside the grace window the exact turn is still protected.
        self.assertFalse(_should_expire_chat_typing("xiaoke", state, 3600))
        # Once the grace window has passed the unresolved claim may expire.
        expired = {**state, "stale_completion_at": time.time() - 121.0}
        self.assertTrue(_should_expire_chat_typing("xiaoke", expired, 3600))

    def test_other_session_stale_completion_never_stamps_grace(self) -> None:
        handler = self.handler(user_ts="new-turn")
        handler.state.typing_state["turn_token"] = "b" * 32
        handler.state.contact_typing_states["xiaoke"] = handler.state.typing_state

        matched = handler._complete_xiaoke_turn_if_match("a" * 32, "other-session")

        self.assertFalse(matched)
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertNotIn("stale_completion_at", handler.state.typing_state)

    def test_empty_token_same_session_beacon_stamps_grace_then_expires(self) -> None:
        handler = self.handler()

        matched = handler._complete_xiaoke_turn_if_match("", "cctg")

        self.assertFalse(matched)
        state = handler.state.typing_state
        # Never an immediate clear: a late beacon racing a fresh injection
        # must leave the claim typing and only start the grace countdown.
        self.assertTrue(state["is_typing"])
        self.assertIsInstance(state.get("stale_completion_at"), float)
        self.assertEqual(handler.state.contact_typing_states["xiaoke"], state)
        # Inside the grace window the claim is still protected.
        self.assertFalse(_should_expire_chat_typing("xiaoke", state, 3600))
        # Once the grace window has passed the unresolved claim may expire.
        expired = {**state, "stale_completion_at": time.time() - 121.0}
        self.assertTrue(_should_expire_chat_typing("xiaoke", expired, 3600))

    def test_empty_token_other_session_beacon_never_stamps(self) -> None:
        handler = self.handler()

        matched = handler._complete_xiaoke_turn_if_match("", "other-session")

        self.assertFalse(matched)
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertNotIn("stale_completion_at", handler.state.typing_state)

    def test_repeated_empty_token_beacons_keep_earliest_stamp(self) -> None:
        handler = self.handler()

        handler._complete_xiaoke_turn_if_match("", "cctg")
        first_stamp = handler.state.typing_state["stale_completion_at"]
        time.sleep(0.02)
        handler._complete_xiaoke_turn_if_match("", "cctg")

        self.assertEqual(handler.state.typing_state["stale_completion_at"], first_stamp)

    def test_exact_completion_still_wins_after_empty_token_beacon(self) -> None:
        handler = self.handler()
        handler._complete_xiaoke_turn_if_match("", "cctg")

        matched = handler._complete_xiaoke_turn_if_match("a" * 32, "cctg")

        self.assertTrue(matched)
        self.assertFalse(handler.state.typing_state["is_typing"])
        self.assertTrue(handler.state.typing_state["completed"])

    def test_empty_token_beacon_on_idle_state_is_a_noop(self) -> None:
        handler = self.handler(active=False)

        matched = handler._complete_xiaoke_turn_if_match("", "cctg")

        self.assertFalse(matched)
        self.assertFalse(handler.state.typing_state["is_typing"])
        self.assertNotIn("stale_completion_at", handler.state.typing_state)

    def test_signal_only_append_stamps_claim_without_history_record(self) -> None:
        handler = self.handler()
        handler._check_auth = lambda: True
        handler._contact_id_from_body = lambda _body: "xiaoke"
        handler._source_for_request = lambda *_args: "ccc-stop-hook"
        append_calls = []
        handler._chat_for_contact = lambda _contact: types.SimpleNamespace(
            append=lambda **record: append_calls.append(record) or {**record}
        )

        handler._handle_chat_append({
            "role": "assistant",
            "text": "",
            "source": "ccc-stop-hook",
            "turn_token": "",
            "session_id": "cctg",
            "metadata": {"xiaoke_turn_token": "", "xiaoke_session_id": "cctg"},
        })

        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["signal_only"])
        self.assertEqual(append_calls, [])
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertIsInstance(
            handler.state.typing_state.get("stale_completion_at"), float
        )

    def test_concurrent_sends_reserve_before_history_and_only_one_injects(self) -> None:
        append_entered = threading.Event()
        release_append = threading.Event()
        append_calls = []
        inject_calls = []

        class BlockingChat:
            def append(self, **record):
                append_calls.append(record)
                append_entered.set()
                if len(append_calls) == 1:
                    self_outer.assertTrue(release_append.wait(2))
                return {**record, "ts": f"turn-{len(append_calls)}"}

        self_outer = self
        chat = BlockingChat()
        base = self.handler(active=False)
        base.state.contact_chats = {"xiaoke": chat}
        base.state.chat = chat
        base.state.settings = {}

        def make_handler():
            candidate = object.__new__(PushHandler)
            candidate.state = base.state
            candidate.responses = []
            candidate._send_json = lambda status, payload: candidate.responses.append((status, payload))
            candidate._source_for_request = lambda *_args: "android-app"
            candidate._channel_transport_enabled_for = lambda _contact: False
            candidate._chat_for_contact = lambda _contact: chat
            candidate._inject_to_session = lambda *args, **kwargs: (
                inject_calls.append((args, kwargs)) or (True, "")
            )
            return candidate

        first = make_handler()
        second = make_handler()
        first_thread = threading.Thread(
            target=lambda: first._handle_chat_send({"contact_id": "xiaoke", "text": "first"})
        )
        first_thread.start()
        self.assertTrue(append_entered.wait(2))

        second._handle_chat_send({"contact_id": "xiaoke", "text": "second"})

        self.assertEqual(second.responses[-1][0], 409)
        self.assertEqual(second.responses[-1][1]["error"], "xiaoke_turn_active")
        self.assertEqual(len(append_calls), 1)
        self.assertEqual(inject_calls, [])
        release_append.set()
        first_thread.join(2)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(len(append_calls), 1)
        self.assertEqual(len(inject_calls), 1)
        injected_text = inject_calls[0][0][1]
        self.assertTrue(inject_calls[0][1]["force_direct_tmux"])
        self.assertRegex(injected_text, r"^\[CCC_APP_TURN:[0-9a-f]{32}:cctg\]\n")
        self.assertEqual(
            base.state.typing_state["turn_token"],
            injected_text.split(":", 2)[1],
        )

    def test_channel_enabled_app_turn_still_uses_synchronous_tmux_stop_path(self) -> None:
        handler = self.send_handler()
        # Production may retain a deprecated Claude conversation UUID here;
        # it must never be treated as a tmux target.
        handler.state.active_session = "09e039bb-817e-4674-addd-b28aee740e69"
        handler._channel_transport_enabled_for = lambda _contact: True
        channel_calls = []
        injection_calls = []
        handler._send_to_channel_transport = lambda **kwargs: (
            channel_calls.append(kwargs) or (True, "", {"queued": True})
        )
        handler._inject_to_session = lambda *args, **kwargs: (
            injection_calls.append((args, kwargs)) or TmuxInjectionResult(True)
        )

        handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        self.assertEqual(channel_calls, [])
        self.assertEqual(len(injection_calls), 1)
        self.assertEqual(injection_calls[0][0][0], "cctg")
        self.assertTrue(injection_calls[0][1]["force_direct_tmux"])
        self.assertEqual(
            injection_calls[0][1]["paste_settle_seconds"],
            1.2,
        )
        self.assertRegex(injection_calls[0][0][1], r"^\[CCC_APP_TURN:[0-9a-f]{32}:cctg\]\n")
        self.assertEqual(handler.responses[-1][0], 200)
        turn = handler.responses[-1][1]["turn"]
        self.assertEqual(turn, {
            "contact_id": "xiaoke",
            "user_ts": "turn-1",
            "session": "cctg",
            "transport": "tmux",
        })
        self.assertEqual(handler.state.typing_state["session"], "cctg")
        self.assertEqual(handler.state.typing_state["transport"], "tmux")

        with patch(
            "push.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            handler._handle_chat_stop(self.request())

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["tmux", "send-keys", "-t", "cctg", "C-c"])
        self.assertTrue(handler.responses[-1][1]["stopped"])

    def test_history_failure_releases_only_its_send_reservation(self) -> None:
        handler = self.handler(active=False)
        handler.state.contact_chats = {"xiaoke": types.SimpleNamespace(
            append=lambda **_record: (_ for _ in ()).throw(RuntimeError("disk full"))
        )}
        handler._chat_for_contact = lambda contact: handler.state.contact_chats[contact]
        handler._source_for_request = lambda *_args: "android-app"

        handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        self.assertEqual(handler.responses[-1][0], 500)
        self.assertEqual(handler.state.xiaoke_send_reservation, {})
        self.assertFalse(handler.state.typing_state["is_typing"])

    def test_force_direct_tmux_bypasses_ready_async_bus(self) -> None:
        handler = self.handler(active=False)
        handler.state.bus_send_path = "/tmp/bus_send.py"
        process = MagicMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("push.os.path.exists", return_value=True) as path_exists,
            patch("push.subprocess.Popen", return_value=process) as popen,
            patch("push.subprocess.run", return_value=completed) as run,
        ):
            ok, err = handler._inject_to_session(
                "cctg",
                "hello",
                force_direct_tmux=True,
            )

        self.assertTrue(ok, err)
        path_exists.assert_not_called()
        self.assertEqual(popen.call_count, 1)
        load_args = popen.call_args.args[0]
        self.assertEqual(load_args[:3], ["tmux", "load-buffer", "-b"])
        self.assertRegex(load_args[3], r"^ccc-direct-[0-9a-f]{32}$")
        self.assertEqual(load_args[4], "-")
        paste_calls = [
            call.args[0] for call in run.call_args_list
            if call.args[0][1] == "paste-buffer"
        ]
        self.assertEqual(paste_calls[0][2:4], ["-b", load_args[3]])
        delete_calls = [
            call.args[0] for call in run.call_args_list
            if call.args[0][1] == "delete-buffer"
        ]
        self.assertEqual(delete_calls, [["tmux", "delete-buffer", "-b", load_args[3]]])

    def test_attachment_settle_scales_only_server_image_type_and_size(self) -> None:
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([], has_user_text=False),
            1.2,
        )
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([
                {"type": "file", "size": 50 * 1024 * 1024},
            ], has_user_text=False),
            1.2,
        )
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([
                {"type": "image", "size": 4 * 1024 * 1024},
                {"type": "file", "size": 1 * 1024 * 1024},
            ], has_user_text=False),
            1.2,
        )
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([
                {"type": "image", "size": 4 * 1024 * 1024},
            ], has_user_text=False),
            12.0,
        )
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([
                {"type": "image", "size": 50 * 1024 * 1024},
                {"type": "image", "size": 50 * 1024 * 1024},
            ], has_user_text=False),
            20.0,
        )
        # Untrusted/malformed internal values cannot turn the pause into an
        # arbitrary sleep; the ordinary text window remains the fallback.
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([
                {"type": "image", "size": "4194304"},
                {"type": "image", "size": True},
                {"type": "image", "size": -1},
            ], has_user_text=False),
            1.2,
        )
        # A caption does not shorten the image ingest window (2026-09-03:
        # captioned photos stalled on the prompt line with the 1.2s window).
        self.assertEqual(
            _xiaoke_attachment_paste_settle_seconds([
                {"type": "image", "size": 4 * 1024 * 1024},
            ], has_user_text=True),
            12.0,
        )

    def test_image_turn_adapts_with_or_without_caption(self) -> None:
        image = {
            "type": "image",
            "size": 4 * 1024 * 1024,
            "filename": "cat.jpg",
            "stored_path": "/tmp/cat.jpg",
        }
        observed: list[tuple[str, float]] = []
        for text in ("", "caption for the image"):
            with self.subTest(text=text or "image-only"):
                handler = self.send_handler()
                handler._consume_staged_attachments = lambda *_args: [{
                    "attachment_id": "server-owned-id",
                    "attachment_url": "/attachments/server-owned.jpg",
                    **image,
                }]
                handler._inject_to_session = lambda *args, **kwargs: (
                    observed.append((args[1], kwargs["paste_settle_seconds"]))
                    or TmuxInjectionResult(True)
                )

                handler._handle_chat_send({
                    "contact_id": "xiaoke",
                    "text": text,
                    "attachment_ids": ["server-owned-id"],
                    # Must not be trusted or reach the helper.  The patched
                    # consume method above is the authoritative test source.
                    "_pwa_staged_attachments": [{"type": "image", "size": 50 * 1024 * 1024}],
                })

                self.assertEqual(handler.responses[-1][0], 200)
                self.assertEqual(handler.responses[-1][1]["turn"]["session"], "cctg")

        self.assertEqual([entry[1] for entry in observed], [12.0, 12.0])
        self.assertIn("[用户发了图片: cat.jpg]", observed[0][0])
        self.assertIn("caption for the image", observed[1][0])

    def test_client_cannot_supply_internal_staged_attachment_records(self) -> None:
        handler = self.send_handler()
        injection_calls: list[tuple[tuple, dict]] = []
        handler._consume_staged_attachments = lambda *_args: []
        handler._inject_to_session = lambda *args, **kwargs: (
            injection_calls.append((args, kwargs)) or TmuxInjectionResult(True)
        )

        handler._handle_chat_send({
            "contact_id": "xiaoke",
            "text": "",
            "_pwa_staged_attachments": [{
                "type": "image",
                "size": 4 * 1024 * 1024,
                "filename": "attacker.jpg",
                "stored_path": "/tmp/attacker.jpg",
            }],
        })

        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(injection_calls, [])

    def test_scheduled_direct_fallback_submits_once_without_settle(self) -> None:
        operations: list[str] = []
        sent_keys: list[str] = []

        class Loader:
            returncode = 0

            def communicate(self, *, input=None, timeout=None):
                del input, timeout
                operations.append("load")
                return b"", b""

        def run_tmux(args, **_kwargs):
            operations.append(args[1])
            if args[1] == "send-keys":
                sent_keys.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("push.subprocess.Popen", return_value=Loader()),
            patch("push.subprocess.run", side_effect=run_tmux),
            patch("push.time.sleep") as sleep,
        ):
            ok, error = _inject_to_tmux_session(types.SimpleNamespace(), "cctg", "scheduled prompt")

        self.assertTrue(ok, error)
        self.assertEqual(operations, [
            "has-session", "load", "paste-buffer", "send-keys", "delete-buffer",
        ])
        self.assertEqual(sent_keys, ["Enter"])
        sleep.assert_not_called()

    def test_xiaoke_force_direct_settles_before_one_submit_for_text_and_attachments(self) -> None:
        cases = {
            "text": "plain terminal text",
            "single-image": "[用户发了图片: cat.jpg]\n本地路径: /tmp/cat.jpg",
            "multi-image-multiline": (
                "[CCC_APP_TURN:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:cctg]\n"
                "[Image #1]\n[Image #2]\n"
                "第一行说明\n第二行说明"
            ),
        }

        for name, text in cases.items():
            with self.subTest(name=name):
                operations: list[str] = []
                loaded: list[bytes | None] = []
                load_timeouts: list[float | None] = []
                sent_keys: list[str] = []

                class Loader:
                    returncode = 0

                    def communicate(self, *, input=None, timeout=None):
                        loaded.append(input)
                        load_timeouts.append(timeout)
                        operations.append("load")
                        return b"", b""

                def run_tmux(args, **_kwargs):
                    operations.append(args[1])
                    if args[1] == "send-keys":
                        sent_keys.append(args[-1])
                    return subprocess.CompletedProcess(args, 0, "", "")

                with (
                    patch("push.subprocess.Popen", return_value=Loader()),
                    patch("push.subprocess.run", side_effect=run_tmux),
                    patch(
                        "push.time.sleep",
                        side_effect=lambda seconds: operations.append(f"settle:{seconds}"),
                    ) as sleep,
                ):
                    handler = self.handler(active=False)
                    handler.state.bus_send_path = ""
                    result = handler._inject_to_session(
                        "cctg",
                        text,
                        force_direct_tmux=True,
                        paste_settle_seconds=1.2,
                    )

                self.assertTrue(result.success, result.error)
                self.assertEqual(loaded, [text.encode("utf-8")])
                self.assertEqual(load_timeouts, [3])
                self.assertEqual(operations, [
                    "has-session", "load", "paste-buffer", "settle:1.2", "send-keys", "delete-buffer",
                ])
                self.assertEqual(sent_keys, ["Enter"])
                sleep.assert_called_once_with(1.2)

    def test_empty_direct_tmux_injection_never_submits_enter(self) -> None:
        with (
            patch("push.subprocess.Popen") as popen,
            patch("push.subprocess.run") as run,
            patch("push.time.sleep") as sleep,
        ):
            result = _direct_tmux_injection("cctg", "\n \t")

        self.assertFalse(result.success)
        self.assertEqual(result.phase, "validate")
        popen.assert_not_called()
        run.assert_not_called()
        sleep.assert_not_called()

    def test_load_buffer_timeout_kills_reaps_deletes_and_releases_turn_lock(self) -> None:
        handler = self.send_handler()
        loader = MagicMock()
        loader.communicate.side_effect = [
            subprocess.TimeoutExpired(["tmux", "load-buffer"], 3),
            (b"", b""),
        ]
        completed = subprocess.CompletedProcess([], 0, "", "")

        with (
            patch("push.subprocess.Popen", return_value=loader),
            patch("push.subprocess.run", return_value=completed) as run,
        ):
            thread = threading.Thread(
                target=lambda: handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})
            )
            thread.start()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        loader.kill.assert_called_once()
        self.assertEqual(loader.communicate.call_count, 2)
        self.assertEqual(handler.responses[-1][0], 502)
        self.assertFalse(handler.responses[-1][1]["injection_uncertain"])
        self.assertFalse(handler.state.typing_state["is_typing"])
        delete_calls = [
            call.args[0] for call in run.call_args_list
            if call.args[0][1] == "delete-buffer"
        ]
        self.assertEqual(len(delete_calls), 1)
        acquired = []
        probe = threading.Thread(
            target=lambda: (
                handler.state.xiaoke_stop_lock.acquire(),
                acquired.append(True),
                handler.state.xiaoke_stop_lock.release(),
            )
        )
        probe.start()
        probe.join(1)
        self.assertEqual(acquired, [True])

    def test_load_buffer_failure_is_safe_idle_and_deletes_named_buffer(self) -> None:
        loader = MagicMock(returncode=1)
        loader.returncode = 1
        loader.communicate.return_value = (b"", b"permission denied")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch("push.subprocess.Popen", return_value=loader),
            patch("push.subprocess.run", return_value=completed) as run,
        ):
            result = _direct_tmux_injection("cctg", "hello")

        self.assertIsInstance(result, TmuxInjectionResult)
        self.assertFalse(result.success)
        self.assertEqual(result.phase, "load")
        self.assertFalse(result.injection_uncertain)
        self.assertIn("permission denied", result.error)
        self.assertFalse(any(call.args[0][1] == "paste-buffer" for call in run.call_args_list))
        self.assertEqual(sum(call.args[0][1] == "delete-buffer" for call in run.call_args_list), 1)

    def test_paste_failure_is_safe_idle_and_deletes_named_buffer(self) -> None:
        handler = self.send_handler()
        loader = MagicMock(returncode=0)
        loader.returncode = 0
        loader.communicate.return_value = (b"", b"")

        def run_tmux(args, **_kwargs):
            if args[1] == "paste-buffer":
                return subprocess.CompletedProcess(args, 1, "", "paste rejected")
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("push.subprocess.Popen", return_value=loader),
            patch("push.subprocess.run", side_effect=run_tmux) as run,
        ):
            handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        self.assertEqual(handler.responses[-1][0], 502)
        self.assertFalse(handler.responses[-1][1]["injection_uncertain"])
        self.assertFalse(handler.state.typing_state["is_typing"])
        self.assertFalse(any(
            call.args[0][1] == "send-keys" and call.args[0][-1] == "C-c"
            for call in run.call_args_list
        ))
        self.assertEqual(sum(call.args[0][1] == "delete-buffer" for call in run.call_args_list), 1)

    def test_enter_failure_with_confirmed_cleanup_returns_safe_idle(self) -> None:
        handler = self.send_handler()
        loader = MagicMock(returncode=0)
        loader.returncode = 0
        loader.communicate.return_value = (b"", b"")

        def run_tmux(args, **_kwargs):
            if args[1] == "send-keys" and args[-1] == "Enter":
                return subprocess.CompletedProcess(args, 1, "", "enter rejected")
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("push.subprocess.Popen", return_value=loader),
            patch("push.subprocess.run", side_effect=run_tmux) as run,
        ):
            handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        self.assertEqual(handler.responses[-1][0], 502)
        self.assertFalse(handler.responses[-1][1]["injection_uncertain"])
        self.assertFalse(handler.state.typing_state["is_typing"])
        ctrl_c = [
            call.args[0] for call in run.call_args_list
            if call.args[0][1] == "send-keys" and call.args[0][-1] == "C-c"
        ]
        self.assertEqual(ctrl_c, [["tmux", "send-keys", "-t", "cctg", "C-c"]])

    def test_enter_failure_cleanup_failure_keeps_exact_turn_for_send_and_stop(self) -> None:
        handler = self.send_handler()
        loader = MagicMock(returncode=0)
        loader.returncode = 0
        loader.communicate.return_value = (b"", b"")

        def failing_cleanup(args, **_kwargs):
            if args[1] == "send-keys":
                return subprocess.CompletedProcess(args, 1, "", "key rejected")
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("push.subprocess.Popen", return_value=loader),
            patch("push.subprocess.run", side_effect=failing_cleanup),
        ):
            handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        self.assertEqual(handler.responses[-1][0], 502)
        self.assertTrue(handler.responses[-1][1]["injection_uncertain"])
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertTrue(handler.state.typing_state["injection_uncertain"])
        self.assertEqual(handler.state.typing_state["since"], "turn-1")
        self.assertEqual(handler.state.typing_state["session"], "cctg")

        handler._handle_chat_send({"contact_id": "xiaoke", "text": "new"})
        self.assertEqual(handler.responses[-1][0], 409)
        self.assertEqual(handler.responses[-1][1]["error"], "xiaoke_turn_active")

        stop = object.__new__(PushHandler)
        stop.state = handler.state
        stop.responses = []
        stop.interrupted = []
        stop._send_json = lambda status, payload: stop.responses.append((status, payload))
        stop._set_chat_interrupted = lambda contact_id, **kwargs: stop.interrupted.append(
            (contact_id, kwargs)
        )
        with patch(
            "push.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            stop._handle_chat_stop(self.request())

        self.assertEqual(run.call_args.args[0], ["tmux", "send-keys", "-t", "cctg", "C-c"])
        self.assertEqual(stop.responses[-1][0], 200)
        self.assertTrue(stop.responses[-1][1]["stopped"])
        self.assertFalse(stop.state.typing_state["is_typing"])

    def test_enter_timeout_with_unconfirmed_cleanup_never_claims_idle(self) -> None:
        handler = self.send_handler()
        loader = MagicMock(returncode=0)
        loader.returncode = 0
        loader.communicate.return_value = (b"", b"")

        def timed_out_enter(args, **_kwargs):
            if args[1] == "send-keys" and args[-1] == "Enter":
                raise subprocess.TimeoutExpired(args, 3)
            if args[1] == "send-keys" and args[-1] == "C-c":
                return subprocess.CompletedProcess(args, 1, "", "cleanup rejected")
            return subprocess.CompletedProcess(args, 0, "", "")

        with (
            patch("push.subprocess.Popen", return_value=loader),
            patch("push.subprocess.run", side_effect=timed_out_enter),
        ):
            handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})

        self.assertEqual(handler.responses[-1][0], 502)
        self.assertTrue(handler.responses[-1][1]["injection_uncertain"])
        self.assertTrue(handler.state.typing_state["is_typing"])
        self.assertEqual(handler.state.typing_state["injection_phase"], "enter")
        self.assertEqual(handler.state.typing_state["since"], "turn-1")

    def test_concurrent_direct_injection_cannot_overwrite_named_buffer(self) -> None:
        first_loaded = threading.Event()
        release_first = threading.Event()
        buffers: dict[str, bytes] = {}
        pasted: list[bytes] = []
        guard = threading.Lock()
        loader_count = 0

        class FakeLoader:
            def __init__(self, args):
                nonlocal loader_count
                self.name = args[3]
                self.returncode = 0
                with guard:
                    loader_count += 1
                    self.number = loader_count

            def communicate(self, input=None, timeout=None):
                with guard:
                    buffers[self.name] = input
                if self.number == 1:
                    first_loaded.set()
                    self_outer.assertTrue(release_first.wait(2))
                return b"", b""

        self_outer = self

        def fake_popen(args, **_kwargs):
            return FakeLoader(args)

        def fake_run(args, **_kwargs):
            if args[1] == "paste-buffer":
                name = args[3]
                with guard:
                    pasted.append(buffers[name])
            elif args[1] == "delete-buffer":
                with guard:
                    buffers.pop(args[3], None)
            return subprocess.CompletedProcess(args, 0, "", "")

        results = []
        with (
            patch("push.subprocess.Popen", side_effect=fake_popen),
            patch("push.subprocess.run", side_effect=fake_run),
        ):
            first = threading.Thread(
                target=lambda: results.append(_direct_tmux_injection("cctg", "first-marker"))
            )
            first.start()
            self.assertTrue(first_loaded.wait(2))
            second = threading.Thread(
                target=lambda: results.append(_direct_tmux_injection("cctg", "second-marker"))
            )
            second.start()
            second.join(2)
            self.assertFalse(second.is_alive())
            release_first.set()
            first.join(2)

        self.assertFalse(first.is_alive())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.success for result in results))
        self.assertCountEqual(pasted, [b"first-marker", b"second-marker"])
        self.assertEqual(buffers, {})

    def test_stop_waits_until_synchronous_injection_finishes(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        ctrl_c_calls = []
        handler = self.handler(active=False)
        handler.state.settings = {}
        handler.state.contact_chats = {"xiaoke": types.SimpleNamespace(
            append=lambda **record: {**record, "ts": "turn-1"}
        )}
        handler._chat_for_contact = lambda _contact: handler.state.contact_chats["xiaoke"]
        handler._source_for_request = lambda *_args: "android-app"
        handler._channel_transport_enabled_for = lambda _contact: False

        def blocking_inject(*_args, **kwargs):
            self.assertTrue(kwargs["force_direct_tmux"])
            entered.set()
            self.assertTrue(release.wait(2))
            return True, ""

        handler._inject_to_session = blocking_inject
        send_thread = threading.Thread(
            target=lambda: handler._handle_chat_send({"contact_id": "xiaoke", "text": "hello"})
        )
        send_thread.start()
        self.assertTrue(entered.wait(2))

        stop_handler = object.__new__(PushHandler)
        stop_handler.state = handler.state
        stop_handler.responses = []
        stop_handler.interrupted = []
        stop_handler._send_json = lambda status, payload: stop_handler.responses.append((status, payload))
        stop_handler._set_chat_interrupted = lambda contact_id, **kwargs: stop_handler.interrupted.append(
            (contact_id, kwargs)
        )
        stop_thread = threading.Thread(
            target=lambda: stop_handler._handle_chat_stop(self.request())
        )
        with patch("push.subprocess.run") as run:
            run.side_effect = lambda args, **_kwargs: (
                ctrl_c_calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")
            )
            stop_thread.start()
            stop_thread.join(0.1)
            self.assertTrue(stop_thread.is_alive())
            self.assertEqual(ctrl_c_calls, [])
            release.set()
            send_thread.join(2)
            stop_thread.join(2)

        self.assertFalse(send_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(ctrl_c_calls, [["tmux", "send-keys", "-t", "cctg", "C-c"]])

    def test_repository_stop_hook_returns_exact_marker_identity(self) -> None:
        token = "a" * 32
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{token}:cctg]\nhello"),
            self.assistant_record("done"),
        ], "done")
        self.assertEqual(payload["turn_token"], token)
        self.assertEqual(payload["session_id"], "cctg")
        self.assertEqual(payload["metadata"]["xiaoke_turn_token"], token)

    def test_repository_stop_hook_turns_exact_xhs_marker_into_metadata(self) -> None:
        token = "a" * 32
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{token}:cctg]\nlogin"),
            self.assistant_record("请登录。\n[[CCC_XHS_LOGIN_CARD:v1]]"),
        ], "请登录。\n[[CCC_XHS_LOGIN_CARD:v1]]")
        self.assertEqual(payload["text"], "请登录。")
        self.assertTrue(payload["metadata"]["xhs_login_card"])
        self.assertNotIn("CCC_XHS_LOGIN_CARD", payload["text"])

    def test_repository_stop_hook_marker_only_keeps_visible_card_label(self) -> None:
        token = "a" * 32
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{token}:cctg]\nlogin"),
            self.assistant_record("[[CCC_XHS_LOGIN_CARD:v1]]"),
        ], "[[CCC_XHS_LOGIN_CARD:v1]]")
        self.assertEqual(payload["text"], "小红书登录已失效，点下方卡片重新登录。")
        self.assertTrue(payload["metadata"]["xhs_login_card"])

    def test_repository_stop_hook_does_not_trigger_on_inline_xhs_marker(self) -> None:
        token = "a" * 32
        text = "示例：[[CCC_XHS_LOGIN_CARD:v1]]"
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{token}:cctg]\nexample"),
            self.assistant_record(text),
        ], text)
        self.assertEqual(payload["text"], text)
        self.assertNotIn("xhs_login_card", payload["metadata"])

    def test_delayed_old_hook_never_borrows_newer_turn_marker(self) -> None:
        old_token = "a" * 32
        new_token = "b" * 32
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{old_token}:cctg]\nold"),
            self.assistant_record("old answer"),
            self.user_record(f"[CCC_APP_TURN:{new_token}:cctg]\nnew"),
        ], "old answer")
        self.assertEqual(payload.get("turn_token"), old_token)
        self.assertNotEqual(payload.get("turn_token"), new_token)

    def test_interrupt_restored_prompt_row_resolves_last_marker(self) -> None:
        # Ctrl-C restores the stopped prompt into the CLI input; the next
        # injection appends after it, so one submitted user row carries both
        # markers.  The completion must name the newest injected turn.
        stopped_token = "a" * 32
        active_token = "b" * 32
        payload = self.run_repository_stop_hook([
            self.user_record(
                f"[CCC_APP_TURN:{stopped_token}:cctg]\nstopped prompt"
                f"[CCC_APP_TURN:{active_token}:cctg]\nnew prompt"
            ),
            self.assistant_record("merged answer"),
        ], "merged answer")
        self.assertEqual(payload.get("turn_token"), active_token)
        self.assertEqual(payload.get("session_id"), "cctg")

    def test_duplicate_assistant_text_across_turns_downgrades_to_beacon(self) -> None:
        # Ambiguous correlation must never name an exact token, but it still
        # proves a turn ended here — the hook downgrades to an empty-token
        # sessionful beacon instead of staying silent.
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{'a' * 32}:cctg]\nold"),
            self.assistant_record("same answer"),
            self.user_record(f"[CCC_APP_TURN:{'b' * 32}:cctg]\nnew"),
            self.assistant_record("same answer"),
        ], "same answer")
        self.assertEqual(payload["turn_token"], "")
        self.assertEqual(payload["session_id"], "cctg")
        self.assertEqual(payload["metadata"]["xiaoke_turn_token"], "")
        self.assertEqual(payload["metadata"]["xiaoke_session_id"], "cctg")

    def test_missing_assistant_location_downgrades_to_beacon(self) -> None:
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{'a' * 32}:cctg]\nhello"),
            self.assistant_record("different answer"),
        ], "not flushed")
        self.assertEqual(payload["turn_token"], "")
        self.assertEqual(payload["session_id"], "cctg")

    def test_terminal_turn_after_app_turn_emits_sessionful_beacon(self) -> None:
        # 02:53 incident, phase two: the user switched to typing in the
        # terminal (no marker), so the resolver sees current=None for the
        # final turn.  The hook must still emit an empty-token beacon carrying
        # the session from the older marker so the stuck claim can expire.
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{'a' * 32}:cctg]\napp msg"),
            self.assistant_record("app answer"),
            self.user_record("terminal typed"),
            self.assistant_record("terminal answer"),
        ], "terminal answer")
        self.assertEqual(payload["turn_token"], "")
        self.assertEqual(payload["session_id"], "cctg")
        self.assertEqual(payload["text"], "terminal answer")

    def test_markerless_transcript_posts_without_identity(self) -> None:
        # A CLI session that never received an App turn has no session to
        # name; the payload must carry no identity fields at all.
        payload = self.run_repository_stop_hook([
            self.user_record("terminal typed"),
            self.assistant_record("terminal answer"),
        ], "terminal answer")
        self.assertNotIn("turn_token", payload)
        self.assertNotIn("session_id", payload)
        self.assertNotIn("metadata", payload)

    def test_empty_assistant_text_with_session_posts_signal_only_beacon(self) -> None:
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{'a' * 32}:cctg]\nhello"),
        ], "")
        self.assertEqual(payload["turn_token"], "")
        self.assertEqual(payload["session_id"], "cctg")
        self.assertEqual(payload["text"], "")

    def test_streamed_assistant_parts_survive_tool_result_rows(self) -> None:
        token = "a" * 32
        payload = self.run_repository_stop_hook([
            self.user_record(f"[CCC_APP_TURN:{token}:cctg]\nhello"),
            self.assistant_record("part one"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            self.assistant_record("part two"),
        ], "part one\n\npart two")
        self.assertEqual(payload.get("turn_token"), token)
        self.assertEqual(payload.get("session_id"), "cctg")


if __name__ == "__main__":
    unittest.main()
