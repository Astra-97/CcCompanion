"""螃蟹桌宠 Clawd 形态 (2026-10-23, App v1.9.226) 测试.

覆盖:
  1. crab_pet_state 存储: 动作名/中文名归一; 上报校验; random 不重复当前且不含 snooze;
     seq 单调; applied_seq 确认指令; 落盘 0600 + 重启读回
  2. GET /pet/crab/state、/pet/crab/acts 与 POST /pet/crab/state、/pet/crab/act:
     缺/错 token 401 (strict_auth=false 也不放行); 带 token 200; 坏 body 400/413
  3. POST /pet/crab/act → chat_stream_bus 发 pet_act 事件; 上报响应补发未确认指令
  4. SSE: pet_act 只投 contacts=all 订阅, 投影只留 event/act/seq/ts
  5. POST /pet/tap 带 act → metadata act/act_zh + 文案「当时螃蟹在「xx」」; 认不出忽略

全部 mock, 不碰线上 8291 服务。
"""
from __future__ import annotations

import io
import json
import os
import random
import stat
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

import crab_pet_state
from chat_history import ChatStreamBus
from push import PushHandler
from _pet_tap_test import SECRET, PetTapBase
from _nfc_tap_test import _Headers
from _background_chat_stream_test import _CaptureWriter


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "crab_pet_state.json"
        self.now = 1_000_000.0
        self.store = crab_pet_state.CrabPetStore(self.path, rng=random.Random(3), clock=lambda: self.now)

    def test_act_table(self) -> None:
        self.assertEqual(len(crab_pet_state.ACTS), 34)
        self.assertEqual(len(set(crab_pet_state.ACT_ZH.values())), 34)
        self.assertNotIn("snooze", crab_pet_state.RANDOM_POOL)
        self.assertEqual(len(crab_pet_state.RANDOM_POOL), 33)

    def test_normalize(self) -> None:
        n = crab_pet_state.normalize_act
        self.assertEqual(n("coffee"), "coffee")
        self.assertEqual(n(" Coffee "), "coffee")
        self.assertEqual(n("喝咖啡"), "coffee")
        self.assertEqual(n("小克螃蟹·送你玫瑰"), "rose")
        self.assertEqual(n("berry picking"), "berry_picking")
        self.assertEqual(n("Clawd-BerryPicking"), None)  # 文件名不是动作名
        self.assertEqual(n("clawd-coffee"), "coffee")
        for bad in (None, 3, "", "nope", "random"):
            self.assertIsNone(n(bad))

    def test_report_and_snapshot(self) -> None:
        self.assertEqual(self.store.snapshot(), {"state": None, "command": None})
        st = self.store.report({
            "act": "coffee", "display_act": "snooze", "asleep": True, "showing": True,
            "reason": "x" * 100, "ts": 123, "applied_seq": 0,
        })
        self.assertEqual(st["act"], "coffee")
        self.assertEqual(len(st["reason"]), 32)
        self.now += 5
        snap = self.store.snapshot()["state"]
        self.assertEqual(snap["act_zh"], "喝咖啡")
        self.assertEqual(snap["display_act_zh"], "睡着了")
        self.assertEqual(snap["age_s"], 5)
        self.assertTrue(snap["asleep"])
        self.assertTrue(snap["enabled"])
        with self.assertRaises(ValueError):
            self.store.report({"act": "nope"})
        with self.assertRaises(ValueError):
            self.store.report({"act": "coffee", "pending_act": "nope"})

    def test_random_command_never_current_never_snooze_and_seq_monotonic(self) -> None:
        self.store.report({"act": "coffee"})
        last = 0
        for _ in range(200):
            cmd = self.store.command("random")
            self.assertNotIn(cmd["act"], ("coffee", "snooze"))
            self.assertEqual(cmd["requested"], "random")
            self.assertGreater(cmd["seq"], last)
            last = cmd["seq"]
        self.assertEqual(self.store.command("小克螃蟹·睡着了")["act"], "snooze")
        with self.assertRaises(ValueError):
            self.store.command("nope")

    def test_applied_seq_acks_command(self) -> None:
        cmd = self.store.command("rose")
        self.assertEqual(self.store.pending_command(), {"act": "rose", "seq": cmd["seq"], "acked": False})
        self.store.report({"act": "coffee", "applied_seq": cmd["seq"] - 1})
        self.assertIsNotNone(self.store.pending_command())
        self.store.report({"act": "coffee", "pending_act": "rose", "applied_seq": cmd["seq"]})
        self.assertIsNone(self.store.pending_command())
        snap = self.store.snapshot()
        self.assertTrue(snap["command"]["acked"])
        self.assertEqual(snap["state"]["pending_act_zh"], "送你玫瑰")

    def test_persisted_0600_and_reloaded(self) -> None:
        self.store.report({"act": "heart", "showing": True})
        cmd = self.store.command("phone")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        again = crab_pet_state.CrabPetStore(self.path)
        snap = again.snapshot()
        self.assertEqual(snap["state"]["act"], "heart")
        self.assertEqual(snap["command"]["seq"], cmd["seq"])
        # 坏文件不崩
        self.path.write_text("{oops", encoding="utf-8")
        self.assertEqual(crab_pet_state.CrabPetStore(self.path).snapshot(), {"state": None, "command": None})


class DiagTest(StoreTest):
    """App v1.9.227 诊断字段 (方小南 10-24「桌宠消失了」): 只收标量, 不认识的类型丢掉。"""

    def test_diag_sanitized(self) -> None:
        st = self.store.report({
            "act": "heart", "showing": True,
            "diag": {"view": True, "attached": True, "shown": False, "x": 12, "alpha": 0.123456,
                     "nested": {"a": 1}, "list": [1], "s": "x" * 100, 5: "bad-key"},
        })
        self.assertEqual(st["diag"], {"view": True, "attached": True, "shown": False, "x": 12,
                                      "alpha": 0.123, "s": "x" * 64})

    def test_no_diag_keeps_old_shape(self) -> None:
        st = self.store.report({"act": "heart"})
        self.assertNotIn("diag", st)
        st = self.store.report({"act": "heart", "diag": "nope"})
        self.assertNotIn("diag", st)


class CrabPetRouteTest(PetTapBase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = crab_pet_state.CrabPetStore(Path(tmp.name) / "s.json", rng=random.Random(1))
        self.bus = ChatStreamBus()

    def _wire(self, h) -> PushHandler:
        h.state.crab_pet = self.store
        h.state.chat_stream_bus = self.bus
        return h

    def post(self, path, body, *, token=SECRET, strict=True) -> PushHandler:
        # PetTapBase.pet builds + runs do_POST; inject our state via a temporary class patch.
        store, bus = self.store, self.bus
        orig = PushHandler.do_POST

        def wrapped(h):
            h.state.crab_pet = store
            h.state.chat_stream_bus = bus
            return orig(h)

        PushHandler.do_POST = wrapped
        try:
            return self.pet(token=token, body=body, path=path, strict=strict)
        finally:
            PushHandler.do_POST = orig

    def get(self, path, *, token=SECRET, strict=True) -> PushHandler:
        h = object.__new__(PushHandler)
        h.responses = []
        h.wfile = io.BytesIO()
        h.rfile = io.BytesIO(b"")
        h.state = types.SimpleNamespace(
            shared_secret=SECRET, strict_auth=strict, allowed_ips=["10.0.0.1"],
            crab_pet=self.store, chat_stream_bus=self.bus,
        )
        headers = {}
        if token is not None:
            headers["X-Auth-Token"] = token
        h.headers = _Headers(headers)
        h.path = path
        h._check_ip_allowed = lambda: True
        h._is_public_get = lambda: False
        h._send_json = lambda status, payload, **_kw: h.responses.append((status, payload))
        h.do_GET()
        return h

    def test_auth_required_everywhere(self) -> None:
        for token in (None, "bad"):
            for strict in (True, False):
                for path in ("/pet/crab/state", "/pet/crab/acts"):
                    self.assertEqual(self.get(path, token=token, strict=strict).responses,
                                     [(401, {"ok": False, "error": "unauthorized"})])
                for path, body in (("/pet/crab/state", {"act": "coffee"}), ("/pet/crab/act", {"act": "rose"})):
                    self.assertEqual(self.post(path, body, token=token, strict=strict).responses,
                                     [(401, {"ok": False, "error": "unauthorized"})])
        self.assertIsNone(self.store.snapshot()["state"])
        self.assertIsNone(self.store.snapshot()["command"])

    def test_get_empty_then_state(self) -> None:
        self.assertEqual(self.get("/pet/crab/state").responses, [(200, {"ok": True, "state": None, "command": None})])
        h = self.post("/pet/crab/state", {"act": "coffee", "display_act": "coffee", "showing": True, "reason": "show"})
        self.assertEqual(h.responses, [(200, {"ok": True, "command": None})])
        status, payload = self.get("/pet/crab/state").responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"]["act"], "coffee")
        self.assertEqual(payload["state"]["act_zh"], "喝咖啡")
        self.assertEqual(payload["state"]["reason"], "show")
        acts = self.get("/pet/crab/acts").responses[0][1]["acts"]
        self.assertEqual(len(acts), 34)
        self.assertIn({"act": "rose", "zh": "送你玫瑰"}, acts)

    def test_bad_bodies(self) -> None:
        self.assertEqual(self.post("/pet/crab/state", {"act": "nope"}).responses[0][0], 400)
        self.assertEqual(self.post("/pet/crab/state", b"").responses[0][0], 400)
        self.assertEqual(self.post("/pet/crab/state", b"[1]").responses[0][0], 400)
        self.assertEqual(self.post("/pet/crab/state", b"{oops").responses[0][0], 400)
        self.assertEqual(self.post("/pet/crab/state", b"{" + b" " * 5000 + b"}").responses[0][0], 413)
        bad = self.post("/pet/crab/act", {"act": "nope"}).responses[0]
        self.assertEqual(bad[0], 400)
        self.assertIn("random", bad[1]["acts"])
        self.assertEqual(self.channel_calls, [])

    def test_act_publishes_pet_act_and_report_returns_pending(self) -> None:
        q = self.bus.subscribe()
        h = self.post("/pet/crab/act", {"act": "送你玫瑰"})
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual((payload["act"], payload["act_zh"]), ("rose", "送你玫瑰"))
        self.assertEqual(payload["stream_listeners"], 1)
        self.assertEqual(len(q), 1)
        ev = q[0]
        self.assertEqual((ev["event"], ev["act"], ev["seq"]), ("pet_act", "rose", payload["seq"]))
        # App 离线: 下次上报响应里补发; 确认后不再发
        r1 = self.post("/pet/crab/state", {"act": "coffee", "applied_seq": 0}).responses[0][1]
        self.assertEqual(r1["command"], {"act": "rose", "seq": payload["seq"], "acked": False})
        r2 = self.post("/pet/crab/state", {"act": "rose", "applied_seq": payload["seq"]}).responses[0][1]
        self.assertIsNone(r2["command"])
        # 不碰小克会话
        self.assertEqual(self.channel_calls, [])
        self.assertEqual(self.bubbles, [])

    def test_random_act_differs_from_reported(self) -> None:
        self.post("/pet/crab/state", {"act": "coffee"})
        for _ in range(30):
            act = self.post("/pet/crab/act", {"act": "random"}).responses[0][1]["act"]
            self.assertNotIn(act, ("coffee", "snooze"))

    def test_pet_tap_carries_act(self) -> None:
        h = self.pet(body={"count": 1, "act": "coffee"})
        self.assertEqual(h.responses, [(200, {"ok": True, "deduped": False})])
        meta = self.channel_calls[-1]["metadata"]
        self.assertEqual((meta["act"], meta["act_zh"]), ("coffee", "喝咖啡"))
        self.assertIn("（当时螃蟹在「喝咖啡」）", self.channel_calls[-1]["text"])
        self.pet(body={"count": 1, "act": "snooze", "asleep": True})
        self.assertIn("（螃蟹本来在睡觉）（当时螃蟹在「睡着了」）", self.channel_calls[-1]["text"])
        self.pet(body={"count": 1, "act": "nope"})
        self.assertNotIn("act", self.channel_calls[-1]["metadata"])
        self.assertNotIn("当时螃蟹在", self.channel_calls[-1]["text"])


class PetActStreamTest(unittest.TestCase):
    CONTACTS = {"xiaoke": object(), "kairos": object()}

    def handler(self, path: str) -> PushHandler:
        h = object.__new__(PushHandler)
        h.path = path
        h.command = "GET"
        h.headers = {"X-Auth-Token": "test-secret"}
        h.client_address = ("127.0.0.1", 1)
        h.state = types.SimpleNamespace(
            shared_secret="test-secret", strict_auth=True, web_session_enabled=False,
            contact_chats=dict(self.CONTACTS), chat_stream_bus=ChatStreamBus(),
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

    def test_projection_keeps_only_public_keys(self) -> None:
        out = PushHandler._public_chat_stream_event({
            "event": "pet_act", "act": "rose", "seq": 5, "ts": "now",
            "contact_id": "xiaoke", "secret": "x",
        })
        self.assertEqual(out, {"event": "pet_act", "act": "rose", "seq": 5, "ts": "now"})

    def test_wildcard_gets_pet_act(self) -> None:
        wire = self.run_stream(
            "/chat/stream?contacts=all&heartbeat=background", b'"act": "rose"',
            [{"event": "pet_act", "act": "rose", "seq": 5, "ts": "now"}],
        )
        self.assertIn('"event": "pet_act"', wire)

    def test_single_contact_stream_never_gets_pet_act(self) -> None:
        wire = self.run_stream(
            "/chat/stream?contact_id=xiaoke", b'"text": "mine"',
            [
                {"event": "pet_act", "act": "rose", "seq": 5, "ts": "now"},
                {"event": "chunk", "stream_id": "s", "contact_id": "xiaoke", "text": "mine", "ts": "now"},
            ],
        )
        self.assertNotIn("pet_act", wire)


if __name__ == "__main__":
    unittest.main()
