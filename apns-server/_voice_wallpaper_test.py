#!/usr/bin/env python3
"""Custom call wallpaper store + endpoints (2026-10-21)."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import push
import voice_wallpaper as vw

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64


class StoreTest(unittest.TestCase):
    def test_sniff(self) -> None:
        self.assertEqual(vw.sniff_image_ext(JPEG), "jpg")
        self.assertEqual(vw.sniff_image_ext(PNG), "png")
        self.assertEqual(vw.sniff_image_ext(WEBP), "webp")
        self.assertIsNone(vw.sniff_image_ext(b"GIF89a" + b"\x00" * 10))
        self.assertIsNone(vw.sniff_image_ext(b"<svg"))

    def test_empty_config_then_save_replace_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = vw.WallpaperStore(Path(tmp) / "voice-wallpaper")
            self.assertEqual(store.config(), {"ok": True, "sha256": "", "url": "", "dim": vw.DEFAULT_DIM})
            first = store.save(JPEG)
            self.assertEqual(len(first["sha256"]), 16)
            self.assertEqual(first["url"], "/voice-call/wallpaper")
            second = store.save(PNG)
            self.assertNotEqual(first["sha256"], second["sha256"])
            files = sorted(p.name for p in store.directory.iterdir())
            self.assertEqual(files, ["wallpaper.png"])  # old jpg + temp files gone
            self.assertEqual(store.clear()["sha256"], "")
            self.assertIsNone(store.current_file())

    def test_rejects_bad_input_without_touching_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = vw.WallpaperStore(Path(tmp))
            store.save(JPEG)
            sha = store.sha()
            for bad, code in ((b"", "empty"), (b"hello", "unsupported_media_type"),
                              (JPEG + b"\x00" * vw.MAX_BYTES, "too_large")):
                with self.assertRaises(ValueError) as ctx:
                    store.save(bad)
                self.assertEqual(str(ctx.exception), code)
            self.assertEqual(store.sha(), sha)

    def test_dim_is_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = vw.WallpaperStore(Path(tmp))
            Path(tmp, "config.json").write_text('{"dim": 5}', encoding="utf-8")
            self.assertEqual(store.dim(), 0.9)
            Path(tmp, "config.json").write_text('{"dim": "x"}', encoding="utf-8")
            self.assertEqual(store.dim(), vw.DEFAULT_DIM)


class _Headers(dict):
    def get(self, key, default=None):  # case-insensitive enough for tests
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class EndpointTest(unittest.TestCase):
    def _handler(self, body: bytes = b"", authed: bool = True, path: str = "/voice-call/wallpaper"):
        handler = object.__new__(push.PushHandler)
        handler.path = path
        handler.headers = _Headers({"Content-Length": str(len(body))})
        handler.rfile = io.BytesIO(body)
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._native_pairing_auth_matches = lambda: authed
        return handler

    def test_upload_requires_native_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(push.PushHandler, "_VOICE_WALLPAPER_DIR", Path(tmp)):
            h = self._handler(JPEG, authed=False)
            h._handle_voice_wallpaper_upload()
            self.assertEqual(h.responses[-1][0], 401)
            self.assertIsNone(vw.WallpaperStore(Path(tmp)).current_file())

    def test_upload_and_bad_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(push.PushHandler, "_VOICE_WALLPAPER_DIR", Path(tmp)):
            h = self._handler(PNG)
            h._handle_voice_wallpaper_upload()
            status, body = h.responses[-1]
            self.assertEqual(status, 200)
            self.assertTrue(body["sha256"])
            h = self._handler(b"not an image at all")
            h._handle_voice_wallpaper_upload()
            self.assertEqual(h.responses[-1][0], 415)
            self.assertEqual(vw.WallpaperStore(Path(tmp)).current_file().name, "wallpaper.png")


if __name__ == "__main__":
    unittest.main()
