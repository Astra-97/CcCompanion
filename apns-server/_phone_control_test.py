"""手机操控 /phone/* 测试 (2026-10-24, App v1.9.227 Shizuku).

覆盖:
  1. 纯函数: validate_task 各 action 参数校验 / 手机路径白名单 / Range 解析
  2. PhoneControlHub: 入队→领取→回结果; 未领超时; 领后超时; 取消; 进度/取消回执;
     二进制结果; 档位未开提前拒; 结果 TTL 清理 (含 blob 文件); outbox 挂文件 sha256
  3. 真 HTTP (ThreadingHTTPServer + 真 PushHandler, 假 state):
     - 鉴权: 无 token / 错 token / 只带网页 cookie → 401; strict_auth=false 也不放行
     - 本机限定: 经 cloudflared (CF-Connecting-IP 公网) 投递 → 401, 手机侧接口照常可用
     - 长轮询 204 / 200; 假手机线程领任务回结果; 截图 blob 往返
     - /phone/files 断点续传: Range 206 + Content-Range, 越界 416, 无 Range 200
     - phone-ctl CLI 端到端 (env 指向测试服务): status/tap/push(带续传的假手机)/shot

全部本地, 不碰线上 8291 服务。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import phone_control as pc
from phone_control import PhoneControlHub, PhoneTaskError

HERE = Path(__file__).resolve().parent
SECRET = "phone-test-secret-0123456789abcdef"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


class ValidateTest(unittest.TestCase):
    def test_unknown_action(self) -> None:
        with self.assertRaises(PhoneTaskError) as cm:
            pc.validate_task("rm_rf", {})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "unknown_action")

    def test_open_package(self) -> None:
        self.assertEqual(pc.validate_task("open", {"package": "com.tencent.mm"}), {"package": "com.tencent.mm"})
        for bad in ("com", "com.tencent.mm; reboot", "", None, "a" * 300 + ".b"):
            with self.assertRaises(PhoneTaskError):
                pc.validate_task("open", {"package": bad})

    def test_key_names(self) -> None:
        self.assertEqual(pc.validate_task("key", {"key": "HOME"})["keycode"], 3)
        self.assertEqual(pc.validate_task("key", {"key": "recent"})["keycode"], 187)
        self.assertEqual(pc.validate_task("key", {"key": "volume-up"})["keycode"], 24)
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("key", {"key": "factory_reset"})

    def test_tap_swipe_bounds(self) -> None:
        self.assertEqual(pc.validate_task("tap", {"x": 10, "y": "20"}), {"x": 10, "y": 20})
        for bad in ({"x": -1, "y": 0}, {"x": 1}, {"x": True, "y": 1}, {"x": 1.5, "y": 1}, {"x": 99999, "y": 1}):
            with self.assertRaises(PhoneTaskError):
                pc.validate_task("tap", bad)
        sw = pc.validate_task("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        self.assertEqual(sw["duration_ms"], 300)

    def test_text(self) -> None:
        out = pc.validate_task("text", {"text": "你好 hi"})
        self.assertEqual(out, {"text": "你好 hi", "mode": "auto", "enter": False})
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("text", {"text": ""})
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("text", {"text": "x" * 2001})
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("text", {"text": "x", "mode": "root"})

    def test_phone_paths(self) -> None:
        self.assertEqual(pc.normalize_phone_path("/sdcard/Music/a.mp3"), "/sdcard/Music/a.mp3")
        self.assertEqual(pc.normalize_phone_path("/storage/emulated/0//Download/"), "/storage/emulated/0/Download")
        self.assertEqual(pc.normalize_phone_path("/sdcard"), "/sdcard")
        for bad in ("sdcard/a", "/data/data/x", "/sdcard/../data/x", "/sdcardX/a", "/sdcard/a\nb", "/", 5):
            with self.assertRaises(PhoneTaskError, msg=repr(bad)):
                pc.normalize_phone_path(bad)
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("pull", {"path": "/sdcard"})

    def test_download_and_shell_args(self) -> None:
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("download", {"file_id": "nothex", "dest": "/sdcard/a"})
        out = pc.validate_task("download", {"file_id": "a" * 32, "dest": "/sdcard/Music/x.mp3"})
        self.assertFalse(out["overwrite"])
        sh = pc.validate_task("shell", {"cmd": "ls /sdcard"})
        self.assertEqual(sh["timeout_s"], 30)
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("shell", {"cmd": "  "})

    def test_ui_and_clicktext_args(self) -> None:
        self.assertEqual(pc.validate_task("ui", {}), {"max_nodes": 300, "all": False, "windows": False})
        self.assertEqual(pc.validate_task("ui", {"max_nodes": 50, "all": True, "windows": True}),
                         {"max_nodes": 50, "all": True, "windows": True})
        for bad in ({"max_nodes": 0}, {"max_nodes": 5000}, {"max_nodes": "x"}):
            with self.assertRaises(PhoneTaskError):
                pc.validate_task("ui", bad)
        self.assertEqual(pc.validate_task("clicktext", {"text": "  发送 "}),
                         {"text": "发送", "index": 0, "exact": False})
        self.assertEqual(pc.validate_task("clicktext", {"text": "OK", "index": 2, "exact": True}),
                         {"text": "OK", "index": 2, "exact": True})
        for bad in ({}, {"text": ""}, {"text": "   "}, {"text": "a\nb"}, {"text": "x" * 201},
                    {"text": "a", "index": -1}, {"text": "a", "index": 100}):
            with self.assertRaises(PhoneTaskError):
                pc.validate_task("clicktext", bad)
        self.assertEqual(pc.ACTIONS["ui"][0], "screen")
        self.assertEqual(pc.ACTIONS["clicktext"][0], "input")

    def test_active_backend(self) -> None:
        self.assertEqual(pc.active_backend("ready", "on"), "shizuku")
        self.assertEqual(pc.active_backend("binding", None), "shizuku")
        self.assertEqual(pc.active_backend("no_permission", "on"), "accessibility")
        self.assertEqual(pc.active_backend(None, "on"), "accessibility")
        self.assertEqual(pc.active_backend("not_running", "enabled"), "none")
        self.assertEqual(pc.active_backend(None, None), "none")
        self.assertTrue(pc.SHIZUKU_ONLY_ACTIONS.isdisjoint(pc.A11Y_ONLY_ACTIONS))
        self.assertEqual(pc.SHIZUKU_ONLY_ACTIONS, {"download", "ls", "stat", "pull", "shell"})

    def test_parse_range(self) -> None:
        self.assertIsNone(pc.parse_range(None, 100))
        self.assertIsNone(pc.parse_range("items=0-1", 100))
        self.assertIsNone(pc.parse_range("bytes=0-1,5-6", 100))
        self.assertEqual(pc.parse_range("bytes=10-", 100), (10, 99))
        self.assertEqual(pc.parse_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(pc.parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(pc.parse_range("bytes=-30", 100), (70, 99))
        for bad in ("bytes=100-", "bytes=50-40", "bytes=-0", "bytes=abc", "bytes=-"):
            with self.assertRaises(PhoneTaskError, msg=bad) as cm:
                pc.parse_range(bad, 100)
            self.assertEqual(cm.exception.status, 416)


class HubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.hub = PhoneControlHub(Path(self.tmp.name) / "pc", clock=self.clock)

    def test_startup_clears_stale_dirs(self) -> None:
        stale = self.hub.blob_dir / "old.bin"
        stale.write_bytes(b"x")
        PhoneControlHub(Path(self.tmp.name) / "pc", clock=self.clock)
        self.assertFalse(stale.exists())

    def test_enqueue_claim_result(self) -> None:
        t = self.hub.enqueue("tap", {"x": 1, "y": 2})
        self.assertEqual(t["status"], "pending")
        self.assertFalse(t["device"]["seen"])
        claimed = self.hub.claim(wait_s=0, caps="status,input", shizuku="ready", app_version="1.9.227")
        self.assertEqual(claimed["id"], t["id"])
        self.assertEqual(claimed["args"], {"x": 1, "y": 2})
        self.assertIsNone(self.hub.claim(wait_s=0))
        self.assertEqual(self.hub.get(t["id"])["status"], "claimed")
        self.hub.submit_result(t["id"], {"ok": True, "result": {"done": 1}})
        view = self.hub.get(t["id"])
        self.assertEqual(view["status"], "done")
        self.assertEqual(view["result"], {"done": 1})
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.submit_result(t["id"], {"ok": True})
        self.assertEqual(cm.exception.status, 409)
        audit = (Path(self.tmp.name) / "pc" / "phone_control_audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "tap"', audit)

    def test_fifo_order(self) -> None:
        a = self.hub.enqueue("status", {})
        self.clock.t += 1
        b = self.hub.enqueue("foreground", {})
        self.assertEqual(self.hub.claim()["id"], a["id"])
        self.assertEqual(self.hub.claim()["id"], b["id"])

    def test_failed_result_keeps_message(self) -> None:
        t = self.hub.enqueue("open", {"package": "com.x.y"})
        self.hub.claim()
        self.hub.submit_result(t["id"], {"ok": False, "error": "shizuku_not_running", "message": "Shizuku 没在运行"})
        v = self.hub.get(t["id"])
        self.assertEqual((v["status"], v["error"], v["message"]), ("failed", "shizuku_not_running", "Shizuku 没在运行"))

    def test_claim_timeout_when_phone_offline(self) -> None:
        t = self.hub.enqueue("status", {})
        self.clock.t += pc.CLAIM_TIMEOUT_S + 1
        v = self.hub.get(t["id"])
        self.assertEqual((v["status"], v["error"]), ("timeout", "phone_not_claimed"))
        self.assertIn("手机没来领任务", v["message"])

    def test_claimed_timeout(self) -> None:
        t = self.hub.enqueue("tap", {"x": 1, "y": 1})
        self.hub.claim()
        self.clock.t += 16
        v = self.hub.get(t["id"])
        self.assertEqual((v["status"], v["error"]), ("timeout", "phone_no_result"))

    def test_custom_timeout_bounds(self) -> None:
        t = self.hub.enqueue("download", {"file_id": self._stage(b"abc")["file_id"], "dest": "/sdcard/a"},
                             timeout_s=7200)
        self.assertEqual(t["timeout_s"], 7200)
        with self.assertRaises(PhoneTaskError):
            self.hub.enqueue("tap", {"x": 1, "y": 1}, timeout_s=9999)

    def test_shell_timeout_covers_command(self) -> None:
        t = self.hub.enqueue("shell", {"cmd": "sleep 100", "timeout_s": 120})
        self.assertGreaterEqual(t["timeout_s"], 135)

    def test_cancel_and_progress(self) -> None:
        t = self.hub.enqueue("download", {"file_id": self._stage(b"x" * 10)["file_id"], "dest": "/sdcard/x"})
        self.hub.claim()
        self.assertEqual(self.hub.progress(t["id"], 5, 10), {"ok": True, "cancelled": False})
        self.assertEqual(self.hub.get(t["id"])["progress"]["done"], 5)
        self.assertEqual(self.hub.cancel(t["id"])["status"], "cancelled")
        self.assertTrue(self.hub.progress(t["id"], 6, 10)["cancelled"])
        with self.assertRaises(PhoneTaskError):
            self.hub.submit_result(t["id"], {"ok": True})

    def test_tier_disabled_rejected_when_phone_online(self) -> None:
        self.hub.note_device(caps="status")
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.enqueue("screenshot", {})
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "tier_disabled"))
        self.assertIn("截屏", cm.exception.message)
        self.hub.enqueue("status", {})
        # 很久没来 → 不再按旧档位拒, 交给手机判
        self.clock.t += pc.DEVICE_ONLINE_S + 5
        self.hub.enqueue("screenshot", {})

    def test_backend_reported_in_device_view(self) -> None:
        d = self.hub.device()
        self.assertEqual((d["backend"], d["accessibility"]), ("none", None))
        self.hub.note_device(caps="status,input", shizuku="no_permission", accessibility="on")
        d = self.hub.device()
        self.assertEqual((d["backend"], d["accessibility"]), ("accessibility", "on"))
        self.assertIn("无障碍", d["backend_reason"])
        self.hub.note_device(caps="status,input", shizuku="ready", accessibility="on")
        self.assertEqual(self.hub.device()["backend"], "shizuku")
        self.hub.note_device(caps="", shizuku="not_running", accessibility="on")
        d = self.hub.device()
        self.assertEqual(d["backend"], "none")
        self.assertIn("总开关", d["backend_reason"])
        self.hub.note_device(caps="status", shizuku="not_running", accessibility="BAD STATE!")
        self.assertEqual(self.hub.device()["accessibility"], "on")  # 脏值不覆盖
        self.clock.t += pc.DEVICE_ONLINE_S + 5
        self.assertEqual(self.hub.device()["backend"], "none")

    def test_a11y_state_from_local_channel_when_poll_off(self) -> None:
        self.hub.claim_local(app_version="1.9.234", accessibility="enabled")
        d = self.hub.device()
        self.assertIsNone(d["accessibility"])
        self.assertEqual(d["accessibility_any"], "enabled")
        self.assertEqual(d["backend"], "none")

    def test_shizuku_only_and_a11y_only_prereject(self) -> None:
        self.hub.note_device(caps="status,screen,input,files,shell", shizuku="no_permission", accessibility="on")
        for action, args in (("shell", {"cmd": "id"}), ("ls", {"path": "/sdcard"}),
                             ("stat", {"path": "/sdcard/a"}), ("pull", {"path": "/sdcard/a"})):
            with self.assertRaises(PhoneTaskError) as cm:
                self.hub.enqueue(action, args)
            self.assertEqual((cm.exception.status, cm.exception.code), (409, "needs_shizuku"))
            self.assertIn("需要 Shizuku", cm.exception.message)
        # 无障碍能做的照常排队
        for action, args in (("tap", {"x": 1, "y": 2}), ("screenshot", {}), ("ui", {}),
                             ("clicktext", {"text": "发送"}), ("text", {"text": "你好"}), ("open", {"package": "com.a.b"})):
            self.assertEqual(self.hub.enqueue(action, args)["status"], "pending")
        # Shizuku 就绪但无障碍没开: ui / clicktext 提前拒
        self.hub.note_device(caps="status,screen,input", shizuku="ready", accessibility="off")
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.enqueue("ui", {})
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "needs_accessibility"))
        # Shizuku 就绪时 shell 照常 (档位开着)
        self.hub.note_device(caps="status,shell", shizuku="ready", accessibility="off")
        self.hub.enqueue("shell", {"cmd": "id"})
        # 老 App 不报 a11y → 不提前拒, 交给手机判
        hub2 = PhoneControlHub(Path(self.tmp.name) / "pc2", clock=self.clock)
        hub2.note_device(caps="status,screen,input", shizuku="not_running")
        hub2.enqueue("ui", {})
        # binding 可能马上就绪 → 不拒
        hub2.note_device(caps="status,files", shizuku="binding")
        hub2.enqueue("ls", {"path": "/sdcard"})

    def test_clicktext_text_masked_in_audit(self) -> None:
        t = self.hub.enqueue("clicktext", {"text": "我的银行卡"})
        self.hub.cancel(t["id"])
        audit = (Path(self.tmp.name) / "pc" / "phone_control_audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("银行卡", audit)
        self.assertIn("<5 字>", audit)

    def test_blob_roundtrip_and_ttl(self) -> None:
        t = self.hub.enqueue("screenshot", {})
        self.hub.claim()
        with self.assertRaises(PhoneTaskError):
            self.hub.blob(t["id"])
        data = b"\xff\xd8jpegdata"
        info = self.hub.put_blob(t["id"], io.BytesIO(data), len(data), "image/jpeg")
        self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())
        self.hub.submit_result(t["id"], {"ok": True, "result": {"width": 1}})
        blob = self.hub.blob(t["id"])
        self.assertEqual(Path(blob["path"]).read_bytes(), data)
        self.assertEqual(blob["content_type"], "image/jpeg")
        self.assertEqual(self.hub.get(t["id"])["blob"]["size"], len(data))
        self.clock.t += pc.RESULT_TTL_S + 1
        self.hub.sweep()
        self.assertFalse(Path(blob["path"]).exists())
        with self.assertRaises(PhoneTaskError):
            self.hub.get(t["id"])

    def test_blob_short_body_and_wrong_action(self) -> None:
        t = self.hub.enqueue("screenshot", {})
        self.hub.claim()
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.put_blob(t["id"], io.BytesIO(b"abc"), 10, "image/jpeg")
        self.assertEqual(cm.exception.code, "short_body")
        self.assertEqual(list(self.hub.blob_dir.iterdir()), [])
        t2 = self.hub.enqueue("tap", {"x": 1, "y": 1})
        self.hub.claim()
        with self.assertRaises(PhoneTaskError):
            self.hub.put_blob(t2["id"], io.BytesIO(b"abc"), 3, "image/jpeg")

    def test_ok_screenshot_without_blob_fails(self) -> None:
        t = self.hub.enqueue("screenshot", {})
        self.hub.claim()
        self.hub.submit_result(t["id"], {"ok": True})
        self.assertEqual(self.hub.get(t["id"])["error"], "missing_blob")

    def _stage(self, data: bytes) -> dict:
        src = Path(self.tmp.name) / f"src-{len(data)}-{time.monotonic_ns()}.bin"
        src.write_bytes(data)
        return self.hub.stage_file(src)

    def test_stage_and_download_binding(self) -> None:
        data = os.urandom(3000)
        staged = self._stage(data)
        self.assertEqual(staged["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(staged["size"], 3000)
        t = self.hub.enqueue("download", {"file_id": staged["file_id"], "dest": "/sdcard/Music/a.mp3"})
        self.assertEqual(t["args"]["url"], f"/phone/files/{staged['file_id']}")
        self.assertEqual(t["args"]["sha256"], staged["sha256"])
        entry = self.hub.outbox_file(staged["file_id"])
        self.assertEqual(Path(entry["path"]).read_bytes(), data)
        self.hub.claim()
        self.hub.submit_result(t["id"], {"ok": True, "result": {}})
        # 下载结束后文件只多留 10 分钟
        self.clock.t += pc.OUTBOX_AFTER_DONE_S + 1
        with self.assertRaises(PhoneTaskError):
            self.hub.outbox_file(staged["file_id"])
        self.assertFalse(Path(entry["path"]).exists())

    def test_stage_missing_and_unknown_file(self) -> None:
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.stage_file("/nonexistent/zzz.mp3")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(PhoneTaskError):
            self.hub.enqueue("download", {"file_id": "b" * 32, "dest": "/sdcard/a"})

    def test_long_poll_wakes_on_enqueue(self) -> None:
        hub = PhoneControlHub(Path(self.tmp.name) / "pc2")
        got: list = []
        th = threading.Thread(target=lambda: got.append(hub.claim(wait_s=5)))
        th.start()
        time.sleep(0.2)
        self.assertTrue(hub.device()["polling_now"])
        started = time.monotonic()
        hub.enqueue("status", {})
        th.join(3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(got[0]["action"], "status")

    def test_get_wait_wakes_on_result(self) -> None:
        hub = PhoneControlHub(Path(self.tmp.name) / "pc3")
        t = hub.enqueue("status", {})
        hub.claim()
        threading.Timer(0.2, lambda: hub.submit_result(t["id"], {"ok": True, "result": {}})).start()
        started = time.monotonic()
        self.assertEqual(hub.get(t["id"], wait_s=5)["status"], "done")
        self.assertLess(time.monotonic() - started, 2)


# ── 真 HTTP ─────────────────────────────────────────────────────────────────


def _start_server(tmpdir: str, *, strict: bool = True):
    import push

    state = types.SimpleNamespace(
        shared_secret=SECRET,
        strict_auth=strict,
        allowed_ips=[],
        token_store_path=str(Path(tmpdir) / "tokens" / "active.json"),
    )
    state.phone_control = PhoneControlHub(Path(tmpdir) / "pc")

    class Handler(push.PushHandler):
        def log_message(self, *a, **k):  # noqa: D401 - quiet
            pass

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    return server, state


def http(base: str, method: str, path: str, body=None, *, token: str | None = SECRET,
         headers: dict | None = None, raw: bytes | None = None, timeout: float = 15):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    if token is not None:
        req.add_header("X-Auth-Token", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class FakePhone(threading.Thread):
    """假手机: 长轮询领任务, 按 action 回结果; download 走 Range 续传 (第一次故意只拿一半)。"""

    def __init__(self, base: str, caps: str = "status,screen,input,files", *,
                 shizuku: str = "ready", a11y: str | None = None) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.caps = caps
        self.shizuku = shizuku
        self.a11y = a11y
        self.stop = threading.Event()
        self.files: dict[str, bytes] = {}
        self.range_headers: list[str] = []

    def run(self) -> None:
        while not self.stop.is_set():
            extra = f"&a11y={self.a11y}" if self.a11y else ""
            st, _, body = http(self.base, "GET",
                               f"/phone/tasks/next?wait=1&caps={self.caps}&shizuku={self.shizuku}{extra}&v=1.9.227")
            if st != 200:
                continue
            task = json.loads(body)["task"]
            try:
                self.handle(task)
            except Exception as exc:  # pragma: no cover - debug aid
                http(self.base, "POST", f"/phone/tasks/{task['id']}/result",
                     {"ok": False, "error": "fake_phone", "message": repr(exc)})

    def result(self, task, payload) -> None:
        http(self.base, "POST", f"/phone/tasks/{task['id']}/result", payload)

    def handle(self, task) -> None:
        a = task["action"]
        if a == "status":
            self.result(task, {"ok": True, "result": {"battery": 88, "charging": False, "screen_on": True,
                                                      "foreground": "com.android.launcher"}})
        elif a == "tap":
            self.result(task, {"ok": True, "result": {"x": task["args"]["x"]}})
        elif a == "ui":
            self.result(task, {"ok": True, "result": {
                "package": "com.tencent.mm", "screen_w": 1080, "screen_h": 2400, "total": 57,
                "truncated": False,
                "nodes": [
                    {"i": 0, "d": 0, "cls": "FrameLayout", "b": [0, 0, 1080, 2400]},
                    {"i": 3, "d": 4, "cls": "EditText", "text": "", "id": "input", "b": [40, 2200, 860, 2300],
                     "click": True, "edit": True, "focus": True},
                    {"i": 4, "d": 4, "cls": "Button", "text": "发送", "id": "send", "b": [880, 2200, 1040, 2300],
                     "click": True},
                    {"i": 5, "d": 4, "cls": "EditText", "text": "••••", "b": [0, 0, 10, 10], "pw": True},
                ]}})
        elif a == "clicktext":
            if task["args"]["text"] == "不存在":
                self.result(task, {"ok": False, "error": "not_found",
                                   "message": "屏幕上没找到「不存在」（phone-ctl ui 看看有什么）"})
            else:
                self.result(task, {"ok": True, "result": {
                    "via": "click_action", "index": task["args"]["index"], "matches": 2,
                    "target": {"text": task["args"]["text"]}, "x": 960, "y": 2250}})
        elif a == "screenshot":
            data = b"\xff\xd8\xff" + b"J" * 500
            st, _, _ = http(self.base, "POST", f"/phone/tasks/{task['id']}/blob", raw=data,
                            headers={"Content-Type": "image/jpeg"})
            assert st == 200
            self.result(task, {"ok": True, "result": {"width": 640, "height": 1408, "orig_width": 1440,
                                                      "orig_height": 3168, "scale": 640 / 1440}})
        elif a == "download":
            args = task["args"]
            size = args["size"]
            got = b""
            # 第一次只拿前半段 (模拟断线), 第二次 Range 续传
            st, _, part = http(self.base, "GET", args["url"], headers={"Range": f"bytes=0-{size // 2 - 1}"})
            assert st == 206, st
            got += part
            http(self.base, "POST", f"/phone/tasks/{task['id']}/progress", {"done": len(got), "total": size})
            rng = f"bytes={len(got)}-"
            self.range_headers.append(rng)
            st, hdrs, rest = http(self.base, "GET", args["url"], headers={"Range": rng})
            assert st == 206, st
            got += rest
            ok = len(got) == size and hashlib.sha256(got).hexdigest() == args["sha256"]
            self.files[args["dest"]] = got
            self.result(task, {"ok": ok, "result": {"path": args["dest"], "size": len(got), "resumed": 1,
                                                    "attempts": 1},
                               "error": None if ok else "sha_mismatch"})
        else:
            self.result(task, {"ok": False, "error": "unsupported", "message": a})


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server, self.state = _start_server(self.tmp.name)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_auth_fail_closed(self) -> None:
        for path in ("/phone/device", "/phone/tasks/next", "/phone/tasks", "/phone/files/" + "a" * 32):
            for token in (None, "wrong"):
                st, _, body = http(self.base, "GET", path, token=token)
                self.assertEqual(st, 401, (path, token))
        st, _, _ = http(self.base, "POST", "/phone/tasks", {"action": "status"}, token=None,
                        headers={"Cookie": "ccc_web_session=abc"})
        self.assertEqual(st, 401)
        self.assertEqual(self.state.phone_control.list_tasks(), [])

    def test_strict_auth_false_still_fail_closed(self) -> None:
        self.state.strict_auth = False
        st, _, _ = http(self.base, "POST", "/phone/tasks", {"action": "status"}, token=None)
        self.assertEqual(st, 401)

    def test_enqueue_local_only(self) -> None:
        # 经 cloudflared 进来的公网请求 (loopback + CF-Connecting-IP 公网) 不许投递/查结果/挂文件
        cf = {"CF-Connecting-IP": "203.0.113.9"}
        st, _, _ = http(self.base, "POST", "/phone/tasks", {"action": "status"}, headers=cf)
        self.assertEqual(st, 401)
        st, _, _ = http(self.base, "GET", "/phone/device", headers=cf)
        self.assertEqual(st, 401)
        st, _, _ = http(self.base, "POST", "/phone/files", {"path": "/etc/hostname"}, headers=cf)
        self.assertEqual(st, 401)
        # 手机侧接口经 cloudflared 照常可用
        st, _, _ = http(self.base, "GET", "/phone/tasks/next?wait=0", headers=cf)
        self.assertEqual(st, 204)

    def test_bad_requests(self) -> None:
        st, _, body = http(self.base, "POST", "/phone/tasks", {"action": "nope"})
        self.assertEqual(st, 400)
        self.assertEqual(json.loads(body)["error"], "unknown_action")
        st, _, _ = http(self.base, "POST", "/phone/tasks", raw=b"[1]", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, _, _ = http(self.base, "GET", "/phone/tasks/" + "c" * 32)
        self.assertEqual(st, 404)
        st, _, _ = http(self.base, "GET", "/phone/nope")
        self.assertEqual(st, 404)

    def test_phone_roundtrip_and_screenshot_blob(self) -> None:
        phone = FakePhone(self.base)
        phone.start()
        self.addCleanup(phone.stop.set)
        st, _, body = http(self.base, "POST", "/phone/tasks", {"action": "screenshot", "args": {}})
        self.assertEqual(st, 200)
        tid = json.loads(body)["task"]["id"]
        st, _, body = http(self.base, "GET", f"/phone/tasks/{tid}?wait=5")
        task = json.loads(body)["task"]
        self.assertEqual(task["status"], "done", task)
        self.assertEqual(task["blob"]["content_type"], "image/jpeg")
        st, hdrs, data = http(self.base, "GET", f"/phone/tasks/{tid}/blob")
        self.assertEqual(st, 200)
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        st, _, body = http(self.base, "GET", "/phone/device")
        dev = json.loads(body)["device"]
        self.assertTrue(dev["online"])
        self.assertEqual(dev["shizuku"], "ready")
        self.assertIn("screen", dev["caps"])

    def test_files_range(self) -> None:
        src = Path(self.tmp.name) / "song.mp3"
        data = os.urandom(10_000)
        src.write_bytes(data)
        st, _, body = http(self.base, "POST", "/phone/files", {"path": str(src)})
        self.assertEqual(st, 200)
        f = json.loads(body)["file"]
        url = f["url"]
        st, hdrs, got = http(self.base, "GET", url)
        self.assertEqual((st, got), (200, data))
        self.assertEqual(hdrs.get("Accept-Ranges"), "bytes")
        st, hdrs, got = http(self.base, "GET", url, headers={"Range": "bytes=4000-"})
        self.assertEqual(st, 206)
        self.assertEqual(got, data[4000:])
        self.assertEqual(hdrs.get("Content-Range"), "bytes 4000-9999/10000")
        st, _, got = http(self.base, "GET", url, headers={"Range": "bytes=10-19"})
        self.assertEqual((st, got), (206, data[10:20]))
        st, _, _ = http(self.base, "GET", url, headers={"Range": "bytes=10000-"})
        self.assertEqual(st, 416)
        st, _, _ = http(self.base, "GET", url, token=None)
        self.assertEqual(st, 401)
        st, _, _ = http(self.base, "POST", "/phone/files", {"path": "relative.mp3"})
        self.assertEqual(st, 400)

    def _cli(self, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ, PHONE_CTL_BASE=self.base, PHONE_CTL_SECRET=SECRET)
        return subprocess.run([sys.executable, str(HERE / "scripts" / "phone-ctl"), *argv],
                              env=env, capture_output=True, text=True, timeout=timeout)

    def test_cli_end_to_end(self) -> None:
        phone = FakePhone(self.base)
        phone.start()
        self.addCleanup(phone.stop.set)
        r = self._cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("在线", r.stdout)
        self.assertIn("电量: 88%", r.stdout)
        r = self._cli("tap", "100", "200")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("点了 (100, 200)", r.stdout)
        src = Path(self.tmp.name) / "周杰伦-晴天.mp3"
        data = os.urandom(200_001)
        src.write_bytes(data)
        r = self._cli("push", str(src), "/sdcard/Music/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sha256 校验通过", r.stdout)
        self.assertEqual(phone.files["/sdcard/Music/周杰伦-晴天.mp3"], data)
        self.assertEqual(phone.range_headers, ["bytes=100000-"])
        out = Path(self.tmp.name) / "shot.jpg"
        r = self._cli("shot", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(out.read_bytes().startswith(b"\xff\xd8"))
        self.assertIn("点按坐标", r.stdout)
        r = self._cli("open", "com.tencent.mm")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unsupported", r.stderr)

    def test_poll_and_claim_report_accessibility(self) -> None:
        st, _, _ = http(self.base, "GET", "/phone/tasks/next?wait=0&caps=status,input&shizuku=no_permission&a11y=on")
        self.assertEqual(st, 204)
        st, _, body = http(self.base, "GET", "/phone/device")
        d = json.loads(body)["device"]
        self.assertEqual((d["accessibility"], d["backend"]), ("on", "accessibility"))
        st, _, body = http(self.base, "POST", "/phone/tasks/claim", {"v": "1.9.234", "a11y": "enabled"})
        self.assertEqual(st, 200)
        self.assertEqual(self.state.phone_control.device()["local"]["seen"], True)
        self.assertEqual(self.state.phone_control._local["accessibility"], "enabled")

    def test_cli_accessibility_backend(self) -> None:
        phone = FakePhone(self.base, caps="status,screen,input,files,shell", shizuku="no_permission", a11y="on")
        phone.start()
        self.addCleanup(phone.stop.set)
        time.sleep(0.3)
        r = self._cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("操控后端: accessibility", r.stdout)
        self.assertIn("无障碍: 已开启", r.stdout)
        r = self._cli("ui")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Button "发送" id=send @960,2250 click', r.stdout)
        self.assertIn("password", r.stdout)
        self.assertIn("com.tencent.mm", r.stdout)
        r = self._cli("clicktext", "发送", "--index", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("点了「发送」（第 1 个，共 2 个匹配，无障碍点击） @(960,2250)", r.stdout)
        r = self._cli("clicktext", "不存在")
        self.assertEqual(r.returncode, 1)
        self.assertIn("没找到", r.stderr)
        r = self._cli("sh", "id")
        self.assertEqual(r.returncode, 1)
        self.assertIn("需要 Shizuku", r.stderr)
        r = self._cli("ls")
        self.assertEqual(r.returncode, 1)
        self.assertIn("需要 Shizuku", r.stderr)

    def test_cli_tier_disabled_message(self) -> None:
        phone = FakePhone(self.base, caps="status")
        phone.start()
        self.addCleanup(phone.stop.set)
        time.sleep(0.3)
        r = self._cli("shot")
        self.assertEqual(r.returncode, 1)
        self.assertIn("手机端没开「截屏」权限", r.stderr)


if __name__ == "__main__":
    unittest.main()
