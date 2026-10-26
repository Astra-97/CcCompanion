from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from x_login import X_LOGIN_ORIGIN, X_LOGIN_URL, XLoginError, XLoginManager


DEVICE = "device_1234567890abcdef"
FAKE_COOKIES = "auth_token=fakeauth; ct0=fakect0; twid=u%3D1; ignored=drop"
OK_STATUS = json.dumps({"ok": True, "data": {"authenticated": True, "user": {"screenName": "ggg1446850"}}}).encode()
UNAUTH_STATUS = json.dumps({"ok": False, "error": {"code": "not_authenticated", "message": "HTTP 401"}}).encode()


class ScriptedRunner:
    """Returns queued (returncode, stdout) per call; records argv/kwargs."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        rc, out = self.responses.pop(0) if self.responses else (0, OK_STATUS)
        if isinstance(rc, BaseException):
            raise rc
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=b"")


IMPORT = ["ssh", "tokyo", "/fixed/import_cookies.py"]
STATUS = ["ssh", "tokyo", "/fixed/xcli", "status", "--json"]


class XLoginManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0

    def manager(self, runner, **kw):
        return XLoginManager(
            import_command=IMPORT, status_command=STATUS, runner=runner,
            clock=lambda: self.now, allowed_contacts={"xiaoke"}, **kw,
        )

    def start(self, m, contact="xiaoke"):
        return m.start(contact_id=contact, device_id=DEVICE, origin=X_LOGIN_ORIGIN)

    def import_once(self, m, nonce, **overrides):
        values = dict(nonce=nonce, contact_id="xiaoke", device_id=DEVICE,
                      origin=X_LOGIN_ORIGIN, cookie_header=FAKE_COOKIES)
        values.update(overrides)
        return m.import_cookies(**values)

    def test_start_returns_fixed_login_url(self):
        result = self.start(self.manager(ScriptedRunner()))
        self.assertEqual(result["login_url"], X_LOGIN_URL)
        self.assertEqual(X_LOGIN_URL, "https://x.com/i/flow/login")

    def test_success_imports_via_stdin_then_reprobes(self):
        runner = ScriptedRunner((0, b'{"ok":true,"status":"stored","screen_name":"ggg1446850"}'), (0, OK_STATUS))
        m = self.manager(runner)
        result = self.import_once(m, self.start(m)["nonce"])
        self.assertEqual(result, {"ok": True, "status": "stored", "account": "ggg1446850"})
        (argv1, kw1), (argv2, kw2) = runner.calls
        self.assertEqual(argv1, IMPORT)
        self.assertEqual(json.loads(kw1["input"]), {"cookies": {"auth_token": "fakeauth", "ct0": "fakect0", "twid": "u%3D1"}})
        self.assertEqual(argv2, STATUS)
        for argv, kw in runner.calls:
            self.assertNotIn("fakeauth", " ".join(argv))
            self.assertNotIn("env", kw)
        # gate primed: logged in, no further probe
        self.assertFalse(m.needs_login())
        self.assertEqual(len(runner.calls), 2)

    def test_fake_cookie_verify_failed_branch(self):
        runner = ScriptedRunner((3, b'{"ok":false,"status":"verify_failed"}'))
        m = self.manager(runner)
        with self.assertRaises(XLoginError) as ctx:
            self.import_once(m, self.start(m)["nonce"])
        self.assertEqual((ctx.exception.status, ctx.exception.code), (422, "verify_failed"))
        self.assertEqual(len(runner.calls), 1)

    def test_wrong_account_and_unavailable(self):
        for out, code in ((b'{"ok":false,"status":"wrong_account"}', "wrong_account"),
                          (b'{"ok":false,"status":"verify_unavailable"}', "verify_unavailable"),
                          (b'garbage', "sync_failed")):
            m = self.manager(ScriptedRunner((3, out)))
            with self.assertRaises(XLoginError) as ctx:
                self.import_once(m, self.start(m)["nonce"])
            self.assertEqual(ctx.exception.code, code)

    def test_reprobe_rejecting_after_store_is_verify_failed(self):
        m = self.manager(ScriptedRunner((0, b'{"ok":true,"status":"stored"}'), (1, UNAUTH_STATUS)))
        with self.assertRaises(XLoginError) as ctx:
            self.import_once(m, self.start(m)["nonce"])
        self.assertEqual(ctx.exception.code, "verify_failed")
        self.assertTrue(m.needs_login())

    def test_missing_required_cookie_never_runs_remote(self):
        runner = ScriptedRunner()
        m = self.manager(runner)
        with self.assertRaises(XLoginError) as ctx:
            self.import_once(m, self.start(m)["nonce"], cookie_header="auth_token=only")
        self.assertEqual(ctx.exception.code, "login_incomplete")
        self.assertEqual(runner.calls, [])

    def test_nonce_one_shot_and_binding(self):
        m = self.manager(ScriptedRunner((3, b'{"ok":false,"status":"verify_failed"}')))
        nonce = self.start(m)["nonce"]
        with self.assertRaises(XLoginError):
            self.import_once(m, nonce)
        with self.assertRaises(XLoginError) as ctx:
            self.import_once(m, nonce)
        self.assertEqual(ctx.exception.code, "nonce_used")
        with self.assertRaises(XLoginError) as ctx:
            self.start(m, contact="kimi")
        self.assertEqual(ctx.exception.code, "contact_rejected")
        with self.assertRaises(XLoginError) as ctx:
            m.start(contact_id="xiaoke", device_id=DEVICE, origin="web")
        self.assertEqual(ctx.exception.code, "bad_origin")

    def test_needs_login_probe_shapes_and_cache(self):
        cases = [
            ((1, UNAUTH_STATUS), True),
            ((0, OK_STATUS), False),
            ((1, json.dumps({"ok": False, "error": {"code": "rate_limited"}}).encode()), False),
            ((0, b"not json"), False),
            ((subprocess.TimeoutExpired("ssh", 20), b""), False),
        ]
        for response, expected in cases:
            runner = ScriptedRunner(response)
            m = self.manager(runner)
            self.assertEqual(m.needs_login(), expected, response)
            self.assertEqual(runner.calls[0][0], STATUS)
        runner = ScriptedRunner((1, UNAUTH_STATUS))
        m = self.manager(runner, status_cache_seconds=600)
        self.assertTrue(m.needs_login())
        self.now += 599
        self.assertTrue(m.needs_login())
        self.assertEqual(len(runner.calls), 1)
        self.now += 2
        m.needs_login()
        self.assertEqual(len(runner.calls), 2)


class PushWiringTests(unittest.TestCase):
    source = Path(__file__).with_name("push.py").read_text(encoding="utf-8")

    def test_endpoints_body_limits_and_android_gate(self):
        self.assertIn('"/x-login/start": 4 * 1024', self.source)
        self.assertIn('"/x-login/import": 16 * 1024', self.source)
        start = self.source.index("    def _handle_x_login_start")
        body = self.source[start:self.source.index("    def _handle_register")]
        self.assertEqual(body.count('self._source_for_request() != "android-app"'), 2)
        self.assertEqual(body.count('"retired"'), 2)

    def test_trusted_metadata_gate_strips_x_card_without_probe(self):
        import push

        handler = push.PushHandler.__new__(push.PushHandler)
        manager = SimpleNamespace(needs_login=lambda: False, allowed_contacts={"xiaoke"})
        handler.state = SimpleNamespace(x_login=manager)
        cleaned = handler._trusted_login_card_metadata(
            {"x_login_card": True, "other": 1}, contact_id="xiaoke", source="ccc-stop-hook")
        self.assertEqual(cleaned, {"other": 1})
        manager.needs_login = lambda: True
        kept = handler._trusted_login_card_metadata(
            {"x_login_card": True}, contact_id="xiaoke", source="ccc-stop-hook")
        self.assertEqual(kept, {"x_login_card": True})
        # needs_login but contact not allowed -> stripped
        self.assertEqual(handler._trusted_login_card_metadata(
            {"x_login_card": True}, contact_id="kimi", source="kimi"), {})
        handler.state = SimpleNamespace(x_login=None)
        self.assertEqual(handler._trusted_login_card_metadata(
            {"x_login_card": True}, contact_id="xiaoke", source="ccc-stop-hook"), {})

    def test_marker_grammar(self):
        import push

        extract = push.PushHandler._kimi_extract_x_login_card
        marker = "[[CCC_X_LOGIN_CARD:v1]]"
        self.assertEqual(extract(f"去登录吧\n{marker}", allowed=True), ("去登录吧", True))
        self.assertEqual(extract(f"去登录吧\n{marker}", allowed=False), (f"去登录吧\n{marker}", False))
        self.assertEqual(extract(f"行内 {marker} 不算", allowed=True)[1], False)
        self.assertEqual(extract(f"{marker}\n后面还有字", allowed=True)[1], False)
        self.assertEqual(extract(f"{marker}\n{marker}", allowed=True)[1], False)

    def test_xiaoke_hint_only_on_x_topic_and_needs_login(self):
        import push

        handler = push.PushHandler.__new__(push.PushHandler)
        calls = []
        manager = SimpleNamespace(needs_login=lambda: calls.append(1) or True, allowed_contacts={"xiaoke"})
        handler.state = SimpleNamespace(x_login=manager)
        self.assertEqual(handler._xiaoke_x_login_hint("今天吃什么"), "")
        self.assertEqual(calls, [])
        self.assertIn("[[CCC_X_LOGIN_CARD:v1]]", handler._xiaoke_x_login_hint("去推特看看"))
        self.assertIn("CCC_X_LOGIN_CARD", handler._xiaoke_x_login_hint("刷一下 X 吧"))
        self.assertEqual(handler._xiaoke_x_login_hint("Xbox 好玩"), "")
        manager.needs_login = lambda: False
        self.assertEqual(handler._xiaoke_x_login_hint("看看 twitter"), "")

    def test_kimi_prompt_section(self):
        import push

        handler = push.PushHandler.__new__(push.PushHandler)
        handler.state = SimpleNamespace(sticker_catalog=SimpleNamespace(snapshot=lambda: {}))
        prompt = handler._kimi_prompt("hi", x_login_card_allowed=True)
        self.assertIn("[X登录卡片]", prompt)
        self.assertIn("[[CCC_X_LOGIN_CARD:v1]]", prompt)
        self.assertNotIn("[X登录卡片]", handler._kimi_prompt("hi"))


if __name__ == "__main__":
    unittest.main()
