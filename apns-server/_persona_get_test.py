"""GET /persona/xiaoke: 人格文件只读端点 (app 记忆库备份附带 persona-xiaoke.md)。"""

import hashlib
import tempfile
import types
import unittest
from pathlib import Path

from push import PushHandler


def _handler(persona_path: Path, secret: str = "s3cret", supplied: str = ""):
    h = object.__new__(PushHandler)
    h.responses = []
    h._send_json = lambda status, body=None, **_k: h.responses.append((status, body))
    h.state = types.SimpleNamespace(shared_secret=secret)
    h.headers = {"X-Auth-Token": supplied} if supplied else {}
    h._PERSONA_PATH = persona_path
    return h


class PersonaGetTest(unittest.TestCase):
    def test_returns_content_with_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "xiaoke.md"
            raw = "# 小克\n螃蟹版\n".encode("utf-8")
            p.write_bytes(raw)
            h = _handler(p, supplied="s3cret")
            self.assertTrue(h._native_pairing_auth_matches())
            h._handle_persona_get()
            status, body = h.responses[-1]
            self.assertEqual(status, 200)
            self.assertEqual(body["content"], raw.decode("utf-8"))
            self.assertEqual(body["filename"], "persona-xiaoke.md")
            self.assertEqual(body["bytes"], len(raw))
            self.assertEqual(body["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertTrue(body["updatedAt"].endswith("Z"))

    def test_missing_file_is_404(self):
        with tempfile.TemporaryDirectory() as d:
            h = _handler(Path(d) / "nope.md", supplied="s3cret")
            h._handle_persona_get()
            self.assertEqual(h.responses[-1][0], 404)

    def test_auth_fails_closed(self):
        p = Path("/nonexistent")
        self.assertFalse(_handler(p, supplied="")._native_pairing_auth_matches())
        self.assertFalse(_handler(p, supplied="wrong")._native_pairing_auth_matches())
        self.assertFalse(_handler(p, secret="", supplied="x")._native_pairing_auth_matches())


if __name__ == "__main__":
    unittest.main()
