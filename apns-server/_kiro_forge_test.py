"""Tests for the Kiro controlled/auto forge pipeline (kiro 自动 forge 2026-10-08).

Mirrors _kimi_forge_test.py: the ACP swap is faked at the client boundary for
handler-level tests, and a scripted stdio process covers the wire-level
``KiroACPClient.forge_new_session``.  No test spawns the real kiro-cli or
reaches the real model.
"""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contacts import dispatch_contact_post
from kiro_acp import KiroACPClient, KiroACPError
from kiro_terminal import KiroTerminalBusy
from push import (
    PushHandler,
    _clamp_kimi_forge_seed_retain,
    _load_kiro_recent_messages,
    _parse_kiro_auto_forge_threshold,
    _scan_kiro_session_tasks,
)


# ---------------------------------------------------------------------------
# Handler-level fakes
# ---------------------------------------------------------------------------

class FakeChat:
    def __init__(self):
        self.rows = []

    def append(self, **row):
        self.rows.append(row)
        return row

    def tail(self, n=50):
        return self.rows[-n:]


class FakeACP:
    def __init__(self, *, active="session_old", usage=90.0, busy=False, summary="旧会话摘要内容"):
        self.active = active
        self.usage = usage
        self.busy = busy
        self.summary = summary
        self.forged = []
        self.seeds = []
        self.fail_forge = None

    def load_session_id(self):
        return self.active

    def context_usage_percent(self):
        return self.usage

    def forge_new_session(self, *, model=None, effort=None):
        if self.fail_forge is not None:
            raise self.fail_forge
        self.forged.append({"model": model, "effort": effort})
        self.active = "session_new"
        self.usage = None
        return "session_new", self.summary

    def prompt_existing(self, text, *, session_id, turn_id, on_update=None, **_kwargs):
        self.seeds.append((session_id, text))
        return None


def _make_handler(tmp: Path, acp: object) -> PushHandler:
    chat = FakeChat()
    state = types.SimpleNamespace(
        kiro_turn_lock=threading.RLock(),
        kiro_active_turn={},
        kiro_prepare_token="",
        kiro_acp=acp,
        kiro_auto_forge_enabled=True,
        kiro_auto_forge_threshold_percent=80.0,
        # Keep handler tests hermetic: no verbatim tail unless a test opts in
        # by seeding FakeChat rows and raising retain.
        kiro_auto_forge_retain_messages=0,
        token_store_path=str(tmp / "tokens" / "device_tokens.json"),
        contact_chats={"kiro": chat},
    )
    handler = object.__new__(PushHandler)
    handler.state = state
    handler.responses = []
    handler.notifications = []
    handler._send_json = lambda status, payload: handler.responses.append((status, payload))
    handler._kiro_model_selection = lambda: "auto"
    handler._kiro_effort_selection = lambda: ""
    handler._handoff_kiro_terminal_to_writer = lambda _token: True
    handler._send_chat_notification = lambda title, body: (
        handler.notifications.append((title, body))
    )
    return handler


TASKS = {
    "finished": [{
        "task_id": "done-1", "description": "查案",
        "status": "completed", "kind": "agent",
        "report_path": "/root/.kiro/task-reports/done-1.md",
    }],
    "pending": [{
        "task_id": "run-1", "description": "长跑",
        "status": "running", "kind": "bash",
    }],
}


class KiroForgeRouteTest(unittest.TestCase):
    def test_forge_route_registered(self):
        handler = types.SimpleNamespace()
        called = []
        handler._handle_kiro_forge = lambda body: called.append(body)
        self.assertTrue(dispatch_contact_post(handler, "/kiro/forge", {}))
        self.assertEqual([{}], called)


class HandleKiroForgeTest(unittest.TestCase):
    def test_controlled_forge_hands_off_tasks_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP()
            handler = _make_handler(Path(tmp), acp)
            with patch("push._scan_kiro_session_tasks", lambda _sid, **_: dict(TASKS)) as scan:
                handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["ok"])
            self.assertEqual("session_old", payload["previous_session_id"])
            self.assertEqual("session_new", payload["active_session_id"])
            self.assertEqual(1, payload["finished_tasks"])
            self.assertEqual(1, payload["pending_tasks"])
            self.assertTrue(payload["seed_submitted"])
            # The ACP swap ran with the App-owned model/effort selection.
            self.assertEqual([{"model": "auto", "effort": None}], acp.forged)
            self.assertEqual("session_new", acp.active)
            # The seed carries the summary and the task handoff.
            self.assertEqual(1, len(acp.seeds))
            seed_session, seed_text = acp.seeds[0]
            self.assertEqual("session_new", seed_session)
            self.assertIn("受控 forge", seed_text)
            self.assertIn("旧会话摘要内容", seed_text)
            self.assertIn("run-1", seed_text)
            self.assertIn("done-1", seed_text)
            self.assertIn("模型 auto", seed_text)
            self.assertIn("90%", seed_text)
            # Handoff record persisted next to the token store.
            handoff = Path(payload["handoff_record"])
            record = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual("session_old", record["old_session_id"])
            self.assertEqual("session_new", record["new_session_id"])
            self.assertEqual("run-1", record["pending_tasks"][0]["task_id"])
            self.assertEqual("旧会话摘要内容", record["summary"])
            self.assertEqual("auto", record["model"])
            self.assertEqual(90.0, record["usage_percent"])
            self.assertEqual(0o600, handoff.stat().st_mode & 0o777)
            # User-facing notice: assistant history row plus one APNs banner.
            chat = handler.state.contact_chats["kiro"]
            self.assertEqual(1, len(chat.rows))
            self.assertEqual("assistant", chat.rows[0]["role"])
            self.assertEqual("system:kiro-forge", chat.rows[0]["source"])
            self.assertIn("session_new", chat.rows[0]["text"])
            self.assertIn("长跑", chat.rows[0]["text"])
            self.assertEqual(1, len(handler.notifications))
            # The prepare reservation is always released.
            self.assertEqual("", handler.state.kiro_prepare_token)

    def test_forge_refuses_while_turn_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP()
            handler = _make_handler(Path(tmp), acp)
            handler.state.kiro_active_turn = {"user_ts": "t1"}
            handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("kiro_turn_active", payload["error"])
            self.assertEqual([], acp.forged)
            self.assertEqual("session_old", acp.active)

    def test_forge_without_active_session_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(active="")
            handler = _make_handler(Path(tmp), acp)
            handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("no_active_kiro_session", payload["error"])
            self.assertEqual([], acp.forged)

    def test_forge_refuses_while_acp_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(busy=True)
            handler = _make_handler(Path(tmp), acp)
            with patch("push._scan_kiro_session_tasks") as scan:
                handler._handle_kiro_forge({})
            scan.assert_not_called()
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("kiro_busy", payload["error"])
            self.assertEqual([], acp.forged)
            self.assertEqual("session_old", acp.active)

    def test_terminal_busy_blocks_forge(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP()
            handler = _make_handler(Path(tmp), acp)

            def busy_handoff(_token):
                raise KiroTerminalBusy("tui mid-turn")

            handler._handoff_kiro_terminal_to_writer = busy_handoff
            handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("kiro_terminal_busy", payload["error"])
            self.assertEqual([], acp.forged)
            self.assertEqual("", handler.state.kiro_prepare_token)

    def test_swap_failure_keeps_pointer_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP()
            acp.fail_forge = KiroACPError("boom")
            handler = _make_handler(Path(tmp), acp)
            with patch("push._scan_kiro_session_tasks", return_value={"finished": [], "pending": []}):
                handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(503, status)
            self.assertEqual("kiro_unavailable", payload["error"])
            self.assertEqual("session_old", acp.active)
            self.assertEqual([], acp.seeds)
            chat = handler.state.contact_chats["kiro"]
            self.assertEqual([], chat.rows)
            self.assertEqual([], handler.notifications)


class AutoForgePipelineTest(unittest.TestCase):
    """Threshold auto-forge reuses the controlled-forge handoff pipeline."""

    def _auto_handler(self, tmp: str, acp: FakeACP, *, threshold=80.0, enabled=True) -> PushHandler:
        handler = _make_handler(Path(tmp), acp)
        handler.state.kiro_auto_forge_threshold_percent = threshold
        handler.state.kiro_auto_forge_enabled = enabled
        return handler

    def test_disabled_never_forges(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=95.0)
            handler = self._auto_handler(tmp, acp, enabled=False)
            session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], acp.forged)
            self.assertEqual([], acp.seeds)

    def test_usage_unknown_never_forges(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=None)
            handler = self._auto_handler(tmp, acp)
            session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], acp.forged)

    def test_usage_below_threshold_keeps_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=50.0)
            handler = self._auto_handler(tmp, acp)
            session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], acp.forged)

    def test_auto_forge_runs_full_handoff_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=80.0)
            handler = self._auto_handler(tmp, acp, threshold=80.0)
            with patch("push._scan_kiro_session_tasks", return_value=dict(TASKS)) as scan:
                session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_new", True), (session_id, forged))
            # Same pipeline as the controlled forge: inventory of the old
            # session, pointer swap, handoff record, seed, chat + push notice.
            scan.assert_called_once_with("session_old", workspace=None)
            self.assertEqual("session_new", acp.active)
            handoff = Path(handler.state.token_store_path).parent / "kiro_forge_handoff.json"
            record = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual("session_old", record["old_session_id"])
            self.assertEqual("session_new", record["new_session_id"])
            self.assertEqual("run-1", record["pending_tasks"][0]["task_id"])
            self.assertEqual(0o600, handoff.stat().st_mode & 0o777)
            self.assertEqual(1, len(acp.seeds))
            seed_session, seed_text = acp.seeds[0]
            self.assertEqual("session_new", seed_session)
            self.assertIn("run-1", seed_text)
            # No silent forge: the notice names the automatic trigger.
            chat = handler.state.contact_chats["kiro"]
            self.assertEqual(1, len(chat.rows))
            self.assertEqual("assistant", chat.rows[0]["role"])
            self.assertEqual("system:kiro-forge", chat.rows[0]["source"])
            self.assertIn("自动 forge", chat.rows[0]["text"])
            self.assertIn("session_new", chat.rows[0]["text"])
            self.assertEqual(1, len(handler.notifications))
            self.assertIn("自动", handler.notifications[0][0])

    def test_busy_acp_skips_this_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=95.0, busy=True)
            handler = self._auto_handler(tmp, acp)
            with patch("push._scan_kiro_session_tasks") as scan:
                session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            scan.assert_not_called()
            self.assertEqual([], acp.forged)
            self.assertEqual([], acp.seeds)
            self.assertEqual("session_old", acp.active)
            chat = handler.state.contact_chats["kiro"]
            self.assertEqual([], chat.rows)
            self.assertEqual([], handler.notifications)

    def test_failed_swap_keeps_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=95.0)
            acp.fail_forge = KiroACPError("boom")
            handler = self._auto_handler(tmp, acp)
            with patch("push._scan_kiro_session_tasks", return_value={"finished": [], "pending": []}):
                session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], acp.seeds)
            self.assertEqual([], handler.notifications)

    def test_pointer_changed_since_measurement_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=95.0)
            handler = self._auto_handler(tmp, acp)
            with patch("push._scan_kiro_session_tasks") as scan:
                session_id, forged = handler._maybe_forge_kiro_session("session_other")
            self.assertEqual(("session_other", False), (session_id, forged))
            scan.assert_not_called()
            self.assertEqual([], acp.forged)
            self.assertEqual("session_old", acp.active)

    def test_handoff_crash_after_swap_still_returns_new_session(self):
        """指针已提交后交接崩溃：消息必须跟着新指针走，绝不丢这一轮。"""
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP(usage=95.0)
            handler = self._auto_handler(tmp, acp)

            def explode(_ctx):
                raise RuntimeError("disk full")

            handler._write_kiro_forge_handoff = explode
            with patch("push._scan_kiro_session_tasks", return_value={"finished": [], "pending": []}):
                session_id, forged = handler._maybe_forge_kiro_session("session_old")
            self.assertEqual(("session_new", True), (session_id, forged))
            self.assertEqual("session_new", acp.active)


class ForgeSeedRetainMessagesTest(unittest.TestCase):
    """Hybrid forge seed: summary plus the old conversation's verbatim tail."""

    def _seed_chat(self, chat: FakeChat) -> None:
        def row(role, text, source="cc-app:kiro", **extra):
            record = {"role": role, "text": text, "source": source}
            record.update(extra)
            chat.rows.append(record)

        row("user", "第一句用户消息")
        row("assistant", "第一句助手回复", source="kiro-acp")
        # Noise that must never enter the verbatim tail.
        row("assistant", "已开启新的 Kiro 会话", source="kiro-acp:new-session")
        row("assistant", "旧 forge 通知不该进", source="system:kiro-forge")
        row("assistant", "💭 浮现了 1 条记忆", source="memory-recall:kiro")
        row("task", "· 执行 完成", source="kiro-acp:activity")
        row("assistant", "被重新发言覆盖", source="kiro-acp", hidden_in_ui=True)
        row("user", "")
        row("user", "最近的用户消息")
        row("assistant", "最近的助手回复", source="kiro-acp")

    def _forge_with_chat(self, tmp: Path, chat: FakeChat, *, retain: int):
        acp = FakeACP()
        handler = _make_handler(tmp, acp)
        handler.state.contact_chats["kiro"] = chat
        handler.state.kiro_auto_forge_retain_messages = retain
        with patch("push._scan_kiro_session_tasks", lambda _sid, **_: {"finished": [], "pending": []}):
            handler._handle_kiro_forge({})
        return handler, acp

    def test_seed_tail_injects_recent_verbatim_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = FakeChat()
            self._seed_chat(chat)
            handler, acp = self._forge_with_chat(Path(tmp), chat, retain=80)
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertEqual(4, payload["retained_messages"])
            _session, seed = acp.seeds[0]
            self.assertIn("以下为旧会话最近 4 条对话原文，供延续上下文", seed)
            self.assertIn("[用户] 第一句用户消息", seed)
            self.assertIn("[助手] 最近的助手回复", seed)
            self.assertLess(seed.index("第一句用户消息"), seed.index("最近的助手回复"))
            for noise in ("已开启新的 Kiro 会话", "旧 forge 通知", "浮现了", "执行 完成", "被重新发言覆盖"):
                self.assertNotIn(noise, seed)

    def test_retain_zero_keeps_summary_only_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = FakeChat()
            self._seed_chat(chat)
            handler, acp = self._forge_with_chat(Path(tmp), chat, retain=0)
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertEqual(0, payload["retained_messages"])
            _session, seed = acp.seeds[0]
            self.assertIn("受控 forge", seed)
            self.assertNotIn("对话原文", seed)
            self.assertNotIn("第一句用户消息", seed)

    def test_only_last_n_messages_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = FakeChat()
            for index in range(6):
                chat.rows.append({"role": "user", "text": f"用户消息{index}", "source": "cc-app:kiro"})
                chat.rows.append({"role": "assistant", "text": f"助手回复{index}", "source": "kiro-acp"})
            handler, acp = self._forge_with_chat(Path(tmp), chat, retain=3)
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertEqual(3, payload["retained_messages"])
            _session, seed = acp.seeds[0]
            self.assertNotIn("用户消息3", seed)
            self.assertNotIn("助手回复3", seed)
            self.assertNotIn("用户消息4", seed)
            i_asst4 = seed.index("助手回复4")
            i_user5 = seed.index("用户消息5")
            i_asst5 = seed.index("助手回复5")
            self.assertTrue(i_asst4 < i_user5 < i_asst5)

    def test_byte_cap_drops_oldest_messages(self):
        chat = FakeChat()
        chat.rows.append({"role": "user", "text": "老消息" + "长" * 200, "source": "cc-app:kiro"})
        chat.rows.append({"role": "user", "text": "次老消息", "source": "cc-app:kiro"})
        chat.rows.append({"role": "assistant", "text": "新消息", "source": "kiro-acp"})
        result = _load_kiro_recent_messages(chat, limit=80, max_bytes=10)
        texts = [text for _role, text in result["messages"]]
        self.assertEqual(["新消息"], texts)
        self.assertEqual(2, result["dropped"])
        self.assertFalse(result["truncated"])
        # A single oversized latest message is truncated, not dropped.
        big = FakeChat()
        big.rows.append({"role": "assistant", "text": "巨" * 200, "source": "kiro-acp"})
        result = _load_kiro_recent_messages(big, limit=80, max_bytes=60)
        self.assertEqual(1, len(result["messages"]))
        self.assertTrue(result["truncated"])
        self.assertTrue(result["messages"][0][1].endswith("…[截断]"))
        self.assertLessEqual(
            len(result["messages"][0][1].encode("utf-8")), 60 + len(" …[截断]".encode("utf-8"))
        )

    def test_unreadable_history_degrades_to_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP()
            handler = _make_handler(Path(tmp), acp)

            class BrokenChat(FakeChat):
                def tail(self, n=50):
                    raise OSError("history gone")

            handler.state.contact_chats["kiro"] = BrokenChat()
            handler.state.kiro_auto_forge_retain_messages = 80
            with patch("push._scan_kiro_session_tasks", lambda _sid, **_: {"finished": [], "pending": []}):
                handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["seed_submitted"])
            self.assertEqual(0, payload["retained_messages"])
            _session, seed = acp.seeds[0]
            self.assertIn("受控 forge", seed)
            self.assertNotIn("对话原文", seed)

    def test_seed_prompt_failure_degrades_but_forge_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            acp = FakeACP()

            def failing_prompt(*_args, **_kwargs):
                raise KiroACPError("seed refused")

            acp.prompt_existing = failing_prompt
            handler = _make_handler(Path(tmp), acp)
            with patch("push._scan_kiro_session_tasks", lambda _sid, **_: dict(TASKS)):
                handler._handle_kiro_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["seed_submitted"])
            self.assertEqual("session_new", acp.active)
            # The notice warns that the seed never landed.
            chat = handler.state.contact_chats["kiro"]
            self.assertIn("seed 未能提交", chat.rows[0]["text"])


class ScanKiroSessionTasksTest(unittest.TestCase):
    """kiro Delegate 任务盘点（双来源）。

    2.21.2 实证落点：全局 ``<kiro-cli 数据目录>/.subagents/<agent>.json``；
    ``<workspace>/.kiro/.subagents/`` 保留为第二来源。文件格式
    （AgentExecution：agent/task/status/launched_at/pid/exit_code/output/
    user_notified/summary/cwd，status∈running/completed/failed）与真实
    任务样本逐字段吻合。所有用例都传独立 data_dir，绝不碰真实全局目录。
    """

    def _make_workspace(self, root: Path) -> Path:
        workspace = root / "wksp"
        subagents = workspace / ".kiro" / ".subagents"
        subagents.mkdir(parents=True)
        (subagents / "rust-agent.json").write_text(json.dumps({
            "agent": "rust-agent",
            "task": "查案",
            "status": "completed",
            "launched_at": 1790000000,
            "completed_at": 1790000060,
            "pid": 0,
            "exit_code": 0,
            "output": "done",
            "user_notified": True,
            "summary": "ok",
            "cwd": str(workspace),
        }), encoding="utf-8")
        (subagents / "default_agent.json").write_text(json.dumps({
            "agent": "default_agent",
            "task": "长跑",
            "status": "running",
            "launched_at": 1790000000,
            "pid": 0,
        }), encoding="utf-8")
        (subagents / "broken.json").write_text("{not json", encoding="utf-8")
        return workspace

    @staticmethod
    def _make_data_dir(root: Path) -> Path:
        data_dir = root / "kiro-cli-data"
        data_dir.mkdir(parents=True)
        return data_dir

    @staticmethod
    def _dead_pid() -> int:
        for pid in range(40000, 4_000_000):
            if not Path(f"/proc/{pid}").exists():
                return pid
        raise AssertionError("no free pid found")

    def test_classifies_terminal_and_pending_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=self._make_data_dir(Path(tmp)),
            )
            self.assertEqual(["rust-agent"], [t["task_id"] for t in result["finished"]])
            self.assertEqual(["default_agent"], [t["task_id"] for t in result["pending"]])
            finished = result["finished"][0]
            self.assertEqual("completed", finished["status"])
            self.assertEqual("查案", finished["description"])
            self.assertEqual("delegate", finished["kind"])
            # Kiro has no task-report directory; report paths stay absent.
            self.assertNotIn("report_path", finished)

    def test_failed_execution_lands_in_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            subagents = workspace / ".kiro" / ".subagents"
            (subagents / "default_agent.json").write_text(json.dumps({
                "agent": "default_agent",
                "task": "挂掉",
                "status": "failed",
                "exit_code": 1,
            }), encoding="utf-8")
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=self._make_data_dir(Path(tmp)),
            )
            self.assertEqual(
                ["default_agent", "rust-agent"],
                sorted(t["task_id"] for t in result["finished"]),
            )
            self.assertEqual([], result["pending"])

    def test_running_with_dead_pid_counts_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            subagents = workspace / ".kiro" / ".subagents"
            (subagents / "default_agent.json").write_text(json.dumps({
                "agent": "default_agent",
                "task": "孤儿",
                "status": "running",
                "pid": self._dead_pid(),
            }), encoding="utf-8")
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=self._make_data_dir(Path(tmp)),
            )
            self.assertEqual([], result["pending"])
            self.assertEqual("failed", result["finished"][0]["status"])
            self.assertEqual("default_agent", result["finished"][0]["task_id"])

    def test_running_with_live_pid_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            subagents = workspace / ".kiro" / ".subagents"
            (subagents / "default_agent.json").write_text(json.dumps({
                "agent": "default_agent",
                "task": "活着",
                "status": "running",
                "pid": os.getpid(),
            }), encoding="utf-8")
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=self._make_data_dir(Path(tmp)),
            )
            self.assertEqual(["default_agent"], [t["task_id"] for t in result["pending"]])

    def test_rejects_foreign_or_empty_session_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            data_dir = self._make_data_dir(Path(tmp))
            self.assertEqual(
                {"finished": [], "pending": []},
                _scan_kiro_session_tasks("", workspace=workspace, data_dir=data_dir),
            )
            self.assertEqual(
                {"finished": [], "pending": []},
                _scan_kiro_session_tasks("../escape", workspace=workspace, data_dir=data_dir),
            )
            self.assertEqual(
                {"finished": [], "pending": []},
                _scan_kiro_session_tasks(
                    "session_x", workspace=workspace / "missing", data_dir=data_dir / "missing",
                ),
            )

    def test_drops_task_files_with_unsafe_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            subagents = workspace / ".kiro" / ".subagents"
            (subagents / "evil.json").write_text(json.dumps({
                "agent": "../../../etc/passwd",
                "task": "逃逸",
                "status": "completed",
            }), encoding="utf-8")
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=self._make_data_dir(Path(tmp)),
            )
            ids = [t["task_id"] for t in result["finished"] + result["pending"]]
            self.assertEqual(["default_agent", "rust-agent"], sorted(ids))

    def test_unreadable_and_unknown_format_files_warn_and_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            subagents = workspace / ".kiro" / ".subagents"
            # 格式漂移：顶层不是对象（未来 kiro 改版）也必须安全跳过。
            (subagents / "drifted.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
            with self.assertLogs("cc-apns-server", level="WARNING") as caught:
                result = _scan_kiro_session_tasks(
                    "session_x", workspace=workspace, data_dir=self._make_data_dir(Path(tmp)),
                )
            self.assertEqual(["rust-agent"], [t["task_id"] for t in result["finished"]])
            self.assertEqual(["default_agent"], [t["task_id"] for t in result["pending"]])
            warnings = "\n".join(caught.output)
            self.assertIn("broken.json", warnings)
            self.assertIn("drifted.json", warnings)

    # ---------- 全局数据目录来源（2.21.2 实证落点） ----------

    def _write_global_task(
        self, data_dir: Path, agent: str, *, task: str, status: str,
        cwd: str | None, mtime: float | None = None,
    ) -> Path:
        subagents = data_dir / ".subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        path = subagents / f"{agent}.json"
        payload = {
            "agent": agent,
            "task": task,
            "status": status,
            "launched_at": 1790000000,
            "pid": 0,
        }
        if cwd is not None:
            payload["cwd"] = cwd
        path.write_text(json.dumps(payload), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_global_data_dir_scanned_with_matching_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            data_dir = self._make_data_dir(Path(tmp))
            self._write_global_task(
                data_dir, "kiro_default", task="求和", status="completed",
                cwd=str(workspace),
            )
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=data_dir,
            )
            by_id = {t["task_id"]: t for t in result["finished"] + result["pending"]}
            self.assertEqual("求和", by_id["kiro_default"]["description"])
            self.assertEqual("completed", by_id["kiro_default"]["status"])

    def test_global_entry_with_foreign_cwd_kept_and_annotated(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            data_dir = self._make_data_dir(Path(tmp))
            self._write_global_task(
                data_dir, "kiro_default", task="别处的活", status="completed",
                cwd="/root/Somewhere-Else",
            )
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=data_dir,
            )
            by_id = {t["task_id"]: t for t in result["finished"] + result["pending"]}
            self.assertEqual(
                "[来自 /root/Somewhere-Else] 别处的活",
                by_id["kiro_default"]["description"],
            )

    def test_global_entry_with_missing_or_unknown_cwd_not_annotated(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            data_dir = self._make_data_dir(Path(tmp))
            self._write_global_task(
                data_dir, "kiro_default", task="无 cwd", status="completed", cwd=None,
            )
            self._write_global_task(
                data_dir, "rust-agent-x", task="未知 cwd", status="completed", cwd="Unknown",
            )
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=data_dir,
            )
            by_id = {t["task_id"]: t for t in result["finished"] + result["pending"]}
            self.assertEqual("无 cwd", by_id["kiro_default"]["description"])
            self.assertEqual("未知 cwd", by_id["rust-agent-x"]["description"])

    def test_duplicate_agent_newer_mtime_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            data_dir = self._make_data_dir(Path(tmp))
            # 同一 agent 两处都有：全局更新 → 全局胜。
            newer = self._write_global_task(
                data_dir, "default_agent", task="全局新", status="completed",
                cwd=str(workspace),
            )
            local = workspace / ".kiro" / ".subagents" / "default_agent.json"
            old_ts = newer.stat().st_mtime - 100
            os.utime(local, (old_ts, old_ts))
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=data_dir,
            )
            entries = [
                t for t in result["finished"] + result["pending"]
                if t["task_id"] == "default_agent"
            ]
            self.assertEqual(1, len(entries))
            self.assertEqual("全局新", entries[0]["description"])
            self.assertEqual("completed", entries[0]["status"])

    def test_duplicate_agent_local_newer_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_workspace(Path(tmp))
            data_dir = self._make_data_dir(Path(tmp))
            older = self._write_global_task(
                data_dir, "default_agent", task="全局旧", status="completed",
                cwd=str(workspace),
            )
            local = workspace / ".kiro" / ".subagents" / "default_agent.json"
            new_ts = older.stat().st_mtime + 100
            os.utime(local, (new_ts, new_ts))
            result = _scan_kiro_session_tasks(
                "session_x", workspace=workspace, data_dir=data_dir,
            )
            entries = [
                t for t in result["finished"] + result["pending"]
                if t["task_id"] == "default_agent"
            ]
            self.assertEqual(1, len(entries))
            self.assertEqual("长跑", entries[0]["description"])
            self.assertEqual("running", entries[0]["status"])


class KiroAutoForgeConfigTest(unittest.TestCase):
    """配置解析边界：阈值与 retain 的非法值一律回落默认值。"""

    def test_threshold_parsing_boundaries(self):
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(None))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold("abc"))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(0))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(0.5))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(100.5))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(101))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(float("nan")))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(float("inf")))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(-20))
        self.assertEqual(1.0, _parse_kiro_auto_forge_threshold(1))
        self.assertEqual(100.0, _parse_kiro_auto_forge_threshold(100))
        self.assertEqual(65.0, _parse_kiro_auto_forge_threshold("65"))
        self.assertEqual(80.0, _parse_kiro_auto_forge_threshold(80))

    def test_retain_clamp_boundaries(self):
        # kiro_auto_forge_retain_messages 复用 Kimi 的钳位：[0,160]，非法值 80。
        self.assertEqual(80, _clamp_kimi_forge_seed_retain("abc"))
        self.assertEqual(80, _clamp_kimi_forge_seed_retain(None))
        self.assertEqual(0, _clamp_kimi_forge_seed_retain(-5))
        self.assertEqual(0, _clamp_kimi_forge_seed_retain(0))
        self.assertEqual(80, _clamp_kimi_forge_seed_retain(80))
        self.assertEqual(160, _clamp_kimi_forge_seed_retain(999))


# ---------------------------------------------------------------------------
# Wire-level tests: KiroACPClient.forge_new_session over scripted stdio
# ---------------------------------------------------------------------------

class _FakeStdout:
    def __init__(self):
        self._queue = queue.Queue()

    def push(self, obj):
        self._queue.put(json.dumps(obj) + "\n")

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStderr:
    def __init__(self):
        self._queue = queue.Queue()

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    def __init__(self, on_message):
        self._on_message = on_message
        self._buffer = ""

    def write(self, data):
        self._buffer += data
        return len(data)

    def flush(self):
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            if line.strip():
                self._on_message(json.loads(line))


class FakeKiroACPProcess:
    """In-memory stand-in for ``kiro-cli acp`` over stdio (same shape as
    _kiro_acp_test.py's fake)."""

    def __init__(self, handler):
        self._handler = handler
        self.stdout = _FakeStdout()
        self.stderr = _FakeStderr()
        self.stdin = _FakeStdin(self._on_message)
        self.pid = 4_000_000_000  # nonexistent: killpg fails harmlessly
        self._returncode = None
        self.requests = []

    def _on_message(self, message):
        self.requests.append(message)
        for response in self._handler(self, message) or []:
            self.stdout.push(response)

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode if self._returncode is not None else 0

    def crash(self):
        self._returncode = -15
        self.stdout.close()
        self.stderr.close()


def _scripted_factory(processes):
    remaining = list(processes)

    def factory(*_args, **_kwargs):
        if not remaining:
            raise AssertionError("unexpected extra kiro-cli acp spawn")
        return remaining.pop(0)

    return factory


def _chunk(session_id, text):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def _forge_handler(*, summary="总结：在调试 forge 管线", new_ids=("session-new",)):
    """Scripted ACP: prepare creates session-old; forge summarizes then news."""
    state = {"news": 0}

    def handle(_process, message):
        method = message.get("method")
        if method == "initialize":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": 1}}]
        if method == "session/new":
            session_id = "session-old" if state["news"] == 0 else new_ids[min(state["news"] - 1, len(new_ids) - 1)]
            state["news"] += 1
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                "sessionId": session_id,
                "models": {
                    "availableModels": [
                        {"modelId": "auto", "name": "Auto"},
                        {"modelId": "sonnet-test", "name": "Sonnet Test"},
                    ],
                    "currentModelId": "auto",
                },
            }}]
        if method == "session/load":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {
                "sessionId": message["params"]["sessionId"],
                "models": {
                    "availableModels": [
                        {"modelId": "auto", "name": "Auto"},
                        {"modelId": "sonnet-test", "name": "Sonnet Test"},
                    ],
                    "currentModelId": "auto",
                },
            }}]
        if method == "session/set_model":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {}}]
        if method == "_kiro.dev/commands/execute":
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {"success": True, "message": ""}}]
        if method == "session/prompt":
            session_id = message["params"]["sessionId"]
            text = summary if message["params"]["prompt"][0]["type"] == "text" else ""
            return [
                _chunk(session_id, text),
                {"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}},
            ]
        raise AssertionError(f"unexpected method {method}")

    return handle


class KiroACPForgeWireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = str(Path(self.tmp.name) / "kiro_acp_session.json")

    def _client(self, factory):
        return KiroACPClient(
            command="/fake/kiro-cli",
            cwd=self.tmp.name,
            state_path=self.state_path,
            request_timeout=5,
            prompt_timeout=5,
            popen_factory=factory,
        )

    def _pointer_session(self) -> str:
        payload = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        return str(payload.get("session_id") or "")

    def test_forge_summarizes_then_commits_pointer_and_clears_usage(self):
        process = FakeKiroACPProcess(_forge_handler())
        client = self._client(_scripted_factory([process]))
        self.assertEqual("session-old", client.prepare_session())
        # Simulate a header-status reading from the old session.
        client._context_usage_percent = 88.0
        new_session_id, summary = client.forge_new_session()
        self.assertEqual("session-new", new_session_id)
        self.assertEqual("总结：在调试 forge 管线", summary)
        # The durable pointer moved to the new session.
        self.assertEqual("session-new", self._pointer_session())
        self.assertEqual("session-new", client._loaded_session_id)
        # The stale usage cache was cleared for the fresh session.
        self.assertIsNone(client.context_usage_percent())
        # The summarize prompt went to the old session.
        prompts = [r for r in process.requests if r.get("method") == "session/prompt"]
        self.assertEqual(1, len(prompts))
        self.assertEqual("session-old", prompts[0]["params"]["sessionId"])
        self.assertIn("总结", prompts[0]["params"]["prompt"][0]["text"])
        client.close()

    def test_forge_without_pointer_fails_closed(self):
        process = FakeKiroACPProcess(_forge_handler())
        client = self._client(_scripted_factory([process]))
        with self.assertRaises(KiroACPError):
            client.forge_new_session()
        self.assertEqual([], process.requests)

    def test_forge_empty_summary_keeps_old_pointer(self):
        process = FakeKiroACPProcess(_forge_handler(summary="   "))
        client = self._client(_scripted_factory([process]))
        self.assertEqual("session-old", client.prepare_session())
        with self.assertRaises(KiroACPError):
            client.forge_new_session()
        self.assertEqual("session-old", self._pointer_session())
        self.assertEqual("session-old", client._loaded_session_id)
        # No second session/new was issued after the failed summarize.
        news = [r for r in process.requests if r.get("method") == "session/new"]
        self.assertEqual(1, len(news))
        client.close()

    def test_forge_reapplies_model_and_effort_pins_on_new_session(self):
        process = FakeKiroACPProcess(_forge_handler())
        client = self._client(_scripted_factory([process]))
        self.assertEqual("session-old", client.prepare_session())
        client.pin_model("sonnet-test")
        client.pin_effort("low")
        new_session_id, _summary = client.forge_new_session()
        self.assertEqual("session-new", new_session_id)
        # set_model / effort command were replayed after the forge's
        # session/new (the wire persists neither across sessions).
        methods = [r.get("method") for r in process.requests]
        last_new = max(index for index, method in enumerate(methods) if method == "session/new")
        replayed = methods[last_new + 1:]
        self.assertIn("session/set_model", replayed)
        self.assertIn("_kiro.dev/commands/execute", replayed)
        client.close()


if __name__ == "__main__":
    unittest.main()
