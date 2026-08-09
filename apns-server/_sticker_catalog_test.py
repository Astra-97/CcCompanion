import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from sticker_catalog import (
    STICKER_CATALOG_USER_AGENT,
    StickerCatalogService,
    is_valid_sticker_name,
)


class StickerCatalogTests(unittest.TestCase):
    def _manifest(self, root: Path, payload: object) -> Path:
        path = root / "stickers.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_catalog_derives_urls_and_deduplicates_exact_names(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {"stickers": [
                {"name": "爱", "file": "爱.gif"},
                {"name": "爱", "file": "爱.png"},
                {"name": "发呆", "file": "发呆.png"},
            ]})
            service = StickerCatalogService({"enabled": True, "cache_seconds": 15, "sources": [{
                "manifest_path": str(manifest),
                "public_base_url": "https://assets.example/stickers/",
            }]})
            catalog = service.snapshot()
            self.assertEqual(["爱", "发呆"], [entry["name"] for entry in catalog["stickers"]])
            self.assertEqual("https://assets.example/stickers/%E7%88%B1.gif", catalog["stickers"][0]["url"])
            self.assertNotEqual("disabled", catalog["version"])

    def test_rejects_manifest_urls_paths_and_name_mismatch(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {"stickers": [
                {"name": "ok", "file": "../secret.png"},
                {"name": "ok", "file": "other.png"},
                {"name": "ok", "file": "ok.svg"},
                {"name": "url", "file": "url.png", "url": "https://evil.invalid/a.png"},
                {"name": "safe", "file": "safe.webp"},
            ]})
            service = StickerCatalogService({"enabled": True, "sources": [{
                "manifest_path": str(manifest),
                "public_base_url": "https://assets.example/stickers",
            }]})
            self.assertEqual([{"name": "url", "url": "https://assets.example/stickers/url.png"}, {"name": "safe", "url": "https://assets.example/stickers/safe.webp"}], service.snapshot()["stickers"])

    def test_invalid_or_unknown_configuration_fails_closed(self):
        self.assertFalse(is_valid_sticker_name(" two"))
        self.assertTrue(is_valid_sticker_name("happy new year"))
        self.assertFalse(is_valid_sticker_name("two\nwords"))
        self.assertFalse(is_valid_sticker_name("a/b"))
        self.assertFalse(is_valid_sticker_name("[x]"))
        self.assertTrue(is_valid_sticker_name("(扭来扭去)想玩"))
        service = StickerCatalogService({"enabled": True, "sources": [{
            "manifest_path": "/missing.json", "public_base_url": "http://not-https.example"
        }]})
        self.assertEqual([], service.snapshot()["stickers"])

    def test_remote_manifest_uses_fixed_non_secret_user_agent(self):
        response = MagicMock()
        response.read.return_value = '{"stickers":[{"name":"爱","file":"爱.gif"}]}'.encode("utf-8")
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        service = StickerCatalogService({"enabled": True, "sources": [{
            "manifest_url": "https://assets.example/stickers/catalog.json",
            "public_base_url": "https://assets.example/stickers",
        }]})
        with patch("sticker_catalog.build_opener", return_value=opener):
            self.assertEqual(["爱"], [item["name"] for item in service.snapshot()["stickers"]])
        request = opener.open.call_args.args[0]
        self.assertEqual(STICKER_CATALOG_USER_AGENT, request.get_header("User-agent"))
        self.assertNotIn("token", request.header_items().__repr__().lower())
        self.assertNotIn("secret", request.header_items().__repr__().lower())


if __name__ == "__main__":
    unittest.main()
