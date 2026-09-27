"""手机电量感知（2026-09-27）— 安静级注入 + 打扰级边缘触发。

覆盖：metadata 归一化 / 上下文一行渲染 / 低电边缘触发与滞回武装 /
/device/battery 处理器与 native pairing 闸门语义 / _handle_chat_send 在入库前
摘除 metadata.device（电量绝不进聊天历史）。
"""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

import push
from device_battery import (
    DeviceBatteryStore,
    format_device_battery_prompt,
    normalize_device_battery,
    normalize_low_threshold_percent,
)
from push import PushHandler


class NormalizeDeviceBatteryTest(unittest.TestCase):
    def test_accepts_exact_shape(self):
        self.assertEqual(
            {"battery_pct": 23, "charging": False},
            normalize_device_battery({"battery_pct": 23, "charging": False}),
        )
        self.assertEqual(
            {"battery_pct": 100, "charging": True},
            normalize_device_battery({"battery_pct": 100, "charging": True}),
        )

    def test_rejects_out_of_range_and_wrong_types(self):
        for bad in (
            None, "23", [], {"battery_pct": -1, "charging": False},
            {"battery_pct": 101, "charging": False},
            {"battery_pct": True, "charging": False},  # bool 不是合法百分比
            {"battery_pct": 23.5, "charging": False},  # 只接受整百分比
            {"battery_pct": 23},  # 缺 charging
            {"battery_pct": 23, "charging": "yes"},
            {"battery_pct": "23", "charging": False},
        ):
            self.assertIsNone(normalize_device_battery(bad), bad)

    def test_threshold_normalization(self):
        self.assertEqual(20, normalize_low_threshold_percent(None))
        self.assertEqual(20, normalize_low_threshold_percent("abc"))
        self.assertEqual(15, normalize_low_threshold_percent(15))
        self.assertEqual(5, normalize_low_threshold_percent(1))
        self.assertEqual(95, normalize_low_threshold_percent(200))


class FormatDeviceBatteryPromptTest(unittest.TestCase):
    def test_renders_one_line(self):
        self.assertEqual(
            "[设备状态] 手机电量 23%（未充电）",
            format_device_battery_prompt({"battery_pct": 23, "charging": False}),
        )
        self.assertEqual(
            "[设备状态] 手机电量 80%（充电中）",
            format_device_battery_prompt({"battery_pct": 80, "charging": True}),
        )

    def test_missing_or_invalid_data_is_silent(self):
        self.assertEqual("", format_device_battery_prompt(None))
        self.assertEqual("", format_device_battery_prompt({"battery_pct": 300, "charging": False}))


class DeviceBatteryStoreEdgeTriggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "device_battery.json"

    def make_store(self):
        return DeviceBatteryStore(self.path)

    def test_crossing_notifies_once_then_requires_rearm(self):
        store = self.make_store()
        _state, notify = store.update({"battery_pct": 19, "charging": False}, 20)
        self.assertTrue(notify)
        # 继续在低位徘徊：不重复轰炸。
        _state, notify = store.update({"battery_pct": 18, "charging": False}, 20)
        self.assertFalse(notify)
        _state, notify = store.update({"battery_pct": 10, "charging": False}, 20)
        self.assertFalse(notify)

    def test_rearm_only_at_threshold_plus_five(self):
        store = self.make_store()
        store.update({"battery_pct": 19, "charging": False}, 20)
        # 回到 22（低于 20+5）：尚未重新武装。
        _state, notify = store.update({"battery_pct": 22, "charging": False}, 20)
        self.assertFalse(notify)
        _state, notify = store.update({"battery_pct": 19, "charging": False}, 20)
        self.assertFalse(notify)
        # 回升到 25：重新武装；再次跌破才提醒。
        store.update({"battery_pct": 25, "charging": False}, 20)
        _state, notify = store.update({"battery_pct": 19, "charging": False}, 20)
        self.assertTrue(notify)

    def test_charging_suppresses_and_rearms(self):
        store = self.make_store()
        # 低电但充电中：不提醒，且重新武装。
        _state, notify = store.update({"battery_pct": 12, "charging": True}, 20)
        self.assertFalse(notify)
        # 拔掉电源仍低电：穿越提醒。
        _state, notify = store.update({"battery_pct": 12, "charging": False}, 20)
        self.assertTrue(notify)

    def test_state_file_keeps_only_latest_value(self):
        store = self.make_store()
        store.update({"battery_pct": 60, "charging": True}, 20)
        state, _notify = store.update({"battery_pct": 55, "charging": False}, 20)
        reloaded = self.make_store().snapshot()
        self.assertEqual(55, reloaded["battery_pct"])
        self.assertEqual(False, reloaded["charging"])
        self.assertEqual(state["low_armed"], reloaded["low_armed"])
        self.assertTrue(reloaded["updated_at"] > 0)

    def test_armed_bit_survives_restart(self):
        store = self.make_store()
        _state, notify = store.update({"battery_pct": 19, "charging": False}, 20)
        self.assertTrue(notify)
        # 重启后仍处于解除武装状态，不会补发一次提醒。
        _state, notify = self.make_store().update({"battery_pct": 18, "charging": False}, 20)
        self.assertFalse(notify)


def _battery_handler(store, *, threshold=20, secret="s3cret"):
    handler = object.__new__(PushHandler)
    handler.state = types.SimpleNamespace(
        device_battery=store,
        battery_low_threshold_percent=threshold,
        shared_secret=secret,
    )
    handler.responses = []
    handler.notifications = []
    handler._send_json = lambda status, payload: handler.responses.append((status, payload))
    handler._send_chat_notification = lambda title, body: handler.notifications.append((title, body))
    return handler


class DeviceBatteryReportHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DeviceBatteryStore(Path(self.tmp.name) / "device_battery.json")
        self.handler = _battery_handler(self.store)

    def report(self, body):
        self.handler._handle_device_battery_report(body)
        return self.handler.responses[-1]

    def test_bad_body_is_400(self):
        status, payload = self.report({"battery_pct": "low"})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertEqual([], self.handler.notifications)

    def test_low_battery_pushes_once_with_configured_threshold(self):
        status, payload = self.report({"battery_pct": 14, "charging": False})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["notified"])
        self.assertEqual(20, payload["threshold_percent"])
        self.assertEqual(1, len(self.handler.notifications))
        self.assertIn("14%", self.handler.notifications[0][1])
        # 再次上报不再提醒。
        _status, payload = self.report({"battery_pct": 13, "charging": False})
        self.assertFalse(payload["notified"])
        self.assertEqual(1, len(self.handler.notifications))

    def test_charging_never_notifies(self):
        _status, payload = self.report({"battery_pct": 5, "charging": True})
        self.assertFalse(payload["notified"])
        self.assertEqual([], self.handler.notifications)

    def test_threshold_comes_from_state_config(self):
        handler = _battery_handler(self.store, threshold=30)
        handler._handle_device_battery_report({"battery_pct": 25, "charging": False})
        status, payload = handler.responses[-1]
        self.assertEqual(200, status)
        self.assertTrue(payload["notified"])
        self.assertEqual(30, payload["threshold_percent"])


class NativePairingGateTest(unittest.TestCase):
    """/device/battery 走与 /kimi/ 相同的 fail-closed native pairing 闸门。"""

    def make_handler(self, headers, secret="s3cret"):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(shared_secret=secret)
        handler.headers = headers
        return handler

    def test_missing_or_wrong_token_fails_closed(self):
        self.assertFalse(self.make_handler({})._native_pairing_auth_matches())
        self.assertFalse(self.make_handler({"X-Auth-Token": "wrong"})._native_pairing_auth_matches())

    def test_empty_secret_fails_closed(self):
        handler = self.make_handler({"X-Auth-Token": "anything"}, secret="")
        self.assertFalse(handler._native_pairing_auth_matches())

    def test_exact_token_passes(self):
        handler = self.make_handler({"X-Auth-Token": "s3cret"})
        self.assertTrue(handler._native_pairing_auth_matches())


class ChatSendDeviceExtractionTest(unittest.TestCase):
    """metadata.device 在 _handle_chat_send 入口摘除：进内部字段，不进历史。"""

    def setUp(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace()
        handler.responses = []
        handler.captured = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
        handler._chat_contact_directory = lambda: [
            {"id": "xiaoke", "capabilities": ["chat"]},
            {"id": "kimi", "capabilities": ["chat"]},
        ]
        handler._consume_staged_attachments = lambda body, contact_id: []
        self.handler = handler
        self._real_dispatch = push.dispatch_contact_send

        def fake_dispatch(proxy, contact_id, body):
            handler.captured.append((contact_id, dict(body)))
            return True

        push.dispatch_contact_send = fake_dispatch
        self.addCleanup(setattr, push, "dispatch_contact_send", self._real_dispatch)

    def send(self, body):
        self.handler._handle_chat_send(body)
        return self.handler

    def test_device_battery_moves_to_internal_field_only(self):
        self.send({
            "text": "hi",
            "contact_id": "xiaoke",
            "metadata": {"device": {"battery_pct": 23, "charging": False}, "via": "card"},
        })
        self.assertEqual(1, len(self.handler.captured))
        _contact, body = self.handler.captured[0]
        self.assertEqual(
            {"battery_pct": 23, "charging": False}, body.get("_device_battery"),
        )
        self.assertNotIn("device", body.get("metadata") or {})
        self.assertEqual("card", (body.get("metadata") or {}).get("via"))

    def test_device_only_metadata_leaves_no_metadata_key(self):
        self.send({
            "text": "hi",
            "metadata": {"device": {"battery_pct": 80, "charging": True}},
        })
        _contact, body = self.handler.captured[0]
        self.assertNotIn("metadata", body)
        self.assertEqual({"battery_pct": 80, "charging": True}, body.get("_device_battery"))

    def test_caller_supplied_internal_field_is_dropped(self):
        self.send({
            "text": "hi",
            "_device_battery": {"battery_pct": 1, "charging": False},
        })
        _contact, body = self.handler.captured[0]
        self.assertIsNone(body.get("_device_battery"))

    def test_invalid_device_is_silently_dropped(self):
        self.send({"text": "hi", "metadata": {"device": {"battery_pct": "?"}}})
        _contact, body = self.handler.captured[0]
        self.assertIsNone(body.get("_device_battery"))
        self.assertNotIn("metadata", body)

    def test_old_app_without_device_is_untouched(self):
        self.send({"text": "hi"})
        _contact, body = self.handler.captured[0]
        self.assertIsNone(body.get("_device_battery"))
        self.assertNotIn("metadata", body)


class PromptDeviceLineTest(unittest.TestCase):
    def make_handler(self):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace()
        return handler

    def test_kimi_prompt_carries_line_inside_source_block(self):
        handler = self.make_handler()
        prompt = handler._kimi_prompt(
            "在吗",
            device_line="[设备状态] 手机电量 23%（未充电）",
        )
        self.assertIn(
            "contact_id: kimi\n[设备状态] 手机电量 23%（未充电）\n\nAstra 正在",
            prompt,
        )

    def test_kimi_prompt_without_device_line_keeps_old_shape(self):
        handler = self.make_handler()
        prompt = handler._kimi_prompt("在吗")
        self.assertIn("contact_id: kimi\n\nAstra 正在", prompt)
        self.assertNotIn("设备状态", prompt)

    def test_kimi_group_prompt_appends_device_block(self):
        handler = self.make_handler()
        prompt = handler._kimi_group_prompt(
            "你们好", sender_name="Astra",
            device_line="[设备状态] 手机电量 15%（未充电）",
        )
        self.assertIn("[设备状态] 手机电量 15%（未充电）", prompt)
        self.assertNotIn("设备状态", handler._kimi_group_prompt("你们好", sender_name="Astra"))

    def test_kairos_task_prompt_renders_device_line(self):
        handler = self.make_handler()
        prompt = handler._kairos_prompt_for_task({
            "text": "看看这个",
            "device_battery": {"battery_pct": 42, "charging": True},
        })
        self.assertIn(
            "contact_id: kairos\n[设备状态] 手机电量 42%（充电中）\n",
            prompt,
        )
        self.assertNotIn(
            "设备状态",
            handler._kairos_prompt_for_task({"text": "看看这个"}),
        )


if __name__ == "__main__":
    unittest.main()
