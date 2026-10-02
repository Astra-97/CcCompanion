"""语音译文后台预热（2026-10-02 Astra 拍板「加」）的回归测试。

病根：思维链入库前已被 translate_thinking_auto 同步翻好（点「译」秒出），
语音气泡却是 App 点「译」才冷调 POST /chat/translate，等一次 OpenRouter。
修复：_handle_voice_push 在 TTS 生成前启动 daemon 线程调
translate_api.translate_text 预热磁盘缓存（同一文本同一缓存键），
App 点「译」即命中缓存。任何失败静默，绝不影响推送主流程。
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_history import ChatHistory
from push import PushHandler


class _FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item


class _SyncThread:
    """同步执行版 threading.Thread：测试里让预热「线程」当场跑完，避免竞态。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _make_handler(attachments_dir: str):
    chat = _FakeChat()
    handler = object.__new__(PushHandler)
    handler.state = types.SimpleNamespace(attachments_dir=Path(attachments_dir))
    handler.responses = []
    handler._send_json = lambda status, payload: handler.responses.append((status, payload))
    handler._contact_id_from_body = lambda body: str(body.get("contact_id") or "xiaoke")
    handler._chat_for_contact = lambda _contact_id: chat
    handler._send_chat_notification = lambda *_args, **_kwargs: None
    return handler, chat


class VoiceTranslatePrewarmTest(unittest.TestCase):
    def _stub_tts(self, handler, tmp: str):
        stored_name = "voice_prewarm_test.wav"
        (Path(tmp) / stored_name).write_bytes(b"RIFF" + b"\x00" * 100)
        handler._run_stackchan_voice_helper = lambda *_args, **_kwargs: (
            True,
            {"stored_name": stored_name, "mime_type": "audio/wav", "bytes": 104},
        )

    def test_prewarm_called_with_record_text_before_tts(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, chat = _make_handler(tmp)
            self._stub_tts(handler, tmp)
            calls = []
            handler._prewarm_voice_translation = lambda text: calls.append(text)

            handler._handle_voice_push({"text": "  hello there [softly] "})

            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual(["hello there [softly]"], calls)
            self.assertEqual("hello there [softly]", chat.records[0]["text"])

    def test_prewarm_failure_does_not_break_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, chat = _make_handler(tmp)
            self._stub_tts(handler, tmp)
            with patch("threading.Thread", _SyncThread), patch(
                "translate_api.translate_text", side_effect=RuntimeError("boom")
            ):
                handler._handle_voice_push({"text": "hello"})

            self.assertEqual(200, handler.responses[-1][0])
            self.assertEqual(1, len(chat.records))

    def test_prewarm_invokes_translate_text_with_same_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, _chat = _make_handler(tmp)
            self._stub_tts(handler, tmp)
            with patch("threading.Thread", _SyncThread), patch(
                "translate_api.translate_text", return_value={"translated": "x", "cached": False, "truncated": False, "usage": {}}
            ) as translate_mock:
                handler._handle_voice_push({"text": "hello [laughs]"})

            self.assertEqual(200, handler.responses[-1][0])
            translate_mock.assert_called_once_with("hello [laughs]")

    def test_kimi_rejection_does_not_prewarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, _chat = _make_handler(tmp)
            calls = []
            handler._prewarm_voice_translation = lambda text: calls.append(text)

            handler._handle_voice_push({"text": "hello", "contact_id": "kimi"})

            self.assertEqual(415, handler.responses[-1][0])
            self.assertEqual([], calls)

    def test_empty_text_does_not_prewarm(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler, _chat = _make_handler(tmp)
            calls = []
            handler._prewarm_voice_translation = lambda text: calls.append(text)

            handler._handle_voice_push({"text": "   "})

            self.assertEqual(400, handler.responses[-1][0])
            self.assertEqual([], calls)


class ChatAppendPrewarmTest(unittest.TestCase):
    """/chat/append 挂点：小克语音消息实际从这里入库（assistant + audio 附件）。

    第一版只挂了 /voice/push，而小克语音走 bus_stop_hook → /chat/append
    （source=claude-code），预热从未触发（2026-10-02 Astra 实测仍转圈）。
    """

    def _append_handler(self):
        handler = object.__new__(PushHandler)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chat = ChatHistory(Path(tmp.name) / "history.jsonl")
        handler.state = types.SimpleNamespace(
            shared_secret="",
            strict_auth=True,
            contact_chats={"xiaoke": chat},
            attachments_dir=Path(tmp.name),
            settings={},
            tokens=types.SimpleNamespace(all_active=lambda: []),
            apns_enabled=False,
        )
        handler.headers = {}
        handler._chat_for_contact = lambda _contact: chat
        handler._source_for_request = lambda *a: "claude-code"
        handler._has_pending_group_reply = lambda: False
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.chat = chat
        return handler

    def _voice_body(self, text="hello [softly] world"):
        return {
            "contact_id": "xiaoke",
            "role": "assistant",
            "source": "claude-code",
            "text": text,
            "attachment_url": "/attachments/abc123.mp3",
            "attachment_type": "audio",
            "attachment_filename": "abc123.mp3",
            "metadata": {"type": "voice", "audio_url": "/attachments/abc123.mp3"},
        }

    def test_assistant_audio_append_prewarms_with_record_text(self):
        handler = self._append_handler()
        calls = []
        handler._prewarm_voice_translation = lambda text: calls.append(text)

        handler._handle_chat_append(self._voice_body())

        status, payload = handler.responses[-1]
        self.assertEqual(200, status, payload)
        self.assertEqual(["hello [softly] world"], calls)

    def test_non_audio_assistant_append_does_not_prewarm(self):
        handler = self._append_handler()
        calls = []
        handler._prewarm_voice_translation = lambda text: calls.append(text)

        body = self._voice_body()
        body["attachment_type"] = None
        body.pop("attachment_url")
        body.pop("attachment_filename")
        body.pop("metadata")
        handler._handle_chat_append(body)

        status, payload = handler.responses[-1]
        self.assertEqual(200, status, payload)
        self.assertEqual([], calls)

    def test_user_audio_append_does_not_prewarm(self):
        handler = self._append_handler()
        calls = []
        handler._prewarm_voice_translation = lambda text: calls.append(text)

        body = self._voice_body()
        body["role"] = "user"
        handler._handle_chat_append(body)

        status, payload = handler.responses[-1]
        self.assertEqual(200, status, payload)
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
