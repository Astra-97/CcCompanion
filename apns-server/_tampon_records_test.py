import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from push import PushHandler
from tampon_records import TamponRecordConflictError, TamponRecordStore, TamponRecordValidationError, apply_action


START = {
    "operation_id": "op-start-1", "action": "start", "at_ms": 1_787_581_800_000,
    "zone_id": "Asia/Shanghai", "size": "REGULAR",
}


class TamponRecordsTests(unittest.TestCase):
    def test_frozen_action_contract_and_server_owned_fields(self):
        state, result, _ = apply_action({"records": [], "operations": {}}, START)
        record = result["record"]
        self.assertEqual(record["started_at_ms"], START["at_ms"])
        self.assertEqual(record["zone_id"], "Asia/Shanghai")
        self.assertEqual(record["created_by"], "ASTRA")
        self.assertEqual(record["source"], "android")
        for field in ("actor", "source", "night", "status", "created_by", "closed_by"):
            with self.assertRaises(TamponRecordValidationError):
                apply_action({"records": [], "operations": {}}, {**START, field: "forged"})
        self.assertEqual(state["records"][0]["status"], "OPEN")

    def test_replace_is_atomic_and_replay_returns_same_operation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TamponRecordStore(Path(directory) / "tampon_records.json")
            opened, _ = store.apply(START)
            body = {
                "operation_id": "op-replace-1", "action": "replace", "at_ms": 1_787_598_000_000,
                "zone_id": "Asia/Shanghai", "expected_open_id": opened["record"]["id"],
                "amount": "FULL", "next_size": "SUPER",
            }
            result, replay = store.apply(body)
            self.assertFalse(replay)
            self.assertEqual(result["closed_record"]["ended_at_ms"], body["at_ms"])
            self.assertTrue(result["closed_record"]["night"])
            self.assertEqual(result["record"]["started_at_ms"], body["at_ms"])
            retry, replay = store.apply(body)
            self.assertTrue(replay)
            self.assertEqual(retry, result)
            state = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["records"]), 2)
            self.assertEqual(len([item for item in state["records"] if item["status"] == "OPEN"]), 1)

    def test_stale_and_invalid_close_do_not_change_open_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TamponRecordStore(Path(directory) / "tampon_records.json")
            opened, _ = store.apply(START)
            with self.assertRaises(TamponRecordConflictError):
                store.apply({**START, "size": "MINI"})
            with self.assertRaises(TamponRecordConflictError):
                store.apply({
                    "operation_id": "stale", "action": "remove", "at_ms": START["at_ms"] + 1,
                    "zone_id": "Asia/Shanghai", "expected_open_id": "not-current", "amount": "LIGHT",
                })
            with self.assertRaises(TamponRecordValidationError):
                store.apply({
                    "operation_id": "backwards", "action": "remove", "at_ms": START["at_ms"] - 1,
                    "zone_id": "Asia/Shanghai", "expected_open_id": opened["record"]["id"], "amount": "LIGHT",
                })
            self.assertEqual(json.loads(store.path.read_text())["records"][0]["status"], "OPEN")

    def test_close_requires_compare_and_swap_id_and_start_rejects_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TamponRecordStore(Path(directory) / "tampon_records.json")
            opened, _ = store.apply(START)
            with self.assertRaises(TamponRecordValidationError):
                store.apply({
                    "operation_id": "missing-cas", "action": "remove", "at_ms": START["at_ms"] + 1,
                    "zone_id": "Asia/Shanghai", "amount": "LIGHT",
                })
            with self.assertRaises(TamponRecordValidationError):
                apply_action({"records": [], "operations": {}}, {
                    **START, "operation_id": "start-with-cas", "expected_open_id": opened["record"]["id"],
                })
            state = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["records"]), 1)
            self.assertEqual(state["records"][0]["status"], "OPEN")

    def test_state_file_is_written_private_even_with_open_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TamponRecordStore(Path(directory) / "tampon_records.json")
            opened, _ = store.apply(START)
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            store.apply({
                "operation_id": "private-remove", "action": "remove", "at_ms": START["at_ms"] + 1,
                "zone_id": "Asia/Shanghai", "expected_open_id": opened["record"]["id"], "amount": "LIGHT",
            })
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_night_boundary_and_dst_use_record_start_zone(self):
        state, opened, _ = apply_action({"records": [], "operations": {}}, {
            "operation_id": "dst-start", "action": "start", "at_ms": 1_772_940_600_000,
            "zone_id": "America/New_York", "size": "MINI",  # 2026-03-07 22:30 -05
        })
        _, closed, _ = apply_action(state, {
            "operation_id": "dst-close", "action": "remove", "at_ms": 1_772_955_000_000,
            "zone_id": "America/New_York", "expected_open_id": opened["record"]["id"], "amount": "MEDIUM",
        })
        self.assertTrue(closed["closed_record"]["night"])

        state, opened, _ = apply_action({"records": [], "operations": {}}, {
            "operation_id": "early", "action": "start", "at_ms": 1_787_579_940_000,
            "zone_id": "Asia/Shanghai", "size": "MINI",  # 21:59 local
        })
        _, closed, _ = apply_action(state, {
            "operation_id": "early-close", "action": "remove", "at_ms": 1_787_596_200_000,
            "zone_id": "Asia/Shanghai", "expected_open_id": opened["record"]["id"], "amount": "MEDIUM",
        })
        self.assertFalse(closed["closed_record"]["night"])

    def test_handler_success_and_conflict_return_android_snapshots(self):
        handler = object.__new__(PushHandler)
        handler.responses = []
        handler._send_json = lambda status, payload, **_kwargs: handler.responses.append((status, payload))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tampon_records.json"
            with patch.object(PushHandler, "_TAMPON_RECORDS_PATH", path):
                handler._handle_tampon_records_action(START)
                ok = handler.responses[-1]
                self.assertEqual(ok[0], 200)
                self.assertEqual(ok[1]["schema"], "tampon_records.v1")
                self.assertIn("records", ok[1])
                self.assertIn("server_now_ms", ok[1])
                handler._handle_tampon_records_action({
                    "operation_id": "stale", "action": "remove", "at_ms": START["at_ms"] + 1,
                    "zone_id": "Asia/Shanghai", "expected_open_id": "bad", "amount": "LIGHT",
                })
                conflict = handler.responses[-1]
                self.assertEqual(conflict[0], 409)
                self.assertEqual(conflict[1]["reason"], "stale")
                self.assertIn("records", conflict[1]["snapshot"])

    def test_native_auth_is_required_before_tampon_routes_dispatch(self):
        handler = object.__new__(PushHandler)
        handler.path = "/tampon-records?limit=100"
        handler.state = SimpleNamespace(strict_auth=False)
        handler.responses = []
        handler._is_public_get = lambda: False
        handler._check_ip_allowed = lambda: True
        handler._native_pairing_auth_matches = lambda: False
        handler._send_json = lambda status, payload, **_kwargs: handler.responses.append((status, payload))
        handler.do_GET()
        self.assertEqual(handler.responses[0][0], 401)


if __name__ == "__main__":
    unittest.main()
