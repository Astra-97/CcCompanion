"""「小克的本地能力」服务端测试 (2026-10-31, App v1.9.233).

覆盖:
  1. validate_task 本地 action: alarm / timer / health / calendar_sync / ring / clip 参数校验
  2. PhoneControlHub 本地任务: 入队 → 唤醒钩子 → /claim 按 id 领 → result → followup;
     老轮询 (无 local=1) 不领本地任务; calendar_sync / widget_refresh 只留最新一条;
     手机上报「关着」后提前 403; calendar_sync 不带 events 时从 loader 取; 超时文案
  3. phone_local: NotifBuffer (验证码过滤 / 去重 / 0600 / TTL / 每 App 注入限频),
     PlaceEvents 去抖, load_schedule_events, build_widget_payload, 注入文案
  4. SSE: phone_task 只投 contacts=all, 投影只留 event/task_id/action/ts
  5. 真 HTTP: /phone/tasks/claim, /followup, /phone/notifs (POST+GET), /phone/places/event,
     /phone/widget, phone-ctl alarm / notifs / local 端到端 (假手机)

全部本地, 不碰线上 8291 服务。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import phone_control as pc
import phone_local as pl
from chat_history import ChatStreamBus
from phone_control import PhoneControlHub, PhoneTaskError
from push import PushHandler
from _background_chat_stream_test import _CaptureWriter
from _phone_control_test import SECRET, FakeClock, http

HERE = Path(__file__).resolve().parent
BJ = pl.TZ_BEIJING


class ValidateLocalTest(unittest.TestCase):
    def test_alarm(self) -> None:
        out = pc.validate_task("alarm", {"hour": 7, "minutes": "30", "message": " 起床 ", "days": "workdays"})
        self.assertEqual(out, {"feature": "alarm", "hour": 7, "minutes": 30, "message": "起床",
                               "days": [1, 2, 3, 4, 5], "skip_ui": True})
        self.assertEqual(pc.validate_task("alarm", {"hour": 0, "minutes": 0, "days": [7, 1, 1]})["days"], [1, 7])
        for bad in ({"hour": 24, "minutes": 0}, {"hour": 1}, {"hour": 1, "minutes": 0, "days": [0]},
                    {"hour": 1, "minutes": 0, "days": ["x"]}, {"hour": 1, "minutes": 0, "message": "x" * 201},
                    {"hour": 1, "minutes": 0, "days": [True]}):
            with self.assertRaises(PhoneTaskError, msg=bad):
                pc.validate_task("alarm", bad)

    def test_timer_health_ring_clip(self) -> None:
        self.assertEqual(pc.validate_task("timer", {"seconds": 300})["seconds"], 300)
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("timer", {"seconds": 0})
        self.assertEqual(pc.validate_task("health", {"date": "2026-10-30"}),
                         {"feature": "health", "date": "2026-10-30", "days": 1})
        for bad in ({"date": "2026-13-01"}, {"date": "yesterday"}, {"date": "2026-10-30", "days": 32}):
            with self.assertRaises(PhoneTaskError):
                pc.validate_task("health", bad)
        ring = pc.validate_task("ring", {})
        self.assertEqual((ring["seconds"], ring["volume_pct"]), (60, 80))
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("ring", {"seconds": 601})
        self.assertEqual(pc.validate_task("clip", {"text": "取件码 1234"})["notify"], True)
        for bad in ({}, {"text": ""}, {"text": "a\x00b"}, {"text": "x" * (pc.MAX_CLIP_CHARS + 1)}):
            with self.assertRaises(PhoneTaskError):
                pc.validate_task("clip", bad)
        for action in ("show_alarms", "ring_stop", "widget_refresh", "places"):
            self.assertEqual(pc.validate_task(action, {}), {"feature": pc.LOCAL_ACTION_FEATURE[action]})

    def test_calendar_events_normalized(self) -> None:
        out = pc.validate_task("calendar_sync", {"window_start": "2026-10-17", "events": [
            {"id": "a1", "title": " 私教课 ", "date": "2026-10-31", "time": "19:00",
             "remind_minutes_before": 60, "note": "n", "done": False, "extra": "dropped"},
            {"id": "bad id!", "title": "x", "date": "2026-10-31"},
            {"id": "b2", "title": "交报告", "date": "2026-11-01", "time": "25:00", "remind_minutes_before": True},
            "junk",
        ]})
        self.assertEqual(out["events"][0], {"id": "a1", "title": "私教课", "date": "2026-10-31", "time": "19:00",
                                            "remind_minutes_before": 60, "note": "n", "done": False})
        self.assertEqual(out["events"][1]["time"], None)
        self.assertEqual(out["events"][1]["remind_minutes_before"], None)
        self.assertEqual(len(out["events"]), 2)
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("calendar_sync", {"events": []})  # 缺 window_start


class LocalHubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.woken: list[dict] = []
        self.hub = PhoneControlHub(
            Path(self.tmp.name) / "pc", clock=self.clock,
            calendar_loader=lambda: {"events": [{"id": "e1", "title": "t", "date": "2026-10-31"}],
                                     "window_start": "2026-10-17"},
            on_local_enqueue=lambda task: self.woken.append(task) or 3,
        )

    def test_claim_by_id_result_followup(self) -> None:
        t = self.hub.enqueue("alarm", {"hour": 7, "minutes": 30})
        self.assertEqual(t["stream_listeners"], 3)
        self.assertEqual([w["id"] for w in self.woken], [t["id"]])
        self.assertEqual(t["tier"], "local")
        # 老 App 的轮询 (不带 local=1) 不领本地任务
        self.assertIsNone(self.hub.claim())
        task = self.hub.claim_local(task_id=t["id"], app_version="1.9.233",
                                    features={"alarm": {"enabled": True, "missing": ["悬浮窗"]}, "bogus": {}})
        self.assertEqual(task["action"], "alarm")
        self.assertEqual(task["args"]["hour"], 7)
        self.assertIsNone(self.hub.claim_local(task_id=t["id"]))  # 已被领
        local = self.hub.device()["local"]
        self.assertTrue(local["seen"] and local["fresh"])
        self.assertEqual(local["features"], {"alarm": {"enabled": True, "missing": ["悬浮窗"]}})
        self.hub.submit_result(t["id"], {"ok": True, "result": {"state": "awaiting_tap"}})
        self.hub.add_followup(t["id"], {"event": "tapped", "ok": True, "state": "set_verified",
                                         "result": {"next_alarm_at": 1}, "junk": object.__name__})
        view = self.hub.get(t["id"])
        self.assertEqual(view["status"], "done")
        self.assertEqual(view["claimed_via"], "stream")
        self.assertEqual(view["followup"][0]["state"], "set_verified")
        self.assertNotIn("junk", view["followup"][0])
        for _ in range(pc.MAX_FOLLOWUPS - 1):
            self.hub.add_followup(t["id"], {"event": "x"})
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.add_followup(t["id"], {"event": "x"})
        self.assertEqual(cm.exception.status, 429)
        # 闹钟类结果留 6 小时 (等「她点了」的后续), 普通任务 15 分钟
        self.clock.t += pc.RESULT_TTL_S + 10
        self.assertEqual(self.hub.get(t["id"])["status"], "done")
        self.clock.t += pc.LOCAL_RESULT_TTL_S
        with self.assertRaises(PhoneTaskError):
            self.hub.get(t["id"])

    def test_followup_rules(self) -> None:
        normal = self.hub.enqueue("status", {})
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.add_followup(normal["id"], {"event": "x"})
        self.assertEqual(cm.exception.code, "followup_not_expected")
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.add_followup("0" * 32, {"event": "x"})
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(PhoneTaskError):
            self.hub.claim_local(task_id="../etc")

    def test_poll_with_local_flag_and_oldest_first(self) -> None:
        a = self.hub.enqueue("clip", {"text": "a"})
        self.clock.t += 1
        self.hub.enqueue("status", {})
        got = self.hub.claim(include_local=True)
        self.assertEqual(got["id"], a["id"])
        self.assertEqual(self.hub.get(a["id"])["claimed_via"], "poll")
        self.assertEqual(self.hub.claim()["action"], "status")
        self.assertIsNone(self.hub.claim_local())

    def test_coalesce_calendar_and_widget(self) -> None:
        first = self.hub.enqueue("calendar_sync", None)
        self.assertEqual(first["args"]["events"][0]["id"], "e1")
        second = self.hub.enqueue("calendar_sync", {})
        w1 = self.hub.enqueue("widget_refresh", {})
        w2 = self.hub.enqueue("widget_refresh", {})
        self.assertEqual(self.hub.get(first["id"])["status"], "cancelled")
        self.assertEqual(self.hub.get(first["id"])["error"], "superseded")
        self.assertEqual(self.hub.get(w1["id"])["status"], "cancelled")
        self.assertEqual(self.hub.get(second["id"])["status"], "pending")
        self.assertEqual(self.hub.get(w2["id"])["status"], "pending")
        # 不合并的 action 照常排队
        self.hub.enqueue("ring", {})
        self.hub.enqueue("ring", {})
        self.assertEqual(sum(1 for t in self.hub.list_tasks() if t["action"] == "ring"
                             and t["status"] == "pending"), 2)

    def test_calendar_without_loader(self) -> None:
        hub = PhoneControlHub(Path(self.tmp.name) / "pc2")
        with self.assertRaises(PhoneTaskError) as cm:
            hub.enqueue("calendar_sync", {})
        self.assertEqual(cm.exception.status, 503)

    def test_feature_disabled_precheck(self) -> None:
        self.hub.claim_local(features={"clip": {"enabled": False}, "ring": {"enabled": True}})
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.enqueue("clip", {"text": "x"})
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "feature_disabled"))
        self.assertIn("剪贴板", cm.exception.message)
        self.hub.enqueue("ring", {})
        # 一周没来上报 → 状态不新鲜, 不再提前拒 (排队等它)
        self.clock.t += pc.LOCAL_SEEN_FRESH_S + 1
        self.hub.enqueue("clip", {"text": "x"})
        # Shizuku 档位上报不影响本地能力
        self.hub.note_device(caps="status")
        self.hub.enqueue("health", {"date": "2026-10-30"})

    def test_not_claimed_message_mentions_realtime(self) -> None:
        t = self.hub.enqueue("ring", {})
        self.clock.t += pc.LOCAL_CLAIM_TIMEOUT_S + 1
        view = self.hub.get(t["id"])
        self.assertEqual(view["status"], "timeout")
        self.assertIn("实时提醒", view["message"])
        cal = self.hub.enqueue("calendar_sync", {})
        self.clock.t += pc.LOCAL_CLAIM_TIMEOUT_S + 1
        self.assertEqual(self.hub.get(cal["id"])["status"], "pending")  # 日历同步可等 30 分钟

    def test_audit_hides_private_text(self) -> None:
        t = self.hub.enqueue("clip", {"text": "我的地址是某某路 1 号"})
        self.hub.cancel(t["id"])
        r = self.hub.enqueue("ring", {"message": "快接电话"})
        self.hub.cancel(r["id"])
        audit = (Path(self.tmp.name) / "pc" / "phone_control_audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("某某路", audit)
        self.assertNotIn("快接电话", audit)

    def test_wake_hook_failure_does_not_fail_enqueue(self) -> None:
        def boom(_task):
            raise RuntimeError("bus down")
        hub = PhoneControlHub(Path(self.tmp.name) / "pc3", on_local_enqueue=boom)
        self.assertEqual(hub.enqueue("clip", {"text": "x"})["status"], "pending")


class NotifBufferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "notif.json"
        self.clock = FakeClock()

    def item(self, title: str, text: str = "", pkg: str = "me.ele") -> dict:
        return {"pkg": pkg, "app": "饿了么", "title": title, "text": text, "posted_at": 1_761_900_000_000}

    def test_filter_dedupe_persist_ttl(self) -> None:
        buf = pl.NotifBuffer(self.path, clock=self.clock)
        kept, notify = buf.add([
            self.item("骑手已取餐", "预计 12:30 送达"),
            self.item("骑手已取餐", "预计 12:30 送达"),           # 重复
            self.item("您的验证码是 123456"),                     # 验证码
            self.item("登录提醒", "动态密码 8888"),
            self.item("Your code is 1234"),
            {"pkg": "rm -rf", "title": "x"},                     # 包名不合法
            {"pkg": "me.ele", "title": "", "text": ""},          # 空
            "junk",
        ])
        self.assertEqual([k["title"] for k in kept], ["骑手已取餐"])
        self.assertEqual(len(notify), 1)
        self.assertAlmostEqual(kept[0]["posted_at"], 1_761_900_000.0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # 同一 App 3 分钟内不再注入, 但照样进缓冲
        self.clock.t += 10
        kept, notify = buf.add([self.item("骑手快到了")])
        self.assertEqual((len(kept), len(notify)), (1, 0))
        kept, notify = buf.add([self.item("已发货", pkg="com.cainiao.wireless")])
        self.assertEqual(len(notify), 1)
        self.clock.t += pl.NOTIF_NOTICE_GAP_S
        self.assertEqual(len(buf.add([self.item("已送达")])[1]), 1)
        # 重启后还在; since 过滤
        buf2 = pl.NotifBuffer(self.path, clock=self.clock)
        self.assertEqual(len(buf2.since()), 4)
        self.assertEqual([i["title"] for i in buf2.since(60)], ["已送达"])
        # 48 小时过期
        self.clock.t += pl.NOTIF_TTL_S + 1
        self.assertEqual(buf2.since(), [])
        self.assertEqual(pl.NotifBuffer(self.path, clock=self.clock).since(), [])
        self.assertEqual(buf2.clear(), 0)

    def test_ring_buffer_cap_and_batch_cap(self) -> None:
        buf = pl.NotifBuffer(self.path, clock=self.clock)
        for n in range(15):
            buf.add([self.item(f"单 {n}-{k}") for k in range(30)])
        items = buf.since()
        self.assertEqual(len(items), pl.NOTIF_MAX_ITEMS)
        self.assertEqual(items[-1]["title"], f"单 14-{pl.NOTIF_BATCH_MAX - 1}")

    def test_notice_text_masks_codes(self) -> None:
        text = pl.notif_notice_text({"app": "菜鸟", "pkg": "com.cainiao.wireless",
                                     "title": "包裹到驿站", "text": "取件 8-2-30117"})
        self.assertEqual(text, "外卖/快递通知：菜鸟: 包裹到驿站｜取件 8-2-****")
        self.assertTrue(pl.looks_secret("PIN 1234"))
        self.assertFalse(pl.looks_secret("骑手已取餐"))


class PlaceEventsTest(unittest.TestCase):
    def test_debounce_and_validation(self) -> None:
        clock = FakeClock()
        ev = pl.PlaceEvents(clock=clock)
        self.assertEqual(ev.normalize({"place": " 健身 房 ", "transition": "enter"}), ("健身 房", "enter"))
        for bad in ({"place": "家"}, {"place": "", "transition": "enter"}, {"place": "x" * 21, "transition": "exit"},
                    {"place": 1, "transition": "enter"}, [], {"place": "家", "transition": "dwell"}):
            self.assertIsNone(ev.normalize(bad), bad)
        self.assertTrue(ev.accept("家", "enter"))
        self.assertFalse(ev.accept("家", "enter"))
        self.assertTrue(ev.accept("家", "exit"))
        clock.t += pl.PLACE_DEBOUNCE_S
        self.assertTrue(ev.accept("家", "enter"))
        self.assertEqual(len(ev.recent), 3)
        self.assertEqual(pl.place_notice_text("公司", "enter"), "方小南到了「公司」附近")
        self.assertEqual(pl.place_notice_text("公司", "exit"), "方小南离开了「公司」")


class ScheduleTest(unittest.TestCase):
    NOW = datetime(2026, 10, 31, 14, 0, tzinfo=BJ)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.json"
        self.path.write_text(json.dumps([
            {"id": "old", "title": "很久以前", "date": "2026-09-01", "time": "10:00"},
            {"id": "a", "title": "早上的会", "date": "2026-10-31", "time": "09:00"},
            {"id": "b", "title": "私教课", "date": "2026-10-31", "time": "19:00", "remind_minutes_before": 60},
            {"id": "c", "title": "交报告", "date": "2026-10-31"},
            {"id": "d", "title": "已完成", "date": "2026-10-31", "time": "20:00", "done": True},
            {"id": "e", "title": "明早", "date": "2026-11-01", "time": "08:00"},
            {"id": "f", "title": "明晚", "date": "2026-11-01", "time": "21:00"},
            {"id": "g", "title": "后天", "date": "2026-11-02", "time": "08:00"},
            {"id": "bad", "title": "", "date": "2026-11-01"},
        ], ensure_ascii=False), encoding="utf-8")

    def test_load_schedule_events(self) -> None:
        out = pl.load_schedule_events(self.path, now=self.NOW)
        self.assertEqual(out["window_start"], "2026-10-17")
        ids = [e["id"] for e in out["events"]]
        self.assertNotIn("old", ids)
        self.assertNotIn("bad", ids)
        self.assertIn("d", ids)  # 已完成也下发 (手机端标 ✓)
        self.assertEqual(ids[:2], ["c", "a"])  # 全天排在当天最前
        # 下发参数能过 validate_task
        self.assertEqual(len(pc.validate_task("calendar_sync", out)["events"]), len(ids))
        self.assertEqual(pl.load_schedule_events(Path(self.tmp.name) / "missing.json", now=self.NOW)["events"], [])
        (Path(self.tmp.name) / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaises(PhoneTaskError):
            pl.load_schedule_events(Path(self.tmp.name) / "broken.json", now=self.NOW)

    def test_widget_payload(self) -> None:
        records = [
            {"role": "assistant", "text": "[bqb:摸摸头]\n第一行很长" + "字" * 100 + "\n第二行", "ts": "t1"},
            {"role": "user", "text": "我的消息", "ts": "t2"},
            {"role": "assistant", "text": "隐藏", "hidden_in_ui": True, "ts": "t3"},
            {"role": "assistant", "text": "米家", "metadata": {"mihome_event": True}, "ts": "t4"},
        ]
        out = pl.build_widget_payload(self.path, records, now=self.NOW)
        self.assertEqual([s["title"] for s in out["schedule"]], ["交报告", "私教课", "明早"])
        self.assertEqual(out["schedule"][0]["day"], "今天")
        self.assertEqual(out["schedule"][2]["day"], "明天")
        self.assertEqual(out["schedule_more"], 1)
        self.assertEqual(out["message"]["ts"], "t1")
        self.assertTrue(out["message"]["text"].startswith("第一行很长"))
        self.assertTrue(out["message"]["text"].endswith("…"))
        self.assertEqual(len(out["message"]["text"]), 80)
        self.assertIsNone(pl.build_widget_payload(self.path, [], now=self.NOW)["message"])


class PhoneTaskStreamTest(unittest.TestCase):
    def handler(self, path: str) -> PushHandler:
        h = object.__new__(PushHandler)
        h.path = path
        h.command = "GET"
        h.headers = {"X-Auth-Token": "test-secret"}
        h.client_address = ("127.0.0.1", 1)
        h.state = types.SimpleNamespace(
            shared_secret="test-secret", strict_auth=True, web_session_enabled=False,
            contact_chats={"xiaoke": object(), "kairos": object()}, chat_stream_bus=ChatStreamBus(),
        )
        h.responses = []
        h._send_json = lambda status, payload, **_kw: h.responses.append((status, payload))
        h.send_response = lambda status: None
        h.send_header = lambda name, value: None
        h.end_headers = lambda: None
        return h

    def run_stream(self, path: str, stop: bytes, events: list[dict]) -> str:
        h = self.handler(path)
        writer = _CaptureWriter(stop_when=stop)
        h.wfile = writer
        t = threading.Thread(target=h._handle_chat_stream, daemon=True)
        t.start()
        deadline = time.monotonic() + 1.0
        while h.state.chat_stream_bus.subscriber_count() != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        for ev in events:
            h.state.chat_stream_bus.publish(ev)
        self.assertTrue(writer.seen.wait(1.0))
        t.join(1.0)
        return writer.snapshot().decode("utf-8")

    EVENT = {"event": "phone_task", "task_id": "a" * 32, "action": "alarm", "ts": "now",
             "args": {"text": "secret"}, "contact_id": "xiaoke"}

    def test_projection(self) -> None:
        self.assertEqual(PushHandler._public_chat_stream_event(self.EVENT),
                         {"event": "phone_task", "task_id": "a" * 32, "action": "alarm", "ts": "now"})

    def test_wildcard_only(self) -> None:
        wire = self.run_stream("/chat/stream?contacts=all&heartbeat=background", b'"action": "alarm"', [self.EVENT])
        self.assertIn('"event": "phone_task"', wire)
        self.assertNotIn("secret", wire)
        wire = self.run_stream("/chat/stream?contact_id=xiaoke", b'"text": "mine"', [
            self.EVENT,
            {"event": "chunk", "stream_id": "s", "contact_id": "xiaoke", "text": "mine", "ts": "now"},
        ])
        self.assertNotIn("phone_task", wire)

    def test_wake_hook_publishes(self) -> None:
        import push
        bus = ChatStreamBus()
        q = bus.subscribe()
        fake = types.SimpleNamespace(chat_stream_bus=bus)
        n = push.ServerState._wake_phone_local_task(fake, {"id": "b" * 32, "action": "ring", "args": {"x": 1}})
        self.assertEqual(n, 1)
        rec = q.popleft()
        self.assertEqual((rec["event"], rec["task_id"], rec["action"]), ("phone_task", "b" * 32, "ring"))
        self.assertNotIn("args", rec)
        self.assertEqual(push.ServerState._wake_phone_local_task(types.SimpleNamespace(), {"id": "x", "action": "y"}), 0)


# ── 真 HTTP ─────────────────────────────────────────────────────────────────


class _Notifier:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def enqueue(self, text: str) -> bool:
        self.texts.append(text)
        return True


def _start_server(tmpdir: str):
    import push

    state = types.SimpleNamespace(
        shared_secret=SECRET, strict_auth=True, allowed_ips=[],
        token_store_path=str(Path(tmpdir) / "tokens" / "active.json"),
        schedule_events_path=str(Path(tmpdir) / "events.json"),
        contact_chats={"xiaoke": types.SimpleNamespace(tail=lambda n: [
            {"role": "assistant", "text": "晚上记得拉伸", "ts": "2026-10-31T10:00:00+00:00"}])},
        xiaoke_low_key_notifier=_Notifier(),
    )
    state.phone_control = PhoneControlHub(
        Path(tmpdir) / "pc",
        calendar_loader=lambda: pl.load_schedule_events(state.schedule_events_path),
    )
    state.phone_notifs = pl.NotifBuffer(Path(tmpdir) / "pc" / "notif_buffer.json")
    state.phone_places = pl.PlaceEvents()

    class Handler(push.PushHandler):
        def log_message(self, *a, **k):  # noqa: D401 - quiet
            pass

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


class FakeLocalApp(threading.Thread):
    """假 App: 用 /phone/tasks/claim (不带 id) 领本地任务, 按 action 回结果 (+ 闹钟的后续)。"""

    FEATURES = {"alarm": {"enabled": True, "missing": []}, "clip": {"enabled": False},
                "health": {"enabled": True, "missing": ["后台读取"]}}

    def __init__(self, base: str) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            st, _, body = http(self.base, "POST", "/phone/tasks/claim", {"v": "1.9.233", "features": self.FEATURES})
            task = json.loads(body).get("task") if st == 200 else None
            if not task:
                time.sleep(0.05)
                continue
            tid = task["id"]
            if task["action"] == "alarm":
                http(self.base, "POST", f"/phone/tasks/{tid}/result", {"ok": True, "result": {
                    "state": "awaiting_tap", "via": "notification", "days": task["args"]["days"],
                    "reason": "App 在后台"}})
                http(self.base, "POST", f"/phone/tasks/{tid}/followup",
                     {"event": "tapped", "ok": True, "state": "set_verified"})
            elif task["action"] == "places":
                http(self.base, "POST", f"/phone/tasks/{tid}/result", {"ok": True, "result": {
                    "places": [{"name": "家", "radius_m": 150}], "background_location": False}})
            else:
                http(self.base, "POST", f"/phone/tasks/{tid}/result",
                     {"ok": False, "error": "permission_missing", "message": "没给权限"})


class LocalHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server, self.state = _start_server(self.tmp.name)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_auth(self) -> None:
        for method, path in (("POST", "/phone/tasks/claim"), ("POST", "/phone/notifs"),
                             ("POST", "/phone/places/event"), ("GET", "/phone/widget"),
                             ("GET", "/phone/notifs"), ("POST", "/phone/tasks/" + "a" * 32 + "/followup")):
            st, _, _ = http(self.base, method, path, {} if method == "POST" else None, token="wrong")
            self.assertEqual(st, 401, path)
        # 读通知缓冲只许本机; 手机侧的推送接口经 cloudflared 可用
        cf = {"CF-Connecting-IP": "203.0.113.9"}
        st, _, _ = http(self.base, "GET", "/phone/notifs", headers=cf)
        self.assertEqual(st, 401)
        st, _, _ = http(self.base, "POST", "/phone/notifs/clear", {}, headers=cf)
        self.assertEqual(st, 401)
        st, _, _ = http(self.base, "POST", "/phone/tasks/claim", {}, headers=cf)
        self.assertEqual(st, 200)

    def test_claim_by_id_and_followup(self) -> None:
        st, _, body = http(self.base, "POST", "/phone/tasks", {"action": "ring", "args": {"message": "找手机"}})
        self.assertEqual(st, 200, body)
        tid = json.loads(body)["task"]["id"]
        st, _, body = http(self.base, "GET", "/phone/tasks/next?wait=0")
        self.assertEqual(st, 204)  # 老轮询看不到
        st, _, body = http(self.base, "POST", "/phone/tasks/claim", {"task_id": tid, "v": "1.9.233"})
        task = json.loads(body)["task"]
        self.assertEqual((task["id"], task["action"], task["args"]["message"]), (tid, "ring", "找手机"))
        st, _, body = http(self.base, "POST", "/phone/tasks/claim", {"task_id": tid})
        self.assertEqual(json.loads(body), {"ok": True, "task": None})
        http(self.base, "POST", f"/phone/tasks/{tid}/result", {"ok": True, "result": {"state": "ringing"}})
        st, _, body = http(self.base, "POST", f"/phone/tasks/{tid}/followup",
                           {"event": "stopped", "state": "user", "ok": True, "result": {"rang_ms": 4000}})
        self.assertEqual(json.loads(body), {"ok": True, "count": 1})
        st, _, body = http(self.base, "GET", f"/phone/tasks/{tid}")
        self.assertEqual(json.loads(body)["task"]["followup"][0]["result"], {"rang_ms": 4000})
        st, _, _ = http(self.base, "POST", "/phone/tasks/claim", {"task_id": "nope"})
        self.assertEqual(st, 400)
        # 本地任务也能走带 local=1 的长轮询
        http(self.base, "POST", "/phone/tasks", {"action": "widget_refresh"})
        st, _, body = http(self.base, "GET", "/phone/tasks/next?wait=0&local=1")
        self.assertEqual(json.loads(body)["task"]["action"], "widget_refresh")

    def test_notifs_roundtrip(self) -> None:
        st, _, body = http(self.base, "POST", "/phone/notifs", {"items": [
            {"pkg": "me.ele", "app": "饿了么", "title": "骑手已取餐", "text": "预计 12:30 送达"},
            {"pkg": "me.ele", "app": "饿了么", "title": "验证码 1234"},
        ]})
        self.assertEqual(json.loads(body), {"ok": True, "kept": 1, "noticed": 1})
        self.assertEqual(len(self.state.xiaoke_low_key_notifier.texts), 1)
        notice = self.state.xiaoke_low_key_notifier.texts[0]
        self.assertTrue(notice.startswith("【📦 外卖快递·自动触发】"), notice)
        self.assertIn("外卖/快递通知：饿了么: 骑手已取餐", notice)
        st, _, body = http(self.base, "GET", "/phone/notifs?since=3600")
        self.assertEqual([i["title"] for i in json.loads(body)["items"]], ["骑手已取餐"])
        st, _, _ = http(self.base, "GET", "/phone/notifs?since=abc")
        self.assertEqual(st, 400)
        st, _, _ = http(self.base, "POST", "/phone/notifs", {"items": "x"})
        self.assertEqual(st, 400)
        r = self._cli("notifs", "--since", "1h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("饿了么: 骑手已取餐", r.stdout)
        r = self._cli("notifs", "--clear")
        self.assertIn("已清空 1 条", r.stdout)

    def test_place_event(self) -> None:
        st, _, body = http(self.base, "POST", "/phone/places/event", {"place": "健身房", "transition": "enter"})
        self.assertEqual(json.loads(body), {"ok": True, "accepted": True})
        st, _, body = http(self.base, "POST", "/phone/places/event", {"place": "健身房", "transition": "enter"})
        self.assertEqual(json.loads(body)["accepted"], False)
        notices = self.state.xiaoke_low_key_notifier.texts
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0].startswith("【📍 到达·自动触发】"))
        self.assertIn("方小南到了「健身房」附近", notices[0])
        st, _, _ = http(self.base, "POST", "/phone/places/event",
                        {"place": "家", "transition": "enter", "lat": 1.0, "test": True})
        self.assertEqual(st, 200)
        self.assertEqual(len(notices), 1)  # test=true 不注入
        st, _, body = http(self.base, "GET", "/phone/places/events")
        events = json.loads(body)["events"]
        self.assertEqual([e["place"] for e in events], ["健身房", "家"])
        self.assertNotIn("lat", json.dumps(events))
        st, _, _ = http(self.base, "POST", "/phone/places/event", {"place": "家", "transition": "nope"})
        self.assertEqual(st, 400)

    def test_widget(self) -> None:
        today = datetime.now(BJ).date().isoformat()
        Path(self.state.schedule_events_path).write_text(json.dumps([
            {"id": "a", "title": "全天的事", "date": today}], ensure_ascii=False), encoding="utf-8")
        st, hdrs, body = http(self.base, "GET", "/phone/widget")
        out = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("Cache-Control"), "no-store")
        self.assertEqual(out["schedule"][0]["title"], "全天的事")
        self.assertEqual(out["message"]["text"], "晚上记得拉伸")
        Path(self.state.schedule_events_path).write_text("{", encoding="utf-8")
        st, _, body = http(self.base, "GET", "/phone/widget")
        out = json.loads(body)
        self.assertEqual((st, out["ok"], out["error"], out["schedule"]), (200, False, "schedule_unreadable", []))

    def test_cli_calendar_no_wait_also_refreshes_widget(self) -> None:
        Path(self.state.schedule_events_path).write_text(json.dumps([
            {"id": "a", "title": "x", "date": datetime.now(BJ).date().isoformat(), "time": None}]), encoding="utf-8")
        r = self._cli("calendar", "sync", "--no-wait")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已排队同步「小克日程」（1 条", r.stdout)
        r = self._cli("calendar", "sync", "--no-wait")
        actions = sorted((t["action"], t["status"]) for t in self.state.phone_control.list_tasks())
        self.assertEqual(actions, [("calendar_sync", "cancelled"), ("calendar_sync", "pending"),
                                   ("widget_refresh", "cancelled"), ("widget_refresh", "pending")])

    def _cli(self, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ, PHONE_CTL_BASE=self.base, PHONE_CTL_SECRET=SECRET)
        return subprocess.run([sys.executable, str(HERE / "scripts" / "phone-ctl"), *argv],
                              env=env, capture_output=True, text=True, timeout=timeout)

    def test_cli_local_end_to_end(self) -> None:
        app = FakeLocalApp(self.base)
        app.start()
        self.addCleanup(app.stop.set)
        r = self._cli("alarm", "7:30", "--msg", "起床", "--days", "1,2,3", "--wait", "10")
        self.assertEqual(r.returncode, 2, r.stderr)  # awaiting_tap
        self.assertIn("闹钟 07:30，重复: 周一、二、三「起床」", r.stdout)
        self.assertIn("已发通知等她点一下确认", r.stdout)
        tid = r.stdout.split("phone-ctl task ")[1].split()[0]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            r = self._cli("task", tid)
            if "后续" in r.stdout:
                break
            time.sleep(0.1)
        self.assertIn("set_verified", r.stdout)
        r = self._cli("places", "--wait", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("家  半径 150m", r.stdout)
        self.assertIn("没允许", r.stdout)
        r = self._cli("health", "yesterday", "--wait", "10")
        self.assertEqual(r.returncode, 1)
        self.assertIn("没给权限", r.stderr)
        r = self._cli("local")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("剪贴板: 关", r.stdout)
        self.assertIn("健康数据: 开，缺: 后台读取", r.stdout)
        r = self._cli("clip", "hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("手机端关着「剪贴板」", r.stderr)
        r = self._cli("alarm", "25:00")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HH:MM", r.stderr)


if __name__ == "__main__":
    unittest.main()
