from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from netease_login import (
    NETEASE_LOGIN_ORIGIN,
    NeteaseLoginError,
    NeteaseLoginManager,
    _render_vael_env,
)


DEVICE = "device_1234567890abcdef"
MUSIC_U = "a" * 64
CSRF = "b" * 32
COOKIES = f"MUSIC_U={MUSIC_U}; __csrf={CSRF}; NMTID=dropped; other=drop"


class FakeRunner:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")


class NeteaseLoginManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cred = root / "server" / ".netease_cred"
        self.cred.parent.mkdir()
        self.vael_env = root / "env" / "vael-mcp.env"
        self.vael_env.parent.mkdir()
        self.vael_env.write_text("NETEASE_COOKIE=\nNETEASE_CSRF=\nNETEASE_READONLY=1\n", encoding="utf-8")
        self.now = 100.0
        self.runner = FakeRunner()
        self.manager = NeteaseLoginManager(
            cred_file=self.cred,
            vael_env_file=self.vael_env,
            restart_command=["systemctl", "restart", "netease-a.service", "netease-b.service"],
            runner=self.runner,
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def start(self):
        return self.manager.start(
            contact_id="kimi", device_id=DEVICE, origin=NETEASE_LOGIN_ORIGIN
        )

    def import_once(self, nonce: str, **overrides):
        values = {
            "nonce": nonce,
            "contact_id": "kimi",
            "device_id": DEVICE,
            "origin": NETEASE_LOGIN_ORIGIN,
            "cookie_header": COOKIES,
        }
        values.update(overrides)
        return self.manager.import_cookies(**values)

    def test_import_writes_0600_files_and_restarts_with_fixed_argv(self):
        result = self.import_once(self.start()["nonce"])
        self.assertEqual(result, {"ok": True, "status": "stored"})

        # Anko3o credential file: exactly one MUSIC_U line, mode 0600.
        self.assertEqual(self.cred.read_text(encoding="utf-8"), f"MUSIC_U={MUSIC_U}\n")
        self.assertEqual(stat.S_IMODE(self.cred.stat().st_mode), 0o600)

        # Vael env: cookie/csrf replaced in place, unrelated keys preserved.
        env_text = self.vael_env.read_text(encoding="utf-8")
        self.assertIn(f'NETEASE_COOKIE="MUSIC_U={MUSIC_U};__csrf={CSRF}"', env_text)
        self.assertIn(f'NETEASE_CSRF="{CSRF}"', env_text)
        self.assertIn("NETEASE_READONLY=1", env_text)
        self.assertNotIn("NMTID", env_text)
        self.assertEqual(stat.S_IMODE(self.vael_env.stat().st_mode), 0o600)

        # Restart uses the fixed argv only: no shell, no env, no cookie leak.
        argv, kwargs = self.runner.calls[0]
        self.assertEqual(argv, ["systemctl", "restart", "netease-a.service", "netease-b.service"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("env", kwargs)
        self.assertNotIn("input", kwargs)
        self.assertTrue(all(MUSIC_U not in part for part in argv))

    def test_nonce_is_single_use_even_when_restart_fails(self):
        nonce = self.start()["nonce"]
        self.manager._runner = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout=b"", stderr=b"")
        with self.assertRaises(NeteaseLoginError) as first:
            self.import_once(nonce)
        self.assertEqual(first.exception.code, "sync_failed")
        with self.assertRaises(NeteaseLoginError) as second:
            self.import_once(nonce)
        self.assertEqual(second.exception.code, "nonce_used")

    def test_expiry_and_binding_are_enforced(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(NeteaseLoginError) as mismatch:
            self.import_once(nonce, device_id="other_device_1234567890")
        self.assertEqual(mismatch.exception.code, "binding_mismatch")

        nonce = self.start()["nonce"]
        self.now += 301
        with self.assertRaises(NeteaseLoginError) as expired:
            self.import_once(nonce)
        self.assertEqual(expired.exception.code, "nonce_expired")

    def test_required_cookie_check_happens_before_consuming_nonce(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(NeteaseLoginError) as incomplete:
            self.import_once(nonce, cookie_header=f"__csrf={CSRF}; NMTID=xyz")
        self.assertEqual(incomplete.exception.code, "login_incomplete")
        # Validation failure did not burn a valid capability.
        self.assertTrue(self.import_once(nonce)["ok"])

    def test_cookie_value_charset_is_enforced(self):
        nonce = self.start()["nonce"]
        with self.assertRaises(NeteaseLoginError) as invalid:
            self.import_once(nonce, cookie_header=f'MUSIC_U={MUSIC_U[:-1]}"; __csrf={CSRF}')
        self.assertEqual(invalid.exception.code, "bad_cookie")
        with self.assertRaises(NeteaseLoginError):
            self.import_once(nonce, cookie_header=f"MUSIC_U={MUSIC_U[:-1]} x; __csrf={CSRF}")

    def test_missing_csrf_is_accepted_for_anko3o_only_import(self):
        nonce = self.start()["nonce"]
        result = self.import_once(nonce, cookie_header=f"MUSIC_U={MUSIC_U}")
        self.assertTrue(result["ok"])
        env_text = self.vael_env.read_text(encoding="utf-8")
        self.assertIn(f'NETEASE_COOKIE="MUSIC_U={MUSIC_U}"', env_text)
        self.assertIn('NETEASE_CSRF=""', env_text)

    def test_needs_login_tracks_the_credential_file(self):
        self.assertTrue(self.manager.needs_login())
        self.cred.write_text("MUSIC_U=\n", encoding="utf-8")
        self.assertTrue(self.manager.needs_login())
        self.cred.write_text(f"MUSIC_U={MUSIC_U}\n", encoding="utf-8")
        self.assertFalse(self.manager.needs_login())

    def test_contact_allowlist_is_enforced(self):
        with self.assertRaises(NeteaseLoginError) as rejected:
            self.manager.start(
                contact_id="stranger", device_id=DEVICE, origin=NETEASE_LOGIN_ORIGIN
            )
        self.assertEqual(rejected.exception.code, "contact_rejected")


class RenderVaelEnvTests(unittest.TestCase):
    def test_missing_keys_are_appended(self):
        rendered = _render_vael_env("NETEASE_READONLY=1\n", "MUSIC_U=x;__csrf=y", "y")
        self.assertEqual(
            rendered,
            'NETEASE_READONLY=1\nNETEASE_COOKIE="MUSIC_U=x;__csrf=y"\nNETEASE_CSRF="y"\n',
        )

    def test_existing_keys_are_replaced_without_duplication(self):
        rendered = _render_vael_env(
            'NETEASE_COOKIE="MUSIC_U=old"\nNETEASE_CSRF="old"\nNETEASE_READONLY=0\n',
            "MUSIC_U=new;__csrf=n",
            "n",
        )
        self.assertEqual(rendered.count("NETEASE_COOKIE="), 1)
        self.assertNotIn("old", rendered)
        self.assertIn("NETEASE_READONLY=0", rendered)


if __name__ == "__main__":
    unittest.main()
