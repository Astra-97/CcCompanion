import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tool_dispatcher


class MorningScheduleIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schedule_file = Path(self.temp.name) / "tool_dispatcher.json"
        self.deliveries = []

    def tearDown(self):
        self.temp.cleanup()

    def _write_rule(self, rule_id="morning_greeting", text="早安触发"):
        self.schedule_file.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "rules": [
                        {
                            "id": rule_id,
                            "enabled": True,
                            "time": "07:30",
                            "tz": "UTC",
                            "contact_id": "xiaoke",
                            "text": text,
                            "catch_up_grace_minutes": 60,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _dispatcher(self):
        def deliver(contact_id, text, rule_id):
            self.deliveries.append((contact_id, text, rule_id))
            return True, ""

        return tool_dispatcher.ToolDispatcher(
            tool_dispatcher.ScheduleStore(self.schedule_file), deliver
        )

    def _tick(self, dispatcher):
        return dispatcher.tick(
            datetime.datetime(2026, 7, 18, 7, 30, tzinfo=datetime.timezone.utc)
        )

    def test_morning_rule_appends_nonempty_schedule_safely(self):
        self._write_rule()
        completed = mock.Mock(returncode=0, stdout="09:00 私教课\n全天 整理资料\n")
        with mock.patch.object(
            tool_dispatcher.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(self._tick(self._dispatcher()), 1)

        run.assert_called_once_with(
            ["/usr/local/bin/schedule-ctl", "today"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(self.deliveries[0][0], "xiaoke")
        self.assertEqual(self.deliveries[0][2], "morning_greeting")
        self.assertIn("今日日程：09:00 私教课；全天 整理资料", self.deliveries[0][1])

    def test_non_morning_rule_does_not_read_schedule(self):
        self._write_rule(rule_id="diary_reminder", text="日记触发")
        with mock.patch.object(tool_dispatcher.subprocess, "run") as run:
            self.assertEqual(self._tick(self._dispatcher()), 1)
        run.assert_not_called()
        self.assertEqual(self.deliveries[0][1], "日记触发")

    def test_schedule_timeout_degrades_to_original_morning_text(self):
        self._write_rule()
        with mock.patch.object(
            tool_dispatcher.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("schedule-ctl", 5),
        ):
            self.assertEqual(self._tick(self._dispatcher()), 1)
        self.assertEqual(self.deliveries[0][1], "早安触发")

    def test_empty_schedule_keeps_original_morning_text(self):
        self._write_rule()
        completed = mock.Mock(returncode=0, stdout="\n")
        with mock.patch.object(
            tool_dispatcher.subprocess, "run", return_value=completed
        ):
            self.assertEqual(self._tick(self._dispatcher()), 1)
        self.assertEqual(self.deliveries[0][1], "早安触发")

    def test_timezone_resolution_remains_available(self):
        resolved = tool_dispatcher._resolve_tz("Asia/Shanghai")
        self.assertIsNotNone(resolved)
        self.assertEqual(getattr(resolved, "key", None), "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
