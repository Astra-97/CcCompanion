import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from sticker_catalog import (
    STICKER_CATALOG_USER_AGENT,
    StickerCatalogService,
    is_valid_category_id,
    is_valid_sticker_name,
)


class StickerCatalogTests(unittest.TestCase):
    def _manifest(self, root: Path, payload: object) -> Path:
        root.mkdir(parents=True, exist_ok=True)
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

    def test_optional_label_is_safe_display_data_not_a_token_or_url(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {"stickers": [
                {"name": "哥哥熊·抱抱·2", "file": "哥哥熊·抱抱·2.gif", "label": "抱抱"},
                {"name": "坏标签", "file": "坏标签.gif", "label": "[bqb:注入]"},
                {"name": "同名", "file": "同名.gif", "label": "同名"},
            ]})
            service = StickerCatalogService({"enabled": True, "sources": [{
                "manifest_path": str(manifest),
                "public_base_url": "https://assets.example/stickers",
            }]})
            entries = service.snapshot()["stickers"]
            self.assertEqual("哥哥熊·抱抱·2", entries[0]["name"])
            self.assertEqual("抱抱", entries[0]["label"])
            self.assertEqual(
                "https://assets.example/stickers/%E5%93%A5%E5%93%A5%E7%86%8A%C2%B7%E6%8A%B1%E6%8A%B1%C2%B72.gif",
                entries[0]["url"],
            )
            self.assertNotIn("label", entries[1])
            self.assertNotIn("label", entries[2])

    def test_invalid_or_unknown_configuration_fails_closed(self):
        self.assertFalse(is_valid_sticker_name(" two"))
        self.assertTrue(is_valid_sticker_name("happy new year"))
        self.assertFalse(is_valid_sticker_name("two\nwords"))
        self.assertFalse(is_valid_sticker_name("a/b"))
        self.assertFalse(is_valid_sticker_name("[x]"))
        self.assertTrue(is_valid_sticker_name("(扭来扭去)想玩"))
        self.assertTrue(is_valid_category_id("xiaodou_2"))
        self.assertFalse(is_valid_category_id("哥哥熊"))
        self.assertFalse(is_valid_category_id("-xiaodou"))
        self.assertFalse(is_valid_category_id("xiaodou-"))
        service = StickerCatalogService({"enabled": True, "sources": [{
            "manifest_path": "/missing.json", "public_base_url": "http://not-https.example"
        }]})
        self.assertEqual([], service.snapshot()["stickers"])

    def test_categories_are_server_driven_and_source_config_wins(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {
                "category": {"id": "manifest-bear", "name": "哥哥熊"},
                "stickers": [{"name": "爱", "file": "爱.gif"}],
            })
            service = StickerCatalogService({"enabled": True, "sources": [{
                "manifest_path": str(manifest),
                "public_base_url": "https://assets.example/stickers",
                "category_id": "configured-bear",
                "category_name": "自嘲熊",
            }]})
            catalog = service.snapshot()
            self.assertEqual([{"id": "configured-bear", "name": "自嘲熊"}], catalog["categories"])
            self.assertEqual("configured-bear", catalog["stickers"][0]["category_id"])

    def test_manifest_category_and_legacy_manifest_are_compatible(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            categorized = self._manifest(root, {
                "category": {"id": "brother-bear", "name": "哥哥熊"},
                "stickers": [{"name": "哥哥", "file": "哥哥.gif"}],
            })
            legacy = self._manifest(root / "legacy", {"stickers": [
                {"name": "爱", "file": "爱.gif"},
            ]})
            service = StickerCatalogService({"enabled": True, "sources": [
                {"manifest_path": str(categorized), "public_base_url": "https://assets.example/bear"},
                {"manifest_path": str(legacy), "public_base_url": "https://assets.example/legacy"},
            ]})
            catalog = service.snapshot()
            self.assertEqual([{"id": "brother-bear", "name": "哥哥熊"}], catalog["categories"])
            self.assertEqual("brother-bear", catalog["stickers"][0]["category_id"])
            self.assertNotIn("category_id", catalog["stickers"][1])

    def test_aggregate_manifest_uses_only_declared_matching_categories(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {
                "categories": [{"id": "cats", "name": "猫猫"}],
                "stickers": [
                    {"name": "团团", "file": "团团.png", "category_id": "cats"},
                    {"name": "无类", "file": "无类.png", "category_id": "unknown"},
                ],
            })
            service = StickerCatalogService({"enabled": True, "sources": [{
                "manifest_path": str(manifest), "public_base_url": "https://assets.example/user-stickers",
            }]})
            catalog = service.snapshot()
            self.assertEqual([{"id": "cats", "name": "猫猫"}], catalog["categories"])
            self.assertEqual("cats", catalog["stickers"][0]["category_id"])
            self.assertNotIn("category_id", catalog["stickers"][1])

    def test_invalid_config_category_cannot_fall_back_to_manifest_metadata(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {
                "category": {"id": "brother-bear", "name": "哥哥熊"},
                "stickers": [{"name": "爱", "file": "爱.gif"}],
            })
            service = StickerCatalogService({"enabled": True, "sources": [{
                "manifest_path": str(manifest),
                "public_base_url": "https://assets.example/stickers",
                "category_id": "Not-safe",
                "category_name": "哥哥熊",
            }]})
            catalog = service.snapshot()
            self.assertEqual([], catalog["categories"])
            self.assertNotIn("category_id", catalog["stickers"][0])

    def test_category_is_included_in_fingerprint_and_duplicate_names_stay_first_wins(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._manifest(root / "first", {"stickers": [
                {"name": "爱", "file": "爱.gif"},
            ]})
            second = self._manifest(root / "second", {"stickers": [
                {"name": "爱", "file": "爱.png"},
            ]})
            base = {
                "enabled": True,
                "sources": [
                    {
                        "manifest_path": str(first),
                        "public_base_url": "https://assets.example/first",
                        "category_id": "first", "category_name": "第一类",
                    },
                    {
                        "manifest_path": str(second),
                        "public_base_url": "https://assets.example/second",
                        "category_id": "second", "category_name": "第二类",
                    },
                ],
            }
            catalog = StickerCatalogService(base).snapshot()
            self.assertEqual(["爱"], [entry["name"] for entry in catalog["stickers"]])
            self.assertEqual("first", catalog["stickers"][0]["category_id"])
            changed = {**base, "sources": [
                {**base["sources"][0], "category_name": "第一组"},
                base["sources"][1],
            ]}
            self.assertNotEqual(catalog["version"], StickerCatalogService(changed).snapshot()["version"])

    def test_conflicting_category_name_fails_closed_without_hiding_sticker(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._manifest(root / "first", {"stickers": [
                {"name": "第一张", "file": "第一张.gif"},
            ]})
            second = self._manifest(root / "second", {"stickers": [
                {"name": "第二张", "file": "第二张.gif"},
            ]})
            service = StickerCatalogService({"enabled": True, "sources": [
                {
                    "manifest_path": str(first),
                    "public_base_url": "https://assets.example/first",
                    "category_id": "shared", "category_name": "第一类",
                },
                {
                    "manifest_path": str(second),
                    "public_base_url": "https://assets.example/second",
                    "category_id": "shared", "category_name": "第二类",
                },
            ]})
            catalog = service.snapshot()
            self.assertEqual([{"id": "shared", "name": "第一类"}], catalog["categories"])
            self.assertEqual("shared", catalog["stickers"][0]["category_id"])
            self.assertEqual("第二张", catalog["stickers"][1]["name"])
            self.assertNotIn("category_id", catalog["stickers"][1])

    def test_matching_category_id_and_name_merge_across_sources(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._manifest(root / "first", {"stickers": [
                {"name": "第一张", "file": "第一张.gif"},
            ]})
            second = self._manifest(root / "second", {"stickers": [
                {"name": "第二张", "file": "第二张.gif"},
            ]})
            common = {"category_id": "shared", "category_name": "同一类"}
            service = StickerCatalogService({"enabled": True, "sources": [
                {
                    "manifest_path": str(first),
                    "public_base_url": "https://assets.example/first",
                    **common,
                },
                {
                    "manifest_path": str(second),
                    "public_base_url": "https://assets.example/second",
                    **common,
                },
            ]})
            catalog = service.snapshot()
            self.assertEqual([{"id": "shared", "name": "同一类"}], catalog["categories"])
            self.assertEqual(["shared", "shared"], [
                item.get("category_id") for item in catalog["stickers"]
            ])

    def test_category_with_no_valid_sticker_does_not_claim_id(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = self._manifest(root / "empty", {"stickers": [
                {"name": "损坏", "file": "不匹配.gif"},
            ]})
            valid = self._manifest(root / "valid", {"stickers": [
                {"name": "有效", "file": "有效.gif"},
            ]})
            service = StickerCatalogService({"enabled": True, "sources": [
                {
                    "manifest_path": str(empty),
                    "public_base_url": "https://assets.example/empty",
                    "category_id": "shared", "category_name": "空类别",
                },
                {
                    "manifest_path": str(valid),
                    "public_base_url": "https://assets.example/valid",
                    "category_id": "shared", "category_name": "有效类别",
                },
            ]})
            catalog = service.snapshot()
            self.assertEqual([{"id": "shared", "name": "有效类别"}], catalog["categories"])
            self.assertEqual("shared", catalog["stickers"][0]["category_id"])

    def test_max_items_does_not_expose_unreachable_categories(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._manifest(root / "first", {"stickers": [
                {"name": "第一张", "file": "第一张.gif"},
            ]})
            second = self._manifest(root / "second", {"stickers": [
                {"name": "第二张", "file": "第二张.gif"},
            ]})
            service = StickerCatalogService({"enabled": True, "max_items": 1, "sources": [
                {
                    "manifest_path": str(first),
                    "public_base_url": "https://assets.example/first",
                    "category_id": "first", "category_name": "第一类",
                },
                {
                    "manifest_path": str(second),
                    "public_base_url": "https://assets.example/second",
                    "category_id": "second", "category_name": "第二类",
                },
            ]})
            catalog = service.snapshot()
            self.assertEqual(["第一张"], [item["name"] for item in catalog["stickers"]])
            self.assertEqual([{"id": "first", "name": "第一类"}], catalog["categories"])

    def test_label_is_included_in_fingerprint(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {"stickers": [
                {"name": "哥哥熊·抱抱·2", "file": "哥哥熊·抱抱·2.gif", "label": "抱抱"},
            ]})
            config = {"enabled": True, "sources": [{
                "manifest_path": str(manifest),
                "public_base_url": "https://assets.example/stickers",
            }]}
            before = StickerCatalogService(config).snapshot()["version"]
            self._manifest(Path(tmp), {"stickers": [
                {"name": "哥哥熊·抱抱·2", "file": "哥哥熊·抱抱·2.gif", "label": "贴贴"},
            ]})
            after = StickerCatalogService(config).snapshot()["version"]
            self.assertNotEqual(before, after)

    def test_invalidate_rebuilds_even_when_monotonic_is_below_cache_ttl(self):
        with TemporaryDirectory() as tmp:
            manifest = self._manifest(Path(tmp), {"stickers": [{"name": "旧", "file": "旧.png"}]})
            service = StickerCatalogService({"enabled": True, "cache_seconds": 300, "sources": [{
                "manifest_path": str(manifest), "public_base_url": "https://assets.example/stickers",
            }]})
            with patch("sticker_catalog.time.monotonic", return_value=100.0):
                self.assertEqual(["旧"], [item["name"] for item in service.snapshot()["stickers"]])
                self._manifest(Path(tmp), {"stickers": [{"name": "新", "file": "新.png"}]})
                service.invalidate()
                self.assertEqual(["新"], [item["name"] for item in service.snapshot()["stickers"]])

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
