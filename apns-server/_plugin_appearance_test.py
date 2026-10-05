"""插件命名空间外观壁纸端点回归测试 (2026-10-05, offline; stub handler + tmp 目录).

Covers:
1. wallpaper   — 有自定义壁纸: GET /plugins/<id>/appearance 给 bg_url,
                 appearance-asset 流式返回图片字节 + 正确 mime
2. no_wallpaper — bgUri 为空 (App 内置默认壁纸): bg_url=null, asset 404
3. bad_name    — bgUri 指向非法文件名 (../、/、前导 .): bg_url=null, asset 404
4. auth        — 无凭据一律 401; scoped token 不放行; 未知插件 404
5. fallback    — user_settings 无 appearance 时回落旧版 appearance_settings.json
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from plugins_store import PluginStore  # noqa: E402
from push import PushHandler  # noqa: E402

SECRET = "plugin-appearance-test-secret"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-wallpaper-bytes"


class PluginAppearanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="_plugin_appearance_test_"))
        self.assets_dir = self.tmp / "appearance_assets"
        self.assets_dir.mkdir(parents=True)
        self.user_settings_path = self.tmp / "user_settings.json"
        self.appearance_settings_path = self.tmp / "appearance_settings.json"
        # 类级路径指到 tmp, 不碰真实 state
        for attr, value in (
            ("_APPEARANCE_ASSETS_DIR", self.assets_dir),
            ("_USER_SETTINGS_PATH", self.user_settings_path),
            ("_APPEARANCE_SETTINGS_PATH", self.appearance_settings_path),
        ):
            original = getattr(PushHandler, attr)
            setattr(PushHandler, attr, value)
            self.addCleanup(setattr, PushHandler, attr, original)
        # 内置插件: packing + other
        self.builtin_dir = self.tmp / "plugins-builtin"
        for pid in ("packing", "other-plugin"):
            root = self.builtin_dir / pid
            root.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({
                "id": pid, "name": pid, "version": "0.1.0",
                "description": "test", "entry": "index.html",
            }), encoding="utf-8")
            (root / "index.html").write_text(f"<html>{pid}</html>", encoding="utf-8")
        self.store = PluginStore(data_dir=self.tmp / "plugins", builtin_dir=self.builtin_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def handler(self, path: str, headers: dict | None = None) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
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
        handler.rfile = io.BytesIO()
        handler.sent = {"status": None, "headers": {}}
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

    def write_user_settings(self, appearance: dict) -> None:
        self.user_settings_path.write_text(
            json.dumps({"appearance": appearance}), encoding="utf-8")

    # ---------- 1. 有壁纸 ----------

    def test_wallpaper_served(self):
        (self.assets_dir / "wallpaper.png").write_bytes(PNG_BYTES)
        self.write_user_settings({"bgUri": "/appearance-assets/wallpaper.png", "veil": {"alpha": 0.4}})

        h = self.handler("/plugins/packing/appearance", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["bg_url"], "/plugins/packing/appearance-asset")
        self.assertEqual(payload["veil"], {"alpha": 0.4})
        # 只暴露壁纸相关子集, 不倒整份 settings
        self.assertNotIn("bgUri", payload)
        self.assertNotIn(SECRET, json.dumps(payload))

        h = self.handler("/plugins/packing/appearance-asset", headers=self.auth())
        h.do_GET()
        self.assertEqual(h.sent["status"], 200)
        self.assertEqual(h.sent["headers"].get("content-type"), "image/png")
        self.assertEqual(h.sent["headers"].get("cache-control"), "no-cache")
        self.assertEqual(h.wfile.getvalue(), PNG_BYTES)

        # 对所有插件 id 生效
        h = self.handler("/plugins/other-plugin/appearance", headers=self.auth())
        h.do_GET()
        self.assertEqual(self.last(h)[1]["bg_url"], "/plugins/other-plugin/appearance-asset")

    # ---------- 2. 无壁纸 ----------

    def test_no_wallpaper(self):
        self.write_user_settings({"bgUri": ""})

        h = self.handler("/plugins/packing/appearance", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 200)
        self.assertIsNone(payload["bg_url"])

        h = self.handler("/plugins/packing/appearance-asset", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "no_wallpaper")
        self.assertEqual(h.wfile.getvalue(), b"")

        # 文件缺失 (bgUri 有值但磁盘上没有) 同样按无壁纸处理
        self.write_user_settings({"bgUri": "/appearance-assets/gone.png"})
        h = self.handler("/plugins/packing/appearance", headers=self.auth())
        h.do_GET()
        self.assertIsNone(self.last(h)[1]["bg_url"])
        h = self.handler("/plugins/packing/appearance-asset", headers=self.auth())
        h.do_GET()
        self.assertEqual(self.last(h)[0], 404)

    # ---------- 3. 坏文件名 ----------

    def test_bad_filename_rejected(self):
        for bad in (
            "/appearance-assets/../user_settings.json",
            "/appearance-assets/nested/wallpaper.png",
            "/appearance-assets/.hidden",
            "/appearance-assets/",
            "/appearance-assets",
            "content://media/image/1234",   # App 本地 content:// URI 不是服务端资源
        ):
            self.write_user_settings({"bgUri": bad})
            h = self.handler("/plugins/packing/appearance", headers=self.auth())
            h.do_GET()
            status, payload, _ = self.last(h)
            self.assertEqual(status, 200, bad)
            self.assertIsNone(payload["bg_url"], bad)

            h = self.handler("/plugins/packing/appearance-asset", headers=self.auth())
            h.do_GET()
            self.assertEqual(self.last(h)[0], 404, bad)
            self.assertEqual(h.wfile.getvalue(), b"", f"no body leak for {bad!r}")

    # ---------- 4. 鉴权 ----------

    def test_auth_fail_closed(self):
        (self.assets_dir / "wallpaper.png").write_bytes(PNG_BYTES)
        self.write_user_settings({"bgUri": "/appearance-assets/wallpaper.png"})

        for path in ("/plugins/packing/appearance", "/plugins/packing/appearance-asset"):
            h = self.handler(path)
            h.do_GET()
            self.assertEqual(self.last(h)[0], 401, path)
            self.assertEqual(h.wfile.getvalue(), b"", path)

            h = self.handler(path, headers={"X-Auth-Token": "wrong-secret"})
            h.do_GET()
            self.assertEqual(self.last(h)[0], 401, path)

        # scoped token 只配碰 data/*, 对外观端点无效
        token, _ = self.store.mint_scoped_token("packing", SECRET)
        h = self.handler("/plugins/packing/appearance", headers={"X-Plugin-Token": token})
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

        # 未知插件 404
        h = self.handler("/plugins/nonexistent/appearance", headers=self.auth())
        h.do_GET()
        self.assertEqual(self.last(h)[0], 404)
        h = self.handler("/plugins/nonexistent/appearance-asset", headers=self.auth())
        h.do_GET()
        self.assertEqual(self.last(h)[0], 404)

    # ---------- 5. 旧版设置文件回落 ----------

    def test_fallback_to_appearance_settings_file(self):
        (self.assets_dir / "wallpaper.png").write_bytes(PNG_BYTES)
        # user_settings 无 appearance 字段
        self.user_settings_path.write_text(json.dumps({"packing_list": {}}), encoding="utf-8")
        self.appearance_settings_path.write_text(
            json.dumps({"bgUri": "/appearance-assets/wallpaper.png"}), encoding="utf-8")

        h = self.handler("/plugins/packing/appearance", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 200)
        self.assertEqual(payload["bg_url"], "/plugins/packing/appearance-asset")


if __name__ == "__main__":
    unittest.main()
