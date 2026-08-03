import inspect
import threading
import types
import unittest
from unittest.mock import Mock, patch

from kimi_web_client import KimiWebClient, KimiWebError
from push import PushHandler, ServerState


class FakeKimiWeb:
    def __init__(self, *, start_error=None, status=None, quota=None):
        self.start_error = start_error
        self.status = status or {}
        self.quota = quota or {}
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def get_session_status(self, session_id):
        self.calls.append(("status", session_id))
        return self.status

    def get_quota(self):
        self.calls.append("quota")
        return self.quota


class KimiWebIsolationTest(unittest.TestCase):
    def handler(self, web):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(
            kimi_web=web,
            kimi_acp=types.SimpleNamespace(load_session_id=lambda: ""),
            kimi_active_turn={},
            kimi_auto_forge_context_threshold=0.75,
        )
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    def test_server_state_initialization_does_not_start_kimi_web(self):
        source = inspect.getsource(ServerState.__init__)
        self.assertNotIn("self.kimi_web.start()", source)

    def test_context_query_starts_kimi_lazily(self):
        web = FakeKimiWeb(status={"context_tokens": 25, "max_context_tokens": 100})
        handler = self.handler(web)

        self.assertEqual(0.25, handler._kimi_context_usage("session-1"))
        self.assertEqual(["start", ("status", "session-1")], web.calls)

    def test_context_query_failure_degrades_only_kimi(self):
        web = FakeKimiWeb(start_error=KimiWebError("unavailable"))
        handler = self.handler(web)

        self.assertEqual(0.0, handler._kimi_context_usage("session-1"))
        self.assertEqual(["start"], web.calls)

    def test_quota_query_starts_kimi_lazily_and_falls_back(self):
        available = FakeKimiWeb(quota={"remaining": 3})
        handler = self.handler(available)
        self.assertEqual({"remaining": 3}, handler._kimi_quota_snapshot())
        self.assertEqual(["start", "quota"], available.calls)

        unavailable = FakeKimiWeb(start_error=KimiWebError("unavailable"))
        handler = self.handler(unavailable)
        self.assertEqual({}, handler._kimi_quota_snapshot())
        self.assertEqual(["start"], unavailable.calls)

    def test_status_start_failure_is_confined_to_kimi_endpoint(self):
        web = FakeKimiWeb(start_error=KimiWebError("unavailable"))
        handler = self.handler(web)

        handler._handle_kimi_status()

        self.assertEqual(["start"], web.calls)
        self.assertEqual(
            [(503, {"ok": False, "error": "kimi_web_unavailable"})],
            handler.responses,
        )

    def test_status_request_timeout_is_confined_to_kimi_endpoint(self):
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client.start = Mock()
        client._read_token = lambda: "test-token"
        handler = self.handler(client)

        with patch("kimi_web_client.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            handler._handle_kimi_status()

        # The handler ensures availability before building the response, and
        # the quota helper independently preserves its lazy-start contract.
        self.assertEqual(2, client.start.call_count)
        self.assertEqual(200, handler.responses[-1][0])
        self.assertEqual({}, handler.responses[-1][1]["quota"])

    def test_real_client_normalizes_network_os_errors_only(self):
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client._read_token = lambda: "test-token"

        for failure in (TimeoutError("timed out"), OSError("connection reset")):
            with self.subTest(failure=type(failure).__name__), patch(
                "kimi_web_client.urllib.request.urlopen", side_effect=failure
            ):
                with self.assertRaises(KimiWebError):
                    client.get_quota()

        with patch(
            "kimi_web_client.urllib.request.urlopen",
            side_effect=RuntimeError("programming bug"),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming bug"):
                client.get_quota()

    def test_real_client_start_failure_cleans_up_and_returns(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        process.wait.return_value = 0
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client._try_reuse_existing_server = lambda: False
        client._wait_for_server = Mock(side_effect=KimiWebError("not ready"))
        outcome = []

        def run_start():
            try:
                client.start()
            except Exception as exc:
                outcome.append(exc)

        with patch("kimi_web_client.subprocess.Popen", return_value=process), patch(
            "kimi_web_client.os.killpg"
        ) as killpg:
            thread = threading.Thread(target=run_start)
            thread.start()
            thread.join(timeout=1)

        self.assertFalse(thread.is_alive(), "failed Kimi startup must not deadlock in close()")
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], KimiWebError)
        self.assertIsNone(client._process)
        killpg.assert_called_once_with(process.pid, 15)
        process.wait.assert_called_once_with(timeout=3)

    def test_real_client_concurrent_start_launches_one_process(self):
        process = Mock(pid=23456)
        process.poll.return_value = None
        process.wait.return_value = 0
        client = KimiWebClient(command="/unused/kimi", start_timeout=5)
        client._try_reuse_existing_server = lambda: False
        readiness_entered = threading.Event()
        release_readiness = threading.Event()
        errors = []

        def wait_for_server():
            readiness_entered.set()
            if not release_readiness.wait(timeout=1):
                raise AssertionError("test did not release readiness")

        def run_start():
            try:
                client.start()
            except Exception as exc:
                errors.append(exc)

        client._wait_for_server = wait_for_server
        with patch("kimi_web_client.subprocess.Popen", return_value=process) as popen:
            first = threading.Thread(target=run_start)
            second = threading.Thread(target=run_start)
            first.start()
            self.assertTrue(readiness_entered.wait(timeout=1))
            second.start()
            release_readiness.set()
            first.join(timeout=1)
            second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, popen.call_count)

    def test_shutdown_kimi_cleanup_failure_does_not_block_shared_cleanup(self):
        calls = []
        state = object.__new__(ServerState)
        state.kairos_terminal = types.SimpleNamespace(release=lambda: calls.append("kairos"))

        def fail_kimi_close():
            calls.append("kimi")
            raise OSError("cleanup failed")

        state.kimi_web = types.SimpleNamespace(close=fail_kimi_close)
        state.codex_app_bridge = types.SimpleNamespace(close=lambda: calls.append("codex"))
        state.client = types.SimpleNamespace(close=lambda: calls.append("apns"))

        state.shutdown()

        self.assertEqual(["kairos", "kimi", "codex", "apns"], calls)


if __name__ == "__main__":
    unittest.main()
