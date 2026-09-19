from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from xhs_login import XHS_LOGIN_ORIGIN, XhsLoginError, XhsLoginManager


DEVICE = "device_1234567890abcdef"


def status_ok_payload(*, user_id: str = "687393f9000000001e006842", guest: bool = False) -> bytes:
    return json.dumps({
        "ok": True,
        "schema_version": "1",
        "data": {
            "authenticated": True,
            "user": {"id": user_id, "nickname": "n", "guest": guest},
        },
    }).encode("utf-8")


def status_error_payload(code: str) -> bytes:
    return json.dumps({
        "ok": False,
        "schema_version": "1",
        "error": {"code": code, "message": "redacted"},
    }).encode("utf-8")


class FakeRunner:
    def __init__(self, stdout: bytes = b'{"ok":true}', returncode: int = 0) -> None:
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=b"")


class XhsLoginManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.runner = FakeRunner()
        self.manager = XhsLoginManager(
            import_command=["ssh", "memory-sg", "/fixed/import_cookies.py"],
            status_command=["ssh", "memory-sg", "/fixed/status"],
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

    def test_default_remains_kairos_only(self):
        self.assertTrue(self.start()["ok"])
        with self.assertRaises(XhsLoginError) as rejected:
            self.manager.start(
                contact_id="xiaoke", device_id=DEVICE, origin=XHS_LOGIN_ORIGIN
            )
        self.assertEqual(rejected.exception.code, "contact_rejected")

    def test_explicit_allowlist_enables_kairos_and_xiaoke(self):
        manager = XhsLoginManager(
            import_command=["ssh", "memory-sg", "/fixed/import_cookies.py"],
            allowed_contacts={"kairos", "xiaoke"},
            runner=self.runner,
            clock=lambda: self.now,
        )
        for contact in ("kairos", "xiaoke"):
            result = manager.start(
                contact_id=contact, device_id=DEVICE, origin=XHS_LOGIN_ORIGIN
            )
            self.assertTrue(result["ok"])

    def test_only_allowed_contact_and_origin_can_start(self):
        for contact, origin in (("mallory", XHS_LOGIN_ORIGIN), ("kairos", "web")):
            with self.assertRaises(XhsLoginError):
                self.manager.start(contact_id=contact, device_id=DEVICE, origin=origin)

    def test_explicit_empty_allowlist_denies_all_contacts(self):
        manager = XhsLoginManager(
            import_command=["ssh", "memory-sg", "/fixed/import_cookies.py"],
            allowed_contacts=set(),
            runner=self.runner,
            clock=lambda: self.now,
        )
        for contact in ("kairos", "xiaoke"):
            with self.assertRaises(XhsLoginError) as rejected:
                manager.start(
                    contact_id=contact,
                    device_id=DEVICE,
                    origin=XHS_LOGIN_ORIGIN,
                )
            self.assertEqual(rejected.exception.code, "contact_rejected")

    def test_nonce_remains_bound_to_contact(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(XhsLoginError) as mismatch:
            self.import_once(nonce, contact_id="xiaoke")
        self.assertEqual(mismatch.exception.code, "binding_mismatch")

    def test_http_body_limits_run_before_json_read(self):
        source = Path("push.py").read_text(encoding="utf-8")
        post = source[source.index("    def do_POST(self):"):source.index("    # ---------- handlers ----------")]
        limits = post.index('xhs_body_limits = {')
        body_read = post.index('body = self._read_body()', limits)
        self.assertLess(limits, body_read)
        self.assertIn('"/xhs-login/start": 4 * 1024', post)
        self.assertIn('"/xhs-login/import": 32 * 1024', post)

    def test_successful_cookie_import_invalidates_stale_comment_failures(self):
        source = Path("push.py").read_text(encoding="utf-8")
        start_handler = source[
            source.index("    def _handle_xhs_login_start"):
            source.index("    def _handle_xhs_login_import")
        ]
        import_handler = source[
            source.index("    def _handle_xhs_login_import"):
            source.index("    def _handle_register")
        ]
        self.assertNotIn("invalidate_xhs_comment_failures", start_handler)
        self.assertIn("invalidate_xhs_comment_failures", import_handler)

    def test_needs_login_tracks_status_probe(self):
        self.runner.stdout = status_ok_payload()
        self.assertFalse(self.manager.needs_login())
        self.runner.stdout = status_error_payload("not_authenticated")
        # Cached probe still serves; advance the clock past the cache TTL.
        self.assertFalse(self.manager.needs_login())
        self.now += 121
        self.assertTrue(self.manager.needs_login())

        self.now += 121
        self.runner.stdout = status_error_payload("verification_required")
        self.assertTrue(self.manager.needs_login())

    def test_needs_login_flags_guest_or_incomplete_profiles(self):
        self.runner.stdout = status_ok_payload(guest=True)
        self.assertTrue(self.manager.needs_login())

        self.now += 121
        self.runner.stdout = status_ok_payload(user_id="")
        self.assertTrue(self.manager.needs_login())

        self.now += 121
        self.runner.stdout = json.dumps({
            "ok": True, "data": {"authenticated": False},
        }).encode("utf-8")
        self.assertTrue(self.manager.needs_login())

    def test_needs_login_fails_closed_on_probe_errors(self):
        self.runner.returncode = 1
        self.runner.stdout = b"not json"
        self.assertFalse(self.manager.needs_login())

        self.now += 121
        # An ok:true payload with a failing exit code is not trusted either.
        self.runner.stdout = status_ok_payload()
        self.assertFalse(self.manager.needs_login())

        self.now += 121
        self.runner.returncode = 0
        # ip_blocked/signature_error are not login-card states.
        self.runner.stdout = status_error_payload("ip_blocked")
        self.assertFalse(self.manager.needs_login())

        self.now += 121
        self.runner.stdout = b'{"ok":"maybe"}'
        self.assertFalse(self.manager.needs_login())

        self.now += 121
        self.manager._runner = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ssh down"))
        self.assertFalse(self.manager.needs_login())

        self.now += 121
        self.manager._runner = lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ssh", timeout=15)
        )
        self.assertFalse(self.manager.needs_login())

    def test_needs_login_uses_fixed_status_argv_without_stdin(self):
        self.runner.stdout = status_error_payload("not_authenticated")
        self.assertTrue(self.manager.needs_login())
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(argv, ["ssh", "memory-sg", "/fixed/status"])
        self.assertNotIn("input", kwargs)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("env", kwargs)

    def test_successful_import_clears_needs_login_cache(self):
        self.runner.stdout = status_error_payload("not_authenticated")
        self.assertTrue(self.manager.needs_login())

        def import_then_status(argv, **kwargs):
            if "input" in kwargs:
                return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok":true}', stderr=b"")
            return subprocess.CompletedProcess(argv, 0, stdout=status_ok_payload(), stderr=b"")

        self.manager._runner = import_then_status
        self.import_once(self.start()["nonce"])
        # Without cache invalidation this would still serve the stale True.
        self.assertFalse(self.manager.needs_login())


if __name__ == "__main__":
    unittest.main()
