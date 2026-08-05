import unittest
from datetime import date
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from health_records import (
    HealthRecordValidationError,
    format_health_context_prompt,
    is_explicit_health_share,
    legacy_period_fields,
    normalize_health_context,
    period_record_matches_date,
    validate_period_payload,
)
from push import PushHandler


class HealthRecordsTests(unittest.TestCase):
    def test_handler_persists_period_cycle_and_deduplicates_retry(self):
        handler = object.__new__(PushHandler)
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health_records.json"
            with patch.object(PushHandler, "_HEALTH_RECORDS_PATH", path):
                payload = {
                    "type": "period_cycle",
                    "start_date": "2026-07-28",
                    "end_date": "2026-08-02",
                    "client_record_id": "cycle-retry-1",
                    "source": "android",
                    "actor": "user",
                }
                handler._handle_health_records_post(payload)
                handler._handle_health_records_post(payload)

            self.assertEqual(handler.responses[0][0], 200)
            self.assertEqual(handler.responses[1][0], 200)
            self.assertTrue(handler.responses[1][1]["deduplicated"])
            self.assertEqual(len(json.loads(path.read_text())), 1)

    def test_period_cycle_payload_is_canonical(self):
        record = validate_period_payload(
            {
                "type": "period_cycle",
                "start_date": "2026-07-28",
                "end_date": "2026-08-02",
                "luteal_start_date": "2026-08-08",
                "luteal_end_date": "2026-08-14",
                "next_period_date": "2026-08-25",
                "client_record_id": "cycle-1",
                "actor": "ai",
            }
        )
        self.assertEqual(record["start_date"], "2026-07-28")
        self.assertEqual(record["luteal_end_date"], "2026-08-14")
        self.assertEqual(record["client_record_id"], "cycle-1")
        self.assertEqual(record["actor"], "ai")

    def test_invalid_date_order_and_unpaired_luteal_range_are_rejected(self):
        with self.assertRaises(HealthRecordValidationError):
            validate_period_payload({"start_date": "2026-08-02", "end_date": "2026-08-01"})
        with self.assertRaises(HealthRecordValidationError):
            validate_period_payload({"start_date": "2026-08-02", "luteal_start_date": "2026-08-08"})

    def test_legacy_period_value_becomes_explicit_day_record(self):
        record = legacy_period_fields({"value": 3, "note": "旧客户端"}, timestamp_ms=1_727_424_000_000)
        self.assertEqual(record["day_number"], 3)
        self.assertEqual(record["start_date"], "2024-09-27")
        self.assertEqual(record["note"], "旧客户端")

    def test_cycle_matches_period_and_prediction_dates(self):
        record = {
            "start_date": "2026-07-28",
            "end_date": "2026-08-02",
            "luteal_start_date": "2026-08-08",
            "luteal_end_date": "2026-08-14",
            "next_period_date": "2026-08-25",
        }
        self.assertTrue(period_record_matches_date(record, date(2026, 8, 1)))
        self.assertTrue(period_record_matches_date(record, date(2026, 8, 10)))
        self.assertTrue(period_record_matches_date(record, date(2026, 8, 25)))

    def test_health_context_is_allowlisted_and_bounded(self):
        context = normalize_health_context(
            {
                "schema": "period_cycle.v1",
                "record": {
                    "start_date": "2026-07-28",
                    "next_period_date": "2026-08-25",
                    "secret": "must not pass",
                },
            }
        )
        self.assertEqual(context["record"], {
            "start_date": "2026-07-28",
            "next_period_date": "2026-08-25",
        })
        prompt = format_health_context_prompt(context)
        self.assertIn("开始日期", prompt)
        self.assertNotIn("must not pass", prompt)

    def test_empty_health_context_is_not_injected(self):
        self.assertEqual(format_health_context_prompt({}), "")

    def test_ordinary_chat_message_is_not_a_health_share(self):
        context = {"schema": "period_cycle.v1", "record": {"start_date": "2026-07-28"}}
        self.assertFalse(
            is_explicit_health_share("那排骨明天中午能吃吗。。", {"health_context": context})
        )
        self.assertFalse(is_explicit_health_share("健康的排骨", {"health_context": context}))
        self.assertFalse(is_explicit_health_share("", None))

    def test_health_app_share_is_detected(self):
        self.assertTrue(
            is_explicit_health_share("健康数据 2026-08-05\n\n运动\n步数: 2,624", {})
        )
        self.assertTrue(is_explicit_health_share("健康数据：今天", None))

    def test_client_share_flag_is_honoured(self):
        self.assertTrue(is_explicit_health_share("在吗", {"health_share": True}))
        self.assertTrue(is_explicit_health_share("在吗", {"health_share": "true"}))
        self.assertTrue(is_explicit_health_share("在吗", {"via": "health_share"}))
        self.assertTrue(
            is_explicit_health_share("在吗", {"health_context": {"share": True}})
        )
        self.assertTrue(
            is_explicit_health_share(
                "在吗", {"health_context": {"source": "health_app_share"}}
            )
        )
        self.assertFalse(is_explicit_health_share("在吗", {"health_share": False}))
        self.assertFalse(is_explicit_health_share("在吗", {"via": "card"}))


if __name__ == "__main__":
    unittest.main()
