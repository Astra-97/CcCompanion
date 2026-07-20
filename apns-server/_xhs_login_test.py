from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from xhs_login import XHS_LOGIN_ORIGIN, XhsLoginError, XhsLoginManager


DEVICE = "device_1234567890abcdef"


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok":true}', stderr=b"")


class XhsLoginManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.runner = FakeRunner()
        self.manager = XhsLoginManager(
            import_command=["ssh", "memory-sg", "/fixed/import_cookies.py"],
            runner=self.runner,
            clock=lambda: self.now,
        )

    def start(self):
        return self.manager.start(
            contact_id="kairos", device_id=DEVICE, origin=XHS_LOGIN_ORIGIN
        )

    def import_once(self, nonce: str, **overrides):
        values = {
            "nonce": nonce,
            "contact_id": "kairos",
            "device_id": DEVICE,
            "origin": XHS_LOGIN_ORIGIN,
            "cookie_header": "a1=one; webId=two; web_session=three; ignored=drop",
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
        self.assertEqual(payload["cookies"], {"a1": "one", "webId": "two", "web_session": "three"})
        self.assertTrue(all("one" not in part for part in argv))

    def test_nonce_is_single_use_even_when_remote_fails(self):
        nonce = self.start()["nonce"]
        self.runner.__call__ = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout=b"", stderr=b"")
        # Replace callable captured by manager because special methods are type-resolved.
        self.manager._runner = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout=b"", stderr=b"")
        with self.assertRaises(XhsLoginError) as first:
            self.import_once(nonce)
        self.assertEqual(first.exception.code, "sync_failed")
        with self.assertRaises(XhsLoginError) as second:
            self.import_once(nonce)
        self.assertEqual(second.exception.code, "nonce_used")

    def test_expiry_and_binding_are_enforced(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(XhsLoginError) as mismatch:
            self.import_once(nonce, device_id="other_device_1234567890")
        self.assertEqual(mismatch.exception.code, "binding_mismatch")

        nonce = self.start()["nonce"]
        self.now += 301
        with self.assertRaises(XhsLoginError) as expired:
            self.import_once(nonce)
        self.assertEqual(expired.exception.code, "nonce_expired")

    def test_required_cookie_and_size_checks_happen_before_consuming_nonce(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(XhsLoginError) as incomplete:
            self.import_once(nonce, cookie_header="a1=one; webId=two")
        self.assertEqual(incomplete.exception.code, "login_incomplete")
        # Validation failure did not burn a valid capability.
        self.assertTrue(self.import_once(nonce)["ok"])

    def test_web_session_sec_does_not_replace_required_web_session(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(XhsLoginError) as incomplete:
            self.import_once(
                nonce, cookie_header="a1=one; webId=two; web_session_sec=secure"
            )
        self.assertEqual(incomplete.exception.code, "login_incomplete")

    def test_rednote_cookie_fields_are_allowlisted(self):
        nonce = self.start()["nonce"]
        self.import_once(
            nonce,
            cookie_header=(
                "a1=one; webId=two; web_session=three; id_token=id; ets=e; "
                "x-rednote-datactry=us; x-rednote-holderctry=us"
            ),
        )
        payload = json.loads(self.runner.calls[-1][1]["input"])["cookies"]
        self.assertEqual(payload["id_token"], "id")
        self.assertEqual(payload["x-rednote-datactry"], "us")

    def test_only_allowed_contact_and_origin_can_start(self):
        for contact, origin in (("xiaoke", XHS_LOGIN_ORIGIN), ("kairos", "web")):
            with self.assertRaises(XhsLoginError):
                self.manager.start(contact_id=contact, device_id=DEVICE, origin=origin)

    def test_http_body_limits_run_before_json_read(self):
        source = Path("push.py").read_text(encoding="utf-8")
        post = source[source.index("    def do_POST(self):"):source.index("    # ---------- handlers ----------")]
        limits = post.index('xhs_body_limits = {')
        body_read = post.index('body = self._read_body()', limits)
        self.assertLess(limits, body_read)
        self.assertIn('"/xhs-login/start": 4 * 1024', post)
        self.assertIn('"/xhs-login/import": 32 * 1024', post)


if __name__ == "__main__":
    unittest.main()
