"""「手机文件」本地能力测试 (2026-11-01, App v1.9.236: 没 Shizuku 时看 / 拷手机文件).

覆盖:
  1. validate_task: file_ls (sort / limit) / file_stat / file_pull (200MB 上限) / file_push (Android/data、obb 除外)
  2. PhoneControlHub 分块上传: 顺序传完 → 正式 blob + sha256; 上一块重传 (offset < received) 截掉重写;
     跳块 409 带 received; 超 200MB / 每块超 16MB 拒; 任务取消后残留分块文件被清掉; 老的整块上传照旧
  3. file_push 用 outbox (和 download 一样); 「手机文件」关着时提前 403; 不认识的 feature 不进上报
  4. 真 HTTP + phone-ctl (假 App 走 /phone/tasks/claim): ls -t -n / stat / pull (分块 + 续传 + sha 校验) /
     push (默认 Download/小克/, 也能放她指定的中文目录 + --force) / 越界路径与开关关着的报错; Shizuku 就绪时仍走老路
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
import unittest
from pathlib import Path

import phone_control as pc
from phone_control import PhoneControlHub, PhoneTaskError
from _phone_control_test import SECRET, FakeClock, http
from _phone_local_test import _start_server

HERE = Path(__file__).resolve().parent
MB = 1024 * 1024


class ValidateFilesTest(unittest.TestCase):
    def test_file_ls(self) -> None:
        out = pc.validate_task("file_ls", {"path": "/sdcard/DCIM/", "sort": "mtime", "limit": 20})
        self.assertEqual(out, {"feature": "files", "path": "/sdcard/DCIM", "limit": 20, "sort": "mtime"})
        self.assertEqual(pc.validate_task("file_ls", {}),
                         {"feature": "files", "path": "/sdcard", "limit": 200, "sort": "name"})
        for bad in ({"sort": "random"}, {"limit": 0}, {"limit": 5001}, {"path": "/data/data"},
                    {"path": "/sdcard/../etc"}, {"path": "relative"}):
            with self.assertRaises(PhoneTaskError, msg=bad):
                pc.validate_task("file_ls", bad)

    def test_file_stat_and_pull(self) -> None:
        self.assertEqual(pc.validate_task("file_stat", {"path": "/storage/emulated/0/a.txt"})["path"],
                         "/storage/emulated/0/a.txt")
        out = pc.validate_task("file_pull", {"path": "/sdcard/Download/x.pdf"})
        self.assertEqual((out["max_bytes"], out["chunk_bytes"]), (200 * MB, 8 * MB))
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("file_pull", {"path": "/sdcard"})  # 根目录不是文件
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("file_pull", {"path": "/sdcard/x", "max_bytes": 200 * MB + 1})
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("file_pull", {"path": "/sdcard/x", "chunk_bytes": 17 * MB})

    def test_file_push_dest(self) -> None:
        fid = "a" * 32
        out = pc.validate_task("file_push", {"file_id": fid, "dest": "/sdcard/aaa天行执舰官/夏以昼PV/16_爱，宇宙，诗王座.mp3"})
        self.assertEqual(out["dest"], "/sdcard/aaa天行执舰官/夏以昼PV/16_爱，宇宙，诗王座.mp3")
        self.assertEqual(pc.validate_task("file_push", {"file_id": fid,
                                                         "dest": "/storage/emulated/0/Music/"})["dest"],
                         "/storage/emulated/0/Music")
        for dest in ("/sdcard", "/sdcard/", "/sdcard/Android/data/com.x/a", "/sdcard/android/OBB",
                     "/storage/emulated/0/Android/data", "/data/local/tmp/a", "/sdcard/Download/../a"):
            with self.assertRaises(PhoneTaskError, msg=dest) as cm:
                pc.validate_task("file_push", {"file_id": fid, "dest": dest})
            self.assertIn(cm.exception.code, ("bad_dest", "bad_path"))
        with self.assertRaises(PhoneTaskError):
            pc.validate_task("file_push", {"file_id": "x", "dest": "/sdcard/Download/小克/a"})

    def test_feature_tables(self) -> None:
        for action in ("file_ls", "file_stat", "file_pull", "file_push"):
            self.assertEqual(pc.ACTIONS[action][0], "local")
            self.assertEqual(pc.LOCAL_ACTION_FEATURE[action], "files")
        self.assertIn("files", pc.LOCAL_FEATURES)
        self.assertIn("file_pull", pc.BLOB_ACTIONS)
        self.assertIn("file_push", pc.OUTBOX_ACTIONS)
        self.assertNotIn("file_ls", pc.SHIZUKU_ONLY_ACTIONS)


class ChunkUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.hub = PhoneControlHub(self.tmp.name, clock=self.clock)

    def _claimed_pull(self) -> str:
        t = self.hub.enqueue("file_pull", {"path": "/sdcard/Download/a.bin"})
        got = self.hub.claim_local(task_id=t["id"])
        self.assertIsNotNone(got)
        return t["id"]

    def _put(self, tid: str, data: bytes, offset: int, total: int) -> dict:
        return self.hub.put_blob_chunk(tid, io.BytesIO(data), len(data), offset, total, "application/pdf")

    def test_sequential_chunks_retry_and_gap(self) -> None:
        tid = self._claimed_pull()
        data = os.urandom(10_000)
        r = self._put(tid, data[:4000], 0, len(data))
        self.assertEqual((r["received"], r["complete"]), (4000, False))
        self.assertEqual(self.hub.get(tid)["progress"]["done"], 4000)
        # 跳块: 409 + received
        with self.assertRaises(PhoneTaskError) as cm:
            self._put(tid, data[8000:], 8000, len(data))
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "upload_offset_mismatch"))
        self.assertEqual(cm.exception.payload()["received"], 4000)
        # 上一块重传 (响应丢了): 截掉重写, 不重复
        self._put(tid, data[:4000], 0, len(data))
        self._put(tid, data[4000:8000], 4000, len(data))
        r = self._put(tid, data[8000:], 8000, len(data))
        self.assertTrue(r["complete"])
        self.assertEqual(r["sha256"], hashlib.sha256(data).hexdigest())
        blob = self.hub.blob(tid)
        self.assertEqual(Path(blob["path"]).read_bytes(), data)
        self.assertEqual((blob["size"], blob["content_type"]), (len(data), "application/pdf"))
        self.assertEqual(list(Path(self.tmp.name, "phone_blobs").glob(".*.part")), [])
        self.hub.submit_result(tid, {"ok": True, "result": {"size": len(data)}})
        self.assertEqual(self.hub.get(tid)["status"], "done")

    def test_limits(self) -> None:
        tid = self._claimed_pull()
        with self.assertRaises(PhoneTaskError) as cm:
            self._put(tid, b"x", 0, 200 * MB + 1)
        self.assertEqual((cm.exception.status, cm.exception.code), (413, "too_large"))
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.put_blob_chunk(tid, io.BytesIO(b""), 16 * MB + 1, 0, 100 * MB, "")
        self.assertEqual(cm.exception.status, 413)
        with self.assertRaises(PhoneTaskError) as cm:
            self._put(tid, b"abc", 9, 10)
        self.assertEqual(cm.exception.code, "bad_offset")
        with self.assertRaises(PhoneTaskError) as cm:
            self._put(tid, b"abc", 3, 10)  # 服务端还没这次上传
        self.assertEqual(cm.exception.payload()["received"], 0)
        # 截屏任务的分块上限仍是 128MB
        shot = self.hub.enqueue("screenshot", {})
        self.hub.claim(wait_s=0)
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.put_blob_chunk(shot["id"], io.BytesIO(b"x"), 1, 0, 129 * MB, "")
        self.assertEqual(cm.exception.code, "too_large")

    def test_short_body_and_cancel_cleanup(self) -> None:
        tid = self._claimed_pull()
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.put_blob_chunk(tid, io.BytesIO(b"abc"), 5, 0, 10, "")
        self.assertEqual((cm.exception.code, cm.exception.payload()["received"]), ("short_body", 0))
        self._put(tid, b"12345", 0, 10)
        parts = list(Path(self.tmp.name, "phone_blobs").glob(".*.part"))
        self.assertEqual(len(parts), 1)
        self.hub.cancel(tid)
        self.assertFalse(parts[0].exists())
        with self.assertRaises(PhoneTaskError):
            self._put(tid, b"67890", 5, 10)
        self.assertEqual(list(Path(self.tmp.name, "phone_blobs").glob(".*.part")), [])

    def test_success_without_blob_fails(self) -> None:
        tid = self._claimed_pull()
        self._put(tid, b"12345", 0, 10)  # 只传了一半就说成功
        self.hub.submit_result(tid, {"ok": True, "result": {}})
        task = self.hub.get(tid)
        self.assertEqual((task["status"], task["error"]), ("failed", "missing_blob"))

    def test_file_push_uses_outbox_and_feature_precheck(self) -> None:
        src = Path(self.tmp.name) / "a.txt"
        src.write_bytes(b"hello")
        staged = self.hub.stage_file(src)
        t = self.hub.enqueue("file_push", {"file_id": staged["file_id"], "dest": "/sdcard/Download/小克/a.txt"})
        self.assertEqual(t["args"]["name"], "a.txt")
        self.assertEqual(t["args"]["url"], f"/phone/files/{staged['file_id']}")
        self.assertEqual((t["args"]["size"], t["args"]["sha256"]), (5, staged["sha256"]))
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.enqueue("file_push", {"file_id": "b" * 32, "dest": "/sdcard/Download/小克/a.txt"})
        self.assertEqual(cm.exception.code, "file_not_found")
        # 走 App 传文件和拉文件同一个 200MB 上限
        big = Path(self.tmp.name) / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(200 * MB + 1)
        staged_big = self.hub.stage_file(big)
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.enqueue("file_push", {"file_id": staged_big["file_id"], "dest": "/sdcard/Music/big.bin"})
        self.assertEqual((cm.exception.status, cm.exception.code), (413, "too_large"))
        # App 上报「手机文件」关着 → 提前 403
        self.hub.note_local(features={"files": {"enabled": False, "missing": ["所有文件访问权限"]},
                                      "bogus": {"enabled": True}})
        self.assertNotIn("bogus", self.hub.device()["local"]["features"])
        with self.assertRaises(PhoneTaskError) as cm:
            self.hub.enqueue("file_ls", {})
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "feature_disabled"))
        self.assertIn("手机文件（只读/拷出）", cm.exception.message)


class FakeFilesApp(threading.Thread):
    """假 App: /phone/tasks/claim 领「手机文件」任务。pull 分块上传, 第一块之后故意「断线」一次再续传。"""

    def __init__(self, base: str, files: dict[str, bytes], *, chunk: int = 64 * 1024,
                 enabled: bool = True) -> None:
        super().__init__(daemon=True)
        self.base = base
        self.files = files
        self.chunk = chunk
        self.enabled = enabled
        self.stop = threading.Event()
        self.pushed: dict[str, bytes] = {}
        self.push_args: list[dict] = []
        self.uploads: list[tuple[int, int]] = []

    def run(self) -> None:
        while not self.stop.is_set():
            features = {"files": {"enabled": self.enabled, "missing": []}}
            st, _, body = http(self.base, "POST", "/phone/tasks/claim", {"v": "1.9.236", "features": features})
            task = json.loads(body).get("task") if st == 200 else None
            if not task:
                time.sleep(0.05)
                continue
            try:
                self.handle(task)
            except Exception as exc:  # pragma: no cover - debug aid
                self.result(task, {"ok": False, "error": "fake_app", "message": repr(exc)})

    def result(self, task, payload) -> None:
        http(self.base, "POST", f"/phone/tasks/{task['id']}/result", payload)

    def handle(self, task) -> None:
        a, args = task["action"], task["args"]
        if a == "file_ls":
            if args["path"].startswith("/sdcard/Android/data"):
                self.result(task, {"ok": False, "error": "restricted",
                                   "message": "Android/data、Android/obb 是系统保护目录，所有文件访问权限也读不到"})
                return
            entries = [{"name": "old.txt", "size": 10, "mtime": 1_700_000_000_000, "dir": False},
                       {"name": "new.jpg", "size": 3_000_000, "mtime": 1_800_000_000_000, "dir": False},
                       {"name": "Camera", "size": 0, "mtime": 1_750_000_000_000, "dir": True}]
            if args["sort"] == "mtime":
                entries.sort(key=lambda e: -e["mtime"])
            self.result(task, {"ok": True, "result": {"path": args["path"], "entries": entries[: args["limit"]],
                                                      "total": 3, "truncated": args["limit"] < 3}})
        elif a == "file_stat":
            data = self.files.get(args["path"])
            self.result(task, {"ok": True, "result": {"path": args["path"], "exists": data is not None,
                                                      "dir": False, "size": len(data or b""),
                                                      "mtime": 1_800_000_000_000}})
        elif a == "file_pull":
            data = self.files.get(args["path"])
            if data is None:
                self.result(task, {"ok": False, "error": "not_found", "message": f"手机上没有这个文件：{args['path']}"})
                return
            if len(data) > args["max_bytes"]:
                self.result(task, {"ok": False, "error": "too_large", "message": "太大"})
                return
            offset, dropped = 0, False
            while offset < len(data):
                piece = data[offset: offset + self.chunk]
                st, _, body = http(self.base, "POST",
                                   f"/phone/tasks/{task['id']}/blob?offset={offset}&total={len(data)}",
                                   raw=piece, headers={"Content-Type": "application/octet-stream"})
                self.uploads.append((offset, st))
                reply = json.loads(body)
                assert st == 200, reply
                if not dropped and offset > 0:
                    # 模拟「响应丢了」: 同一块再传一次
                    dropped = True
                    continue
                offset = reply["received"]
            self.result(task, {"ok": True, "result": {"path": args["path"], "size": len(data),
                                                      "sha256": hashlib.sha256(data).hexdigest()}})
        elif a == "file_push":
            st, _, got = http(self.base, "GET", args["url"])
            ok = st == 200 and hashlib.sha256(got).hexdigest() == args["sha256"]
            self.pushed[args["dest"]] = got
            self.push_args.append(args)
            self.result(task, {"ok": ok, "result": {"path": args["dest"], "size": len(got), "sha256": args["sha256"],
                                                    "attempts": 1, "resumed": 0}})
        else:
            self.result(task, {"ok": False, "error": "unsupported", "message": a})


class FilesHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server, self.state = _start_server(self.tmp.name)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _cli(self, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
        env = dict(os.environ, PHONE_CTL_BASE=self.base, PHONE_CTL_SECRET=SECRET)
        return subprocess.run([sys.executable, str(HERE / "scripts" / "phone-ctl"), *argv],
                              env=env, capture_output=True, text=True, timeout=timeout)

    def _app(self, files: dict[str, bytes] | None = None, **kw) -> FakeFilesApp:
        app = FakeFilesApp(self.base, files or {}, **kw)
        app.start()
        self.addCleanup(app.stop.set)
        return app

    def test_chunk_route(self) -> None:
        t = self.state.phone_control.enqueue("file_pull", {"path": "/sdcard/a"})
        http(self.base, "POST", "/phone/tasks/claim", {"task_id": t["id"]})
        st, _, body = http(self.base, "POST", f"/phone/tasks/{t['id']}/blob?offset=0&total=6", raw=b"abc")
        self.assertEqual((st, json.loads(body)["received"]), (200, 3))
        st, _, body = http(self.base, "POST", f"/phone/tasks/{t['id']}/blob?offset=5&total=6", raw=b"f")
        self.assertEqual(st, 409)
        self.assertEqual(json.loads(body)["received"], 3)
        st, _, _ = http(self.base, "POST", f"/phone/tasks/{t['id']}/blob?offset=x&total=6", raw=b"f")
        self.assertEqual(st, 400)
        st, _, body = http(self.base, "POST", f"/phone/tasks/{t['id']}/blob?offset=3&total=6", raw=b"def")
        self.assertEqual(json.loads(body)["sha256"], hashlib.sha256(b"abcdef").hexdigest())
        st, _, _ = http(self.base, "POST", f"/phone/tasks/{t['id']}/blob?offset=0&total=6", raw=b"abc",
                        token="wrong")
        self.assertEqual(st, 401)

    def test_cli_ls_stat_pull_push_via_app(self) -> None:
        payload = os.urandom(300_000)
        app = self._app({"/sdcard/Download/报告.pdf": payload})
        r = self._cli("ls", "/sdcard/DCIM", "-t", "-n", "2", "--wait", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].endswith("new.jpg"), r.stdout)
        self.assertTrue(lines[1].endswith("Camera/"), r.stdout)
        self.assertIn("共 3 项，只列了前 2 项", r.stdout)
        r = self._cli("ls", "/sdcard/Android/data/com.tencent.mm", "--wait", "10")
        self.assertEqual(r.returncode, 1)
        self.assertIn("系统保护目录", r.stderr)
        r = self._cli("stat", "/sdcard/Download/报告.pdf", "--wait", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("300000 字节", r.stdout)
        out = Path(self.tmp.name) / "pulled.pdf"
        r = self._cli("pull", "/sdcard/Download/报告.pdf", str(out), "--wait", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out.read_bytes(), payload)
        self.assertIn("sha256 校验通过", r.stdout)
        self.assertIn("走 App", r.stdout)
        # 第二块被传了两次 (续传路径), 最终文件不重复
        self.assertEqual([o for o, _ in app.uploads].count(65536), 2)
        r = self._cli("pull", "/sdcard/Download/没有.pdf", str(out), "--wait", "10")
        self.assertEqual(r.returncode, 1)
        self.assertIn("手机上没有这个文件", r.stderr)
        src = Path(self.tmp.name) / "歌.mp3"
        src.write_bytes(b"la" * 1000)
        r = self._cli("push", str(src), "--wait", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(app.pushed["/sdcard/Download/小克/歌.mp3"], b"la" * 1000)
        song = Path(self.tmp.name) / "16_爱，宇宙，诗王座.mp3"
        song.write_bytes(b"\x00mp3" * 500)
        r = self._cli("push", str(song), "/sdcard/aaa天行执舰官/夏以昼PV/", "--force", "--wait", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(app.pushed["/sdcard/aaa天行执舰官/夏以昼PV/16_爱，宇宙，诗王座.mp3"], b"\x00mp3" * 500)
        self.assertTrue(app.push_args[-1]["overwrite"])
        r = self._cli("push", str(src), "/sdcard/Android/data/com.x/")
        self.assertEqual(r.returncode, 1)
        self.assertIn("系统保护目录", r.stderr)
        r = self._cli("ls", "/data/data", "--wait", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("只允许 /sdcard/", r.stderr)
        r = self._cli("local")
        self.assertIn("手机文件（只读/拷出）: 开", r.stdout)

    def test_cli_feature_disabled(self) -> None:
        self._app(enabled=False)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.state.phone_control.device()["local"]["seen"]:
            time.sleep(0.05)
        r = self._cli("ls", "--wait", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("手机端关着「手机文件（只读/拷出）」", r.stderr)

    def test_auto_prefers_shizuku_when_ready(self) -> None:
        # Shizuku 就绪 + 开了文件档 → 投老的 ls (Shizuku 后端)
        http(self.base, "GET", "/phone/tasks/next?wait=0&caps=status,files&shizuku=ready&v=1.9.236")
        r = self._cli("ls", "--wait", "1")
        self.assertEqual(r.returncode, 1)
        actions = [t["action"] for t in self.state.phone_control.list_tasks()]
        self.assertEqual(actions, ["ls"])
        # 没开文件档 → 走 App
        http(self.base, "GET", "/phone/tasks/next?wait=0&caps=status&shizuku=ready&v=1.9.236")
        self._cli("ls", "--wait", "1")
        actions = [t["action"] for t in self.state.phone_control.list_tasks()]
        self.assertEqual(actions[-1], "file_ls")


if __name__ == "__main__":
    unittest.main()
