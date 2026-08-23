from __future__ import annotations

import json
import io
import os
import gzip
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import link_preview
import push


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = list((headers or {}).items())
        self._body = body
        self._offset = 0

    def getheaders(self):
        return self._headers

    def read(self, amount):
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class FakeSocket:
    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        pass


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = FakeSocket()
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class QueueFetcher:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class LinkPreviewTests(unittest.TestCase):
    def test_detect_urls_supports_short_links_dedupes_and_limits(self):
        text = "看 https://b23.tv/AbC，和 https://xhslink.com/a1。再发 https://b23.tv/AbC https://e.test/x"
        self.assertEqual(
            link_preview.detect_urls(text, limit=2),
            ["https://b23.tv/AbC", "https://xhslink.com/a1"],
        )

    def test_url_limit_is_hard_capped_at_three(self):
        text = " ".join(f"https://example.com/{i}" for i in range(10))
        self.assertEqual(len(link_preview.detect_urls(text, limit=999)), 3)
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, max_urls=999)
            self.assertEqual(service.max_urls, 3)

    def test_total_timeout_supports_complete_multi_image_notes_but_stays_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                link_preview.LinkPreviewService(td, total_timeout=999).total_timeout,
                30.0,
            )

    def test_ssrf_blocks_literal_and_resolved_private_addresses(self):
        resolver = lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ]
        fetcher = link_preview.SafeHTTPFetcher(resolver=resolver)
        with self.assertRaises(link_preview.UnsafeAddressError):
            fetcher._resolve_public("example.test", 80)
        self.assertFalse(link_preview._is_public_ip("169.254.169.254"))
        self.assertFalse(link_preview._is_public_ip("10.0.0.1"))
        self.assertFalse(link_preview._is_public_ip("::1"))

    def test_explicit_adapter_may_use_local_or_tailscale_but_not_metadata(self):
        answers = {
            "adapter.local": "127.0.0.1",
            "windows.tail": "100.99.30.89",
            "metadata.local": "169.254.169.254",
        }
        resolver = lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))
        ]
        fetcher = link_preview.SafeHTTPFetcher(
            resolver=resolver,
            trusted_hosts={"adapter.local", "windows.tail", "metadata.local"},
        )
        self.assertEqual(fetcher._resolve_public("adapter.local", 80), ["127.0.0.1"])
        self.assertEqual(fetcher._resolve_public("windows.tail", 80), ["100.99.30.89"])
        with self.assertRaises(link_preview.UnsafeAddressError):
            fetcher._resolve_public("metadata.local", 80)

    def test_embedded_ipv4_ssrf_forms_are_rejected_for_user_and_adapter(self):
        blocked = [
            "::ffff:169.254.169.254",
            "::ffff:127.0.0.1",
            "::ffff:100.100.100.200",
            "64:ff9b::6464:64c8",
            "64:ff9b::a00:1",
            "64:ff9b:1::c0a8:1",
            "2002:0a00:0001::1",
            "2001:0000:4136:e378:8000:63bf:f5ff:fffe",
        ]
        for address in blocked:
            with self.subTest(address=address):
                self.assertFalse(link_preview._is_public_ip(address))
                self.assertFalse(link_preview._is_trusted_adapter_ip(address))
        self.assertFalse(link_preview._is_trusted_adapter_ip("100.100.100.200"))

    def test_dns_resolution_obeys_deadline_and_has_bounded_capacity(self):
        pool = link_preview._BoundedDNSPool(workers=1, queue_size=2)
        release = threading.Event()

        def stuck(host, port, type):
            release.wait(0.4)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        fetcher = link_preview.SafeHTTPFetcher(resolver=stuck, dns_pool=pool)
        started = time.monotonic()
        with self.assertRaises(link_preview.LinkPreviewError):
            fetcher._resolve_public("slow.example", 80, time.monotonic() + 0.03)
        self.assertLess(time.monotonic() - started, 0.15)
        for index in range(12):
            with self.assertRaises(link_preview.LinkPreviewError):
                fetcher._resolve_public(f"slow-{index}.example", 80, time.monotonic() + 0.005)
            self.assertLessEqual(pool.outstanding(), pool.capacity)
            self.assertEqual(len(pool._threads), pool.workers)
        release.set()

    def test_dns_result_is_pinned_into_socket_factory(self):
        resolved = "93.184.216.34"
        resolver = lambda host, port, type: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, port))
        ]
        calls = []

        def socket_factory(address, timeout):
            calls.append((address, timeout))
            return FakeSocket()

        fetcher = link_preview.SafeHTTPFetcher(resolver=resolver, socket_factory=socket_factory)
        conn = fetcher._connect("http", "example.com", 80, time.monotonic() + 1)
        self.assertEqual(calls[0][0], (resolved, 80))
        self.assertIsNotNone(conn.sock)

    def test_redirect_revalidates_destination_and_blocks_private_hop(self):
        fetcher = link_preview.SafeHTTPFetcher()
        first = FakeConnection(FakeResponse(302, {"Location": "http://127.0.0.1/admin"}))

        def connect(scheme, host, port, deadline):
            if host == "public.example":
                return first
            raise link_preview.UnsafeAddressError("private redirect")

        fetcher._connect = connect
        with self.assertRaises(link_preview.UnsafeAddressError):
            fetcher.request("https://public.example/a", deadline=time.monotonic() + 1)

    def test_allowed_hosts_blocks_cross_origin_redirect_before_connecting(self):
        fetcher = link_preview.SafeHTTPFetcher()
        first = FakeConnection(FakeResponse(302, {"Location": "https://evil.example/recipe"}))
        calls = []

        def connect(scheme, host, port, deadline):
            calls.append(host)
            return first

        fetcher._connect = connect
        with self.assertRaises(link_preview.LinkPreviewError):
            fetcher.request(
                "https://m.xiachufang.com/recipe/1/",
                deadline=time.monotonic() + 1,
                allowed_hosts=link_preview.XIACHUFANG_HOSTS,
            )
        self.assertEqual(calls, ["m.xiachufang.com"])

    def test_allowed_schemes_blocks_https_downgrade_before_connecting(self):
        fetcher = link_preview.SafeHTTPFetcher()
        first = FakeConnection(FakeResponse(302, {"Location": "http://m.xiachufang.com/recipe/1/"}))
        calls = []

        def connect(scheme, host, port, deadline):
            calls.append((scheme, host))
            return first

        fetcher._connect = connect
        with self.assertRaises(link_preview.LinkPreviewError):
            fetcher.request(
                "https://m.xiachufang.com/recipe/1/",
                deadline=time.monotonic() + 1,
                allowed_hosts=link_preview.XIACHUFANG_HOSTS,
                allowed_schemes={"https"},
            )
        self.assertEqual(calls, [("https", "m.xiachufang.com")])

    def test_compression_is_opt_in_and_bounded(self):
        source = b"<html><body>safe compressed response</body></html>"
        compressed = gzip.compress(source)
        fetcher = link_preview.SafeHTTPFetcher(max_download_bytes=1_024)
        fetcher._connect = lambda *args: FakeConnection(FakeResponse(
            200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}, compressed
        ))
        with self.assertRaises(link_preview.LinkPreviewError):
            fetcher.request("https://example.com", deadline=time.monotonic() + 1)
        payload = fetcher.request(
            "https://example.com", deadline=time.monotonic() + 1, allow_compression=True
        )
        self.assertEqual(payload.body, source)

        oversized = gzip.compress(b"x" * 1_025)
        fetcher._connect = lambda *args: FakeConnection(FakeResponse(
            200, {"Content-Encoding": "gzip"}, oversized
        ))
        with self.assertRaises(link_preview.ResponseTooLargeError):
            fetcher.request(
                "https://example.com", deadline=time.monotonic() + 1, allow_compression=True
            )
        for invalid in (compressed[:-4], b"not-gzip"):
            fetcher._connect = lambda *args, body=invalid: FakeConnection(FakeResponse(
                200, {"Content-Encoding": "gzip"}, body
            ))
            with self.subTest(invalid=invalid), self.assertRaises(link_preview.LinkPreviewError):
                fetcher.request(
                    "https://example.com", deadline=time.monotonic() + 1, allow_compression=True
                )

    def test_cross_origin_redirect_drops_adapter_authorization(self):
        fetcher = link_preview.SafeHTTPFetcher()
        first = FakeConnection(FakeResponse(307, {"Location": "https://other.example/preview"}))
        second = FakeConnection(FakeResponse(200, {"Content-Type": "application/json"}, b"{}"))
        fetcher._connect = lambda scheme, host, port, deadline: first if host == "adapter.example" else second
        fetcher.request(
            "https://adapter.example/preview",
            deadline=time.monotonic() + 1,
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        )
        self.assertNotIn("Authorization", second.requests[0][3])
        self.assertEqual(second.requests[0][0], "GET")

    def test_adapter_forbids_redirect_without_sending_body_or_token_to_new_origin(self):
        first = FakeConnection(FakeResponse(307, {"Location": "https://evil.example/steal"}))
        second = FakeConnection(FakeResponse(200, {"Content-Type": "application/json"}, b"{}"))
        fetcher = link_preview.SafeHTTPFetcher()
        calls = []

        def connect(scheme, host, port, deadline):
            calls.append(host)
            return first if host == "adapter.example" else second

        fetcher._connect = connect
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                xhs_api_url="https://adapter.example/preview",
                xhs_api_token="secret",
                adapter_fetcher=fetcher,
            )
            with self.assertRaises(link_preview.LinkPreviewError):
                service._call_adapter(
                    "https://adapter.example/preview",
                    "https://xhslink.com/private",
                    "xhs-api",
                    time.monotonic() + 1,
                )
        self.assertEqual(calls, ["adapter.example"])
        self.assertEqual(first.requests[0][0], "POST")
        self.assertIn(b"xhslink.com", first.requests[0][2])
        self.assertEqual(first.requests[0][3]["Authorization"], "Bearer secret")
        self.assertEqual(second.requests, [])

    def test_total_timeout_and_download_limits(self):
        fetcher = link_preview.SafeHTTPFetcher(max_download_bytes=8_000)
        fetcher._connect = lambda *args: FakeConnection(
            FakeResponse(200, {"Content-Length": "9000"}, b"x")
        )
        with self.assertRaises(link_preview.ResponseTooLargeError):
            fetcher.request("https://public.example", deadline=time.monotonic() + 1)
        with self.assertRaises(link_preview.LinkPreviewError):
            fetcher.request("https://public.example", deadline=time.monotonic() - 0.01)

    def test_streamed_response_limit(self):
        fetcher = link_preview.SafeHTTPFetcher(max_download_bytes=1024)
        fetcher._connect = lambda *args: FakeConnection(FakeResponse(200, {}, b"x" * 1025))
        with self.assertRaises(link_preview.ResponseTooLargeError):
            fetcher.request("https://public.example", deadline=time.monotonic() + 1)

    def test_html_extracts_og_relative_image_and_visible_body(self):
        html = b"""<html><head><meta property='og:title' content='A title'>
        <meta property='og:description' content='A desc'><meta property='og:image' content='/cover.jpg'>
        <script>secret()</script></head><body><article>Hello <b>world</b></article></body></html>"""
        payload = link_preview.HTTPPayload(
            "https://www.bilibili.com/video/BV1", 200, {"content-type": "text/html; charset=utf-8"}, html
        )
        page = link_preview.extract_html_page("https://b23.tv/x", payload, max_text_chars=1000)
        self.assertEqual(page.title, "A title")
        self.assertEqual(page.image_url, "https://www.bilibili.com/cover.jpg")
        self.assertIn("Hello", page.body_text)
        self.assertNotIn("secret", page.body_text)

    def test_wechat_extracts_trusted_article_fields_and_scoped_clean_body(self):
        html = """<html><head>
        <meta property="og:title" content="工信部：AI 产品成为新热点">
        <meta property="og:description" content="人工智能终端不断丰富">
        <meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg">
        <meta property="og:site_name" content="微信公众平台">
        </head><body>
        <a id="js_name">财闻</a>
        <div id="js_content" style="visibility:hidden">
          <p>第一段正文</p><p>第二段正文</p>
          <section><span>互动一下</span></section>
          <a class="normal_text_link mp_article_text_link" href="/s/other">推荐文章一</a>
          <a class="mp_article_text_link" href="/s/another">推荐文章二</a>
          <p style="display: none">隐藏弹窗文案</p>
        </div>
        <div>阅读原文 点赞 在看 页面壳</div></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/article", 200,
            {"content-type": "text/html; charset=UTF-8"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/article", payload, max_text_chars=1000
        )
        self.assertEqual(page.title, "工信部：AI 产品成为新热点")
        self.assertEqual(page.description, "人工智能终端不断丰富")
        self.assertEqual(page.site_name, "财闻")
        self.assertEqual(page.image_url, "https://mmbiz.qpic.cn/cover.jpg")
        self.assertEqual(page.body_text, "第一段正文\n第二段正文")
        self.assertNotIn("页面壳", page.body_text)
        self.assertNotIn("推荐文章", page.body_text)
        self.assertNotIn("隐藏弹窗", page.body_text)

    def test_wechat_derives_description_from_article_body_when_meta_is_empty(self):
        html = b"""<html><head><meta property='og:title' content='Title'></head>
        <body><span id='js_name'>Publisher</span><div id='js_content'>First paragraph<br>Second</div>"""
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/partial", 200,
            {"content-type": "text/html", "x-cc-preview-truncated": "1"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/partial", payload, max_text_chars=1000
        )
        self.assertEqual(page.description, "First paragraph Second")
        self.assertEqual(page.body_text, "First paragraph\nSecond")

    def test_wechat_inline_nodes_stay_in_one_paragraph_and_br_breaks_line(self):
        html = """<html><head><meta property='og:title' content='Inline'></head><body>
        <span id='js_name'>Publisher</span><div id='js_content'>
        <p>这是一句<strong>强调文字</strong>的正常正文</p>
        <p>第一行<br>第二行</p></div></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/inline", 200,
            {"content-type": "text/html"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/inline", payload, max_text_chars=1000
        )
        self.assertEqual(
            page.body_text,
            "这是一句强调文字的正常正文\n第一行\n第二行",
        )

    def test_wechat_terminal_inline_article_link_without_section_label_is_preserved(self):
        html = """<html><head><meta property='og:title' content='Linked'></head><body>
        <div id='js_content'><p>请参阅<a class='mp_article_text_link'>官方文件</a>获取详情</p>
        </div></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/linked", 200,
            {"content-type": "text/html"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/linked", payload, max_text_chars=1000
        )
        self.assertEqual(page.body_text, "请参阅官方文件获取详情")

    def test_wechat_share_content_page_uses_safe_cgi_data_fallback(self):
        html = r"""<html><head>
        <meta property='og:title' content='分享页标题'>
        </head><body><div id='js_base_container'></div>
        <script>
        window.cgiDataNew = {
          nick_name: '暖暖公众号',
          title: '分享页\u6807题',
          content_noencode: '第一行\x0a第二行：it\'s \u4e2d文 \u{1F31F}',
          cdn_url: 'https://mmbiz.qpic.cn/cover.jpg',
          nested: {cdn_url: 'https://evil.example/nested.jpg'}
        };
        </script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/new", 200,
            {"content-type": "text/html; charset=UTF-8"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/new", payload, max_text_chars=1000
        )
        self.assertEqual(page.title, "分享页标题")
        self.assertEqual(page.site_name, "暖暖公众号")
        self.assertEqual(page.body_text, "第一行\n第二行：it's 中文 🌟")
        self.assertEqual(page.image_url, "https://mmbiz.qpic.cn/cover.jpg")
        self.assertNotIn("evil.example", " ".join(page.image_urls))

    def test_wechat_share_content_page_does_not_evaluate_field_expression(self):
        html = b"""<html><head><meta property='og:title' content='Title'></head><body>
        <div id='js_base_container'></div><script>
        window.cgiDataNew = {
          title: 'Title',
          nick_name: 'Publisher',
          content_noencode: (function(){ return 'forged body'; })(),
          cdn_url: 'https://mmbiz.qpic.cn/cover.jpg'
        };</script></body></html>"""
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/expression", 200,
            {"content-type": "text/html"}, html,
        )
        with self.assertRaises(link_preview.LinkPreviewError):
            link_preview.extract_html_page(
                "https://mp.weixin.qq.com/s/expression", payload, max_text_chars=1000
            )

    def test_wechat_share_content_page_rejects_string_plus_runtime_expression(self):
        html = b"""<html><head><meta property='og:title' content='Title'></head><body>
        <script>window.cgiDataNew = {
          title: 'Title',
          content_noencode: 'trusted prefix' + runtimeExpression,
          cdn_url: 'https://mmbiz.qpic.cn/cover.jpg'
        };</script></body></html>"""
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/concatenated", 200,
            {"content-type": "text/html"}, html,
        )
        with self.assertRaises(link_preview.LinkPreviewError):
            link_preview.extract_html_page(
                "https://mp.weixin.qq.com/s/concatenated", payload, max_text_chars=1000
            )

    def test_wechat_share_content_page_fails_closed_on_regex_or_template_unknown_field(self):
        values = ("/}/", "`unsafe ${runtimeExpression}`")
        for value in values:
            html = f"""<html><head><meta property='og:title' content='Title'></head><body>
            <script>window.cgiDataNew = {{
              unknown: {value},
              title: 'Title',
              content_noencode: 'must not be accepted'
            }};</script></body></html>""".encode()
            payload = link_preview.HTTPPayload(
                "https://mp.weixin.qq.com/s/ambiguous", 200,
                {"content-type": "text/html"}, html,
            )
            with self.subTest(value=value), self.assertRaises(link_preview.LinkPreviewError):
                link_preview.extract_html_page(
                    "https://mp.weixin.qq.com/s/ambiguous", payload, max_text_chars=1000
                )

    def test_wechat_share_content_page_rejects_excessive_nesting(self):
        nested = "[" * 257 + "0" + "]" * 257
        html = f"""<html><head><meta property='og:title' content='Title'></head>
        <script>window.cgiDataNew = {{
          unknown: {nested}, title: 'Title', nick_name: 'Publisher',
          content_noencode: 'must not be accepted'
        }};</script></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/deep", 200,
            {"content-type": "text/html"}, html,
        )
        with self.assertRaises(link_preview.LinkPreviewError):
            link_preview.extract_html_page(
                "https://mp.weixin.qq.com/s/deep", payload, max_text_chars=1000
            )

    def test_wechat_share_content_page_rejects_truncated_cgi_object(self):
        html = b"""<html><head><meta property='og:title' content='Title'></head><body>
        <script>window.cgiDataNew = {
          title: 'Title', nick_name: 'Publisher', content_noencode: 'partial body'
        """
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/truncated", 200,
            {"content-type": "text/html", "x-cc-preview-truncated": "1"}, html,
        )
        with self.assertRaises(link_preview.LinkPreviewError):
            link_preview.extract_html_page(
                "https://mp.weixin.qq.com/s/truncated", payload, max_text_chars=1000
            )

    def test_wechat_share_content_page_requires_meta_title_and_body(self):
        cases = (
            b"""<html><script>window.cgiDataNew = {
              title: 'Script title', nick_name: 'Publisher', content_noencode: 'body'
            };</script></html>""",
            b"""<html><head><meta property='og:title' content='Meta title'></head>
            <script>window.cgiDataNew = {
              title: 'Meta title', nick_name: 'Publisher', content_noencode: ''
            };</script></html>""",
            b"""<html><head><meta property='og:title' content='Meta title'></head>
            <script>window.cgiDataNew = {
              title: 'Different title', nick_name: 'Publisher', content_noencode: 'body'
            };</script></html>""",
            b"""<html><head><meta property='og:title' content='Meta title'></head>
            <script>window.cgiDataNew = {
              title: 'Meta title', nick_name: '', content_noencode: 'body'
            };</script></html>""",
        )
        for html in cases:
            payload = link_preview.HTTPPayload(
                "https://mp.weixin.qq.com/s/missing", 200,
                {"content-type": "text/html"}, html,
            )
            with self.subTest(html=html), self.assertRaises(link_preview.LinkPreviewError):
                link_preview.extract_html_page(
                    "https://mp.weixin.qq.com/s/missing", payload, max_text_chars=1000
                )

    def test_wechat_complete_cgi_before_truncated_shell_still_succeeds(self):
        html = b"""<html><head><meta property='og:title' content='Title'></head><body>
        <script>window.cgiDataNew = {
          title: 'Title', nick_name: 'Publisher', content_noencode: 'complete body',
          cdn_url: 'https://mmbiz.qpic.cn/cover.jpg'
        };</script><div class='later-shell'>this trailing shell is cut"""
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/truncated-shell", 200,
            {"content-type": "text/html", "x-cc-preview-truncated": "1"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/truncated-shell", payload, max_text_chars=1000
        )
        self.assertEqual(page.body_text, "complete body")
        self.assertEqual(page.site_name, "Publisher")

    def test_wechat_challenge_url_and_visible_challenge_are_rejected(self):
        challenge = """<html><body><main>环境异常 当前环境异常，完成验证后即可继续访问。
        <button>去验证</button></main></body></html>""".encode()
        payloads = (
            link_preview.HTTPPayload(
                "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=secret",
                200, {"content-type": "text/html"}, b"<html><body>anything</body></html>",
            ),
            link_preview.HTTPPayload(
                "https://mp.weixin.qq.com/s/article", 200,
                {"content-type": "text/html"}, challenge,
            ),
        )
        for payload in payloads:
            with self.subTest(url=payload.url), self.assertRaises(link_preview.LinkPreviewError):
                link_preview.extract_html_page(
                    "https://mp.weixin.qq.com/s/article", payload, max_text_chars=1000
                )

    def test_wechat_real_article_may_quote_challenge_wording(self):
        html = """<html><head><meta property='og:title' content='验证码研究'></head>
        <body><a id='js_name'>安全研究所</a><div id='js_content'><p>页面提示：</p>
        <blockquote>当前环境异常，完成验证后即可继续访问。去验证</blockquote>
        <p>以上是本文引用的原文。</p></div></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/s/research", 200,
            {"content-type": "text/html"}, html,
        )
        page = link_preview.extract_html_page(
            "https://mp.weixin.qq.com/s/research", payload, max_text_chars=1000
        )
        self.assertIn("当前环境异常", page.body_text)
        self.assertIn("去验证", page.body_text)

    def test_explicit_truncation_keeps_hard_response_budget(self):
        fetcher = link_preview.SafeHTTPFetcher(max_download_bytes=1024)
        response = FakeResponse(200, {"Content-Length": "3000"}, b"x" * 3000)
        connection = FakeConnection(response)
        fetcher._connect = lambda *args: connection
        payload = fetcher.request(
            "https://public.example", deadline=time.monotonic() + 1,
            truncate_at_limit=True,
        )
        self.assertEqual(len(payload.body), 1024)
        self.assertEqual(payload.headers["x-cc-preview-truncated"], "1")

    def test_xhs_prefers_article_jsonld_images_over_generic_logo(self):
        article = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": "note",
            "image": [
                "https://sns-webpic-qc.xhscdn.com/one.jpg?imageView2=2",
                {"@type": "ImageObject", "contentUrl": "https://sns-img-qc.xhscdn.com/two.webp"},
            ],
        }
        html = f"""<html><head>
        <meta property='og:image' content='https://picasso-static.xiaohongshu.com/fe-platform/logo.png'>
        <script type='application/ld+json'>{json.dumps(article)}</script>
        </head><body>note body</body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/explore/note", 200, {"content-type": "text/html"}, html
        )
        page = link_preview.extract_html_page("https://xhslink.com/o/note", payload, max_text_chars=1000)
        self.assertEqual(len(page.image_urls), 2)
        self.assertEqual(page.image_url, page.image_urls[0])
        self.assertTrue(all("xhscdn.com" in item for item in page.image_urls))
        self.assertNotIn("logo", " ".join(page.image_urls))

    def test_xhs_initial_state_known_note_path_is_bounded_fallback(self):
        images = [
            {"urlDefault": f"https://sns-webpic-qc.xhscdn.com/{index}.jpg"}
            for index in range(20)
        ]
        state = json.dumps({"note": {"noteDetailMap": {"id": {"note": {"imageList": images}}}}})
        # Include XHS's real-world non-JSON undefined token outside a string.
        state = state[:-1] + ',"optional":undefined}'
        html = f"""<html><head>
        <meta property='og:image' content='https://picasso-static.xiaohongshu.com/fe-platform/logo.png'>
        </head><body>note<script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/explore/note", 200, {"content-type": "text/html"}, html
        )
        page = link_preview.extract_html_page("https://xhslink.com/o/note", payload, max_text_chars=1000)
        self.assertEqual(len(page.image_urls), link_preview.MAX_PAGE_IMAGES)
        self.assertTrue(page.image_urls[0].endswith("/0.jpg"))
        self.assertTrue(page.image_urls[-1].endswith("/17.jpg"))

    def test_xhs_initial_state_note_description_replaces_platform_slogan(self):
        state = json.dumps({
            "note": {
                "noteDetailMap": {
                    "note": {"note": {"desc": "日常监视是否重置，他一天不阴阳就浑身难受吗😂"}}
                }
            }
        }, ensure_ascii=False)
        html = f"""<html><head>
        <meta property='og:description' content='3 亿人的生活经验，都在小红书'>
        </head><body>navigation<script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/discovery/item/note",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page("https://xhslink.com/o/note", payload, max_text_chars=1000)
        self.assertEqual(page.description, "日常监视是否重置，他一天不阴阳就浑身难受吗😂")
        self.assertEqual(page.body_text, "日常监视是否重置，他一天不阴阳就浑身难受吗😂")

    def test_xhs_exact_final_note_desc_replaces_repeated_page_footer(self):
        caption = "Claude VS ChatGPT 顶级嘲讽 哈哈\n#claude"
        state = json.dumps({
            "note": {
                "noteDetailMap": {
                    "target": {"note": {"noteId": "target", "desc": caption}},
                }
            }
        }, ensure_ascii=False)
        html = f"""<html><body>
        <main>wrong visible shell text</main>
        <footer><div>创作中心</div><div>业务合作</div><div>发现</div><div>RED</div>
        <div>营业执照</div><div>沪ICP备13030189号</div><div>沪公网安备 31010402002533号</div>
        <div>违法和不良信息举报电话：021-12345678</div><div>地址：上海市黄浦区旧址</div>
        <div>营业执照</div><div>沪ICP备13030189号</div></footer>
        <script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/explore/target?xsec_token=secret-value",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page(
            "https://xhslink.com/o/share", payload, max_text_chars=1000
        )
        self.assertEqual(page.body_text, caption)
        self.assertNotIn("ICP备", page.body_text)
        self.assertNotIn("业务合作", page.body_text)

    def test_xhs_multiple_notes_never_concatenate_non_target_desc(self):
        state = json.dumps({
            "note": {
                "noteDetailMap": {
                    "recommended": {"note": {"noteId": "recommended", "desc": "别人的正文"}},
                    "target": {"note": {"noteId": "target", "desc": "目标正文"}},
                    "another": {"note": {"desc": "第三篇正文"}},
                }
            }
        }, ensure_ascii=False)
        html = f"""<html><body>页面壳<script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/discovery/item/target",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page(
            "https://xhslink.com/o/share", payload, max_text_chars=1000
        )
        self.assertEqual(page.description, "目标正文")
        self.assertEqual(page.body_text, "目标正文")
        self.assertNotIn("别人的正文", page.body_text)

    def test_xhs_missing_desc_fallback_only_removes_exact_boilerplate_lines(self):
        state = json.dumps({
            "note": {"noteDetailMap": {"target": {"note": {"noteId": "target"}}}}
        })
        html = f"""<html><body>
        <article><p>今天终于拿到营业执照了，纪念一下</p><p>收到通知后我去直播了</p></article>
        <footer><div>营业执照</div><div>通知</div><div>创作中心</div>
        <div>网络文化经营许可证：沪网文〔2026〕1234-001号</div>
        <div>公司电话：021-12345678</div></footer>
        <script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/discovery/item/target",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page(
            "https://xhslink.com/o/share", payload, max_text_chars=1000
        )
        self.assertIn("今天终于拿到营业执照了，纪念一下", page.body_text)
        self.assertIn("收到通知后我去直播了", page.body_text)
        # Ambiguous one-word author lines are deliberately retained when no
        # exact structured caption exists.
        self.assertIn("\n营业执照\n", f"\n{page.body_text}\n")
        self.assertIn("\n通知\n", f"\n{page.body_text}\n")
        self.assertNotIn("网络文化经营许可证：", page.body_text)
        self.assertNotIn("公司电话：", page.body_text)

    def test_xhs_fallback_filters_real_footer_variants_without_eating_caption(self):
        body = "\n".join((
            "作者正文第一行",
            "通知",
            "直播",
            "营业执照",
            "2024沪公网安备 31010402002533号",
            "|",
            "违法不良信息举报电话：021-12345678",
            "个性化推荐算法 网信算备310104123456789012340019号",
            "© 2014-2026",
            "行吟信息科技（上海）有限公司",
            "更多",
            "关于我们",
            "加载中",
        ))
        cleaned = link_preview._clean_xhs_fallback_body(body)
        self.assertEqual(cleaned, "作者正文第一行\n通知\n直播\n营业执照")

    def test_xhs_encoded_final_note_id_can_match_second_initial_state_script(self):
        first = json.dumps({
            "note": {"noteDetailMap": {"recommended": {"note": {"desc": "别人的正文"}}}}
        }, ensure_ascii=False)
        second = json.dumps({
            "note": {
                "noteDetailMap": {
                    "target-note": {"note": {"noteId": "target-note", "desc": "编码目标正文"}}
                }
            }
        }, ensure_ascii=False)
        html = f"""<html><body>页面壳
        <script>window.__INITIAL_STATE__={first};</script>
        <script>window.__INITIAL_STATE__={second};</script>
        </body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/explore/target%2Dnote",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page(
            "https://xhslink.com/o/share", payload, max_text_chars=1000
        )
        self.assertEqual(page.description, "编码目标正文")
        self.assertEqual(page.body_text, "编码目标正文")
        self.assertNotIn("别人的正文", page.body_text)

    def test_xhs_platform_slogan_is_not_used_without_note_description(self):
        slogans = ("3 亿人的生活经验，都在小红书", "生活经验，都在小红书！")
        for slogan in slogans:
            with self.subTest(slogan=slogan):
                html = f"""<html><head><meta property='og:description' content='{slogan}'>
                </head><body>navigation</body></html>""".encode()
                payload = link_preview.HTTPPayload(
                    "https://www.xiaohongshu.com/discovery/item/note",
                    200,
                    {"content-type": "text/html"},
                    html,
                )
                page = link_preview.extract_html_page(
                    "https://xhslink.com/o/note", payload, max_text_chars=1000
                )
                self.assertEqual(page.description, "")

    def test_xhs_does_not_take_description_from_another_note(self):
        state = json.dumps({
            "note": {
                "noteDetailMap": {
                    "target": {"note": {"desc": ""}},
                    "recommended": {"note": {"desc": "another note's description"}},
                }
            }
        })
        html = f"""<html><head>
        <meta property='og:description' content='3 亿人的生活经验，都在小红书'>
        </head><body>navigation<script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/discovery/item/target",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page("https://xhslink.com/o/short", payload, max_text_chars=1000)
        self.assertEqual(page.description, "")

    def test_xhs_near_miss_user_description_is_preserved(self):
        user_text = "打工人的生活经验都在小红书，我的可不在"
        html = f"""<html><head><meta property='og:description' content='{user_text}'>
        </head><body>navigation</body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/discovery/item/note",
            200,
            {"content-type": "text/html"},
            html,
        )
        page = link_preview.extract_html_page("https://xhslink.com/o/note", payload, max_text_chars=1000)
        self.assertEqual(page.description, user_text)

    def test_js_undefined_normalizer_does_not_change_quoted_text(self):
        source = '{"literal":"x:undefined,", "missing": undefined}'
        normalized = link_preview._replace_js_undefined(source)
        self.assertEqual(json.loads(normalized), {"literal": "x:undefined,", "missing": None})

    def test_xhs_rejects_uncontrolled_jsonld_image_hosts_and_logo(self):
        article = {
            "@type": "Article",
            "image": [
                "http://169.254.169.254/latest/meta-data",
                "https://unrelated.example/tracker.jpg",
                "javascript:alert(1)",
            ],
        }
        html = f"""<html><head>
        <meta property='og:image' content='https://picasso-static.xiaohongshu.com/fe-platform/logo.png'>
        <script type='application/ld+json'>{json.dumps(article)}</script>
        </head><body>note body</body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/explore/note", 200, {"content-type": "text/html"}, html
        )
        page = link_preview.extract_html_page("https://xhslink.com/o/note", payload, max_text_chars=1000)
        self.assertEqual(page.image_url, "")
        self.assertEqual(page.image_urls, ())

    def test_generic_page_keeps_cross_origin_og_image_fallback(self):
        html = b"""<html><head><meta property='og:image' content='https://cdn.example.net/cover.jpg'>
        </head><body>article</body></html>"""
        payload = link_preview.HTTPPayload(
            "https://example.com/article", 200, {"content-type": "text/html"}, html
        )
        page = link_preview.extract_html_page("https://example.com/article", payload, max_text_chars=1000)
        self.assertEqual(page.image_urls, ("https://cdn.example.net/cover.jpg",))

    def test_xhs_multi_images_are_cached_and_injected_without_query_secrets(self):
        article = {
            "@type": "Article",
            "image": [
                "https://sns-webpic-qc.xhscdn.com/one.jpg?token=image-secret-one",
                "https://sns-webpic-qc.xhscdn.com/two.jpg?token=image-secret-two",
            ],
        }
        html = f"""<html><head><script type='application/ld+json'>{json.dumps(article)}</script>
        </head><body>note body</body></html>""".encode()
        page_payload = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/explore/note?token=final-secret",
            200,
            {"content-type": "text/html"},
            html,
        )
        image_one = link_preview.HTTPPayload(
            article["image"][0], 200, {"content-type": "image/jpeg"}, b"one"
        )
        image_two = link_preview.HTTPPayload(
            article["image"][1], 200, {"content-type": "image/jpeg"}, b"two"
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, fetcher=QueueFetcher([page_payload, image_one, image_two]), lease_seconds=1000
            )
            bundle = service.enrich("https://xhslink.com/o/note?token=request-secret")
            preview = bundle.previews[0]
            self.assertEqual(len(preview["image_paths"]), 2)
            self.assertEqual(preview["image_cache_url"], preview["image_cache_urls"][0])
            self.assertTrue(all(Path(path).is_file() for path in preview["image_paths"]))
            self.assertTrue(all(f"- 内容图片：{path}" in bundle.prompt_context for path in preview["image_paths"]))
            self.assertIn("评论未抓取", bundle.prompt_context)
            persisted = Path(preview["content_path"]).read_text()
            sidecar = service._paths("https://xhslink.com/o/note?token=request-secret")[1].read_text()
            combined = json.dumps(preview, ensure_ascii=False) + persisted + sidecar + bundle.prompt_context
            for secret in ("request-secret", "final-secret", "image-secret-one", "image-secret-two"):
                self.assertNotIn(secret, combined)

    def test_pre_upgrade_xhs_cache_is_not_reused(self):
        url = "https://xhslink.com/o/old"
        state = json.dumps({
            "note": {"noteDetailMap": {"fresh": {"note": {"desc": "fresh summary"}}}}
        })
        html = f"""<html><head>
        <meta property='og:description' content='3 亿人的生活经验，都在小红书'>
        </head><body>fresh note<script>window.__INITIAL_STATE__={state};</script></body></html>""".encode()
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                fetcher=QueueFetcher([
                    link_preview.HTTPPayload(
                        "https://www.xiaohongshu.com/explore/fresh",
                        200,
                        {"content-type": "text/html"},
                        html,
                    )
                ]),
            )
            text_path, meta_path = service._paths(url)
            key = service._url_key(url)
            text_path.write_text("stale")
            meta_path.write_text(json.dumps({
                "schema_version": 2,
                "cache_key": key,
                "lease_until": time.time() + 1000,
                "description": "3 亿人的生活经验，都在小红书",
                "image_cache_url": "",
                "image_cache_urls": [],
                "image_paths": [],
            }))
            preview = service.enrich(url).previews[0]
            persisted = Path(preview["content_path"]).read_text()
            self.assertIn("fresh summary", persisted)
            self.assertNotIn("stale", persisted)
            self.assertEqual(preview["description"], "fresh summary")
            self.assertEqual(preview["schema_version"], link_preview.XHS_CACHE_SCHEMA_VERSION)

    def test_wechat_mobile_retry_never_returns_challenge_as_article(self):
        url = "https://mp.weixin.qq.com/s/article"
        challenge = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha",
            200, {"content-type": "text/html"}, b"<html>challenge</html>",
        )
        article = link_preview.HTTPPayload(
            url, 200, {"content-type": "text/html", "content-length": "3000000"},
            b"""<html><head><meta property='og:title' content='Fresh article'></head>
            <body><a id='js_name'>Fresh publisher</a>
            <div id='js_content'>Fresh body</div>""",
        )
        fetcher = QueueFetcher([challenge, article])
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, fetcher=fetcher)
            page = service._fetch_page(url, time.monotonic() + 1)
        self.assertEqual(page.title, "Fresh article")
        self.assertEqual(page.site_name, "Fresh publisher")
        self.assertEqual(len(fetcher.calls), 2)
        for _called_url, kwargs in fetcher.calls:
            self.assertTrue(kwargs["truncate_at_limit"])
            self.assertIn("Mobile", kwargs["headers"]["User-Agent"])
            self.assertEqual(kwargs["headers"]["Referer"], "https://mp.weixin.qq.com/")

    def test_wechat_all_challenges_fail_open_without_cache_artifacts(self):
        url = "https://mp.weixin.qq.com/s/article"
        challenge = link_preview.HTTPPayload(
            "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha",
            200, {"content-type": "text/html"}, b"<html>challenge</html>",
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, fetcher=QueueFetcher([challenge, challenge]), cleanup_grace_seconds=1
            )
            bundle = service.enrich(url)
            self.assertEqual(bundle.previews, ())
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_wechat_article_redirect_off_origin_is_not_trusted(self):
        url = "https://mp.weixin.qq.com/s/article"
        off_origin = link_preview.HTTPPayload(
            "https://evil.example/article", 200, {"content-type": "text/html"},
            b"<html><div id='js_content'>forged article</div></html>",
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, fetcher=QueueFetcher([off_origin, off_origin])
            )
            self.assertEqual(service.enrich(url).previews, ())

    def test_pre_upgrade_wechat_challenge_cache_is_not_reused(self):
        url = "https://mp.weixin.qq.com/s/article"
        html = b"""<html><head><meta property='og:title' content='Fresh'></head>
        <body><a id='js_name'>Publisher</a><div id='js_content'>Real body</div>"""
        article = link_preview.HTTPPayload(
            url, 200, {"content-type": "text/html"}, html,
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([article]))
            text_path, meta_path = service._paths(url)
            key = service._url_key(url)
            text_path.write_text("环境异常 去验证")
            meta_path.write_text(json.dumps({
                "schema_version": link_preview.GENERIC_CACHE_SCHEMA_VERSION,
                "cache_key": key,
                "lease_until": time.time() + 1000,
                "title": "环境异常",
                "image_cache_url": "",
                "image_cache_urls": [],
                "image_paths": [],
            }))
            preview = service.enrich(url).previews[0]
            persisted = Path(preview["content_path"]).read_text()
            self.assertEqual(preview["schema_version"], link_preview.WECHAT_CACHE_SCHEMA_VERSION)
            self.assertEqual(preview["title"], "Fresh")
            self.assertIn("Real body", persisted)
            self.assertNotIn("环境异常", persisted)

    def test_multi_image_lease_protects_every_cached_image(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, lease_seconds=1000, cache_ttl_seconds=1)
            page = self._page(
                "https://example.com/page",
                image_url="https://cdn.example.com/one.jpg",
            )
            page = link_preview.ExtractedPage(
                **{**page.__dict__, "image_urls": (
                    "https://cdn.example.com/one.jpg",
                    "https://cdn.example.com/two.jpg",
                )}
            )
            service._fetch_page = lambda url, deadline: page
            service._download_image_candidate = lambda image_url, deadline, **_kwargs: (
                Path(td) / f"link_image_{service._url_key(image_url)}.jpg",
                image_url.encode(),
            )
            preview = service.enrich("https://example.com/page").previews[0]
            old = time.time() - 100
            for image_path in preview["image_paths"]:
                os.utime(image_path, (old, old))
            service._cleanup_cache()
            self.assertTrue(all(Path(path).is_file() for path in preview["image_paths"]))

    def test_service_persists_atomic_private_text_and_metadata(self):
        html = b"<html><head><title>Preview</title></head><body>full article</body></html>"
        payload = link_preview.HTTPPayload(
            "https://example.com/final", 200, {"content-type": "text/html"}, html
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([payload]), cache_ttl_seconds=60)
            bundle = service.enrich("please read https://example.com/a")
            self.assertEqual(len(bundle.previews), 1)
            preview = bundle.previews[0]
            path = Path(preview["content_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(path.name.startswith("link_"))
            self.assertIn("full article", path.read_text())
            self.assertIn(str(path), bundle.prompt_context)
            self.assertEqual(preview["content_url"], f"/attachments/{path.name}")

    def test_metadata_urls_are_bounded_and_strip_query_secrets(self):
        self.assertEqual(
            link_preview._metadata_url("https://example.com/path?token=secret#frag"),
            "https://example.com/path",
        )
        self.assertEqual(
            link_preview._metadata_url("https://user:password@example.com:443/path"),
            "https://example.com/path",
        )

    def test_url_secret_echoes_are_redacted_from_text_sidecar_and_prompt(self):
        requested = (
            "https://example.com/note?xsec_token=abc%252FDEF12345&q=cat"
            "&slug=how-to-build-a-very-long-widget&article_id=0123456789abcdef"
            "&utm_source=summer_campaign_2026_long#signature=frag-secret-987654"
        )
        final = (
            "https://final-signature-value.example.com/final/abc%2FDEF12345"
            "?sig=final-signature-value"
        )
        image_url = (
            "https://cdn.example.com/media/image-query-secret/abc%252FDEF12345.jpg"
            "?token=image-query-secret"
        )
        page = link_preview.ExtractedPage(
            requested_url=requested,
            final_url=final,
            title=(
                "decoded abc/DEF12345 plus how-to-build-a-very-long-widget "
                "0123456789abcdef summer_campaign_2026_long"
            ),
            description="encoded abc%2FDEF12345 remains",
            site_name="example abc%2FDEF12345",
            image_url=image_url,
            body_text=(
                "raw abc%252FDEF12345 and xsec_token=abc%252FDEF12345 but ordinary cat remains; "
                "how-to-build-a-very-long-widget 0123456789abcdef summer_campaign_2026_long"
            ),
            comments="frag-secret-987654 final-signature-value",
            image_urls=(image_url,),
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: page
            service._download_image_candidate = lambda url, deadline, **_kwargs: None
            bundle = service.enrich(requested)
            preview = bundle.previews[0]
            persisted = Path(preview["content_path"]).read_text()
            sidecar = service._paths(requested)[1].read_text()
            combined = persisted + sidecar + bundle.prompt_context + json.dumps(preview)
            for secret in (
                "abc/DEF12345",
                "abc%2FDEF12345",
                "abc%252FDEF12345",
                "frag-secret-987654",
                "final-signature-value",
                "image-query-secret",
            ):
                self.assertNotIn(secret.lower(), combined.lower())
            self.assertIn("cat remains", persisted)
            self.assertIn("how-to-build-a-very-long-widget", persisted)
            self.assertIn("0123456789abcdef", persisted)
            self.assertIn("summer_campaign_2026_long", persisted)
            self.assertIn(link_preview._REDACTED_URL_SECRET, persisted)

    def test_site_name_is_redacted_in_html_and_adapter_extraction(self):
        url = "https://example.com/note?xsec_token=site-secret-123456"
        html = b"""<html><head><meta property='og:site_name' content='site-secret-123456'>
        </head><body>body</body></html>"""
        page = link_preview.extract_html_page(
            url,
            link_preview.HTTPPayload(url, 200, {"content-type": "text/html"}, html),
            max_text_chars=1000,
        )
        self.assertNotIn("site-secret-123456", page.site_name)

        adapter_payload = link_preview.HTTPPayload(
            "https://adapter.example",
            200,
            {"content-type": "application/json"},
            json.dumps({"text": "body", "site_name": "site-secret-123456"}).encode(),
        )
        adapted = link_preview.LinkPreviewService._adapter_page(url, adapter_payload, "xhs-api", 1000)
        self.assertNotIn("site-secret-123456", adapted.site_name)

    def test_failure_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, fetcher=QueueFetcher([link_preview.LinkPreviewError("offline")])
            )
            self.assertEqual(service.enrich("https://example.com").previews, ())

    def test_windows_renderer_handles_http_or_parse_failure(self):
        bad = link_preview.HTTPPayload(
            "https://example.com/private", 403, {"content-type": "text/html"}, b"forbidden"
        )
        rendered = link_preview.HTTPPayload(
            "https://windows.example/preview",
            200,
            {"content-type": "application/json"},
            json.dumps({"title": "rendered", "text": "full rendered body"}).encode(),
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                windows_api_url="https://windows.example/preview",
                fetcher=QueueFetcher([bad]),
                adapter_fetcher=QueueFetcher([rendered]),
            )
            result = service.enrich("https://example.com/private")
            self.assertEqual(result.previews[0]["provider"], "windows-render")

    def test_xhs_windows_renderer_platform_summary_is_filtered(self):
        bad = link_preview.HTTPPayload(
            "https://www.xiaohongshu.com/discovery/item/note",
            403,
            {"content-type": "text/html"},
            b"forbidden",
        )
        rendered = link_preview.HTTPPayload(
            "https://windows.example/preview",
            200,
            {"content-type": "application/json"},
            json.dumps({
                "title": "note",
                "description": "生活经验，都在小红书！",
                "text": "full rendered note body",
            }).encode(),
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                windows_api_url="https://windows.example/preview",
                fetcher=QueueFetcher([bad]),
                adapter_fetcher=QueueFetcher([rendered]),
            )
            preview = service.enrich("https://xhslink.com/o/note").previews[0]
            self.assertEqual(preview["provider"], "windows-render")
            self.assertEqual(preview["description"], "")

    def test_xhs_windows_renderer_body_uses_conservative_footer_fallback(self):
        rendered = link_preview.HTTPPayload(
            "https://windows.example/preview",
            200,
            {"content-type": "application/json"},
            json.dumps({
                "title": "note",
                "text": "作者写营业执照办理经历\n营业执照\n沪ICP备13030189号\n通知",
            }, ensure_ascii=False).encode(),
        )
        page = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/note", rendered, "windows-render", 1000
        )
        self.assertEqual(page.body_text, "作者写营业执照办理经历\n营业执照\n通知")

    def test_xhs_adapters_use_real_description_only_as_safe_body_fallback(self):
        for provider in ("xhs-api", "windows-render"):
            with self.subTest(provider=provider):
                payload = link_preview.HTTPPayload(
                    "https://adapter.example/preview",
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"title": "note", "description": "只有摘要的真实正文"}, ensure_ascii=False).encode(),
                )
                page = link_preview.LinkPreviewService._adapter_page(
                    "https://xhslink.com/o/note", payload, provider, 1000
                )
                self.assertEqual(page.body_text, "只有摘要的真实正文")

    def test_xhs_adapters_never_promote_platform_description_to_body(self):
        for provider in ("xhs-api", "windows-render"):
            with self.subTest(provider=provider):
                payload = link_preview.HTTPPayload(
                    "https://adapter.example/preview",
                    200,
                    {"content-type": "application/json"},
                    json.dumps({"title": "note", "description": "生活经验都在小红书"}, ensure_ascii=False).encode(),
                )
                page = link_preview.LinkPreviewService._adapter_page(
                    "https://xhslink.com/o/note", payload, provider, 1000
                )
                self.assertEqual(page.body_text, "")

    def test_xhs_api_desc_is_authoritative_over_rendered_page_shell(self):
        adapter = link_preview.HTTPPayload(
            "https://adapter.example/preview",
            200,
            {"content-type": "application/json"},
            json.dumps({
                "title": "note",
                "desc": "真实 caption\n营业执照",
                "text": "错误页面壳\n创作中心\n沪ICP备13030189号",
            }, ensure_ascii=False).encode(),
        )
        page = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/note", adapter, "xhs-api", 1000
        )
        self.assertEqual(page.description, "真实 caption\n营业执照")
        self.assertEqual(page.body_text, "真实 caption\n营业执照")

    def test_xhs_adapter_is_preferred_and_comments_are_written(self):
        adapter = link_preview.HTTPPayload(
            "https://adapter.example/preview",
            200,
            {"content-type": "application/json"},
            json.dumps({"title": "note", "text": "body", "comments": [{"text": "comment"}]}).encode(),
        )
        fetcher = QueueFetcher([adapter])
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, xhs_api_url="https://adapter.example/preview", fetcher=fetcher
            )
            bundle = service.enrich("https://xhslink.com/abc")
            self.assertEqual(bundle.previews[0]["provider"], "xhs-api")
            self.assertIn("comment", Path(bundle.previews[0]["content_path"]).read_text())
            self.assertEqual(fetcher.calls[0][1]["method"], "POST")

    def test_xhs_adapter_filters_only_platform_summary(self):
        cases = (
            ("生活经验都在小红书", ""),
            ("真实笔记正文", "真实笔记正文"),
        )
        for description, expected in cases:
            with self.subTest(description=description), tempfile.TemporaryDirectory() as td:
                adapter = link_preview.HTTPPayload(
                    "https://adapter.example/preview",
                    200,
                    {"content-type": "application/json"},
                    json.dumps({
                        "title": "note",
                        "description": description,
                        "text": "note body",
                    }).encode(),
                )
                service = link_preview.LinkPreviewService(
                    td,
                    xhs_api_url="https://adapter.example/preview",
                    fetcher=QueueFetcher([adapter]),
                )
                preview = service.enrich("https://xhslink.com/o/note").previews[0]
                self.assertEqual(preview["provider"], "xhs-api")
                self.assertEqual(preview["description"], expected)

    def test_non_xhs_page_keeps_same_description_text(self):
        base_page = self._page("https://example.com/note")
        page = link_preview.ExtractedPage(
            **{**base_page.__dict__, "description": "生活经验都在小红书"}
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: page
            preview = service.enrich("https://example.com/note").previews[0]
            self.assertEqual(preview["description"], "生活经验都在小红书")

    def test_concurrent_same_url_fetches_once_and_reuses_file(self):
        html = b"<html><title>T</title><body>body</body></html>"
        payload = link_preview.HTTPPayload("https://example.com", 200, {"content-type": "text/html"}, html)

        class SlowFetcher(QueueFetcher):
            def request(self, url, **kwargs):
                time.sleep(0.05)
                return super().request(url, **kwargs)

        fetcher = SlowFetcher([payload])
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, fetcher=fetcher)
            results = []
            threads = [threading.Thread(target=lambda: results.append(service.enrich("https://example.com"))) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(fetcher.calls), 1)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].previews[0]["content_path"], results[1].previews[0]["content_path"])

    def test_many_unique_urls_do_not_accumulate_locks(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: object()
            service._persist_page = lambda url, page, deadline: {"url": url}
            for index in range(1000):
                service._preview_one(f"https://example.com/{index}", time.monotonic() + 1)
            self.assertEqual(service._locks, {})

    def test_cache_cleanup_is_bounded_and_never_follows_symlinks_or_active_keys(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside_td:
            root = Path(td)
            outside = Path(outside_td) / "outside.txt"
            outside.write_text("keep")
            service = link_preview.LinkPreviewService(
                root,
                cache_ttl_seconds=2,
                max_cache_entries=4,
                max_cache_bytes=80,
                cleanup_grace_seconds=1,
            )
            now = time.time()
            keys = []
            for index in range(8):
                key = service._url_key(f"https://example.com/{index}")
                keys.append(key)
                path = root / f"link_{key}.txt"
                path.write_bytes(b"x" * 30)
                os.utime(path, (now - 10, now - 10))
            symlink_key = "f" * 64
            symlink = root / f"link_{symlink_key}.txt"
            symlink.symlink_to(outside)
            active_path = root / f"link_{keys[0]}.txt"
            active = service._acquire_key_lock(keys[0], time.monotonic() + 1)
            try:
                service._cleanup_cache()
                self.assertTrue(active_path.exists())
            finally:
                service._release_key_lock(keys[0], active)
            self.assertTrue(symlink.is_symlink())
            self.assertEqual(outside.read_text(), "keep")
            os.utime(active_path, (now - 10, now - 10))
            service._cleanup_cache()
            artifacts = service._cache_artifacts()
            self.assertLessEqual(len(artifacts), 4)
            self.assertLessEqual(sum(item[1] for item in artifacts), 80)
            self.assertTrue(symlink.is_symlink())

    @staticmethod
    def _page(url: str, *, image_url: str = "") -> link_preview.ExtractedPage:
        return link_preview.ExtractedPage(
            requested_url=url,
            final_url=url,
            title="T",
            description="D",
            site_name="Example",
            image_url=image_url,
            body_text="body",
        )

    def test_fifty_rapid_urls_never_exceed_hard_cache_quota(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                max_cache_entries=5,
                max_cache_bytes=20_000,
                total_timeout=2,
            )
            service._fetch_page = lambda url, deadline: self._page(url)
            successful = []
            for index in range(50):
                bundle = service.enrich(f"https://example.com/{index}")
                successful.extend(bundle.previews)
            with service._cache_budget_lock:
                entries, size = service._cache_usage_locked()
            self.assertLessEqual(entries, 5)
            self.assertLessEqual(size, 20_000)
            self.assertEqual(len(successful), 5)
            for preview in successful:
                path = Path(preview["content_path"])
                self.assertTrue(path.is_file())
                self.assertTrue((path.parent / f".{path.stem}.json").is_file())

    def test_deadline_exhaustion_writes_nothing_and_stays_within_quota(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                total_timeout=0.5,
                max_cache_entries=2,
                max_cache_bytes=2_000,
            )

            def too_slow(url, deadline):
                time.sleep(0.52)
                return self._page(url)

            service._fetch_page = too_slow
            self.assertEqual(service.enrich("https://example.com/slow").previews, ())
            with service._cache_budget_lock:
                entries, size = service._cache_usage_locked()
            self.assertEqual((entries, size), (0, 0))

    def test_unexpired_lease_survives_cleanup_then_expired_group_deletes_together(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, lease_seconds=900)
            service._fetch_page = lambda url, deadline: self._page(url)
            url = "https://example.com/queued"
            preview = service.enrich(url).previews[0]
            text_path, meta_path = service._paths(url)
            for _ in range(5):
                service._cleanup_cache()
                self.assertTrue(text_path.is_file())
                self.assertTrue(meta_path.is_file())
            meta = json.loads(meta_path.read_text())
            meta["lease_until"] = int(time.time()) - 1
            service._atomic_write(meta_path, json.dumps(meta).encode())
            service._cleanup_cache()
            self.assertFalse(text_path.exists())
            self.assertFalse(meta_path.exists())
            self.assertFalse(Path(preview["content_path"]).exists())

    def test_cache_hit_renews_lease_before_return_and_hides_internal_field(self):
        payload = link_preview.HTTPPayload(
            "https://example.com/hit",
            200,
            {"content-type": "text/html"},
            b"<html><title>T</title><body>body</body></html>",
        )
        with tempfile.TemporaryDirectory() as td:
            fetcher = QueueFetcher([payload])
            service = link_preview.LinkPreviewService(td, fetcher=fetcher, lease_seconds=1000)
            url = "https://example.com/hit"
            first = service.enrich(url).previews[0]
            text_path, meta_path = service._paths(url)
            meta = json.loads(meta_path.read_text())
            meta["lease_until"] = int(time.time()) + 901
            service._atomic_write(meta_path, json.dumps(meta).encode())
            old_mtime = time.time() - 100
            os.utime(text_path, (old_mtime, old_mtime))
            before = meta["lease_until"]
            second = service.enrich(url).previews[0]
            renewed = json.loads(meta_path.read_text())["lease_until"]
            self.assertGreater(renewed, before)
            self.assertGreater(text_path.stat().st_mtime, old_mtime)
            self.assertNotIn("lease_until", second)
            self.assertEqual(len(fetcher.calls), 1)
            self.assertTrue(Path(first["content_path"]).is_file())
            self.assertTrue(meta_path.is_file())

    def test_same_enrich_cleanup_protects_every_returned_group(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            # Exercise the cleanup protection independently from the enforced
            # production minimum lease duration.
            service.lease_seconds = -1
            service._fetch_page = lambda url, deadline: self._page(url)
            url = "https://example.com/current"
            preview = service.enrich(url).previews[0]
            text_path, meta_path = service._paths(url)
            self.assertEqual(preview["content_path"], str(text_path))
            self.assertTrue(text_path.is_file())
            self.assertTrue(meta_path.is_file())
            service._cleanup_cache()
            self.assertFalse(text_path.exists())
            self.assertFalse(meta_path.exists())

    def test_concurrent_distinct_url_projection_never_oversubscribes_quota(self):
        count = 12
        barrier = threading.Barrier(count)
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                total_timeout=5,
                max_cache_entries=3,
                max_cache_bytes=20_000,
            )

            def fetch(url, deadline):
                barrier.wait(timeout=2)
                return self._page(url)

            service._fetch_page = fetch
            results = []
            threads = [
                threading.Thread(
                    target=lambda index=index: results.append(
                        service.enrich(f"https://example.com/concurrent/{index}")
                    )
                )
                for index in range(count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            with service._cache_budget_lock:
                entries, size = service._cache_usage_locked()
            self.assertLessEqual(entries, 3)
            self.assertLessEqual(size, 20_000)
            self.assertEqual(sum(bool(item.previews) for item in results), 3)

    def test_shared_image_survives_second_page_sidecar_rollback(self):
        page1_url = "https://example.com/page-one"
        page2_url = "https://example.com/page-two"
        image_url = "https://cdn.example.com/shared.jpg"
        candidates_ready = threading.Barrier(2)
        page1_committed = threading.Event()
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, total_timeout=5)
            service._fetch_page = lambda url, deadline: self._page(url, image_url=image_url)
            image_key = service._url_key(image_url)
            image_path = Path(td) / f"link_image_{image_key}.jpg"

            def stale_candidate(_url, _deadline, **_kwargs):
                # Both transactions prepare target+bytes before either enters
                # the budget lock.  Page two deliberately remains stale until
                # page one has committed the shared image and sidecar.
                candidate = (image_path, b"shared-image")
                candidates_ready.wait(timeout=2)
                if threading.current_thread().name == "page-two":
                    self.assertTrue(page1_committed.wait(timeout=2))
                return candidate

            service._download_image_candidate = stale_candidate
            _text1, meta1 = service._paths(page1_url)
            text2, meta2 = service._paths(page2_url)
            original_write = service._atomic_write

            def controlled_write(path, data):
                if path == meta2:
                    raise OSError("page two sidecar failure")
                result = original_write(path, data)
                if path == meta1:
                    page1_committed.set()
                return result

            results = {}
            with mock.patch.object(service, "_atomic_write", side_effect=controlled_write):
                first = threading.Thread(
                    name="page-one",
                    target=lambda: results.__setitem__("page1", service.enrich(page1_url)),
                )
                second = threading.Thread(
                    name="page-two",
                    target=lambda: results.__setitem__("page2", service.enrich(page2_url)),
                )
                first.start()
                second.start()
                first.join()
                second.join()

            self.assertEqual(len(results["page1"].previews), 1)
            self.assertEqual(results["page2"].previews, ())
            self.assertTrue(image_path.is_file())
            self.assertEqual(image_path.read_bytes(), b"shared-image")
            page1_meta = json.loads(meta1.read_text())
            self.assertEqual(page1_meta["image_cache_url"], f"/attachments/{image_path.name}")
            self.assertTrue((Path(td) / Path(page1_meta["image_cache_url"]).name).is_file())
            self.assertFalse(text2.exists())
            self.assertFalse(meta2.exists())

    def test_sidecar_half_write_failure_returns_no_path_or_partial_group(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: self._page(url)
            original = service._atomic_write

            def fail_sidecar(path, data):
                if path.name.startswith(".link_"):
                    raise OSError("injected sidecar failure")
                return original(path, data)

            with mock.patch.object(service, "_atomic_write", side_effect=fail_sidecar):
                bundle = service.enrich("https://example.com/half")
            self.assertEqual(bundle.previews, ())
            text_path, meta_path = service._paths("https://example.com/half")
            self.assertFalse(text_path.exists())
            self.assertFalse(meta_path.exists())

    def test_oversized_image_is_skipped_but_page_group_commits(self):
        page = link_preview.HTTPPayload(
            "https://example.com/page",
            200,
            {"content-type": "text/html"},
            b"<html><head><meta property='og:image' content='https://cdn.example.com/x.jpg'></head><body>body</body></html>",
        )
        image = link_preview.HTTPPayload(
            "https://cdn.example.com/x.jpg",
            200,
            {"content-type": "image/jpeg"},
            b"x" * 5_000,
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                fetcher=QueueFetcher([page, image]),
                max_cache_entries=3,
                max_cache_bytes=2_000,
            )
            preview = service.enrich("https://example.com/page").previews[0]
            self.assertEqual(preview["image_cache_url"], "")
            self.assertTrue(Path(preview["content_path"]).is_file())
            self.assertEqual(list(Path(td).glob("link_image_*")), [])

    def test_image_cache_filename_is_full_sha256(self):
        image_url = "https://cdn.example.com/a.jpg?token=1"
        payload = link_preview.HTTPPayload(
            image_url, 200, {"content-type": "image/jpeg"}, b"jpeg"
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([payload]))
            result = service._cache_image(image_url, time.monotonic() + 1)
            digest = link_preview.hashlib.sha256(image_url.encode()).hexdigest()
            self.assertEqual(result, f"/attachments/link_image_{digest}.jpg")

    def test_xhs_image_final_redirect_host_must_remain_allowlisted(self):
        source = "https://sns-webpic-qc.xhscdn.com/source.jpg"
        evil = link_preview.HTTPPayload(
            "https://public-evil.example/tracker.jpg",
            200,
            {"content-type": "image/jpeg"},
            b"evil",
        )
        allowed = link_preview.HTTPPayload(
            "https://sns-img-qc.xhscdn.com/final.jpg",
            200,
            {"content-type": "image/jpeg"},
            b"allowed",
        )
        with tempfile.TemporaryDirectory() as td:
            rejected = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([evil]))
            self.assertIsNone(
                rejected._download_image_candidate(source, time.monotonic() + 1, xhs_only=True)
            )
            self.assertEqual(list(Path(td).glob("link_image_*")), [])

            accepted = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([allowed]))
            candidate = accepted._download_image_candidate(source, time.monotonic() + 1, xhs_only=True)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate[1], b"allowed")

            generic = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([evil]))
            candidate = generic._download_image_candidate(source, time.monotonic() + 1, xhs_only=False)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate[1], b"evil")

    def test_xhs_policy_never_reuses_generic_cache_of_same_source_url(self):
        source = "https://sns-webpic-qc.xhscdn.com/source.jpg"
        evil = link_preview.HTTPPayload(
            "https://public-evil.example/tracker.jpg",
            200,
            {"content-type": "image/jpeg"},
            b"evil",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            generic_fetcher = QueueFetcher([evil])
            generic = link_preview.LinkPreviewService(root, fetcher=generic_fetcher)
            generic_url = generic._cache_image(source, time.monotonic() + 1)
            generic_path = root / Path(generic_url).name
            self.assertTrue(generic_path.is_file())

            xhs_fetcher = QueueFetcher([evil])
            xhs = link_preview.LinkPreviewService(root, fetcher=xhs_fetcher)
            self.assertIsNone(
                xhs._download_image_candidate(source, time.monotonic() + 1, xhs_only=True)
            )
            self.assertEqual(len(xhs_fetcher.calls), 1)
            xhs_key = xhs._image_url_key(source, xhs_only=True)
            self.assertEqual(list(root.glob(f"link_image_{xhs_key}.*")), [])
            self.assertTrue(generic_path.is_file())

    def test_xhs_policy_cache_reuses_verified_image_and_inherits_page_leases(self):
        source = "https://sns-webpic-qc.xhscdn.com/source.jpg"
        allowed = link_preview.HTTPPayload(
            "https://sns-img-qc.xhscdn.com/final.jpg",
            200,
            {"content-type": "image/jpeg"},
            b"allowed",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fetcher = QueueFetcher([allowed])
            service = link_preview.LinkPreviewService(
                root,
                fetcher=fetcher,
                lease_seconds=1000,
                cache_ttl_seconds=1,
            )
            service._fetch_page = lambda url, deadline: self._page(url, image_url=source)
            first = service.enrich("https://www.xiaohongshu.com/explore/one").previews[0]
            second = service.enrich("https://www.xiaohongshu.com/explore/two").previews[0]
            self.assertEqual(len(fetcher.calls), 1)
            self.assertEqual(first["image_paths"], second["image_paths"])
            image_path = Path(first["image_paths"][0])
            expected_key = service._image_url_key(source, xhs_only=True)
            self.assertEqual(image_path.name, f"link_image_{expected_key}.jpg")
            old = time.time() - 100
            os.utime(image_path, (old, old))
            service._cleanup_cache()
            self.assertTrue(image_path.is_file())

    def test_cache_hit_with_missing_image_is_refetched(self):
        page_url = "https://example.com/page"
        image_url = "https://cdn.example.com/image.jpg"
        calls = []
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, lease_seconds=1000)
            service._fetch_page = lambda url, deadline: (
                calls.append(url) or self._page(url, image_url=image_url)
            )
            service._download_image_candidate = lambda url, deadline, **_kwargs: (
                Path(td) / f"link_image_{service._url_key(url)}.jpg",
                b"image",
            )
            first = service.enrich(page_url).previews[0]
            Path(first["image_paths"][0]).unlink()
            second = service.enrich(page_url).previews[0]
            self.assertEqual(len(calls), 2)
            self.assertTrue(Path(second["image_paths"][0]).is_file())

    def test_cache_hit_with_symlink_image_is_refetched_without_returning_symlink(self):
        page_url = "https://example.com/page"
        image_url = "https://cdn.example.com/image.jpg"
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = link_preview.LinkPreviewService(root, lease_seconds=1000)
            service._fetch_page = lambda url, deadline: (
                calls.append(url) or self._page(url, image_url=image_url)
            )
            service._download_image_candidate = lambda url, deadline, **_kwargs: (
                root / f"link_image_{service._url_key(url)}.jpg",
                b"image",
            )
            first = service.enrich(page_url).previews[0]
            cached_image = Path(first["image_paths"][0])
            cached_image.unlink()
            outside = root.parent / f"outside-{service._url_key(page_url)}.jpg"
            outside.write_bytes(b"outside")
            self.addCleanup(lambda: outside.unlink(missing_ok=True))
            cached_image.symlink_to(outside)
            second = service.enrich(page_url).previews[0]
            self.assertEqual(len(calls), 2)
            self.assertEqual(second["image_paths"], [])
            self.assertTrue(cached_image.is_symlink())

    def test_cache_hit_with_mismatched_declared_image_path_is_refetched(self):
        page_url = "https://example.com/page"
        image_url = "https://cdn.example.com/image.jpg"
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = link_preview.LinkPreviewService(root, lease_seconds=1000)
            service._fetch_page = lambda url, deadline: (
                calls.append(url) or self._page(url, image_url=image_url)
            )
            service._download_image_candidate = lambda url, deadline, **_kwargs: (
                root / f"link_image_{service._url_key(url)}.jpg",
                b"image",
            )
            service.enrich(page_url)
            _text_path, meta_path = service._paths(page_url)
            meta = json.loads(meta_path.read_text())
            other = root / ("link_image_" + "9" * 64 + ".jpg")
            other.write_bytes(b"other")
            meta["image_paths"] = [str(other)]
            meta_path.write_text(json.dumps(meta))
            second = service.enrich(page_url).previews[0]
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(second["image_paths"], [str(other)])

    def test_metadata_merge_and_ai_prompt_path_injection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            content_path = root / ("link_" + "b" * 64 + ".txt")
            image_path = root / ("link_image_" + "a" * 64 + ".jpg")
            content_path.write_text("body")
            image_path.write_bytes(b"image")
            bundle = link_preview.LinkPreviewBundle(
                ({
                    "title": "T",
                    "content_path": str(content_path),
                    "comments_status": "included_partial",
                },),
                "SAFE LINK CONTEXT",
            )
            meta = link_preview.merge_preview_metadata({"via": "card"}, bundle)
            self.assertEqual(meta["via"], "card")
            self.assertEqual(meta["link_previews"][0]["content_path"], str(content_path))

            handler = object.__new__(push.PushHandler)
            handler.state = type("State", (), {"attachments_dir": root})()
            prompt = handler._kairos_prompt_for_task({"text": "hello", "link_context": bundle.prompt_context})
            self.assertIn("SAFE LINK CONTEXT", prompt)
            rebuilt = handler._link_context_from_record({"metadata": meta})
            self.assertIn(str(content_path), rebuilt)
            self.assertIn("不可信", rebuilt)
            self.assertIn("必须先读取该全文文件", rebuilt)
            self.assertIn("内容图片只是帖子配图", rebuilt)
            rebuilt_images = handler._link_context_from_record({
                "metadata": {
                    "link_previews": [{
                        "content_path": str(content_path),
                        "image_paths": [str(image_path), "/etc/shadow"],
                        "comments_status": "not_fetched",
                    }]
                }
            })
            self.assertIn(str(image_path), rebuilt_images)
            self.assertNotIn("/etc/shadow", rebuilt_images)
            self.assertIn("评论未抓取", rebuilt_images)
        forged = link_preview.merge_preview_metadata(
            {"link_previews": [{"content_path": "/etc/shadow"}]},
            link_preview.LinkPreviewBundle(),
        )
        self.assertIsNone(forged)

    def test_record_context_rejects_non_cache_missing_directory_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            upload = root / "user-upload.txt"
            upload.write_text("user")
            directory = root / ("link_" + "c" * 64 + ".txt")
            directory.mkdir()
            outside = root.parent / ("outside-" + "d" * 16 + ".txt")
            outside.write_text("outside")
            self.addCleanup(lambda: outside.unlink(missing_ok=True))
            symlink = root / ("link_" + "d" * 64 + ".txt")
            symlink.symlink_to(outside)
            missing = root / ("link_" + "e" * 64 + ".txt")
            image_directory = root / ("link_image_" + "1" * 64 + ".jpg")
            image_directory.mkdir()
            image_symlink = root / ("link_image_" + "2" * 64 + ".jpg")
            image_symlink.symlink_to(outside)
            image_missing = root / ("link_image_" + "3" * 64 + ".jpg")
            valid_target = root / ("link_" + "f" * 64 + ".txt")
            valid_target.write_text("valid only through canonical path")
            alias = root.parent / ("attachments-alias-" + "4" * 16)
            alias.symlink_to(root, target_is_directory=True)
            self.addCleanup(lambda: alias.unlink(missing_ok=True))
            parent_symlink_alias = alias / valid_target.name
            handler = object.__new__(push.PushHandler)
            handler.state = types.SimpleNamespace(attachments_dir=root)
            record = {
                "metadata": {
                    "link_previews": [
                        {"content_path": str(upload), "image_paths": [str(image_directory), str(image_symlink), str(image_missing)]},
                        {"content_path": str(directory)},
                        {"content_path": str(symlink)},
                        {"content_path": str(missing)},
                        {"content_path": str(parent_symlink_alias)},
                    ]
                }
            }
            self.assertEqual(handler._link_context_from_record(record), "")

    def test_kairos_send_attaches_metadata_and_queues_same_context(self):
        handler = object.__new__(push.PushHandler)
        bundle = link_preview.LinkPreviewBundle(
            ({"title": "T", "content_path": "/safe/link.txt"},), "CTX"
        )
        handler._enrich_user_links = lambda text: bundle
        appended = {}

        class Chat:
            def append(self, **kwargs):
                appended.update(kwargs)
                return {"ts": "2026-01-01T00:00:00.000+00:00", **kwargs}

        class State:
            pass

        handler.state = State()
        queued = []
        handler._chat_for_contact = lambda contact: Chat()
        handler._set_typing_for_contact = lambda *args, **kwargs: None
        handler._clear_chat_draft = lambda *args, **kwargs: None
        handler._enqueue_kairos_task = queued.append
        handler._send_json = lambda *args, **kwargs: None
        handler._handle_kairos_chat_send({"text": "https://example.com"}, "kairos")
        self.assertEqual(appended["metadata"]["link_previews"][0]["content_path"], "/safe/link.txt")
        self.assertEqual(queued[0]["link_context"], "CTX")

    def test_xiaoke_busy_queue_receives_preview_metadata_and_ai_context(self):
        handler = object.__new__(push.PushHandler)
        bundle = link_preview.LinkPreviewBundle(
            ({"title": "T", "content_path": "/safe/link.txt"},), "CTX"
        )
        handler._contact_id_from_body = lambda body: "xiaoke"
        handler._source_for_request = lambda *args: "test"
        handler._enrich_user_links = lambda text: bundle
        captured = {}
        handler._queue_xiaoke_busy_chat_send = lambda **kwargs: captured.update(kwargs)

        class State:
            xiaoke_stop_lock = threading.RLock()
            typing_state = {"is_typing": True}
            xiaoke_stopping_claim = {}
            xiaoke_send_reservation = {}

        handler.state = State()
        handler._handle_chat_send({"contact_id": "xiaoke", "text": "https://example.com"})
        self.assertIn("CTX", captured["injection_text"])
        self.assertEqual(
            captured["metadata"]["link_previews"][0]["content_path"],
            "/safe/link.txt",
        )

    def test_apples_user_message_stores_preview_metadata_without_changing_text(self):
        handler = object.__new__(push.PushHandler)
        bundle = link_preview.LinkPreviewBundle(
            ({"title": "T", "content_path": "/safe/link.txt"},), "CTX"
        )
        handler._enrich_user_links = lambda text: bundle
        appended = {}

        class Chat:
            def append(self, **kwargs):
                appended.update(kwargs)
                return {"ts": "2026-01-01T00:00:00.000+00:00", **kwargs}

        handler._chat_for_contact = lambda contact: Chat()
        handler._normalize_mentioned_member_ids = lambda value: set()
        handler._detect_apples_mentions = lambda text: set()
        handler._apples_self_id = lambda: "astra"
        handler._apples_member_name = lambda member: member
        handler._source_for_request = lambda *args: "test"
        handler._send_json = lambda *args, **kwargs: None
        original = "看看 https://example.com"
        handler._handle_apples_chat_send({"text": original}, "apples")
        self.assertEqual(appended["text"], original)
        self.assertEqual(
            appended["metadata"]["link_previews"][0]["content_path"],
            "/safe/link.txt",
        )

    def test_legacy_group_send_cannot_trigger_fetch_with_forged_amian(self):
        handler = object.__new__(push.PushHandler)
        handler._enrich_user_links = lambda text: (_ for _ in ()).throw(AssertionError("must not fetch"))
        handler._group_online_agents = lambda: set()
        handler._source_for_request = lambda *args: "test"
        handler._send_json = lambda *args, **kwargs: None

        class Group:
            normalize_mentions = staticmethod(lambda mentions, text: [])
            targets_for = staticmethod(lambda sender, mentions, online, hop_count=0: [])
            append = staticmethod(lambda sender, text, **kwargs: {"id": "g1", "ts": "t", "text": text})

        handler.state = types.SimpleNamespace(group_chat=Group())
        type(handler)._group_dedupe_cache = {}
        handler._handle_group_send({"sender_id": "amian", "text": "https://example.com/forged"})

    def test_user_upload_caption_attaches_preview_and_kairos_ai_context(self):
        handler = object.__new__(push.PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        attachments = Path(tmp.name)
        bundle = link_preview.LinkPreviewBundle(
            ({"title": "T", "content_path": str(attachments / "link_x.txt")},),
            "UPLOAD LINK CONTEXT",
        )
        handler._enrich_user_links = lambda text: bundle
        appended = {}

        class Chat:
            def append(self, **kwargs):
                appended.update(kwargs)
                return {"ts": "2026-01-01T00:00:00.000+00:00", **kwargs}

        queued = []
        response = {}
        handler.state = types.SimpleNamespace(
            attachments_dir=attachments,
            contact_chats={"kairos": object(), "xiaoke": object()},
        )
        handler.path = "/chat/upload?contact_id=kairos&filename=a.jpg&role=user&text=https%3A%2F%2Fexample.com"
        handler.headers = {"Content-Length": "4"}
        handler.rfile = io.BytesIO(b"jpeg")
        handler._chat_for_contact = lambda contact: Chat()
        handler._source_for_request = lambda *args: "test"
        handler._clear_chat_draft = lambda *args: None
        handler._enqueue_kairos_task = queued.append
        handler._send_json = lambda status, payload: response.update(status=status, payload=payload)
        handler._handle_chat_upload()
        self.assertEqual(response["status"], 200)
        self.assertIn("link_previews", appended["metadata"])
        self.assertEqual(queued[0]["link_context"], "UPLOAD LINK CONTEXT")

    def test_assistant_upload_never_triggers_link_fetch(self):
        handler = object.__new__(push.PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        attachments = Path(tmp.name)
        handler._enrich_user_links = lambda text: (_ for _ in ()).throw(AssertionError("must not fetch"))

        class Chat:
            def append(self, **kwargs):
                return {"ts": "t", **kwargs}

        handler.state = types.SimpleNamespace(
            attachments_dir=attachments,
            contact_chats={"kairos": object(), "xiaoke": object()},
        )
        handler.path = "/chat/upload?contact_id=kairos&filename=a.txt&role=assistant&text=https%3A%2F%2Fexample.com"
        handler.headers = {"Content-Length": "1"}
        handler.rfile = io.BytesIO(b"x")
        handler._chat_for_contact = lambda contact: Chat()
        handler._source_for_request = lambda *args: "test"
        handler._send_json = lambda *args, **kwargs: None
        handler._handle_chat_upload()

    def test_xiaoke_user_upload_caption_injects_same_link_context(self):
        handler = object.__new__(push.PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        attachments = Path(tmp.name)
        bundle = link_preview.LinkPreviewBundle(
            ({"title": "T", "content_path": str(attachments / "link_x.txt")},),
            "XIAOKE UPLOAD CONTEXT",
        )
        handler._enrich_user_links = lambda text: bundle

        class Chat:
            def append(self, **kwargs):
                return {"ts": "t", **kwargs}

        injected = []
        handler.state = types.SimpleNamespace(
            attachments_dir=attachments,
            contact_chats={"kairos": object(), "xiaoke": object()},
            active_session="cctg",
            default_session="cctg",
            channel_transport_fallback_to_tmux=True,
        )
        handler.path = "/chat/upload?contact_id=xiaoke&filename=a.txt&role=user&text=https%3A%2F%2Fexample.com"
        handler.headers = {"Content-Length": "1"}
        handler.rfile = io.BytesIO(b"x")
        handler._chat_for_contact = lambda contact: Chat()
        handler._source_for_request = lambda *args: "test"
        handler._channel_transport_enabled_for = lambda contact: False
        handler._inject_to_session = lambda session, text, **kwargs: (injected.append(text) or True, "")
        handler._send_json = lambda *args, **kwargs: None
        handler._handle_chat_upload()
        self.assertIn("XIAOKE UPLOAD CONTEXT", injected[0])

    def _serve_attachment(self, filename: str, *, head: bool) -> types.SimpleNamespace:
        handler = object.__new__(push.PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        attachments = Path(tmp.name)
        (attachments / filename).write_bytes(b"content")
        handler.state = types.SimpleNamespace(attachments_dir=attachments)
        handler.path = f"/attachments/{filename}"
        result = types.SimpleNamespace(status=None, headers={}, body=io.BytesIO())
        handler.send_response = lambda status: setattr(result, "status", status)
        handler.send_header = lambda key, value: result.headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        handler.wfile = result.body
        handler._send_json = lambda status, payload: setattr(result, "status", status)
        if head:
            handler._handle_attachment_head()
        else:
            handler._handle_attachment_get()
        return result

    def test_link_artifact_get_and_head_are_private_no_store(self):
        for filename in ("link_" + "a" * 64 + ".txt", "link_image_" + "b" * 64 + ".jpg"):
            with self.subTest(filename=filename):
                self.assertEqual(
                    self._serve_attachment(filename, head=False).headers["Cache-Control"],
                    "private, no-store",
                )
                self.assertEqual(
                    self._serve_attachment(filename, head=True).headers["Cache-Control"],
                    "private, no-store",
                )
        self.assertEqual(
            self._serve_attachment("ordinary.jpg", head=False).headers["Cache-Control"],
            "private, no-store",
        )

    def test_xhs_cli_adapter_passes_upgraded_short_url_only_via_stdin(self):
        adapter = {
            "title": "note",
            "body_text": "body",
            "comments": ["作者：评论"],
            "comments_fetched": True,
            "comments_complete": True,
        }
        script = (
            "import json,sys; json.load(sys.stdin); "
            f"print({json.dumps(json.dumps(adapter, ensure_ascii=False))})"
        )
        command = [sys.executable, "-c", script]
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, xhs_cli_command=command)
            bundle = service.enrich("http://xhslink.com/o/abc?xsec_token=opaque#fragment")
        self.assertEqual(bundle.previews[0]["provider"], "xhs-cli")
        self.assertEqual(bundle.previews[0]["comments_status"], "included")
        self.assertIn("必须先读取该全文文件", bundle.prompt_context)
        self.assertIn("内容图片只是帖子配图", bundle.prompt_context)
        self.assertNotIn("xhslink", " ".join(command))

    def test_xhs_cli_adapter_accepts_cn_short_url_and_upgrades_http(self):
        adapter = {
            "title": "note",
            "body_text": "body",
            "comments": ["作者：评论"],
            "comments_fetched": True,
            "comments_complete": True,
        }
        script = (
            "import json,sys; "
            "request=json.load(sys.stdin); "
            "assert request['url'] == 'https://xhslink.cn/o/abc'; "
            f"print({json.dumps(json.dumps(adapter, ensure_ascii=False))})"
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, xhs_cli_command=[sys.executable, "-c", script]
            )
            bundle = service.enrich("http://xhslink.cn/o/abc#fragment")
        self.assertEqual(bundle.previews[0]["provider"], "xhs-cli")
        self.assertEqual(bundle.previews[0]["comments_status"], "included")
        self.assertTrue(service._is_xhs("https://xhslink.cn/o/abc"))

    def test_xhs_cli_retries_once_with_http_resolved_tokenized_url(self):
        short_url = "https://xhslink.com/o/abc"
        final_url = (
            "https://www.xiaohongshu.com/explore/1234567890abcdef"
            "?xsec_token=opaque&xsec_source=pc_share"
        )
        http_payload = link_preview.HTTPPayload(
            final_url,
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><head><meta property='og:title' content='HTTP note'></head>"
            b"<body>HTTP body</body></html>",
        )
        adapter_page = link_preview.ExtractedPage(
            requested_url=final_url,
            final_url=final_url,
            title="adapter note",
            description="",
            site_name="xhs-cli",
            image_url="",
            body_text="adapter body",
            comments="author: comment",
            comments_fetched=True,
            comments_complete=True,
            provider="xhs-cli",
        )
        calls = []

        def adapter(url, deadline):
            calls.append(url)
            if url == short_url:
                raise link_preview.LinkPreviewError("short link rejected")
            return adapter_page

        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                xhs_cli_command=["unused"],
                windows_api_url="https://windows.example/preview",
                fetcher=QueueFetcher([http_payload]),
            )
            service._call_command_adapter = adapter
            bundle = service.enrich(short_url)

        self.assertEqual(calls, [short_url, final_url])
        self.assertEqual(bundle.previews[0]["provider"], "xhs-cli")
        self.assertEqual(bundle.previews[0]["comments_status"], "included")

    def test_xhs_cli_second_failure_keeps_http_preview_without_repeating(self):
        short_url = "https://xhslink.com/o/abc"
        final_url = (
            "https://www.xiaohongshu.com/explore/1234567890abcdef"
            "?xsec_token=opaque"
        )
        http_payload = link_preview.HTTPPayload(
            final_url,
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><head><meta property='og:title' content='HTTP note'></head>"
            b"<body>HTTP body</body></html>",
        )
        calls = []

        def rejected(url, deadline):
            calls.append(url)
            raise link_preview.LinkPreviewError("adapter rejected")

        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td, xhs_cli_command=["unused"], fetcher=QueueFetcher([http_payload])
            )
            service._call_command_adapter = rejected
            bundle = service.enrich(short_url)

        self.assertEqual(calls, [short_url, final_url])
        self.assertEqual(bundle.previews[0]["provider"], "http")
        self.assertEqual(bundle.previews[0]["comments_status"], "not_fetched")

    def test_xhs_cli_does_not_retry_same_untokenized_or_untrusted_final_url(self):
        cases = (
            "https://xhslink.com/o/abc",
            "https://www.xiaohongshu.com/explore/1234567890abcdef",
            "https://evil.example/explore/1234567890abcdef?xsec_token=opaque",
        )
        for final_url in cases:
            with self.subTest(final_url=final_url):
                short_url = "https://xhslink.com/o/abc"
                http_payload = link_preview.HTTPPayload(
                    final_url,
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    b"<html><head><title>HTTP note</title></head><body>HTTP body</body></html>",
                )
                calls = []

                def rejected(url, deadline):
                    calls.append(url)
                    raise link_preview.LinkPreviewError("adapter rejected")

                with tempfile.TemporaryDirectory() as td:
                    service = link_preview.LinkPreviewService(
                        td, xhs_cli_command=["unused"], fetcher=QueueFetcher([http_payload])
                    )
                    service._call_command_adapter = rejected
                    bundle = service.enrich(short_url)

                self.assertEqual(calls, [short_url])
                self.assertEqual(bundle.previews[0]["provider"], "http")
                self.assertEqual(bundle.previews[0]["comments_status"], "not_fetched")

    def test_xhs_cli_url_rejects_userinfo_bad_port_and_host_suffix(self):
        rejected = (
            "https://user@xhslink.com/o/a",
            "https://xhslink.com:444/o/a",
            "https://xhslink.cn.evil.example/o/a",
            "http://xhslink.cn:81/o/a",
            "https://xiaohongshu.com.evil.example/o/a",
            "http://www.xiaohongshu.com/o/a",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(link_preview.LinkPreviewError):
                link_preview.LinkPreviewService._xhs_cli_url(url)

    def test_xhs_cli_timeout_large_output_and_failure_fall_back(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "child-survived"
            child = f"import time,pathlib; time.sleep(.4); pathlib.Path({str(marker)!r}).write_text('bad')"
            parent = (
                "import json,subprocess,sys,time; json.load(sys.stdin); "
                f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(5)"
            )
            service = link_preview.LinkPreviewService(
                td, xhs_cli_command=[sys.executable, "-c", parent]
            )
            with self.assertRaises(link_preview.LinkPreviewError):
                service._call_command_adapter("https://xhslink.com/o/a", time.monotonic() + 0.1)
            time.sleep(0.5)
            self.assertFalse(marker.exists(), "deadline must kill the adapter process group")

            large = "import json,sys; json.load(sys.stdin); sys.stdout.write('x'*2000001)"
            service = link_preview.LinkPreviewService(
                td, xhs_cli_command=[sys.executable, "-c", large]
            )
            with self.assertRaises(link_preview.LinkPreviewError):
                service._call_command_adapter("https://xhslink.com/o/a", time.monotonic() + 3)

        api_payload = link_preview.HTTPPayload(
            "https://adapter.example", 200, {"content-type": "application/json"},
            json.dumps({"title": "api fallback", "text": "body"}).encode(),
        )
        command = [sys.executable, "-c", "import sys; sys.stdin.read(); raise SystemExit(2)"]
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(
                td,
                xhs_cli_command=command,
                xhs_api_url="https://adapter.example/preview",
                adapter_fetcher=QueueFetcher([api_payload]),
            )
            result = service.enrich("https://xhslink.com/o/a")
        self.assertEqual(result.previews[0]["provider"], "xhs-api")

    def test_xhs_cli_empty_comments_are_fetched_not_missing(self):
        payload = link_preview.HTTPPayload(
            "xhs-cli://adapter", 200, {"content-type": "application/json"},
            json.dumps({
                "title": "note", "body_text": "body", "comments": [],
                "comments_fetched": True, "comments_complete": True,
            }).encode(),
        )
        page = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/a", payload, "xhs-cli", 1000
        )
        self.assertTrue(page.comments_fetched)
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: page
            bundle = service.enrich("https://xhslink.com/o/a")
            preview = bundle.previews[0]
            content = Path(preview["content_path"]).read_text()
        self.assertEqual(preview["comments_status"], "fetched_empty")
        self.assertIn("评论抓取状态：已抓取，当前暂无评论", content)
        self.assertIn("评论已抓取，当前返回为空", bundle.prompt_context)

    def test_xhs_comment_scope_and_boolean_flags_are_strict(self):
        partial_payload = link_preview.HTTPPayload(
            "xhs-cli://adapter", 200, {"content-type": "application/json"},
            json.dumps({
                "title": "note", "body_text": "body", "comments": ["甲：首批"],
                "comments_fetched": True, "comments_complete": False,
            }).encode(),
        )
        partial = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/a", partial_payload, "xhs-cli", 1000
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: partial
            bundle = service.enrich("https://xhslink.com/o/a")
            content = Path(bundle.previews[0]["content_path"]).read_text()
        self.assertEqual(bundle.previews[0]["comments_status"], "included_partial")
        self.assertIn("仅抓取首批", content)
        self.assertIn("可能有更多评论或楼中楼", bundle.prompt_context)
        self.assertIn("必须先读取该全文文件", bundle.prompt_context)
        self.assertIn("内容图片只是帖子配图", bundle.prompt_context)

        false_string = link_preview.HTTPPayload(
            "xhs-cli://adapter", 200, {"content-type": "application/json"},
            json.dumps({
                "title": "note", "body_text": "body", "comments": [],
                "comments_fetched": "false", "comments_complete": "true",
            }).encode(),
        )
        page = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/a", false_string, "xhs-cli", 1000
        )
        self.assertFalse(page.comments_fetched)
        self.assertFalse(page.comments_complete)

        auth_payload = link_preview.HTTPPayload(
            "xhs-cli://adapter", 200, {"content-type": "application/json"},
            json.dumps({
                "title": "note", "body_text": "body", "comments": [],
                "comments_fetched": False, "comments_complete": False,
                "comments_auth_required": True,
            }).encode(),
        )
        auth_page = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/a", auth_payload, "xhs-cli", 1000
        )
        self.assertTrue(auth_page.comments_auth_required)
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td)
            service._fetch_page = lambda url, deadline: auth_page
            bundle = service.enrich("https://xhslink.com/o/a")
            content = Path(bundle.previews[0]["content_path"]).read_text()
        self.assertEqual(bundle.previews[0]["comments_status"], "login_required")
        self.assertIn("登录已失效", content)
        self.assertIn("提醒用户重新登录", bundle.prompt_context)

        auth_string = link_preview.HTTPPayload(
            "xhs-cli://adapter", 200, {"content-type": "application/json"},
            json.dumps({
                "title": "note", "body_text": "body", "comments": [],
                "comments_auth_required": "true",
            }).encode(),
        )
        strict_page = link_preview.LinkPreviewService._adapter_page(
            "https://xhslink.com/o/a", auth_string, "xhs-cli", 1000
        )
        self.assertFalse(strict_page.comments_auth_required)

    def test_successful_login_invalidates_only_xhs_comment_failures(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, cache_ttl_seconds=60)
            xhs_auth_url = "https://xhslink.com/o/auth"
            xhs_other_url = "https://xhslink.com/o/other"
            generic_url = "https://example.com/article"
            xhs_included_url = "https://xhslink.com/o/included"
            pages = {
                xhs_auth_url: link_preview.ExtractedPage(
                    requested_url=xhs_auth_url, final_url=xhs_auth_url, title="auth",
                    description="", site_name="小红书", image_url="", body_text="body",
                    comments_auth_required=True, provider="xhs-cli",
                ),
                xhs_other_url: link_preview.ExtractedPage(
                    requested_url=xhs_other_url, final_url=xhs_other_url, title="other",
                    description="", site_name="小红书", image_url="", body_text="body",
                ),
                generic_url: link_preview.ExtractedPage(
                    requested_url=generic_url, final_url=generic_url, title="generic",
                    description="", site_name="Example", image_url="", body_text="body",
                ),
                xhs_included_url: link_preview.ExtractedPage(
                    requested_url=xhs_included_url, final_url=xhs_included_url,
                    title="included", description="", site_name="小红书", image_url="",
                    body_text="body", comments="甲：评论", comments_fetched=True,
                    comments_complete=True, provider="xhs-cli",
                ),
            }
            service._fetch_page = lambda url, deadline: pages[url]
            for url in pages:
                service.enrich(url)

            self.assertEqual(service.invalidate_xhs_comment_failures(), 2)
            # Keep leased paths intact for already-queued turns, but force the
            # next lookup through a fresh fetch by invalidating the schema.
            self.assertTrue(service._paths(xhs_auth_url)[0].exists())
            self.assertTrue(service._paths(xhs_auth_url)[1].exists())
            self.assertIsNone(service._load_cache(xhs_auth_url, time.monotonic() + 1))
            self.assertIsNone(service._load_cache(xhs_other_url, time.monotonic() + 1))
            self.assertTrue(service._paths(generic_url)[1].exists())
            self.assertIsNotNone(service._load_cache(generic_url, time.monotonic() + 1))
            self.assertIsNotNone(service._load_cache(xhs_included_url, time.monotonic() + 1))

    def test_login_boundary_makes_inflight_xhs_comment_failures_uncacheable(self):
        for failure_status in ("login_required", "not_fetched"):
            with self.subTest(failure_status=failure_status), tempfile.TemporaryDirectory() as td:
                url = f"https://xhslink.com/o/race-{failure_status}"
                image_url = "https://sns-webpic-qc.xhscdn.com/race.jpg"
                fetch_started = threading.Event()
                release_fetch = threading.Event()
                calls = []
                service = link_preview.LinkPreviewService(td, total_timeout=5)
                image_key = service._image_url_key(image_url, xhs_only=True)
                image_path = Path(td) / f"link_image_{image_key}.jpg"

                failure_page = link_preview.ExtractedPage(
                    requested_url=url,
                    final_url=url,
                    title="old failure",
                    description="",
                    site_name="小红书",
                    image_url=image_url,
                    body_text="body needed by the in-flight turn",
                    comments_auth_required=failure_status == "login_required",
                    provider="xhs-cli" if failure_status == "login_required" else "http",
                )
                fresh_page = link_preview.ExtractedPage(
                    requested_url=url,
                    final_url=url,
                    title="fresh success",
                    description="",
                    site_name="小红书",
                    image_url=image_url,
                    body_text="fresh body",
                    comments="甲：新评论",
                    comments_fetched=True,
                    comments_complete=True,
                    provider="xhs-cli",
                )

                def fetch(_url, _deadline):
                    calls.append(_url)
                    if len(calls) == 1:
                        fetch_started.set()
                        self.assertTrue(release_fetch.wait(timeout=2))
                        return failure_page
                    return fresh_page

                service._fetch_page = fetch
                service._download_image_candidate = (
                    lambda _url, _deadline, **_kwargs: (image_path, b"image")
                )
                results = []
                worker = threading.Thread(target=lambda: results.append(service.enrich(url)))
                worker.start()
                self.assertTrue(fetch_started.wait(timeout=2))

                # The old implementation skipped this active key and allowed
                # its later failure commit to remain cacheable.
                self.assertEqual(service.invalidate_xhs_comment_failures(), 0)
                release_fetch.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())

                old_preview = results[0].previews[0]
                self.assertEqual(old_preview["comments_status"], failure_status)
                self.assertTrue(Path(old_preview["content_path"]).is_file())
                self.assertTrue(image_path.is_file())
                self.assertIsNone(service._load_cache(url, time.monotonic() + 1))

                fresh = service.enrich(url).previews[0]
                self.assertEqual(len(calls), 2)
                self.assertEqual(fresh["comments_status"], "included")
                self.assertTrue(image_path.is_file())

    def test_login_boundary_invalidates_failure_while_active_reader_uses_it(self):
        with tempfile.TemporaryDirectory() as td:
            url = "https://xhslink.com/o/active-cached-failure"
            cache_read = threading.Event()
            release_reader = threading.Event()
            service = link_preview.LinkPreviewService(td, total_timeout=5)
            failure_page = link_preview.ExtractedPage(
                requested_url=url,
                final_url=url,
                title="cached failure",
                description="",
                site_name="小红书",
                image_url="",
                body_text="leased body",
                comments_auth_required=True,
                provider="xhs-cli",
            )
            service._fetch_page = lambda _url, _deadline: failure_page
            initial = service.enrich(url).previews[0]
            self.assertEqual(initial["comments_status"], "login_required")

            original_load = service._load_cache

            def controlled_load(_url, deadline):
                cached = original_load(_url, deadline)
                cache_read.set()
                self.assertTrue(release_reader.wait(timeout=2))
                return cached

            service._load_cache = controlled_load
            results = []
            worker = threading.Thread(target=lambda: results.append(service.enrich(url)))
            worker.start()
            self.assertTrue(cache_read.wait(timeout=2))

            # The reader already owns a safe in-memory snapshot and leased
            # path. Invalidating the sidecar commit marker does not delete or
            # mutate either, even though this cache key is active.
            self.assertEqual(service.invalidate_xhs_comment_failures(), 1)
            release_reader.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0].previews[0]["comments_status"], "login_required")
            self.assertTrue(Path(results[0].previews[0]["content_path"]).is_file())

            service._load_cache = original_load
            self.assertIsNone(service._load_cache(url, time.monotonic() + 1))

    def test_login_boundary_preserves_inflight_xhs_comment_success(self):
        with tempfile.TemporaryDirectory() as td:
            url = "https://xhslink.com/o/race-success"
            fetch_started = threading.Event()
            release_fetch = threading.Event()
            service = link_preview.LinkPreviewService(td, total_timeout=5)
            page = link_preview.ExtractedPage(
                requested_url=url,
                final_url=url,
                title="included",
                description="",
                site_name="小红书",
                image_url="",
                body_text="body",
                comments="甲：评论",
                comments_fetched=True,
                comments_complete=True,
                provider="xhs-cli",
            )

            def fetch(_url, _deadline):
                fetch_started.set()
                self.assertTrue(release_fetch.wait(timeout=2))
                return page

            service._fetch_page = fetch
            results = []
            worker = threading.Thread(target=lambda: results.append(service.enrich(url)))
            worker.start()
            self.assertTrue(fetch_started.wait(timeout=2))
            self.assertEqual(service.invalidate_xhs_comment_failures(), 0)
            release_fetch.set()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

            self.assertEqual(results[0].previews[0]["comments_status"], "included")
            cached = service._load_cache(url, time.monotonic() + 1)
            self.assertIsNotNone(cached)
            self.assertEqual(cached["comments_status"], "included")

    def test_xhs_cache_schema_upgrade_invalidates_previous_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, cache_ttl_seconds=60)
            url = "https://xhslink.com/o/cache"
            text_path, meta_path = service._paths(url)
            text_path.write_text("old")
            meta_path.write_text(json.dumps({
                "schema_version": 7,
                "cache_key": service._url_key(url),
                "lease_until": int(time.time() + 60),
                "image_paths": [],
            }))
            self.assertIsNone(service._load_cache(url, time.monotonic() + 1))
        self.assertEqual(link_preview.XHS_CACHE_SCHEMA_VERSION, 8)

    def test_xiachufang_recipe_url_validation_and_mobile_rewrite(self):
        allowed = (
            "https://xiachufang.com/recipe/104126605/",
            "https://www.xiachufang.com/recipe/104126605?from=share",
        )
        for url in allowed:
            with self.subTest(url=url):
                self.assertTrue(link_preview.LinkPreviewService._is_xiachufang_recipe(url))
                self.assertEqual(
                    link_preview.LinkPreviewService._xiachufang_mobile_recipe_url(url),
                    "https://m.xiachufang.com/recipe/104126605/",
                )
        rejected = (
            "https://evilxiachufang.com/recipe/104126605/",
            "https://www.xiachufang.com/recipe/not-a-number/",
            "http://m.xiachufang.com/recipe/104126605/",
            "https://user:pass@m.xiachufang.com/recipe/104126605/",
            "https://m.xiachufang.com/not-recipe/104126605/",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(link_preview.LinkPreviewService._is_xiachufang_recipe(url))
                with self.assertRaises(link_preview.LinkPreviewError):
                    link_preview.LinkPreviewService._xiachufang_mobile_recipe_url(url)

    def test_xiachufang_recipe_jsonld_extracts_only_recipe_fields(self):
        recipe = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": "番茄炖牛腩",
            "description": "酸甜下饭。",
            "author": {"@type": "Person", "name": "Astra"},
            "image": ["https://img.example.test/cover.jpg"],
            "recipeIngredient": ["牛腩 500g", "番茄 3 个"],
            "recipeInstructions": [
                {"@type": "HowToStep", "text": "牛腩焯水。"},
                {
                    "@type": "HowToSection",
                    "name": "炖煮",
                    "itemListElement": [
                        {"@type": "HowToStep", "text": "加番茄小火炖一小时。"},
                    ],
                },
            ],
        }
        shell = {"@graph": [{"@type": "WebPage", "name": "推荐菜谱"}, recipe]}
        html = f"""<html><head><script type='application/ld+json'>{json.dumps(shell, ensure_ascii=False)}</script>
        </head><body>滑到最后看推荐菜谱 页面壳</body></html>""".encode()
        payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200,
            {"content-type": "text/html; charset=utf-8"}, html,
        )
        page = link_preview.extract_html_page(
            "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=4_000
        )
        self.assertEqual(page.title, "番茄炖牛腩")
        self.assertEqual(page.description, "酸甜下饭。")
        self.assertEqual(page.site_name, "下厨房")
        self.assertEqual(page.image_url, "https://img.example.test/cover.jpg")
        self.assertIn("作者：Astra", page.body_text)
        self.assertIn("- 牛腩 500g", page.body_text)
        self.assertIn("1. 牛腩焯水。", page.body_text)
        self.assertIn("2. 炖煮", page.body_text)
        self.assertIn("3. 加番茄小火炖一小时。", page.body_text)
        self.assertNotIn("推荐菜谱", page.body_text)
        self.assertNotIn("页面壳", page.body_text)

    def test_xiachufang_zero_based_instruction_string_is_split_without_decimal_false_positive(self):
        recipe = {
            "@type": "Recipe",
            "name": "真实形态步骤",
            "recipeInstructions": "0.鸡腿洗净，1.碗里加一勺生抽,2.加水后炖 20 分钟。",
        }
        html = f"<script type='application/ld+json'>{json.dumps(recipe, ensure_ascii=False)}</script>".encode()
        payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200, {"content-type": "text/html"}, html
        )
        page = link_preview.extract_html_page(
            "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=4_000
        )
        self.assertIn("1. 鸡腿洗净", page.body_text)
        self.assertIn("2. 碗里加一勺生抽", page.body_text)
        self.assertIn("3. 加水后炖 20 分钟。", page.body_text)
        self.assertNotIn("0.鸡腿", page.body_text)

        decimal_recipe = {
            "@type": "Recipe",
            "name": "小数不拆",
            "recipeInstructions": "0.5 千克鸡腿，加入 1.5 勺盐，静置 2.0 小时。",
        }
        decimal_html = f"<script type='application/ld+json'>{json.dumps(decimal_recipe, ensure_ascii=False)}</script>".encode()
        decimal_payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200,
            {"content-type": "text/html"}, decimal_html,
        )
        decimal_page = link_preview.extract_html_page(
            "https://www.xiachufang.com/recipe/104126605/", decimal_payload, max_text_chars=4_000
        )
        self.assertIn("1. 0.5 千克鸡腿，加入 1.5 勺盐，静置 2.0 小时。", decimal_page.body_text)

    def test_xiachufang_recipe_selects_unique_exact_identity_over_recommendation(self):
        recommended = {"@type": "Recipe", "@id": "/recipe/999999/", "name": "推荐菜"}
        target = {
            "@type": "Recipe",
            "mainEntityOfPage": {"@id": "https://m.xiachufang.com/recipe/104126605/"},
            "name": "目标菜谱",
            "recipeInstructions": "目标步骤。",
        }
        html = f"<script type='application/ld+json'>{json.dumps({'@graph': [recommended, target]}, ensure_ascii=False)}</script>".encode()
        payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200, {"content-type": "text/html"}, html
        )
        page = link_preview.extract_html_page(
            "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=1_000
        )
        self.assertEqual(page.title, "目标菜谱")

    def test_xiachufang_multiple_unidentified_recipes_fail_closed(self):
        recipes = {"@graph": [
            {"@type": "Recipe", "name": "推荐菜"},
            {"@type": "Recipe", "name": "另一个推荐菜"},
        ]}
        payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200, {"content-type": "text/html"},
            f"<script type='application/ld+json'>{json.dumps(recipes, ensure_ascii=False)}</script>".encode(),
        )
        with self.assertRaises(link_preview.LinkPreviewError):
            link_preview.extract_html_page(
                "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=1_000
            )

    def test_xiachufang_similar_identity_path_does_not_match_target(self):
        similar = {"@type": "Recipe", "@id": "/recipe/104126605/evil", "name": "相似恶意路径"}
        target = {
            "@type": "Recipe", "@id": "/recipe/104126605/", "name": "精确目标",
            "recipeInstructions": "目标步骤。",
        }
        payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200, {"content-type": "text/html"},
            f"<script type='application/ld+json'>{json.dumps([similar, target], ensure_ascii=False)}</script>".encode(),
        )
        page = link_preview.extract_html_page(
            "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=1_000
        )
        self.assertEqual(page.title, "精确目标")

    def test_xiachufang_final_recipe_id_must_match_requested_target(self):
        recipe = {
            "@type": "Recipe", "@id": "https://m.xiachufang.com/recipe/999999/",
            "name": "错误重定向目标", "recipeInstructions": "不应采用。",
        }
        payload = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/999999/", 200, {"content-type": "text/html"},
            f"<script type='application/ld+json'>{json.dumps(recipe, ensure_ascii=False)}</script>".encode(),
        )
        with self.assertRaises(link_preview.LinkPreviewError):
            link_preview.extract_html_page(
                "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=1_000
            )

    def test_xiachufang_absolute_recipe_identity_requires_https_official_default_port(self):
        invalid = (
            "http://m.xiachufang.com/recipe/104126605/",
            "https://m.xiachufang.com:444/recipe/104126605/",
            "https://evil.example/recipe/104126605/",
        )
        for identity in invalid:
            with self.subTest(identity=identity):
                recipe = {"@type": "Recipe", "@id": identity, "name": "不能匹配"}
                self.assertEqual(link_preview._xiachufang_recipe_candidate_ids(recipe), set())

    def test_xiachufang_challenge_or_missing_recipe_fails_open(self):
        challenge = "<html><body>请完成安全验证，拖动滑块进行滑动验证</body></html>".encode()
        no_recipe = "<html><body>普通页面壳和推荐菜谱</body></html>".encode()
        for body in (challenge, no_recipe):
            with self.subTest(body=body):
                payload = link_preview.HTTPPayload(
                    "https://m.xiachufang.com/recipe/104126605/", 200,
                    {"content-type": "text/html"}, body,
                )
                with self.assertRaises(link_preview.LinkPreviewError):
                    link_preview.extract_html_page(
                        "https://www.xiachufang.com/recipe/104126605/", payload, max_text_chars=1_000
                    )

    def test_xiachufang_mobile_fetch_headers_and_final_host_boundary(self):
        recipe = {"@type": "Recipe", "name": "小炒肉", "recipeInstructions": "炒熟即可。"}
        good = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200, {"content-type": "text/html"},
            f"<script type='application/ld+json'>{json.dumps(recipe, ensure_ascii=False)}</script>".encode(),
        )
        original = "https://www.xiachufang.com/recipe/104126605/?share=ordinary"
        with tempfile.TemporaryDirectory() as td:
            fetcher = QueueFetcher([good])
            service = link_preview.LinkPreviewService(td, fetcher=fetcher, lease_seconds=1000)
            preview = service.enrich(original).previews[0]
            self.assertEqual(fetcher.calls[0][0], "https://m.xiachufang.com/recipe/104126605/")
            headers = fetcher.calls[0][1]["headers"]
            self.assertIn("Mozilla/5.0", headers["User-Agent"])
            self.assertEqual(headers["Referer"], "https://m.xiachufang.com/")
            self.assertEqual(headers["Accept-Encoding"], "gzip")
            self.assertTrue(fetcher.calls[0][1]["allow_compression"])
            self.assertEqual(preview["url"], "https://www.xiachufang.com/recipe/104126605/")
            self.assertEqual(preview["final_url"], "https://m.xiachufang.com/recipe/104126605/")

        off_origin = link_preview.HTTPPayload(
            "https://public-evil.example/recipe/104126605/", 200, {"content-type": "text/html"}, b"<html></html>"
        )
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, fetcher=QueueFetcher([off_origin]))
            self.assertEqual(service.enrich(original).previews, ())

    def test_xiachufang_mobile_retries_with_compressed_https_contract(self):
        recipe = {"@type": "Recipe", "name": "重试菜谱", "recipeInstructions": "完成。"}
        good = link_preview.HTTPPayload(
            "https://m.xiachufang.com/recipe/104126605/", 200, {"content-type": "text/html"},
            f"<script type='application/ld+json'>{json.dumps(recipe, ensure_ascii=False)}</script>".encode(),
        )
        with tempfile.TemporaryDirectory() as td:
            fetcher = QueueFetcher([link_preview.LinkPreviewError("temporary"), good])
            service = link_preview.LinkPreviewService(td, fetcher=fetcher, lease_seconds=1000)
            page = service._fetch_xiachufang_page(
                "https://www.xiachufang.com/recipe/104126605/?token=user-secret&from=unsafe", time.monotonic() + 2
            )
        self.assertEqual(page.title, "重试菜谱")
        self.assertEqual(len(fetcher.calls), 2)
        first_headers = fetcher.calls[0][1]["headers"]
        second_headers = fetcher.calls[1][1]["headers"]
        self.assertNotEqual(first_headers["User-Agent"], second_headers["User-Agent"])
        self.assertEqual(fetcher.calls[0][0], "https://m.xiachufang.com/recipe/104126605/")
        self.assertEqual(
            fetcher.calls[1][0],
            "https://m.xiachufang.com/recipe/104126605/?from=singlemessage&utm_source=weixin",
        )
        self.assertNotIn("user-secret", fetcher.calls[1][0])
        self.assertNotIn("from=unsafe", fetcher.calls[1][0])
        for _url, kwargs in fetcher.calls:
            self.assertEqual(kwargs["headers"]["Accept-Encoding"], "gzip")
            self.assertTrue(kwargs["allow_compression"])
            self.assertEqual(kwargs["allowed_hosts"], link_preview.XIACHUFANG_HOSTS)
            self.assertEqual(kwargs["allowed_schemes"], {"https"})

    def test_xiachufang_cache_schema_upgrade_invalidates_previous_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            service = link_preview.LinkPreviewService(td, cache_ttl_seconds=60)
            url = "https://www.xiachufang.com/recipe/104126605/"
            text_path, meta_path = service._paths(url)
            text_path.write_text("old")
            meta_path.write_text(json.dumps({
                "schema_version": 0,
                "cache_key": service._url_key(url),
                "lease_until": int(time.time() + 60),
                "image_paths": [],
                "image_cache_urls": [],
            }))
            self.assertIsNone(service._load_cache(url, time.monotonic() + 1))
        self.assertEqual(link_preview.XIACHUFANG_CACHE_SCHEMA_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
