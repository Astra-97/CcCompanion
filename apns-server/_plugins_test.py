"""插件系统服务端 MVP 回归测试 (2026-09-25, offline; stub handler + tmp 目录).

Covers:
1. list       — GET /plugins 清单: 内置插件可见, _template 骨架不列出; 无鉴权 401
2. static     — 静态托管 + CSP/nosniff 头, entry 默认, MIME 正确
3. traversal  — 路径穿越 (../, %2e%2e, 反斜杠, 非常规后缀) 一律 404
4. kv         — KV 读写/If-Match 语义: 0 新建, N 更新, 409 带当前 version+body, 428 缺 If-Match
5. isolation  — 插件间数据隔离; scoped token 绑插件 id, 跨插件 401
6. scoped     — 签发闸门 (仅 shared_secret 可签), token 只能碰 data GET/PUT,
                对清单/静态/其他端点无效, 篡改/过期 fail-closed
7. web        — PWA web session cookie 可 GET data (会话身份), 不能 PUT
8. concurrent — 多线程乐观并发写不丢版本
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from plugins_store import PluginStore  # noqa: E402
from push import PushHandler, WEB_SESSION_COOKIE_NAME, WebSessionStore  # noqa: E402

SECRET = "plugins-test-secret"


class PluginsServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="_plugins_test_"))
        self.data_dir = self.tmp / "plugins"
        self.builtin_dir = self.tmp / "plugins-builtin"
        # 内置插件: demo + 一个用于隔离测试的第二插件
        for pid in ("demo-checkin", "other-plugin"):
            root = self.builtin_dir / pid
            root.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({
                "id": pid, "name": pid, "version": "0.1.0",
                "description": "test", "entry": "index.html",
            }), encoding="utf-8")
            (root / "index.html").write_text(f"<html>{pid}</html>", encoding="utf-8")
            (root / "app.js").write_text("console.log(1);", encoding="utf-8")
            (root / "secret.pem").write_text("not-servable", encoding="utf-8")
        # 骨架目录: 以下划线开头, 不应被列出/托管
        tpl = self.builtin_dir / "_template"
        tpl.mkdir(parents=True)
        (tpl / "manifest.json").write_text(json.dumps({"id": "_template"}), encoding="utf-8")
        self.store = PluginStore(data_dir=self.data_dir, builtin_dir=self.builtin_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def handler(self, path: str, method: str = "GET", headers: dict | None = None,
                body: bytes = b"") -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = method
        handler.headers = dict(headers or {})
        handler.client_address = ("127.0.0.1", 1)
        handler.responses = []
        handler._send_json = lambda status, payload, **kw: handler.responses.append(
            (status, payload, kw)
        )
        handler.state = types.SimpleNamespace(
            allowed_ips=[],
            shared_secret=SECRET,
            strict_auth=True,
            web_session_enabled=False,
        )
        handler._plugins_store = self.store
        handler.rfile = io.BytesIO(body)
        handler.sent = {"status": None, "headers": {}, }
        handler.send_response = lambda code: handler.sent.update(status=code)
        handler.send_header = lambda k, v: handler.sent["headers"].__setitem__(str(k).lower(), str(v))
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        return handler

    @staticmethod
    def last(handler):
        return handler.responses[-1]

    def auth(self):
        return {"X-Auth-Token": SECRET}

    # ---------- 1. 清单 ----------

    def test_list_plugins(self):
        plugins = {p["id"]: p for p in self.store.list_plugins()}
        self.assertIn("demo-checkin", plugins)
        self.assertIn("other-plugin", plugins)
        self.assertNotIn("_template", plugins)
        self.assertEqual(plugins["demo-checkin"]["entry"], "index.html")
        self.assertTrue(plugins["demo-checkin"]["builtin"])

        h = self.handler("/plugins")
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

        h = self.handler("/plugins", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        ids = {p["id"] for p in payload["plugins"]}
        self.assertEqual(ids, {"demo-checkin", "other-plugin"})
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_data_dir_overrides_builtin(self):
        override = self.data_dir / "demo-checkin"
        override.mkdir(parents=True)
        (override / "manifest.json").write_text(json.dumps({
            "id": "demo-checkin", "name": "覆盖版", "version": "9.9.9",
            "description": "", "entry": "index.html",
        }), encoding="utf-8")
        (override / "index.html").write_text("<html>override</html>", encoding="utf-8")
        plugins = {p["id"]: p for p in self.store.list_plugins()}
        self.assertEqual(plugins["demo-checkin"]["version"], "9.9.9")
        self.assertFalse(plugins["demo-checkin"]["builtin"])

    # ---------- 2. 静态托管 + CSP ----------

    def test_static_serving_csp(self):
        h = self.handler("/plugins/demo-checkin/", headers=self.auth())
        h.do_GET()
        self.assertEqual(h.sent["status"], 200)
        self.assertEqual(h.sent["headers"].get("content-security-policy"), "default-src 'self'")
        self.assertEqual(h.sent["headers"].get("x-content-type-options"), "nosniff")
        self.assertIn("text/html", h.sent["headers"].get("content-type", ""))
        self.assertIn(b"demo-checkin", h.wfile.getvalue())

        h = self.handler("/plugins/demo-checkin/app.js", headers=self.auth())
        h.do_GET()
        self.assertEqual(h.sent["status"], 200)
        self.assertIn("text/javascript", h.sent["headers"].get("content-type", ""))
        self.assertEqual(h.sent["headers"].get("content-security-policy"), "default-src 'self'")

        # 无鉴权 -> 401
        h = self.handler("/plugins/demo-checkin/")
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

    # ---------- 3. 路径穿越 ----------

    def test_path_traversal_blocked(self):
        evil_paths = [
            "/plugins/demo-checkin/../demo-checkin/manifest.json",
            "/plugins/demo-checkin/%2e%2e/%2e%2e/push.py",
            "/plugins/demo-checkin/..\\secret.pem",
            "/plugins/demo-checkin//app.js",
            "/plugins/demo-checkin/./app.js",
            "/plugins/demo-checkin/secret.pem",   # 非常规后缀
            "/plugins/_template/index.html",       # 骨架目录不可托管
            "/plugins/nonexistent/index.html",
        ]
        for path in evil_paths:
            h = self.handler(path, headers=self.auth())
            h.do_GET()
            status = h.sent["status"] if h.sent["status"] else self.last(h)[0]
            self.assertEqual(status, 404, f"expected 404 for {path!r}, got {status}")
            self.assertEqual(h.wfile.getvalue(), b"", f"no body leak for {path!r}")

    # ---------- 4. KV 协议 ----------

    def test_kv_version_flow(self):
        # GET 不存在的文档 -> 404 version 0
        h = self.handler("/plugins/demo-checkin/data/checkins", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 404)
        self.assertEqual(payload["version"], 0)

        # 缺 If-Match -> 428
        body = json.dumps({"records": []}).encode()
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**self.auth(), "Content-Length": str(len(body))}, body=body)
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 428)

        # If-Match: 0 新建 -> v1
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**self.auth(), "Content-Length": str(len(body)), "If-Match": "0"},
                         body=body)
        h.do_PUT()
        status, payload, _ = self.last(h)
        self.assertEqual((status, payload["version"]), (200, 1))

        # 重复 If-Match: 0 -> 409 (新建语义, 已存在)
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**self.auth(), "Content-Length": str(len(body)), "If-Match": "0"},
                         body=body)
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 409)

        # GET 读回
        h = self.handler("/plugins/demo-checkin/data/checkins", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual((status, payload["version"]), (200, 1))
        self.assertEqual(payload["body"], {"records": []})

        # If-Match: 1 更新 -> v2
        body2 = json.dumps({"records": [{"ts": "2026-09-25T08:00:00Z"}]}).encode()
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**self.auth(), "Content-Length": str(len(body2)), "If-Match": "1"},
                         body=body2)
        h.do_PUT()
        self.assertEqual(self.last(h)[1]["version"], 2)

        # 过期 If-Match: 1 -> 409 带当前 version 和当前内容
        stale = json.dumps({"records": []}).encode()
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**self.auth(), "Content-Length": str(len(stale)), "If-Match": "1"},
                         body=stale)
        h.do_PUT()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "version_conflict")
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["body"], {"records": [{"ts": "2026-09-25T08:00:00Z"}]})

        # 调用方重读合并后用 If-Match: 2 重试 -> v3
        merged = json.dumps({"records": payload["body"]["records"] + [{"ts": "2026-09-25T09:00:00Z"}]}).encode()
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**self.auth(), "Content-Length": str(len(merged)), "If-Match": "2"},
                         body=merged)
        h.do_PUT()
        status, payload, _ = self.last(h)
        self.assertEqual((status, payload["version"]), (200, 3))

        # 重启等价物: 新 store 实例读回持久化数据
        fresh = PluginStore(data_dir=self.data_dir, builtin_dir=self.builtin_dir)
        record = fresh.read_doc("demo-checkin", "checkins")
        self.assertEqual(record["version"], 3)
        self.assertEqual(len(record["body"]["records"]), 2)

        # 非法 doc 名 / 非 data 路径的 PUT
        h = self.handler("/plugins/demo-checkin/data/bad/doc", "PUT",
                         headers={**self.auth(), "Content-Length": "2", "If-Match": "0"}, body=b"{}")
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 404)
        h = self.handler("/plugins/demo-checkin/app.js", "PUT",
                         headers={**self.auth(), "Content-Length": "2", "If-Match": "0"}, body=b"{}")
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 404)

    def test_kv_requires_auth_fail_closed(self):
        body = b"{}"
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={"Content-Length": "2", "If-Match": "0"}, body=body)
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 401)
        h = self.handler("/plugins/demo-checkin/data/checkins")
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

    # ---------- 5. 插件间隔离 ----------

    def test_plugin_isolation(self):
        status, _ = self.store.write_doc("demo-checkin", "checkins", {"records": [1]}, 0)
        self.assertEqual(status, "ok")
        # 同名 doc 在另一插件下不存在
        self.assertIsNone(self.store.read_doc("other-plugin", "checkins"))
        # 数据文件物理隔离在各自插件目录
        self.assertTrue((self.data_dir / "demo-checkin" / "data" / "checkins.json").exists())
        self.assertFalse((self.data_dir / "other-plugin" / "data" / "checkins.json").exists())
        # A 插件的 scoped token 不能读写 B 插件的数据
        token_a, _ = self.store.mint_scoped_token("demo-checkin", SECRET)
        h = self.handler("/plugins/other-plugin/data/checkins",
                         headers={"X-Plugin-Token": token_a})
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)
        body = b"{}"
        h = self.handler("/plugins/other-plugin/data/checkins", "PUT",
                         headers={"X-Plugin-Token": token_a, "Content-Length": "2", "If-Match": "0"},
                         body=body)
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 401)

    # ---------- 6. scoped token ----------

    def mint(self, plugin_id="demo-checkin", headers=None):
        body = b"{}"
        h = self.handler(f"/plugins/{plugin_id}/token", "POST",
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(body)), **(headers or {})},
                         body=body)
        h.do_POST()
        return h

    def test_scoped_token_mint_gate(self):
        # 无鉴权 -> 401; web cookie 无权签发 (fail-closed native pairing 闸门)
        self.assertEqual(self.last(self.mint())[0], 401)
        sessions = WebSessionStore(300)
        web_token, _ = sessions.create()
        h = self.mint(headers={"Cookie": f"{WEB_SESSION_COOKIE_NAME}={web_token}"})
        h.state.web_session_enabled = True
        h.state.web_sessions = sessions
        # 重跑: cookie 也不能签
        h2 = self.mint(headers={"Cookie": f"{WEB_SESSION_COOKIE_NAME}={web_token}"})
        h2.state.web_session_enabled = True
        h2.state.web_sessions = sessions
        self.assertEqual(self.last(h2)[0], 401)
        # shared_secret 可以签
        h3 = self.mint(headers=self.auth())
        status, payload, _ = self.last(h3)
        self.assertEqual(status, 200)
        self.assertTrue(payload["token"].startswith("p1."))
        # 不存在的插件 -> 404
        self.assertEqual(self.last(self.mint("ghost", headers=self.auth()))[0], 404)

    def test_scoped_token_limited_to_data_path(self):
        h = self.mint(headers=self.auth())
        token = self.last(h)[1]["token"]

        # data GET/PUT 可用
        h = self.handler("/plugins/demo-checkin/data/checkins", headers={"X-Plugin-Token": token})
        h.do_GET()
        self.assertEqual(self.last(h)[0], 404)  # 文档不存在但鉴权通过
        body = json.dumps({"records": []}).encode()
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={"X-Plugin-Token": token, "Content-Length": str(len(body)),
                                  "If-Match": "0"},
                         body=body)
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 200)
        # Authorization: Bearer 形式也可用
        h = self.handler("/plugins/demo-checkin/data/checkins",
                         headers={"Authorization": f"Bearer {token}"})
        h.do_GET()
        self.assertEqual(self.last(h)[0], 200)

        # 同 token 对清单/静态/其他鉴权端点无效 (/version 是公开端点, 不在此列)
        for path in ("/plugins", "/plugins/demo-checkin/", "/chat/contacts", "/chat/history"):
            h = self.handler(path, headers={"X-Plugin-Token": token})
            h.do_GET()
            self.assertEqual(self.last(h)[0], 401, f"scoped token must not authorize {path}")

        # 篡改/错误 secret/过期 -> fail-closed
        self.assertFalse(self.store.verify_scoped_token(token + "x", "demo-checkin", SECRET))
        self.assertFalse(self.store.verify_scoped_token(token, "demo-checkin", "wrong-secret"))
        self.assertFalse(self.store.verify_scoped_token(token, "demo-checkin", ""))
        self.assertFalse(self.store.verify_scoped_token(
            token, "demo-checkin", SECRET, now=time.time() + 31 * 86400))
        # 结构校验
        for bad in ("", "p1", "p1.x", "p2.x.y", "p1.!!!.yyy"):
            self.assertFalse(self.store.verify_scoped_token(bad, "demo-checkin", SECRET))

    # ---------- 7. web session 会话身份 ----------

    def test_web_session_can_get_data_but_not_put(self):
        sessions = WebSessionStore(300)
        web_token, _ = sessions.create()
        self.store.write_doc("demo-checkin", "checkins", {"records": []}, 0)
        cookie = {"Cookie": f"{WEB_SESSION_COOKIE_NAME}={web_token}"}

        h = self.handler("/plugins/demo-checkin/data/checkins", headers=cookie)
        h.state.web_session_enabled = True
        h.state.web_sessions = sessions
        h.do_GET()
        self.assertEqual(self.last(h)[0], 200)

        h = self.handler("/plugins", headers=cookie)
        h.state.web_session_enabled = True
        h.state.web_sessions = sessions
        h.do_GET()
        self.assertEqual(self.last(h)[0], 200)

        body = b"{}"
        h = self.handler("/plugins/demo-checkin/data/checkins", "PUT",
                         headers={**cookie, "Content-Length": "2", "If-Match": "1"}, body=body)
        h.state.web_session_enabled = True
        h.state.web_sessions = sessions
        h.do_PUT()
        self.assertEqual(self.last(h)[0], 401)

    # ---------- 8. 并发写不丢版本 ----------

    def test_concurrent_writes_no_lost_version(self):
        threads_n, per_thread = 8, 10

        def worker():
            for _ in range(per_thread):
                while True:
                    cur = self.store.read_doc("demo-checkin", "counter")
                    version = cur["version"] if cur else 0
                    n = (cur["body"] or {}).get("n", 0) if cur else 0
                    status, _ = self.store.write_doc(
                        "demo-checkin", "counter", {"n": n + 1}, version)
                    if status == "ok":
                        break

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        record = self.store.read_doc("demo-checkin", "counter")
        self.assertEqual(record["version"], threads_n * per_thread)
        self.assertEqual(record["body"]["n"], threads_n * per_thread)


if __name__ == "__main__":
    unittest.main(verbosity=2)
