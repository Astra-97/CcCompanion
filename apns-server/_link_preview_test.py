from __future__ import annotations

import json
import io
import os
from pathlib import Path
import socket
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

            def stale_candidate(_url, _deadline):
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

    def test_metadata_merge_and_ai_prompt_path_injection(self):
        bundle = link_preview.LinkPreviewBundle(
            ({"title": "T", "content_path": "/safe/link.txt"},), "SAFE LINK CONTEXT"
        )
        meta = link_preview.merge_preview_metadata({"via": "card"}, bundle)
        self.assertEqual(meta["via"], "card")
        self.assertEqual(meta["link_previews"][0]["content_path"], "/safe/link.txt")

        handler = object.__new__(push.PushHandler)
        handler.state = type("State", (), {"attachments_dir": Path("/safe")})()
        prompt = handler._kairos_prompt_for_task({"text": "hello", "link_context": bundle.prompt_context})
        self.assertIn("SAFE LINK CONTEXT", prompt)
        rebuilt = handler._link_context_from_record({"metadata": meta})
        self.assertIn("/safe/link.txt", rebuilt)
        self.assertIn("不可信", rebuilt)
        forged = link_preview.merge_preview_metadata(
            {"link_previews": [{"content_path": "/etc/shadow"}]},
            link_preview.LinkPreviewBundle(),
        )
        self.assertIsNone(forged)

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
            "public, max-age=86400",
        )


if __name__ == "__main__":
    unittest.main()
