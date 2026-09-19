from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from mt_waimai_login import MT_WAIMAI_LOGIN_ORIGIN, MtWaimaiLoginError, MtWaimaiLoginManager


DEVICE = "device_1234567890abcdef"


class FakeRunner:
    def __init__(self, stdout: bytes = b'{"ok":true}', returncode: int = 0) -> None:
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=b"")


def status_payload(status: str) -> bytes:
    return json.dumps({"ok": True, "status": status, "page_url": "https://example/"}).encode("utf-8")


class MtWaimaiLoginManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.runner = FakeRunner()
        self.manager = MtWaimaiLoginManager(
            import_command=["ssh", "memory-sg", "/fixed/import_cookies.py"],
            status_command=["ssh", "memory-sg", "/fixed/status"],
            runner=self.runner,
            clock=lambda: self.now,
        )

    def start(self, contact: str = "kimi"):
        return self.manager.start(
            contact_id=contact, device_id=DEVICE, origin=MT_WAIMAI_LOGIN_ORIGIN
        )

    def import_once(self, nonce: str, **overrides):
        values = {
            "nonce": nonce,
            "contact_id": "kimi",
            "device_id": DEVICE,
            "origin": MT_WAIMAI_LOGIN_ORIGIN,
            "cookie_header": "token=one; uuid=two; openh5_uuid=three; ignored=drop",
        }
        values.update(overrides)
        return self.manager.import_cookies(**values)

    def test_fixed_argv_and_allowlisted_cookies_use_stdin_only(self):
        result = self.import_once(self.start()["nonce"])
        self.assertEqual(result, {"ok": True, "status": "stored"})
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(argv, ["ssh", "memory-sg", "/fixed/import_cookies.py"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("env", kwargs)
        payload = json.loads(kwargs["input"])
        self.assertEqual(
            payload["cookies"],
            {"token": "one", "uuid": "two", "openh5_uuid": "three"},
        )
        self.assertTrue(all("one" not in part for part in argv))

    def test_start_returns_waimai_login_url(self):
        result = self.start()
        self.assertEqual(
            result["login_url"],
            "https://h5.waimai.meituan.com/login?force=true",
        )

    def test_nonce_is_single_use_even_when_remote_fails(self):
        nonce = self.start()["nonce"]
        self.manager._runner = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=b"", stderr=b""
        )
        with self.assertRaises(MtWaimaiLoginError) as first:
            self.import_once(nonce)
        self.assertEqual(first.exception.code, "sync_failed")
        with self.assertRaises(MtWaimaiLoginError) as second:
            self.import_once(nonce)
        self.assertEqual(second.exception.code, "nonce_used")

    def test_expiry_and_binding_are_enforced(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(MtWaimaiLoginError) as mismatch:
            self.import_once(nonce, contact_id="kairos")
        self.assertEqual(mismatch.exception.code, "binding_mismatch")

        nonce = self.start()["nonce"]
        self.now += 301
        with self.assertRaises(MtWaimaiLoginError) as expired:
            self.import_once(nonce)
        self.assertEqual(expired.exception.code, "nonce_expired")

    def test_required_token_check_happens_before_consuming_nonce(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(MtWaimaiLoginError) as incomplete:
            self.import_once(nonce, cookie_header="uuid=two; openh5_uuid=three")
        self.assertEqual(incomplete.exception.code, "login_incomplete")
        # Validation failure did not burn a valid capability.
        self.assertTrue(self.import_once(nonce)["ok"])

    def test_only_allowed_contact_and_origin_can_start(self):
        for contact, origin in (("mallory", MT_WAIMAI_LOGIN_ORIGIN), ("kimi", "web")):
            with self.assertRaises(MtWaimaiLoginError):
                self.manager.start(contact_id=contact, device_id=DEVICE, origin=origin)

    def test_needs_login_tracks_waimai_status(self):
        self.runner.stdout = status_payload("logged_in")
        self.assertFalse(self.manager.needs_login())
        self.runner.stdout = status_payload("waiting")
        # Cached probe still serves; advance the clock past the cache TTL.
        self.assertFalse(self.manager.needs_login())
        self.now += 301
        self.assertTrue(self.manager.needs_login())

        self.now += 301
        self.runner.stdout = status_payload("needs_verification")
        self.assertTrue(self.manager.needs_login())

    def test_needs_login_fails_closed_on_probe_errors(self):
        self.runner.returncode = 1
        self.assertFalse(self.manager.needs_login())

        self.now += 301
        self.runner.returncode = 0
        self.runner.stdout = b"not json"
        self.assertFalse(self.manager.needs_login())

        self.now += 301
        self.runner.stdout = status_payload("something_new")
        self.assertFalse(self.manager.needs_login())

    def test_successful_import_clears_needs_login_cache(self):
        self.runner.stdout = status_payload("waiting")
        self.assertTrue(self.manager.needs_login())

        def import_then_status(argv, **kwargs):
            if "input" in kwargs:
                return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok":true}', stderr=b"")
            return subprocess.CompletedProcess(argv, 0, stdout=status_payload("logged_in"), stderr=b"")

        self.manager._runner = import_then_status
        self.import_once(self.start()["nonce"])
        # Without cache invalidation this would still serve the stale True.
        self.assertFalse(self.manager.needs_login())

    def test_http_body_limits_cover_mt_waimai_endpoints(self):
        source = Path("push.py").read_text(encoding="utf-8")
        post = source[source.index("    def do_POST(self):"):source.index("    # ---------- handlers ----------")]
        limits = post.index('xhs_body_limits = {')
        body_read = post.index('body = self._read_body()', limits)
        self.assertLess(limits, body_read)
        self.assertIn('"/mt-waimai-login/start": 4 * 1024', post)
        self.assertIn('"/mt-waimai-login/import": 24 * 1024', post)


if __name__ == "__main__":
    unittest.main()
