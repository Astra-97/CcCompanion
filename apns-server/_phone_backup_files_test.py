"""GET /phone-backup/files + /phone-backup/file?name=: app 记忆库备份的附带文件白名单。"""

import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path

import phone_backup_files as pbf
from push import PushHandler


def _handler(config: Path, query: str = "", secret: str = "s3cret", supplied: str = ""):
    h = object.__new__(PushHandler)
    h.responses = []
    h.raw = []
    h._send_json = lambda status, body=None, **_k: h.responses.append((status, body))
    h._send_phone_backup_bytes = lambda name, data, mtime: h.raw.append((name, data))
    h.state = types.SimpleNamespace(shared_secret=secret)
    h.headers = {"X-Auth-Token": supplied} if supplied else {}
    h.path = "/phone-backup/file" + query
    h._PHONE_BACKUP_CONFIG_PATH = config
    return h


class PhoneBackupFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.persona = d / "xiaoke.md"
        self.persona.write_bytes("# 小克\n".encode("utf-8"))
        self.secret_file = d / "not-listed.txt"
        self.secret_file.write_text("nope")
        self.config = d / "phone-backup-files.json"
        self.config.write_text(json.dumps([
            {"name": "persona-xiaoke.md", "path": str(self.persona)},
            {"name": "persona-xiaoke.md", "path": str(self.secret_file)},  # 重名跳过
            {"name": "../evil", "path": str(self.secret_file)},
            {"name": "manifest.json", "path": str(self.secret_file)},
            {"name": "x.tmp", "path": str(self.secret_file)},
            {"name": "relative.md", "path": "relative/path.md"},
            {"name": "gone.md", "path": str(d / "missing.md")},
            "garbage",
        ]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_reports_valid_entries_only(self):
        h = _handler(self.config)
        h._handle_phone_backup_list()
        status, body = h.responses[-1]
        self.assertEqual(status, 200)
        names = [f["name"] for f in body["files"]]
        self.assertEqual(names, ["persona-xiaoke.md", "gone.md"])
        persona = body["files"][0]
        self.assertTrue(persona["available"])
        self.assertEqual(persona["bytes"], len(self.persona.read_bytes()))
        self.assertEqual(persona["sha256"], hashlib.sha256(self.persona.read_bytes()).hexdigest())
        self.assertFalse(body["files"][1]["available"])

    def test_download_by_name_only(self):
        h = _handler(self.config, "?name=persona-xiaoke.md")
        h._handle_phone_backup_file()
        self.assertEqual(h.raw, [("persona-xiaoke.md", self.persona.read_bytes())])
        for query, code in [
            ("?name=../evil", 404),
            ("?name=not-listed.txt", 404),
            ("?name=" + str(self.secret_file), 404),
            ("?name=gone.md", 404),
            ("", 400),
            ("?name=a&name=b", 400),
            ("?name=persona-xiaoke.md&path=/etc/passwd", 400),
        ]:
            h = _handler(self.config, query)
            h._handle_phone_backup_file()
            self.assertEqual(h.raw, [], query)
            self.assertEqual(h.responses[-1][0], code, query)

    def test_config_reread_each_request(self):
        self.config.write_text(json.dumps([{"name": "other.md", "path": str(self.secret_file)}]))
        h = _handler(self.config)
        h._handle_phone_backup_list()
        self.assertEqual([f["name"] for f in h.responses[-1][1]["files"]], ["other.md"])

    def test_missing_or_broken_config_is_empty(self):
        self.assertEqual(pbf.load_entries(Path(self.tmp.name) / "nope.json"), [])
        self.config.write_text("{not json")
        self.assertEqual(pbf.load_entries(self.config), [])

    def test_auth_fails_closed(self):
        self.assertTrue(_handler(self.config, supplied="s3cret")._native_pairing_auth_matches())
        self.assertFalse(_handler(self.config)._native_pairing_auth_matches())
        self.assertFalse(_handler(self.config, supplied="wrong")._native_pairing_auth_matches())
        self.assertFalse(_handler(self.config, secret="", supplied="x")._native_pairing_auth_matches())

    def test_shipped_config_parses(self):
        names = [e.name for e in pbf.load_entries()]
        self.assertIn("persona-xiaoke.md", names)


if __name__ == "__main__":
    unittest.main()
