import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from push import PushHandler, _scan_kimi_session_tasks


class ScanKimiSessionTasksTest(unittest.TestCase):
    def _make_root(self, root: Path) -> Path:
        tasks = root / "wd_test" / "session_x" / "agents" / "main" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "done-1.json").write_text(json.dumps({
            "taskId": "done-1",
            "description": "查案",
            "status": "completed",
            "kind": "agent",
        }), encoding="utf-8")
        (tasks / "run-1.json").write_text(json.dumps({
            "taskId": "run-1",
            "description": "长跑",
            "status": "running",
            "kind": "bash",
        }), encoding="utf-8")
        (tasks / "broken.json").write_text("{not json", encoding="utf-8")
        return tasks

    def test_classifies_terminal_and_pending_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            reports = Path(tmp) / "task-reports"
            self._make_root(root)
            reports.mkdir()
            (reports / "done-1.md").write_text("# 报告", encoding="utf-8")
            result = _scan_kimi_session_tasks(
                "session_x", sessions_root=root, reports_dir=reports,
            )
            self.assertEqual(["done-1"], [t["task_id"] for t in result["finished"]])
            self.assertEqual(["run-1"], [t["task_id"] for t in result["pending"]])
            self.assertEqual(
                str(reports / "done-1.md"), result["finished"][0]["report_path"],
            )
            self.assertNotIn("report_path", result["pending"][0])

    def test_rejects_foreign_or_empty_session_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                {"finished": [], "pending": []},
                _scan_kimi_session_tasks("", sessions_root=root),
            )
            self.assertEqual(
                {"finished": [], "pending": []},
                _scan_kimi_session_tasks("../escape", sessions_root=root),
            )
            self.assertEqual(
                {"finished": [], "pending": []},
                _scan_kimi_session_tasks("session_missing", sessions_root=root),
            )

    def test_drops_task_files_with_unsafe_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            tasks = self._make_root(root)
            (tasks / "evil.json").write_text(json.dumps({
                "taskId": "../../../etc/passwd",
                "description": "逃逸",
                "status": "completed",
            }), encoding="utf-8")
            result = _scan_kimi_session_tasks("session_x", sessions_root=root)
            ids = [t["task_id"] for t in result["finished"] + result["pending"]]
            self.assertEqual(["done-1", "run-1"], sorted(ids))


def _make_handler(tmp: Path, web: object) -> PushHandler:
    chat = types.SimpleNamespace(rows=[], append=lambda **row: chat.rows.append(row) or row)
    state = types.SimpleNamespace(
        kimi_turn_lock=threading.RLock(),
        kimi_active_turn={},
        kimi_prepare_token="",
        kimi_recovery_token="",
        kimi_terminal_acquire_token="",
        kimi_web=web,
        kimi_web_permission_mode="auto",
        kimi_auto_forge_context_threshold=0.0,
        token_store_path=str(tmp / "tokens" / "device_tokens.json"),
        contact_chats={"kimi": chat},
    )
    handler = object.__new__(PushHandler)
    handler.state = state
    handler.responses = []
    handler.notifications = []
    handler._send_json = lambda status, payload: handler.responses.append((status, payload))
    handler._kimi_selection = lambda: ("kimi-code/k3-256k", "high")
    handler._send_chat_notification = lambda title, body: (
        handler.notifications.append((title, body))
    )
    return handler


class FakeWeb:
    def __init__(self, *, active="session_old", busy=False):
        self.active = active
        self.busy = busy
        self.created = []
        self.seeds = []

    def start(self):
        pass

    def load_active_session_id(self):
        return self.active

    def get_session_status(self, session_id, **_kwargs):
        return {"busy": self.busy}

    def create_session(self, *, title, model, thinking, permission_mode):
        self.created.append({"title": title, "model": model, "thinking": thinking})
        self.active = "session_new"
        return "session_new"

    def submit_prompt(self, session_id, text, **_kwargs):
        self.seeds.append((session_id, text))
        return {"prompt_id": "p1"}


class HandleKimiForgeTest(unittest.TestCase):
    def test_controlled_forge_hands_off_tasks_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _make_handler(Path(tmp), web)
            tasks = {
                "finished": [{
                    "task_id": "done-1", "description": "查案",
                    "status": "completed", "kind": "agent",
                    "report_path": "/root/.kimi-code/task-reports/done-1.md",
                }],
                "pending": [{
                    "task_id": "run-1", "description": "长跑",
                    "status": "running", "kind": "bash",
                }],
            }
            with patch("push._scan_kimi_session_tasks", lambda _sid: tasks):
                handler._handle_kimi_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(200, status)
            self.assertTrue(payload["ok"])
            self.assertEqual("session_old", payload["previous_session_id"])
            self.assertEqual("session_new", payload["active_session_id"])
            self.assertEqual(1, payload["finished_tasks"])
            self.assertEqual(1, payload["pending_tasks"])
            self.assertTrue(payload["seed_submitted"])
            # Pointer moved and the seed carries the pending task handoff.
            self.assertEqual("session_new", web.active)
            self.assertEqual(1, len(web.seeds))
            seed_session, seed_text = web.seeds[0]
            self.assertEqual("session_new", seed_session)
            self.assertIn("run-1", seed_text)
            self.assertIn("done-1", seed_text)
            # Handoff record persisted next to the token store.
            handoff = Path(payload["handoff_record"])
            record = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual("session_old", record["old_session_id"])
            self.assertEqual("run-1", record["pending_tasks"][0]["task_id"])
            self.assertEqual(0o600, handoff.stat().st_mode & 0o777)
            # User-facing notice: history row plus one APNs banner.
            chat = handler.state.contact_chats["kimi"]
            self.assertEqual(1, len(chat.rows))
            self.assertEqual("system", chat.rows[0]["role"])
            self.assertIn("session_new", chat.rows[0]["text"])
            self.assertIn("长跑", chat.rows[0]["text"])
            self.assertIn("done-1.md", chat.rows[0]["text"])
            self.assertEqual(1, len(handler.notifications))
            # The control reservation is always released.
            self.assertEqual("", handler.state.kimi_prepare_token)

    def test_forge_refuses_while_busy_and_keeps_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb(busy=True)
            handler = _make_handler(Path(tmp), web)
            with patch("push._scan_kimi_session_tasks") as scan:
                handler._handle_kimi_forge({})
            scan.assert_not_called()
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("kimi_busy", payload["error"])
            self.assertEqual("session_old", web.active)
            self.assertEqual([], web.seeds)
            self.assertEqual("", handler.state.kimi_prepare_token)

    def test_forge_without_active_session_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb(active="")
            handler = _make_handler(Path(tmp), web)
            handler._handle_kimi_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("no_active_kimi_session", payload["error"])
            self.assertEqual([], web.created)

    def test_concurrent_turn_blocks_forge(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = _make_handler(Path(tmp), web)
            handler.state.kimi_active_turn = {"user_ts": "t1"}
            handler._handle_kimi_forge({})
            status, payload = handler.responses[-1]
            self.assertEqual(409, status)
            self.assertEqual("kimi_busy", payload["error"])
            self.assertEqual([], web.created)


class AutoForgePipelineTest(unittest.TestCase):
    """Threshold auto-forge reuses the controlled-forge handoff pipeline."""

    TASKS = {
        "finished": [{
            "task_id": "done-1", "description": "查案",
            "status": "completed", "kind": "agent",
            "report_path": "/root/.kimi-code/task-reports/done-1.md",
        }],
        "pending": [{
            "task_id": "run-1", "description": "长跑",
            "status": "running", "kind": "bash",
        }],
    }

    def _auto_handler(self, tmp: str, web: "FakeWeb", *, threshold: float, usage: float) -> PushHandler:
        handler = _make_handler(Path(tmp), web)
        handler.state.kimi_auto_forge_context_threshold = threshold
        handler._kimi_context_usage = lambda _session: usage
        return handler

    def test_threshold_zero_never_forges(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = self._auto_handler(tmp, web, threshold=0.0, usage=0.99)
            session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], web.created)
            self.assertEqual([], web.seeds)

    def test_usage_below_threshold_keeps_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = self._auto_handler(tmp, web, threshold=0.8, usage=0.5)
            session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], web.created)

    def test_auto_forge_runs_full_handoff_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = self._auto_handler(tmp, web, threshold=0.8, usage=0.9)
            with patch("push._scan_kimi_session_tasks", return_value=dict(self.TASKS)) as scan:
                session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_new", True), (session_id, forged))
            # Same pipeline as the controlled forge: inventory of the old
            # session, pointer swap, handoff record, seed, chat + push notice.
            scan.assert_called_once_with("session_old")
            self.assertEqual("session_new", web.active)
            handoff = Path(handler.state.token_store_path).parent / "kimi_forge_handoff.json"
            record = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual("session_old", record["old_session_id"])
            self.assertEqual("session_new", record["new_session_id"])
            self.assertEqual("run-1", record["pending_tasks"][0]["task_id"])
            self.assertEqual(0o600, handoff.stat().st_mode & 0o777)
            self.assertEqual(1, len(web.seeds))
            seed_session, seed_text = web.seeds[0]
            self.assertEqual("session_new", seed_session)
            self.assertIn("run-1", seed_text)
            # No silent forge: the notice names the automatic trigger.
            chat = handler.state.contact_chats["kimi"]
            self.assertEqual(1, len(chat.rows))
            self.assertEqual("system", chat.rows[0]["role"])
            self.assertEqual("system:kimi-forge", chat.rows[0]["source"])
            self.assertIn("自动 forge", chat.rows[0]["text"])
            self.assertIn("session_new", chat.rows[0]["text"])
            self.assertEqual(1, len(handler.notifications))
            self.assertIn("自动", handler.notifications[0][0])

    def test_busy_session_skips_this_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb(busy=True)
            handler = self._auto_handler(tmp, web, threshold=0.8, usage=0.95)
            with patch("push._scan_kimi_session_tasks") as scan:
                session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            scan.assert_not_called()
            self.assertEqual([], web.created)
            self.assertEqual([], web.seeds)
            self.assertEqual("session_old", web.active)
            chat = handler.state.contact_chats["kimi"]
            self.assertEqual([], chat.rows)
            self.assertEqual([], handler.notifications)

    def test_failed_swap_keeps_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            web.create_session = lambda **_kwargs: ""
            handler = self._auto_handler(tmp, web, threshold=0.8, usage=0.95)
            with patch("push._scan_kimi_session_tasks", return_value={"finished": [], "pending": []}):
                session_id, forged = handler._maybe_forge_kimi_session("session_old")
            self.assertEqual(("session_old", False), (session_id, forged))
            self.assertEqual([], web.seeds)
            self.assertEqual([], handler.notifications)

    def test_pointer_changed_since_measurement_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = FakeWeb()
            handler = self._auto_handler(tmp, web, threshold=0.8, usage=0.95)
            with patch("push._scan_kimi_session_tasks") as scan:
                session_id, forged = handler._maybe_forge_kimi_session("session_other")
            self.assertEqual(("session_other", False), (session_id, forged))
            scan.assert_not_called()
            self.assertEqual([], web.created)
            self.assertEqual("session_old", web.active)


if __name__ == "__main__":
    unittest.main()
