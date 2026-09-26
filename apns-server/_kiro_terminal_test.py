"""kiro 对齐 CC r3 (2026-09-26): 终端页 Kiro 标签 — bridge + handler tests.

Every tmux call goes to an in-memory FakeTmux; no real pane, kiro-cli or
production Kiro session is touched.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from kimi_terminal_observer import KiroTerminalObserver
import kiro_terminal
from kiro_terminal import (
    KIRO_TERMINAL_OWNER_OPTION,
    KIRO_TERMINAL_OWNER_VALUE,
    KIRO_TERMINAL_SESSION_OPTION,
    KiroSessionTurnProbe,
    KiroTerminalBridge,
    KiroTerminalBusy,
    KiroTerminalUnavailable,
)
from push import PushHandler

SID = "3f9b86a0-f469-4bc2-b668-2cdb22394793"
OTHER_SID = "0cfbd059-4bb9-454d-a309-6220ff9174e0"


def _done(stdout: str = "", returncode: int = 0):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class FakeTmux:
    """Just enough tmux for the bridge: one session per name, one pane each."""

    def __init__(self, sessions_dir: Path, *, resume_ok: bool = True):
        self.sessions = {}
        self.calls = []
        self.buffers = {}
        self.sent = []
        self.screen = "›  ask a question or describe a task ↵\n"
        self.sessions_dir = sessions_dir
        self.resume_ok = resume_ok
        self._next_pane = 10
        self._next_pid = 4000
        self.lock = threading.Lock()

    def _session(self, target):
        return self.sessions.get(str(target).lstrip("="))

    def _pane_session(self, pane):
        return next((s for s in self.sessions.values() if s["pane"] == pane), None)

    def __call__(self, argv, **_kwargs):
        with self.lock:
            self.calls.append(list(argv))
            cmd = argv[1]
            if cmd == "has-session":
                return _done(returncode=0 if self._session(argv[3]) else 1)
            if cmd == "show-options":
                session = self._session(argv[4])
                if not session or argv[5] not in session["options"]:
                    return _done(returncode=1)
                return _done(session["options"][argv[5]] + "\n")
            if cmd == "display-message":
                session = self._session(argv[4])
                if not session:
                    return _done(returncode=1)
                return _done(f"{session['pane']}|{1 if session['dead'] else 0}|{session['pid']}\n")
            if cmd == "new-session":
                name = argv[argv.index("-s") + 1]
                if name in self.sessions:
                    return _done(returncode=1)
                self.sessions[name] = {
                    "pane": f"%{self._next_pane}", "pid": self._next_pid, "dead": False,
                    "options": {}, "argv": argv[argv.index("48") + 1:],
                }
                self._next_pane += 1
                self._next_pid += 1
                return _done()
            if cmd == "set-option":
                session = self._session(argv[3])
                if not session:
                    return _done(returncode=1)
                session["options"][argv[4]] = argv[5]
                return _done()
            if cmd == "respawn-pane":
                session = self._session(argv[4])
                if not session:
                    return _done(returncode=1)
                session["pid"] = self._next_pid
                self._next_pid += 1
                session["argv"] = argv[argv.index("/usr/bin/env"):]
                if self.resume_ok:
                    resume = argv[argv.index("--resume-id") + 1]
                    (self.sessions_dir / f"{resume}.lock").write_text(
                        json.dumps({"pid": session["pid"]}), encoding="utf-8",
                    )
                return _done()
            if cmd == "kill-session":
                name = str(argv[3]).lstrip("=")
                session = self.sessions.pop(name, None)
                if session is None:
                    return _done(returncode=1)
                for lock in self.sessions_dir.glob("*.lock"):
                    try:
                        if json.loads(lock.read_text())["pid"] == session["pid"]:
                            lock.unlink()
                    except Exception:
                        pass
                return _done()
            if cmd == "capture-pane":
                if not self._pane_session(argv[3]):
                    return _done(returncode=1)
                return _done(self.screen)
            if cmd == "send-keys":
                if not self._pane_session(argv[3]):
                    return _done(returncode=1)
                self.sent.append(("key", argv[4]))
                return _done()
            if cmd == "set-buffer":
                self.buffers[argv[3]] = argv[5]
                return _done()
            if cmd == "paste-buffer":
                self.sent.append(("paste", self.buffers.pop(argv[3], None)))
                return _done()
            if cmd == "delete-buffer":
                self.buffers.pop(argv[3], None)
                return _done()
            if cmd == "resize-window":
                return _done()
            raise AssertionError(f"unexpected tmux call {argv}")


class FakeProbe:
    def __init__(self):
        self.value = (3, 3)

    def counts(self, _session_id):
        return self.value

    def busy(self, session_id):
        counts = self.counts(session_id)
        return None if counts is None else counts[0] > counts[1]


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kiro-terminal-test-"))
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self.command = self.root / "kiro-cli"
        self.command.write_text("#!/bin/sh\n", encoding="utf-8")
        self.command.chmod(0o755)
        self.tmux = FakeTmux(self.sessions_dir)
        self.probe = FakeProbe()
        self.killed = []

        def killer(pid, sig):
            self.killed.append((pid, sig))
            for session in self.tmux.sessions.values():
                if session["pid"] == abs(pid):
                    session["dead"] = True

        self.bridge = KiroTerminalBridge(
            command=self.command, cwd=self.cwd, tmux_session="ccc-kiro-terminal",
            idle_seconds=60, sessions_dir=self.sessions_dir, runner=self.tmux,
            process_killer=killer, shutdown_wait_seconds=0.05, resume_wait_seconds=0.6,
            turn_probe=self.probe, sleep=lambda _s: None,
        )

    def tearDown(self):
        with self.bridge._lock:
            self.bridge._clear_lease_locked()
        shutil.rmtree(self.root, ignore_errors=True)


class KiroTerminalBridgeTest(BridgeTestBase):
    def test_ensure_resumes_exact_session_with_app_model_and_effort(self):
        pane = self.bridge.ensure(SID, model="claude-opus-5.5", effort="max")
        session = self.tmux.sessions["ccc-kiro-terminal"]
        self.assertEqual(session["pane"], pane)
        self.assertEqual(
            ["/usr/bin/env", "CCC_KIRO_TERMINAL_BRIDGE=1", str(self.command), "chat",
             "--resume-id", SID, "--model", "claude-opus-5.5", "--effort", "max"],
            session["argv"],
        )
        self.assertEqual(KIRO_TERMINAL_OWNER_VALUE, session["options"][KIRO_TERMINAL_OWNER_OPTION])
        self.assertNotIn(SID, session["options"][KIRO_TERMINAL_SESSION_OPTION])  # fingerprint only
        lease = self.bridge.lease_for_pane(pane)
        self.assertRegex(lease, r"^[A-Za-z0-9_-]{32,128}$")
        # A second acquire reuses the same pane and lease.
        self.assertEqual(pane, self.bridge.ensure(SID))
        self.assertEqual(lease, self.bridge.lease_for_pane(pane))
        self.assertEqual(1, sum(1 for c in self.tmux.calls if c[1] == "new-session"))

    def test_unvalidated_model_and_effort_are_not_passed(self):
        self.bridge.ensure(SID, model="bad model; rm", effort="turbo")
        self.assertNotIn("--model", self.tmux.sessions["ccc-kiro-terminal"]["argv"])
        self.assertNotIn("--effort", self.tmux.sessions["ccc-kiro-terminal"]["argv"])

    def test_foreign_session_with_same_name_is_never_adopted_or_killed(self):
        self.tmux.sessions["ccc-kiro-terminal"] = {
            "pane": "%3", "pid": 99, "dead": False, "options": {}, "argv": ["bash"],
        }
        with self.assertRaises(KiroTerminalUnavailable):
            self.bridge.ensure(SID)
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)
        self.assertEqual([], self.killed)

    def test_failed_resume_tears_the_pane_down(self):
        self.tmux.resume_ok = False
        with self.assertRaises(KiroTerminalUnavailable):
            self.bridge.ensure(SID)
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)

    def test_invalid_session_id_is_rejected_before_tmux(self):
        with self.assertRaises(kiro_terminal.KiroTerminalNoActiveSession):
            self.bridge.ensure("../../etc/passwd")
        self.assertEqual([], self.tmux.calls)

    def test_writer_handoff_tears_down_only_an_idle_tui(self):
        self.bridge.ensure(SID)
        self.probe.value = (4, 3)  # a Prompt without a finished turn
        with self.assertRaises(KiroTerminalBusy):
            self.bridge.release_for_writer(SID)
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)
        self.probe.value = None  # unreadable session files: fail closed
        with self.assertRaises(KiroTerminalBusy):
            self.bridge.release_for_writer(SID)
        self.probe.value = (4, 4)
        self.tmux.screen = "│ Kiro is working · Type to steer · Ctrl+S to queue\n"
        with self.assertRaises(KiroTerminalBusy):
            self.bridge.release_for_writer(SID)
        self.tmux.screen = "›  ask a question\n"
        self.assertTrue(self.bridge.release_for_writer(SID))
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)
        self.assertFalse((self.sessions_dir / f"{SID}.lock").exists())

    def test_app_enter_is_busy_until_the_turn_finishes_or_grace_expires(self):
        pane = self.bridge.ensure(SID)
        self.assertTrue(self.bridge.send_text("你好", True))
        self.assertEqual([("paste", "你好"), ("key", "Enter")], self.tmux.sent)
        # kiro-cli has not written the Prompt record yet.
        with self.assertRaises(KiroTerminalBusy):
            self.bridge.release_for_writer(SID)
        self.probe.value = (4, 4)  # that turn finished
        self.assertTrue(self.bridge.release_for_writer(SID))
        # Grace expiry path (slash command that never became a turn).
        pane = self.bridge.ensure(SID)
        self.bridge.send_control_key("Enter")
        with self.bridge._lock:
            at, prompts, finished = self.bridge._submit_marker
            self.bridge._submit_marker = (at - kiro_terminal.KIRO_TERMINAL_SUBMIT_GRACE_SECONDS - 1, prompts, finished)
        self.assertTrue(self.bridge.release_for_writer(SID))
        self.assertTrue(pane)

    def test_handoff_leaves_a_tui_bound_to_another_session_alone(self):
        self.bridge.ensure(OTHER_SID)
        self.assertTrue(self.bridge.release_for_writer(SID))
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)

    def test_handoff_after_restart_uses_tmux_ownership_without_a_lease(self):
        self.bridge.ensure(SID)
        with self.bridge._lock:
            self.bridge._clear_lease_locked()  # a fresh server process
        self.probe.value = (5, 4)
        with self.assertRaises(KiroTerminalBusy):
            self.bridge.release_for_writer(SID)
        self.probe.value = (5, 5)
        self.assertTrue(self.bridge.release_for_writer(SID))
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)

    def test_release_requires_the_exact_lease(self):
        pane = self.bridge.ensure(SID)
        lease = self.bridge.lease_for_pane(pane)
        self.assertFalse(self.bridge.release("x" * 43))
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)
        self.assertTrue(self.bridge.release(lease))
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)
        self.assertFalse(self.bridge.release(lease))

    def test_idle_reaper_waits_for_a_running_turn(self):
        self.bridge.ensure(SID)
        with self.bridge._lock:
            self.bridge._last_activity -= 120
            self.bridge._timer.cancel()
            self.bridge._timer = None
        self.probe.value = (6, 5)
        self.bridge._reap_if_idle()
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)
        with self.bridge._lock:
            self.assertIsNotNone(self.bridge._timer)
            self.bridge._timer.cancel()
            self.bridge._timer = None
            self.bridge._last_activity -= 120
        self.probe.value = (6, 6)
        self.bridge._reap_if_idle()
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)
        self.assertIsNone(self.bridge._lease)

    def test_input_never_targets_an_unverified_pane(self):
        self.assertFalse(self.bridge.send_text("hi", True))
        self.assertFalse(self.bridge.send_control_key("C-c"))
        self.assertEqual([], self.tmux.sent)

    def test_shutdown_release_tears_down_owned_pane(self):
        self.bridge.ensure(SID)
        self.assertTrue(self.bridge.release_for_shutdown())
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)


class KiroSessionTurnProbeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kiro-probe-test-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, prompts, finished, partial=False):
        lines = []
        for index in range(prompts):
            lines.append(json.dumps({"version": "v1", "kind": "Prompt", "data": {"content": [
                {"kind": "text", "data": '"kind":"Prompt" in user text is not a record'}]}}))
            lines.append(json.dumps({"version": "v1", "kind": "AssistantMessage", "data": {}}))
        body = "\n".join(lines) + "\n"
        if partial:
            body += '{"version":"v1","kind":"Pro'
        (self.root / f"{SID}.jsonl").write_text(body, encoding="utf-8")
        (self.root / f"{SID}.json").write_text(json.dumps({"session_state": {"conversation_metadata": {
            "user_turn_metadatas": [{} for _ in range(finished)], "user_turn_start_request": None,
        }}}), encoding="utf-8")

    def test_counts_prompts_against_finished_turns_incrementally(self):
        probe = KiroSessionTurnProbe(self.root)
        self._write(2, 2)
        self.assertEqual((2, 2), probe.counts(SID))
        self.assertFalse(probe.busy(SID))
        self._write(3, 2, partial=True)
        self.assertTrue(probe.busy(SID))
        self.assertEqual((3, 2), probe.counts(SID))
        self._write(3, 3)
        self.assertFalse(probe.busy(SID))

    def test_missing_files_and_bad_ids(self):
        probe = KiroSessionTurnProbe(self.root)
        self.assertEqual((0, 0), probe.counts(SID))
        self.assertIsNone(probe.busy("../x"))
        (self.root / f"{SID}.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(probe.busy(SID))


class KiroTerminalObserverTest(unittest.TestCase):
    def test_kiro_observer_is_kimi_projection_with_kiro_identity(self):
        observer = KiroTerminalObserver()
        epoch = observer.begin(SID, "turn-1")
        observer.record_activity(SID, "turn-1", epoch, {"kind": "activity", "label": "正在使用工具", "title": "cat /root/.secret"})
        observer.record_assistant_text(SID, "turn-1", epoch, "路径 /root/private/x token=abc")
        snapshot = observer.snapshot(SID)
        self.assertEqual("kiro", snapshot["target"])
        self.assertEqual("read_only", snapshot["mode"])
        self.assertIn("Kiro 实时观察 · 只读", snapshot["content"])
        self.assertIn("正在使用工具（名称与参数已隐藏）", snapshot["content"])
        for secret in (".secret", "/root/private", "abc", SID):
            self.assertNotIn(secret, json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual("kiro", KiroTerminalObserver.unavailable_snapshot()["target"])


class FakeKiroACP:
    def __init__(self, tmux, session_id=SID):
        self.tmux = tmux
        self.session_id = session_id
        self.closed = 0
        self.prepared = []

    def load_session_id(self):
        return self.session_id

    def close(self):
        self.closed += 1

    def prepare_session(self, **_kw):
        # Single writer: ACP must never load while the TUI pane is alive.
        self.prepared.append("ccc-kiro-terminal" in self.tmux.sessions)
        return self.session_id

    def prompt_existing(self, text, *, on_update=None, **_kw):
        if on_update:
            on_update("好的。")


class KiroTerminalHandlerTest(BridgeTestBase):
    def _handler(self):
        handler = object.__new__(PushHandler)
        acp = FakeKiroACP(self.tmux)
        chat = types.SimpleNamespace(records=[])

        def append(**record):
            item = {**record, "ts": f"ts-{len(chat.records) + 1}"}
            chat.records.append(item)
            return item

        chat.append = append
        chat.tail = lambda limit: list(chat.records[-limit:])
        from chat_history import ChatStreamBus
        handler.state = types.SimpleNamespace(
            kiro_turn_lock=threading.RLock(), kiro_active_turn={}, kiro_prepare_token="",
            kiro_acp=acp, kiro_terminal=self.bridge, kiro_terminal_observer=KiroTerminalObserver(),
            kiro_preferences=types.SimpleNamespace(
                snapshot_model=lambda: "claude-opus-5.5", snapshot_effort=lambda: "max",
            ),
            contact_typing_states={"kiro": {"is_typing": False, "since": None}},
            chat_draft_lock=threading.Lock(), chat_drafts={}, chat_reply_states={},
            chat_stream_revisions={}, chat_stream_bus=ChatStreamBus(),
            contact_chats={"kiro": chat}, default_session="cctg", active_session="cctg",
        )
        handler.headers = {}
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
        handler._chat_for_contact = lambda contact_id: chat
        handler._consume_staged_attachments = lambda body, contact_id: []
        return handler, acp, chat

    def test_idle_capture_closes_acp_then_launches_leased_tui(self):
        handler, acp, _chat = self._handler()
        handler._handle_kiro_terminal_capture(80)
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual("kiro", payload["session"])
        self.assertEqual("ready", payload["state"])
        self.assertRegex(payload["lease"], r"^[A-Za-z0-9_-]{32,128}$")
        self.assertEqual(1, acp.closed)
        self.assertIn("--resume-id", self.tmux.sessions["ccc-kiro-terminal"]["argv"])
        self.assertNotIn(SID, json.dumps(payload))
        self.assertNotIn("%", payload["lease"])

    def test_capture_during_app_turn_is_read_only_observer_without_tui(self):
        handler, acp, _chat = self._handler()
        handler.state.kiro_active_turn = {"user_ts": "turn-1"}
        epoch = handler.state.kiro_terminal_observer.begin(SID, "turn-1")
        handler.state.kiro_terminal_observer.record_activity(SID, "turn-1", epoch, {"kind": "activity", "label": "正在思考"})
        handler._handle_kiro_terminal_capture(80)
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual(("waiting", "read_only"), (payload["state"], payload["mode"]))
        self.assertIn("Kiro 实时观察 · 只读", payload["content"])
        self.assertNotIn("lease", payload)
        self.assertEqual({}, self.tmux.sessions)
        self.assertEqual(0, acp.closed)

    def test_input_during_app_turn_is_rejected_with_423(self):
        handler, _acp, _chat = self._handler()
        handler.state.kiro_prepare_token = "reserved"
        handler._handle_kiro_terminal_key("C-c")
        self.assertEqual(423, handler.responses[-1][0])
        handler._handle_kiro_terminal_send({"keys": "hi"})
        self.assertEqual(423, handler.responses[-1][0])
        self.assertEqual([], self.tmux.sent)

    def test_session_switching_commands_are_blocked(self):
        handler, _acp, _chat = self._handler()
        for command in ("/chat new", "/clear", "/rewind", "  /CHAT load x"):
            handler._handle_kiro_terminal_send({"keys": command})
            self.assertEqual(400, handler.responses[-1][0], command)
            self.assertEqual("kiro_session_command_blocked", handler.responses[-1][1]["error"])
        self.assertEqual({}, self.tmux.sessions)

    def test_keys_and_text_reach_the_owned_pane(self):
        handler, _acp, _chat = self._handler()
        handler._handle_kiro_terminal_capture(80)
        handler._handle_kiro_terminal_key("Escape")
        self.assertEqual(200, handler.responses[-1][0])
        handler._handle_kiro_terminal_key("rm -rf")
        self.assertEqual(400, handler.responses[-1][0])
        handler._handle_kiro_terminal_send({"keys": "/context show", "enter": True})
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual(
            [("key", "Escape"), ("paste", "/context show"), ("key", "Enter")], self.tmux.sent,
        )
        handler._handle_kiro_terminal_resize(100, 40)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertFalse(handler.responses[-1][1]["deferred"])

    def test_chat_send_hands_idle_tui_back_before_acp_loads(self):
        handler, acp, chat = self._handler()
        handler._handle_kiro_terminal_capture(80)
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)
        with mock.patch("push.threading.Thread", _immediate_thread):
            handler._handle_kiro_chat_send({"text": "你好呀"}, "kiro")
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual([False], acp.prepared)  # the TUI was gone before prepare
        self.assertNotIn("ccc-kiro-terminal", self.tmux.sessions)
        self.assertEqual(["user", "assistant"], [r["role"] for r in chat.records])

    def test_chat_send_while_tui_turn_runs_is_rejected_not_interleaved(self):
        handler, acp, chat = self._handler()
        handler._handle_kiro_terminal_capture(80)
        self.probe.value = (7, 6)
        handler._handle_kiro_chat_send({"text": "你好呀"}, "kiro")
        status, payload = handler.responses[-1]
        self.assertEqual(409, status)
        self.assertEqual("kiro_terminal_busy", payload["error"])
        self.assertEqual([], acp.prepared)
        self.assertEqual([], chat.records)
        self.assertEqual("", handler.state.kiro_prepare_token)
        self.assertIn("ccc-kiro-terminal", self.tmux.sessions)

    def test_release_route_needs_exact_lease(self):
        handler, _acp, _chat = self._handler()
        handler._handle_kiro_terminal_capture(80)
        lease = handler.responses[-1][1]["lease"]
        handler._handle_terminal_release({"target": "kiro", "lease": "y" * 43})
        self.assertEqual((200, False), (handler.responses[-1][0], handler.responses[-1][1]["released"]))
        handler._handle_terminal_release({"target": "kiro", "lease": lease})
        self.assertTrue(handler.responses[-1][1]["released"])
        self.assertEqual({}, self.tmux.sessions)

    def test_no_session_pointer_is_a_friendly_placeholder(self):
        handler, acp, _chat = self._handler()
        acp.session_id = ""
        handler._handle_kiro_terminal_capture(80)
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual("no_active_kiro_session", payload["error"])
        self.assertEqual({}, self.tmux.sessions)

    def test_physical_pane_name_is_not_a_generic_tmux_target(self):
        handler, _acp, _chat = self._handler()
        handler.state.kimi_terminal = types.SimpleNamespace(tmux_session="ccc-kimi-terminal")
        with self.assertRaises(Exception):
            handler._resolve_terminal_session("ccc-kiro-terminal")
        with self.assertRaises(Exception):
            handler._resolve_terminal_session("=ccc-kiro-terminal")

    def test_capture_get_requires_native_pairing(self):
        handler = object.__new__(PushHandler)
        handler.path = "/tmux/capture?session=kiro"
        handler.command = "GET"
        handler.responses = []
        handler._is_public_get = lambda: False
        handler._check_ip_allowed = lambda: True
        handler._native_pairing_auth_matches = lambda: False
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.do_GET()
        self.assertEqual(401, handler.responses[-1][0])


def _immediate_thread(target, **_kwargs):
    class ImmediateThread:
        def start(self):
            target()

    return ImmediateThread()


if __name__ == "__main__":
    unittest.main()
