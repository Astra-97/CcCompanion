from email.message import Message
from pathlib import Path
import io
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_history import ChatHistory
from push import PushHandler


def _headers(pairs):
    msg = Message()
    for name, value in pairs:
        msg[name] = value
    return msg


class MusicCardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatHistory(Path(self.tmp.name) / "chat.jsonl")
        self.token_path = Path(self.tmp.name) / "webhook.token"
        self.token_path.write_text("t" * 32 + "\n", encoding="utf-8")
        self.token_path.chmod(0o600)
        self._old_env = os.environ.get(PushHandler._MUSIC_CARD_TOKEN_FILE_ENV)
        os.environ[PushHandler._MUSIC_CARD_TOKEN_FILE_ENV] = str(self.token_path)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(PushHandler._MUSIC_CARD_TOKEN_FILE_ENV, None)
        else:
            os.environ[PushHandler._MUSIC_CARD_TOKEN_FILE_ENV] = self._old_env
        self.tmp.cleanup()

    def handler(self, body: bytes | None = None, headers=None):
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace(contact_chats={}, chat=self.chat)
        handler.headers = headers or _headers([
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body or b""))),
        ])
        handler.rfile = io.BytesIO(body or b"")
        handler.close_connection = False
        self.responses = []
        handler._send_json = lambda status, payload: self.responses.append((status, payload))
        return handler

    @staticmethod
    def song_payload():
        return {
            "type": "song",
            "text": "最近在循环这首",
            "by": "kimi",
            "song": {
                "songId": "347230",
                "name": "海阔天空",
                "artist": "Beyond",
                "album": "乐与怒",
                "cover": "https://p1.music.126.net/x/cover.jpg",
            },
        }

    @staticmethod
    def lyric_payload():
        payload = MusicCardTest.song_payload()
        payload["type"] = "lyric"
        payload["lyric"] = {
            "at": 62.5,
            "line": {"time": 62.5, "text": "原谅我这一生不羁放纵爱自由", "trans": "forgive me"},
            "prev": {"time": 58.0, "text": "上一句"},
            "next": {"time": 66.0, "text": "下一句"},
        }
        return payload

    # ── validation ──

    def test_validate_song_card(self):
        metadata = PushHandler._music_card_validate(self.song_payload())
        self.assertIsNotNone(metadata)
        self.assertTrue(metadata["music_card"])
        self.assertEqual(metadata["music_card_type"], "song")
        self.assertEqual(metadata["song"]["song_id"], "347230")
        self.assertFalse(metadata["turn_terminal"])
        self.assertIn("?song=347230", metadata["player_url"])

    def test_validate_lyric_card(self):
        metadata = PushHandler._music_card_validate(self.lyric_payload())
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["lyric"]["at"], 62.5)
        self.assertEqual(metadata["lyric"]["line"]["text"], "原谅我这一生不羁放纵爱自由")
        self.assertEqual(metadata["lyric"]["prev"]["text"], "上一句")
        self.assertTrue(metadata["player_url"].endswith("&at=62"))

    def test_validate_rejects_bad_shapes(self):
        self.assertIsNone(PushHandler._music_card_validate(None))
        self.assertIsNone(PushHandler._music_card_validate({"type": "video"}))
        bad = self.song_payload()
        bad["song"]["songId"] = "12; DROP TABLE"
        self.assertIsNone(PushHandler._music_card_validate(bad))
        bad = self.song_payload()
        bad["song"]["name"] = ""
        self.assertIsNone(PushHandler._music_card_validate(bad))
        bad = self.lyric_payload()
        bad["lyric"]["at"] = -1
        self.assertIsNone(PushHandler._music_card_validate(bad))
        bad = self.lyric_payload()
        bad["lyric"]["line"] = {"time": 1, "text": ""}
        self.assertIsNone(PushHandler._music_card_validate(bad))

    def test_validate_strips_non_https_cover(self):
        payload = self.song_payload()
        payload["song"]["cover"] = "javascript:alert(1)"
        metadata = PushHandler._music_card_validate(payload)
        self.assertEqual(metadata["song"]["cover"], "")

    # ── token file / auth ──

    def test_token_file_requires_0600(self):
        self.assertEqual(PushHandler._music_card_token(), "t" * 32)
        self.token_path.chmod(0o644)
        self.assertEqual(PushHandler._music_card_token(), "")

    def test_token_file_missing_or_short(self):
        self.token_path.unlink()
        self.assertEqual(PushHandler._music_card_token(), "")
        self.token_path.write_text("short", encoding="utf-8")
        self.token_path.chmod(0o600)
        self.assertEqual(PushHandler._music_card_token(), "")

    def test_auth_matches_bearer(self):
        handler = self.handler(headers=_headers([("Authorization", "Bearer " + "t" * 32)]))
        self.assertTrue(handler._music_card_auth_matches())
        handler = self.handler(headers=_headers([("Authorization", "Bearer wrong")]))
        self.assertFalse(handler._music_card_auth_matches())
        handler = self.handler(headers=_headers([]))
        self.assertFalse(handler._music_card_auth_matches())

    # ── handler end to end ──

    def test_handle_song_card_appends_metadata(self):
        body = json.dumps(self.song_payload()).encode("utf-8")
        handler = self.handler(body=body)
        handler._handle_music_card()
        self.assertEqual(self.responses[0][0], 200)
        self.assertTrue(self.responses[0][1]["ok"])
        records = self.chat.tail(5)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["role"], "assistant")
        self.assertEqual(rec["source"], "music-mcp:kimi")
        self.assertIn("海阔天空", rec["text"])
        self.assertTrue(rec["metadata"]["music_card"])
        self.assertEqual(rec["metadata"]["music_card_type"], "song")

    def test_handle_lyric_card_appends_metadata(self):
        body = json.dumps(self.lyric_payload()).encode("utf-8")
        handler = self.handler(body=body)
        handler._handle_music_card()
        self.assertEqual(self.responses[0][0], 200)
        rec = self.chat.tail(5)[0]
        self.assertEqual(rec["metadata"]["music_card_type"], "lyric")
        self.assertIn("01:02", rec["text"])
        self.assertEqual(rec["metadata"]["lyric"]["next"]["text"], "下一句")

    def test_handle_rejects_invalid_card(self):
        body = json.dumps({"type": "video"}).encode("utf-8")
        handler = self.handler(body=body)
        handler._handle_music_card()
        self.assertEqual(self.responses[0][0], 400)
        self.assertEqual(self.chat.tail(5), [])

    def test_handle_rejects_unsupported_contact(self):
        payload = self.song_payload()
        payload["contact"] = "someone-else"
        body = json.dumps(payload).encode("utf-8")
        handler = self.handler(body=body)
        handler._handle_music_card()
        self.assertEqual(self.responses[0][0], 400)
        self.assertEqual(self.chat.tail(5), [])

    def test_handle_routes_to_contact_chat(self):
        other = ChatHistory(Path(self.tmp.name) / "xiaoke.jsonl")
        payload = self.song_payload()
        payload["contact"] = "xiaoke"
        body = json.dumps(payload).encode("utf-8")
        handler = self.handler(body=body)
        handler.state.contact_chats = {"xiaoke": other}
        handler._handle_music_card()
        self.assertEqual(self.responses[0][0], 200)
        self.assertEqual(self.responses[0][1]["contact_id"], "xiaoke")
        self.assertEqual(len(other.tail(5)), 1)
        self.assertEqual(self.chat.tail(5), [])


if __name__ == "__main__":
    unittest.main()
