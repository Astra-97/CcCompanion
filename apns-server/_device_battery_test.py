"""手机感知 v4（2026-09-27）— 打扰级气泡 + AI 主动查询，无 metadata 水印。

2026-09-28：气泡改为米家/小石榴同款事件胶囊（mihome_event），并以低调模式
注入 cctg（device_notice.XiaokeLowKeyNotifier，小克忙时排队）。

覆盖：电量/阈值归一化、充拔切换与低电滞回事件、应用开/关事件归一化与
60s 气泡防抖、活动快照归一化与只存最新值、/device/battery、/device/app-event、
/device/activity、GET /device/status 四个处理器与 native pairing 闸门语义。
"""
from __future__ import annotations

import tempfile
import threading
import types
import unittest
from datetime import datetime
from pathlib import Path

import push
from device_notice import (
    SILENT_SUFFIX,
    TZ_BEIJING,
    XiaokeLowKeyNotifier,
    build_device_notice,
)
from device_activity import (
    DeviceActivityStore,
    normalize_activity_snapshot,
    normalize_app_event,
)
from device_battery import (
    DeviceBatteryStore,
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
        self.assertEqual(25, normalize_low_threshold_percent(None))
        self.assertEqual(25, normalize_low_threshold_percent("abc"))
        self.assertEqual(15, normalize_low_threshold_percent(15))
        self.assertEqual(5, normalize_low_threshold_percent(1))
        self.assertEqual(95, normalize_low_threshold_percent(200))


class DeviceBatteryStoreEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "device_battery.json"

    def make_store(self):
        return DeviceBatteryStore(self.path)

    def test_first_report_is_silent(self):
        _state, events = self.make_store().update({"battery_pct": 80, "charging": True}, 25)
        self.assertEqual([], events)

    def test_charging_switch_events(self):
        store = self.make_store()
        store.update({"battery_pct": 60, "charging": False}, 25)
        _state, events = store.update({"battery_pct": 61, "charging": True}, 25)
        self.assertEqual(["charging_started"], events)
        # 持续充电：不重复。
        _state, events = store.update({"battery_pct": 70, "charging": True}, 25)
        self.assertEqual([], events)
        # 拔电。
        _state, events = store.update({"battery_pct": 70, "charging": False}, 25)
        self.assertEqual(["charging_stopped"], events)

    def test_low_battery_fires_once_then_requires_rearm(self):
        store = self.make_store()
        _state, events = store.update({"battery_pct": 24, "charging": False}, 25)
        self.assertEqual(["low_battery"], events)
        # 继续在低位徘徊：不重复轰炸。
        _state, events = store.update({"battery_pct": 20, "charging": False}, 25)
        self.assertEqual([], events)

    def test_rearm_only_at_threshold_plus_five(self):
        store = self.make_store()
        store.update({"battery_pct": 24, "charging": False}, 25)
        # 回到 29（低于 25+5）：尚未重新武装。
        store.update({"battery_pct": 29, "charging": False}, 25)
        _state, events = store.update({"battery_pct": 24, "charging": False}, 25)
        self.assertEqual([], events)
        # 回升到 30：重新武装；再次跌破才提醒。
        store.update({"battery_pct": 30, "charging": False}, 25)
        _state, events = store.update({"battery_pct": 24, "charging": False}, 25)
        self.assertEqual(["low_battery"], events)

    def test_charging_suppresses_and_rearms(self):
        store = self.make_store()
        # 低电但充电中（首次上报）：不提醒低电，且重新武装；首报不报切换事件。
        _state, events = store.update({"battery_pct": 12, "charging": True}, 25)
        self.assertEqual([], events)
        # 拔掉电源仍低电：穿越提醒 + 拔电事件一起出。
        _state, events = store.update({"battery_pct": 12, "charging": False}, 25)
        self.assertIn("low_battery", events)
        self.assertIn("charging_stopped", events)

    def test_state_file_keeps_only_latest_value(self):
        store = self.make_store()
        store.update({"battery_pct": 60, "charging": True}, 25)
        state, _events = store.update({"battery_pct": 55, "charging": False}, 25)
        reloaded = self.make_store().snapshot()
        self.assertEqual(55, reloaded["battery_pct"])
        self.assertEqual(False, reloaded["charging"])
        self.assertEqual(state["low_armed"], reloaded["low_armed"])
        self.assertTrue(reloaded["updated_at"] > 0)

    def test_armed_bit_survives_restart(self):
        store = self.make_store()
        _state, events = store.update({"battery_pct": 24, "charging": False}, 25)
        self.assertEqual(["low_battery"], events)
        # 重启后仍处于解除武装状态，不会补发一次提醒。
        _state, events = self.make_store().update({"battery_pct": 23, "charging": False}, 25)
        self.assertEqual([], events)


class NormalizeAppEventTest(unittest.TestCase):
    def test_accepts_open_and_close(self):
        self.assertEqual(
            {"event": "open", "package": "com.papegames.lysk.cn", "label": "恋与深空"},
            normalize_app_event({
                "event": "open", "package": "com.papegames.lysk.cn", "label": "恋与深空",
            }),
        )
        self.assertEqual(
            "close",
            normalize_app_event({"event": "CLOSE", "package": "a.b", "label": "x"})["event"],
        )

    def test_rejects_bad_event_or_package(self):
        for bad in (
            None, [], {"event": "peek", "package": "a.b"},
            {"event": "open"}, {"event": "open", "package": ""},
            {"event": "open", "package": "a\nb"}, {"event": "open", "package": "x" * 200},
        ):
            self.assertIsNone(normalize_app_event(bad), bad)

    def test_label_falls_back_to_package_and_strips_newlines(self):
        event = normalize_app_event({"event": "open", "package": "a.b", "label": ""})
        self.assertEqual("a.b", event["label"])
        event = normalize_app_event({"event": "open", "package": "a.b", "label": "一\n二"})
        self.assertEqual("一 二", event["label"])


class NormalizeActivitySnapshotTest(unittest.TestCase):
    def test_sorts_usage_top_desc_and_caps(self):
        snapshot = normalize_activity_snapshot({
            "foreground": {"package": "a.b", "label": "A"},
            "usage_top": [
                {"package": "p1", "label": "P1", "minutes": 5},
                {"package": "p2", "label": "P2", "minutes": 42},
                {"package": "bad"},  # minutes 非法，丢弃
            ] + [{"package": f"p{i}", "label": "x", "minutes": 1} for i in range(12)],
            "window_minutes": 60,
        })
        self.assertEqual("a.b", snapshot["foreground"]["package"])
        minutes = [item["minutes"] for item in snapshot["usage_top"]]
        self.assertEqual(sorted(minutes, reverse=True), minutes)
        self.assertLessEqual(len(snapshot["usage_top"]), 10)
        self.assertEqual(60, snapshot["window_minutes"])

    def test_window_minutes_clamped(self):
        self.assertEqual(
            1, normalize_activity_snapshot({"window_minutes": 0})["window_minutes"],
        )
        self.assertEqual(
            1440, normalize_activity_snapshot({"window_minutes": 99999})["window_minutes"],
        )

    def test_rejects_non_dict(self):
        self.assertIsNone(normalize_activity_snapshot(None))
        self.assertIsNone(normalize_activity_snapshot([]))


class DeviceActivityStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "device_activity.json"

    def make_store(self):
        return DeviceActivityStore(self.path)

    def test_app_event_debounce_per_package_and_event_type(self):
        store = self.make_store()
        open_event = {"event": "open", "package": "a.b", "label": "A"}
        close_event = {"event": "close", "package": "a.b", "label": "A"}
        self.assertTrue(store.record_app_event(open_event, now=1000.0))
        # open/close 分别防抖：60 秒内「开了又关」两条都能看到。
        self.assertTrue(store.record_app_event(close_event, now=1030.0))
        # 同类型 60 秒内重复：不冒泡。
        self.assertFalse(store.record_app_event(open_event, now=1030.0))
        self.assertFalse(store.record_app_event(close_event, now=1089.0))
        # 过 60 秒：再次冒泡。
        self.assertTrue(store.record_app_event(open_event, now=1061.0))
        self.assertTrue(store.record_app_event(close_event, now=1091.0))
        # 不同包名互不影响。
        other = {"event": "open", "package": "c.d", "label": "C"}
        self.assertTrue(store.record_app_event(other, now=1062.0))

    def test_snapshot_keeps_only_latest_and_survives_reload(self):
        store = self.make_store()
        store.record_snapshot({"foreground": {"package": "a.b", "label": "A"}, "usage_top": []})
        state = store.record_snapshot({
            "foreground": {"package": "c.d", "label": "C"},
            "usage_top": [{"package": "c.d", "label": "C", "minutes": 12}],
            "window_minutes": 60,
        })
        self.assertEqual("c.d", state["snapshot"]["foreground"]["package"])
        reloaded = self.make_store().snapshot()
        self.assertEqual("c.d", reloaded["snapshot"]["foreground"]["package"])
        self.assertEqual(state["updated_at"], reloaded["updated_at"])

    def test_invalid_inputs_raise(self):
        store = self.make_store()
        with self.assertRaises(ValueError):
            store.record_app_event({"event": "peek", "package": "a.b"})
        with self.assertRaises(ValueError):
            store.record_snapshot(None)


class _FakeChat:
    def __init__(self):
        self.appended = []

    def append(self, **kwargs):
        self.appended.append(kwargs)


class _FakeNotifier:
    def __init__(self):
        self.notices = []

    def enqueue(self, text):
        self.notices.append(text)
        return True


def _device_handler(battery_store, activity_store, *, threshold=25, secret="s3cret"):
    handler = object.__new__(PushHandler)
    handler.state = types.SimpleNamespace(
        device_battery=battery_store,
        device_activity=activity_store,
        battery_low_threshold_percent=threshold,
        shared_secret=secret,
        xiaoke_low_key_notifier=_FakeNotifier(),
    )
    handler.responses = []
    handler.chat = _FakeChat()
    handler._send_json = lambda status, payload: handler.responses.append((status, payload))
    handler._chat_for_contact = lambda contact_id: handler.chat
    return handler


class DeviceHandlerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.battery_store = DeviceBatteryStore(base / "device_battery.json")
        self.activity_store = DeviceActivityStore(base / "device_activity.json")
        self.handler = _device_handler(self.battery_store, self.activity_store)

    def bubbles(self):
        return [item["text"] for item in self.handler.chat.appended]

    def notices(self):
        return self.handler.state.xiaoke_low_key_notifier.notices


class DeviceBatteryReportHandlerTest(DeviceHandlerTestBase):
    def report(self, body):
        self.handler._handle_device_battery_report(body)
        return self.handler.responses[-1]

    def test_bad_body_is_400(self):
        status, payload = self.report({"battery_pct": "low"})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertEqual([], self.bubbles())

    def test_charge_unplug_bubbles(self):
        self.report({"battery_pct": 60, "charging": False})
        status, payload = self.report({"battery_pct": 61, "charging": True})
        self.assertEqual(200, status)
        self.assertEqual(["charging_started"], payload["events"])
        self.assertIn("🔋方小南在充电，电量为61%", self.bubbles())
        _status, payload = self.report({"battery_pct": 70, "charging": False})
        self.assertEqual(["charging_stopped"], payload["events"])
        self.assertIn("🔌方小南拔掉了充电器，电量为70%", self.bubbles())
        # 气泡走米家/小石榴同款胶囊：mihome_event + severity + 非空 items。
        rec = self.handler.chat.appended[0]
        self.assertEqual("assistant", rec["role"])
        self.assertEqual("device-event", rec["source"])
        meta = rec["metadata"]
        self.assertTrue(meta["mihome_event"])
        self.assertEqual("info", meta["severity"])
        self.assertEqual([{
            "device": "手机",
            "change": "charging_started",
            "human": "🔋方小南在充电，电量为61%",
        }], meta["items"])
        self.assertEqual("charging_started", meta["device_event"]["kind"])
        # 她要这些事件进小克会话：不再带 system_event/no_model_context。
        self.assertNotIn("system_event", meta)
        self.assertNotIn("no_model_context", meta)
        # 每条胶囊同步一条 cctg 低调通知（电量主语 + 低调后缀）。
        notices = self.notices()
        self.assertEqual(2, len(notices))
        self.assertTrue(notices[0].startswith("【🔋 手机电量·自动触发】"))
        self.assertIn("🔋方小南在充电，电量为61%", notices[0])
        self.assertTrue(notices[0].endswith(SILENT_SUFFIX))
        self.assertIn("🔌方小南拔掉了充电器，电量为70%", notices[1])

    def test_first_report_is_silent_no_notice(self):
        self.report({"battery_pct": 60, "charging": False})
        self.assertEqual([], self.bubbles())
        self.assertEqual([], self.notices())

    def test_low_battery_bubble_once(self):
        status, payload = self.report({"battery_pct": 24, "charging": False})
        self.assertEqual(200, status)
        self.assertEqual(["low_battery"], payload["events"])
        self.assertEqual(["🪫方小南手机电量低于 25%，建议充电"], self.bubbles())
        meta = self.handler.chat.appended[0]["metadata"]
        self.assertEqual("warn", meta["severity"])
        self.assertEqual("low_battery", meta["items"][0]["change"])
        self.assertEqual(1, len(self.notices()))
        self.assertIn("🪫方小南手机电量低于 25%，建议充电", self.notices()[0])
        _status, payload = self.report({"battery_pct": 23, "charging": False})
        self.assertEqual([], payload["events"])
        self.assertEqual(1, len(self.bubbles()))
        self.assertEqual(1, len(self.notices()))

    def test_threshold_comes_from_state_config(self):
        handler = _device_handler(self.battery_store, self.activity_store, threshold=30)
        handler._handle_device_battery_report({"battery_pct": 28, "charging": False})
        _status, payload = handler.responses[-1]
        self.assertEqual(30, payload["threshold_percent"])
        self.assertIn("🪫方小南手机电量低于 30%，建议充电", [i["text"] for i in handler.chat.appended])

    def test_piggybacked_activity_is_recorded(self):
        self.report({
            "battery_pct": 80, "charging": True,
            "activity": {
                "foreground": {"package": "a.b", "label": "A"},
                "usage_top": [{"package": "a.b", "label": "A", "minutes": 30}],
                "window_minutes": 60,
            },
        })
        snapshot = self.activity_store.snapshot()["snapshot"]
        self.assertEqual("a.b", snapshot["foreground"]["package"])


class DeviceAppEventHandlerTest(DeviceHandlerTestBase):
    def report(self, body):
        self.handler._handle_device_app_event(body)
        return self.handler.responses[-1]

    def test_open_close_bubbles(self):
        status, payload = self.report({
            "event": "open", "package": "com.papegames.lysk.cn", "label": "恋与深空",
        })
        self.assertEqual(200, status)
        self.assertTrue(payload["bubbled"])
        self.assertIn("🎮方小南打开了《恋与深空》", self.bubbles())
        # open/close 分别防抖：紧跟着的关闭也能冒泡（不挂没有下文的「打开了」）。
        _status, payload = self.report({
            "event": "close", "package": "com.papegames.lysk.cn", "label": "恋与深空",
        })
        self.assertTrue(payload["bubbled"])
        self.assertIn("👋方小南关闭了《恋与深空》", self.bubbles())
        metas = [item["metadata"] for item in self.handler.chat.appended]
        self.assertEqual(["app_open", "app_close"], [m["items"][0]["change"] for m in metas])
        self.assertTrue(all(m["mihome_event"] and m["severity"] == "info" for m in metas))
        notices = self.notices()
        self.assertEqual(2, len(notices))
        self.assertTrue(all(n.startswith("【📱 手机应用·自动触发】") for n in notices))
        self.assertIn("🎮方小南打开了《恋与深空》", notices[0])
        self.assertIn("👋方小南关闭了《恋与深空》", notices[1])

    def test_debounced_second_event_not_bubbled(self):
        body = {"event": "open", "package": "a.b", "label": "A"}
        self.report(body)
        _status, payload = self.report(body)
        self.assertFalse(payload["bubbled"])
        self.assertEqual(1, len(self.bubbles()))
        self.assertEqual(1, len(self.notices()))

    def test_bad_body_is_400(self):
        status, payload = self.report({"event": "peek", "package": "a.b"})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])

    def test_same_event_after_debounce_window_bubbles(self):
        body = {"event": "open", "package": "a.b", "label": "A"}
        self.report(body)
        # 直接拨防抖表模拟 61 秒后，同类型再次冒泡。
        self.activity_store._last_bubble_at["a.b|open"] -= 61.0
        _status, payload = self.report(body)
        self.assertTrue(payload["bubbled"])
        self.assertEqual(
            ["🎮方小南打开了《A》", "🎮方小南打开了《A》"], self.bubbles(),
        )


class DeviceNoticeTextTest(unittest.TestCase):
    def test_single_line_with_tag_time_and_suffix(self):
        now = datetime(2026, 9, 28, 1, 5, tzinfo=TZ_BEIJING)
        text = build_device_notice("battery", "🔋方小南在充电，\n电量为76%", now=now)
        self.assertEqual(
            "【🔋 手机电量·自动触发】01:05 🔋方小南在充电， 电量为76%" + SILENT_SUFFIX,
            text,
        )
        self.assertNotIn("\n", text)
        self.assertTrue(build_device_notice("app", "x", now=now).startswith("【📱 手机应用·自动触发】"))


class XiaokeLowKeyNotifierTest(unittest.TestCase):
    def make(self, busy_seq, *, inject_ok=True, max_wait=10.0):
        self.injected = []
        self.slept = []
        self.clock_now = 0.0
        busy_iter = iter(busy_seq)
        self.lock = threading.RLock()

        def is_busy():
            # 断言在锁内判定忙闲（与 App 回合注入同一把锁）。
            self.assertTrue(self.lock._is_owned())
            return next(busy_iter, False)

        def inject(text):
            self.assertTrue(self.lock._is_owned())
            self.injected.append(text)
            return inject_ok

        def sleep(seconds):
            self.slept.append(seconds)
            self.clock_now += seconds

        return XiaokeLowKeyNotifier(
            is_busy=is_busy, inject=inject, lock=self.lock,
            poll_seconds=2.0, max_wait_seconds=max_wait,
            sleep=sleep, clock=lambda: self.clock_now, start_worker=False,
        )

    def test_idle_injects_immediately(self):
        n = self.make([False])
        self.assertTrue(n.enqueue("a"))
        self.assertTrue(n.drain_once())
        self.assertEqual(["a"], self.injected)
        self.assertEqual([], self.slept)

    def test_waits_while_busy_then_injects_fifo(self):
        n = self.make([True, True, False, False])
        n.enqueue("first")
        n.enqueue("second")
        n.drain_once()
        n.drain_once()
        self.assertEqual(["first", "second"], self.injected)
        self.assertEqual([2.0, 2.0], self.slept)
        self.assertFalse(n.drain_once())

    def test_busy_timeout_still_injects(self):
        n = self.make([True] * 100, max_wait=5.0)
        n.enqueue("late")
        self.assertTrue(n.drain_once())
        self.assertEqual(["late"], self.injected)
        self.assertEqual([2.0, 2.0, 2.0], self.slept)

    def test_inject_failure_reported_not_raised(self):
        n = self.make([False], inject_ok=False)
        n.enqueue("x")
        self.assertFalse(n.drain_once())

    def test_blank_text_rejected(self):
        n = self.make([])
        self.assertFalse(n.enqueue("  \n "))
        self.assertEqual(0, n.pending())

    def test_worker_thread_delivers(self):
        done = threading.Event()
        got = []

        def inject(text):
            got.append(text)
            done.set()
            return True

        n = XiaokeLowKeyNotifier(is_busy=lambda: False, inject=inject, lock=threading.RLock())
        n.enqueue("bg")
        self.assertTrue(done.wait(5))
        self.assertEqual(["bg"], got)


class XiaokeBusyProbeTest(unittest.TestCase):
    def state(self, **kw):
        base = dict(xiaoke_stopping_claim={}, xiaoke_send_reservation={},
                    typing_state={"is_typing": False, "since": None})
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_idle(self):
        self.assertFalse(push._xiaoke_busy_for_low_key_notice(self.state()))

    def test_busy_signals(self):
        self.assertTrue(push._xiaoke_busy_for_low_key_notice(
            self.state(typing_state={"is_typing": True, "since": "x"})))
        self.assertTrue(push._xiaoke_busy_for_low_key_notice(
            self.state(xiaoke_send_reservation={"turn_token": "t"})))
        self.assertTrue(push._xiaoke_busy_for_low_key_notice(
            self.state(xiaoke_stopping_claim={"turn_token": "t"})))

    def test_inject_targets_default_session(self):
        calls = []
        orig = push._direct_tmux_injection
        push._direct_tmux_injection = lambda session, text, **kw: (
            calls.append((session, text)) or push.TmuxInjectionResult(True)
        )
        try:
            ok = push._inject_xiaoke_low_key_notice(
                types.SimpleNamespace(default_session="cctg"), "hello")
            ok_fallback = push._inject_xiaoke_low_key_notice(
                types.SimpleNamespace(default_session=""), "hi")
        finally:
            push._direct_tmux_injection = orig
        self.assertTrue(ok and ok_fallback)
        self.assertEqual([("cctg", "hello"), ("cctg", "hi")], calls)


class DeviceActivityAndStatusHandlerTest(DeviceHandlerTestBase):
    def test_activity_report_and_status_shape(self):
        self.handler._handle_device_activity_report({
            "foreground": {"package": "a.b", "label": "A"},
            "usage_top": [
                {"package": "p1", "label": "P1", "minutes": 3},
                {"package": "p2", "label": "P2", "minutes": 45},
            ],
            "window_minutes": 60,
        })
        status, payload = self.handler.responses[-1]
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

        self.handler._handle_device_battery_report({"battery_pct": 66, "charging": True})
        self.handler._handle_device_status_get()
        status, payload = self.handler.responses[-1]
        self.assertEqual(200, status)
        self.assertEqual(66, payload["battery"]["battery_pct"])
        self.assertEqual(True, payload["battery"]["charging"])
        self.assertTrue(payload["battery"]["updated_at"] > 0)
        self.assertEqual("a.b", payload["activity"]["foreground"]["package"])
        # usage_top 按分钟数降序。
        self.assertEqual(
            ["p2", "p1"], [item["package"] for item in payload["activity"]["usage_top"]],
        )
        self.assertEqual(60, payload["activity"]["window_minutes"])
        self.assertEqual(25, payload["battery_low_threshold_percent"])

    def test_status_without_any_report_is_empty_not_dead(self):
        self.handler._handle_device_status_get()
        status, payload = self.handler.responses[-1]
        self.assertEqual(200, status)
        self.assertIsNone(payload["battery"]["battery_pct"])
        self.assertIsNone(payload["activity"]["foreground"])
        self.assertEqual([], payload["activity"]["usage_top"])

    def test_bad_activity_body_is_400(self):
        self.handler._handle_device_activity_report([1, 2, 3])
        status, payload = self.handler.responses[-1]
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])


class ChatSendDeviceScrubTest(unittest.TestCase):
    """升级窗口期隐私回退：旧 App（v1.9.209）的 metadata.device 在 _handle_chat_send
    入口随保留字清洗被丢弃，绝不落聊天历史（2026-09-27 审核返工）。"""

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

    def test_legacy_device_metadata_is_scrubbed_before_dispatch(self):
        self.send({
            "text": "hi",
            "contact_id": "xiaoke",
            "metadata": {"device": {"battery_pct": 23, "charging": False}, "via": "card"},
        })
        self.assertEqual(1, len(self.handler.captured))
        _contact, body = self.handler.captured[0]
        self.assertNotIn("device", body.get("metadata") or {})
        self.assertEqual("card", (body.get("metadata") or {}).get("via"))

    def test_device_only_metadata_leaves_no_metadata_key(self):
        self.send({
            "text": "hi",
            "metadata": {"device": {"battery_pct": 80, "charging": True}},
        })
        _contact, body = self.handler.captured[0]
        self.assertNotIn("device", (body.get("metadata") or {}))


class NativePairingGateTest(unittest.TestCase):
    """/device/* 走与 /kimi/ 相同的 fail-closed native pairing 闸门。"""

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


if __name__ == "__main__":
    unittest.main()
