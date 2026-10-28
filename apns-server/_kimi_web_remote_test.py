"""Remote-mode contract for KimiWebClient (卡拉米 on Tokyo, 2026-10-28)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kimi_web_client import KimiWebClient, KimiWebError


class KimiWebRemoteModeTest(unittest.TestCase):
    def test_remote_mode_never_spawns_a_local_server(self):
        client = KimiWebClient(host="100.64.158.30", port=1, spawn_local=False, start_timeout=5)
        with patch.object(client, "_try_reuse_existing_server", return_value=False), \
                patch("kimi_web_client.subprocess.Popen") as popen:
            with self.assertRaises(KimiWebError):
                client.start()
            popen.assert_not_called()

    def test_remote_mode_reuses_reachable_server(self):
        client = KimiWebClient(host="100.64.158.30", spawn_local=False)
        with patch.object(client, "_try_reuse_existing_server", return_value=True), \
                patch("kimi_web_client.subprocess.Popen") as popen:
            client.start()
            popen.assert_not_called()

    def test_token_path_overrides_kimi_code_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "kimi-web.token"
            token.write_text("local-copy\n", encoding="utf-8")
            client = KimiWebClient(kimi_code_home=Path(tmp) / "missing", token_path=token)
            self.assertEqual(client._read_token(), "local-copy")

    def test_defaults_keep_loopback_spawn_behaviour(self):
        client = KimiWebClient()
        self.assertTrue(client.spawn_local)
        self.assertEqual(client.host, "127.0.0.1")
        self.assertEqual(client._server_token_path, client.kimi_code_home / "server.token")


if __name__ == "__main__":
    unittest.main()
