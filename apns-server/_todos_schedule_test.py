"""Schedule-backed homepage todo contract tests (all fixtures are temporary)."""

import fcntl
import json
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import todos
from push import PushHandler, _format_schedule_todo_when


def _event(event_id: str, title: str, date: str, event_time: str | None, *, done=False, note=None):
    return {
        "id": event_id,
        "title": title,
        "date": date,
        "time": event_time,
        "remind_minutes_before": 30,
        "note": note,
        "created_at": "2026-08-16T12:00:00+08:00",
        "reminded": False,
        "done": done,
    }


class ScheduleTodosTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, events):
        self.path.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_get_projection_keeps_todo_contract_and_stable_schedule_event_id(self):
        self.write([
            _event("stale", "旧法棍", "2026-07-20", None),
            _event("late", "晚间复盘", "2026-08-17", "20:00", note="十分钟"),
            _event("all-day", "整理", "2026-08-16", None, done=True),
        ])

        sections = todos.collect_all(self.path, today=date(2026, 8, 16))

        self.assertEqual(len(sections), 1)
        section = sections[0]
        self.assertEqual(section["section"], "日程")
        self.assertEqual(section["source"], "schedule")
        self.assertEqual(section["count"], 2)
        self.assertEqual(section["pending"], 1)
        self.assertEqual([item["event_id"] for item in section["items"]], ["all-day", "late"])
        self.assertNotIn("stale", [item["event_id"] for item in section["items"]])
        self.assertEqual(section["items"][1]["text"], "晚间复盘")
        self.assertEqual(section["items"][1]["dueDate"], "2026-08-17")
        self.assertEqual(section["items"][1]["note"], "十分钟")
        self.assertIsNone(section["items"][0]["lineIndex"])

    def test_toggle_uses_event_id_expected_done_and_same_atomic_schedule_file(self):
        self.write([_event("evt1", "私教课", "2026-08-16", "19:00")])

        result = todos.toggle("", "", "", event_id="evt1", expected_done=False, schedule_path=self.path)

        self.assertTrue(result["ok"])
        self.assertTrue(result["new_done"])
        self.assertEqual(result["event_id"], "evt1")
        self.assertEqual(result["date"], "2026-08-16")
        self.assertEqual(result["time"], "19:00")
        self.assertTrue(self.read()[0]["done"])
        self.assertEqual(
            todos.toggle("", "", "", event_id="evt1", expected_done=False, schedule_path=self.path),
            {"ok": False, "error": "race_detected"},
        )
        self.assertEqual(
            todos.toggle("", "", "", expected_done=True, schedule_path=self.path),
            {"ok": False, "error": "schedule_event_id_required"},
        )

    def test_schedule_lock_and_corrupt_data_fail_closed(self):
        self.write([_event("evt1", "锁", "2026-08-16", "19:00")])
        with mock.patch.object(todos.fcntl, "flock", wraps=fcntl.flock) as flock:
            result = todos.toggle("", "", "", event_id="evt1", expected_done=False, schedule_path=self.path)
        self.assertTrue(result["ok"])
        self.assertTrue((self.path.parent / ".lock").exists())
        self.assertIn(fcntl.LOCK_EX, [call.args[1] for call in flock.call_args_list])

        original = "{bad json"
        self.path.write_text(original, encoding="utf-8")
        result = todos.toggle("", "", "", event_id="evt1", expected_done=True, schedule_path=self.path)
        self.assertEqual(result, {"ok": False, "error": "schedule_unavailable"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        with self.assertRaises(todos.ScheduleTodoStoreError):
            todos.collect_all(self.path)


class TodosHandlerAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.json"
        self.path.write_text(json.dumps([_event("evt1", "私密日程", "2099-08-16", "09:00")]), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def handler(self, token=""):
        handler = object.__new__(PushHandler)
        handler.history_records = []
        handler.notifications = []

        class _History:
            def append(_self, **record):
                handler.history_records.append(record)
                return {"ts": "test-turn", **record}

        handler.state = types.SimpleNamespace(
            strict_auth=True,
            shared_secret="test-secret",
            todos_schedule_path=str(self.path),
            bus_send_path="/unused",
            contact_chats={"xiaoke": _History()},
        )
        handler.headers = {"X-Auth-Token": token}
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._notify_chain_todo = lambda text: handler.notifications.append(text)
        return handler

    def test_get_and_toggle_require_authentication(self):
        handler = self.handler()
        handler._handle_todos_list()
        self.assertEqual(handler.responses[0][0], 401)
        handler.responses.clear()
        handler._handle_todos_toggle({"event_id": "evt1", "expected_done": False})
        self.assertEqual(handler.responses[0][0], 401)

        handler = self.handler("test-secret")
        handler._handle_todos_list()
        self.assertEqual(handler.responses[0][0], 200)
        self.assertEqual(handler.responses[0][1]["sections"][0]["items"][0]["event_id"], "evt1")
        handler.responses.clear()
        handler._handle_todos_toggle({"event_id": "evt1", "expected_done": False})
        self.assertEqual(handler.responses[0][0], 200)
        self.assertTrue(handler.responses[0][1]["new_done"])
        self.assertEqual(len(handler.history_records), 1)
        record = handler.history_records[0]
        self.assertEqual(record["role"], "user")
        self.assertEqual(record["source"], "todos")
        self.assertEqual(record["text"], "日程更新\n✅ 已完成：私密日程\n2099年8月16日 周日 · 09:00")
        self.assertEqual(record["metadata"], {
            "todo_share": True,
            "event_id": "evt1",
            "done": True,
            "date": "2099-08-16",
            "time": "09:00",
        })
        self.assertEqual(handler.notifications, [record["text"]])

        # A replay/race is not a second user utterance or AI notification.
        handler.responses.clear()
        handler.history_records.clear()
        handler.notifications.clear()
        handler._handle_todos_toggle({"event_id": "evt1", "expected_done": False})
        self.assertEqual(handler.responses[0][0], 400)
        self.assertEqual(handler.history_records, [])
        self.assertEqual(handler.notifications, [])

        handler.responses.clear()
        handler._handle_todos_toggle({"event_id": "evt1", "expected_done": True})
        self.assertEqual(handler.responses[0][0], 200)
        self.assertEqual(handler.history_records[0]["text"], "日程更新\n↩️ 已取消完成：私密日程\n2099年8月16日 周日 · 09:00")
        self.assertEqual(handler.history_records[0]["metadata"]["done"], False)
        self.assertEqual(handler.notifications, [handler.history_records[0]["text"]])

    def test_successful_schedule_write_surfaces_history_append_partial_failure(self):
        handler = self.handler("test-secret")

        class _FailingHistory:
            def append(_self, **_record):
                raise OSError("test history unavailable")

        handler.state.contact_chats["xiaoke"] = _FailingHistory()
        with mock.patch("push.logger.exception") as log_exception:
            handler._handle_todos_toggle({"event_id": "evt1", "expected_done": False})

        self.assertEqual(handler.responses[0][0], 503)
        log_exception.assert_called_once()
        payload = handler.responses[0][1]
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["schedule_updated"])
        self.assertTrue(payload["new_done"])
        self.assertEqual(payload["partial_failure"], "chat_history_append_failed")
        self.assertEqual(payload["error"], "schedule_updated_but_chat_history_append_failed")
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))[0]["done"])
        self.assertEqual(handler.notifications, [])

    def test_add_and_edit_explicitly_reject_schedule_source(self):
        handler = self.handler("test-secret")
        handler._handle_todos_add({"event_id": "evt1", "source": "schedule"})
        self.assertEqual(handler.responses, [(409, {"ok": False, "error": "schedule_add_unsupported"})])
        handler.responses.clear()
        handler._handle_todos_edit({"event_id": "evt1", "source": "schedule"})
        self.assertEqual(handler.responses, [(409, {"ok": False, "error": "schedule_edit_unsupported"})])

    def test_visible_schedule_when_formats_today_future_all_day_and_invalid_values(self):
        today = date(2026, 8, 16)
        self.assertEqual(_format_schedule_todo_when("2026-08-16", "19:00", today=today), "今天 周日 · 19:00")
        self.assertEqual(_format_schedule_todo_when("2026-08-18", "09:30", today=today), "8月18日 周二 · 09:30")
        self.assertEqual(_format_schedule_todo_when("2027-01-02", "09:30", today=today), "2027年1月2日 周六 · 09:30")
        self.assertEqual(_format_schedule_todo_when("2026-08-16", None, today=today), "今天 周日 · 全天")
        self.assertEqual(_format_schedule_todo_when("invalid", "09:30", today=today), "日期待定 · 09:30")


if __name__ == "__main__":
    unittest.main()
