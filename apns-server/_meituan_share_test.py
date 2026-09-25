from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import link_preview
from link_preview import (
    HTTPPayload,
    LinkPreviewError,
    LinkPreviewService,
    MEITUAN_CACHE_SCHEMA_VERSION,
    merge_preview_metadata,
    parse_meituan_share_text,
)


SHARE_TEXT = (
    "暖山静舍·SPA足道连锁（北站北店） 暖山静舍·SPA足道连锁（北站北店）,"
    "¥170/人,皇姑区,皇姑区昆山中路5号 http://dpurl.cn/uxoFqcyz"
)
CANONICAL = (
    "https://www.meituan.com/shop/1012483291680209.html"
    "?utm_source=appshare&utm_term=tracking"
)


class StubFetcher:
    """只模拟 _resolve_meituan_shop 需要的一次跳转结果。"""

    def __init__(self, final_url: str = "", fail: bool = False):
        self.final_url = final_url
        self.fail = fail
        self.calls = 0

    def request(self, url, **kwargs):
        self.calls += 1
        if self.fail:
            raise LinkPreviewError("simulated resolution failure")
        return HTTPPayload(
            url=self.final_url or url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html><title>shop</title></html>",
        )


def make_service(td: str, fetcher) -> LinkPreviewService:
    return LinkPreviewService(td, fetcher=fetcher)


class ParseMeituanShareTextTest(unittest.TestCase):
    def test_full_dianping_share(self):
        fields = parse_meituan_share_text(SHARE_TEXT)
        self.assertEqual(fields["name"], "暖山静舍·SPA足道连锁（北站北店）")
        self.assertEqual(fields["avg_price"], "¥170/人")
        self.assertEqual(fields["region"], "皇姑区")
        self.assertEqual(fields["address"], "皇姑区昆山中路5号")

    def test_name_with_address_like_word_stays_name(self):
        # 「足道」「中心」等字不得把店名挤到地址位。
        fields = parse_meituan_share_text(
            "舒心体检中心,¥300/人,和平区,和平区太原街16号 http://dpurl.cn/abc"
        )
        self.assertEqual(fields["name"], "舒心体检中心")
        self.assertEqual(fields["address"], "和平区太原街16号")

    def test_missing_price(self):
        fields = parse_meituan_share_text(
            "某店名,朝阳区,朝阳区建国路88号 https://www.meituan.com/shop/12345.html"
        )
        self.assertEqual(fields["name"], "某店名")
        self.assertEqual(fields["avg_price"], "")
        self.assertEqual(fields["region"], "朝阳区")
        self.assertEqual(fields["address"], "朝阳区建国路88号")

    def test_missing_name_keeps_empty(self):
        fields = parse_meituan_share_text(
            "皇姑区,¥170/人,皇姑区昆山中路5号 http://dpurl.cn/abc"
        )
        self.assertEqual(fields["name"], "")
        self.assertEqual(fields["region"], "皇姑区")
        self.assertEqual(fields["address"], "皇姑区昆山中路5号")

    def test_only_name(self):
        fields = parse_meituan_share_text("只有店名 http://dpurl.cn/abc")
        self.assertEqual(fields["name"], "只有店名")
        self.assertEqual(fields["avg_price"], "")
        self.assertEqual(fields["region"], "")
        self.assertEqual(fields["address"], "")

    def test_alt_price_form(self):
        fields = parse_meituan_share_text(
            "店名,人均170元,海淀区中关村大街1号 http://dpurl.cn/abc"
        )
        self.assertEqual(fields["avg_price"], "¥170/人")

    def test_newline_separated_share(self):
        fields = parse_meituan_share_text(
            "店名\n店名,¥50/人,铁西区,铁西区建设大路2号 http://dpurl.cn/abc"
        )
        self.assertEqual(fields["name"], "店名")
        self.assertEqual(fields["region"], "铁西区")
        self.assertEqual(fields["address"], "铁西区建设大路2号")

    def test_empty_text(self):
        fields = parse_meituan_share_text("")
        self.assertFalse(any(fields.values()))


class MeituanUrlTest(unittest.TestCase):
    def test_shop_id_from_url(self):
        self.assertEqual(
            link_preview._meituan_shop_id_from_url(
                "https://www.meituan.com/shop/1012483291680209.html?utm_source=x"
            ),
            "1012483291680209",
        )
        self.assertEqual(
            link_preview._meituan_shop_id_from_url("https://www.dianping.com/shop/987654"),
            "987654",
        )
        self.assertEqual(link_preview._meituan_shop_id_from_url("https://www.meituan.com/"), "")
        self.assertEqual(link_preview._meituan_shop_id_from_url("not a url"), "")

    def test_host_detection(self):
        service = LinkPreviewService
        self.assertTrue(service._is_meituan("http://dpurl.cn/uxoFqcyz"))
        self.assertTrue(service._is_meituan("https://www.meituan.com/shop/1.html"))
        self.assertTrue(service._is_meituan("https://m.dianping.com/shop/1"))
        self.assertTrue(service._is_meituan("https://sh.meituan.com/shop/1.html"))
        self.assertFalse(service._is_meituan("https://meituan.com.evil.com/shop/1"))
        self.assertFalse(service._is_meituan("https://example.com/"))

    def test_share_link_detection(self):
        service = LinkPreviewService
        self.assertTrue(service._is_meituan_share_link("http://dpurl.cn/uxoFqcyz"))
        self.assertTrue(service._is_meituan_share_link("https://www.meituan.com/shop/123.html"))
        self.assertTrue(service._is_meituan_share_link("https://www.dianping.com/shop/456"))
        # 非门店的美团链接仍走通用抓取，不被分享解析截胡。
        self.assertFalse(service._is_meituan_share_link("https://www.meituan.com/"))
        self.assertFalse(service._is_meituan_share_link("https://example.com/shop/1"))


class FetchMeituanPageTest(unittest.TestCase):
    def test_resolved_share_builds_full_card(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(final_url=CANONICAL))
            page = service._fetch_meituan_page(
                "http://dpurl.cn/uxoFqcyz", deadline=self._deadline(), share_text=SHARE_TEXT
            )
        self.assertEqual(page.title, "暖山静舍·SPA足道连锁（北站北店）")
        self.assertEqual(page.description, "¥170/人 · 皇姑区 · 皇姑区昆山中路5号")
        self.assertEqual(page.site_name, "美团·大众点评")
        self.assertEqual(page.final_url, "https://www.meituan.com/shop/1012483291680209.html")
        self.assertEqual(page.provider, "meituan-share")
        self.assertIn("门店 ID：1012483291680209", page.body_text)
        self.assertIn("地址：皇姑区昆山中路5号", page.body_text)

    def test_resolution_failure_degrades_to_text_only(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(fail=True))
            page = service._fetch_meituan_page(
                "http://dpurl.cn/uxoFqcyz", deadline=self._deadline(), share_text=SHARE_TEXT
            )
        self.assertEqual(page.title, "暖山静舍·SPA足道连锁（北站北店）")
        self.assertEqual(page.final_url, "http://dpurl.cn/uxoFqcyz")
        self.assertNotIn("门店 ID", page.body_text)

    def test_bare_shop_link_without_fields(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(final_url=CANONICAL))
            page = service._fetch_meituan_page(
                "https://www.meituan.com/shop/1012483291680209.html",
                deadline=self._deadline(),
                share_text="https://www.meituan.com/shop/1012483291680209.html",
            )
        self.assertEqual(page.title, "美团门店")
        self.assertEqual(page.description, "")
        self.assertIn("门店 ID：1012483291680209", page.body_text)

    def test_redirect_off_meituan_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(final_url="https://evil.example.com/phish"))
            with self.assertRaises(LinkPreviewError):
                service._fetch_meituan_page(
                    "http://dpurl.cn/uxoFqcyz", deadline=self._deadline(), share_text=""
                )

    def test_poi_redirect_canonicalizes_back_to_shop_url(self):
        # 移动 UA 下 www.meituan.com/shop/<id>.html 常再跳到 i.meituan.com/poi/<id>。
        with tempfile.TemporaryDirectory() as td:
            service = make_service(
                td, StubFetcher(final_url="http://i.meituan.com/poi/1012483291680209")
            )
            page = service._fetch_meituan_page(
                "http://dpurl.cn/uxoFqcyz", deadline=self._deadline(), share_text=SHARE_TEXT
            )
        self.assertEqual(page.final_url, "https://www.meituan.com/shop/1012483291680209.html")
        self.assertIn("门店 ID：1012483291680209", page.body_text)

    def test_dianping_host_canonicalizes_to_dianping(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(
                td, StubFetcher(final_url="https://m.dianping.com/shop/987654")
            )
            page = service._fetch_meituan_page(
                "https://www.dianping.com/shop/987654",
                deadline=self._deadline(),
                share_text="某店名 https://www.dianping.com/shop/987654",
            )
        self.assertEqual(page.final_url, "https://www.dianping.com/shop/987654")
        self.assertEqual(page.title, "某店名")

    def test_no_fields_and_no_resolution_raises(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(fail=True))
            with self.assertRaises(LinkPreviewError):
                service._fetch_meituan_page(
                    "http://dpurl.cn/uxoFqcyz", deadline=self._deadline(), share_text=""
                )

    @staticmethod
    def _deadline() -> float:
        import time

        return time.monotonic() + 30.0


class EnrichMeituanTest(unittest.TestCase):
    def test_enrich_produces_preview_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            fetcher = StubFetcher(final_url=CANONICAL)
            service = make_service(td, fetcher)
            bundle = service.enrich(SHARE_TEXT)
            self.assertEqual(len(bundle.previews), 1)
            preview = bundle.previews[0]
            self.assertEqual(preview["site_name"], "美团·大众点评")
            self.assertEqual(preview["title"], "暖山静舍·SPA足道连锁（北站北店）")
            self.assertEqual(preview["description"], "¥170/人 · 皇姑区 · 皇姑区昆山中路5号")
            self.assertEqual(
                preview["final_url"], "https://www.meituan.com/shop/1012483291680209.html"
            )
            self.assertEqual(preview["schema_version"], MEITUAN_CACHE_SCHEMA_VERSION)
            self.assertEqual(preview["comments_status"], "not_applicable")
            metadata = merge_preview_metadata(None, bundle)
            self.assertEqual(len(metadata["link_previews"]), 1)
            self.assertTrue(bundle.prompt_context)

    def test_enrich_cache_hit_skips_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            fetcher = StubFetcher(final_url=CANONICAL)
            service = make_service(td, fetcher)
            first = service.enrich(SHARE_TEXT)
            self.assertEqual(len(first.previews), 1)
            calls_after_first = fetcher.calls
            second = service.enrich(SHARE_TEXT)
            self.assertEqual(len(second.previews), 1)
            self.assertEqual(fetcher.calls, calls_after_first)
            self.assertEqual(second.previews[0]["title"], first.previews[0]["title"])

    def test_resolution_failure_still_enriches_from_text(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(fail=True))
            bundle = service.enrich(SHARE_TEXT)
            self.assertEqual(len(bundle.previews), 1)
            self.assertEqual(bundle.previews[0]["title"], "暖山静舍·SPA足道连锁（北站北店）")
            self.assertEqual(bundle.previews[0]["final_url"], "http://dpurl.cn/uxoFqcyz")

    def test_unrelated_text_yields_no_preview(self):
        with tempfile.TemporaryDirectory() as td:
            service = make_service(td, StubFetcher(final_url=CANONICAL))
            bundle = service.enrich("今天天气不错，没有链接。")
            self.assertEqual(bundle.previews, ())


if __name__ == "__main__":
    unittest.main()
