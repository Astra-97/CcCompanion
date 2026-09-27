"""app-bundle-20260927: forwarding into the group routes only by the user's own note.

The App merges an optional note and the forwarded ``[转发自X]`` body into one
user message and marks group forwards with ``metadata.forward`` plus
``metadata.forward_note``.  An ``@`` inside someone else's forwarded words must
not wake a member she did not name.
"""
from __future__ import annotations

import types
import unittest

from push import PushHandler


class _FakeChat:
    def __init__(self):
        self.records = []

    def append(self, **record):
        item = {**record, "ts": f"ts-{len(self.records) + 1}"}
        self.records.append(item)
        return item


class ApplesForwardRoutingTest(unittest.TestCase):
    def setUp(self):
        PushHandler._apples_dedupe_cache = {}
        handler = object.__new__(PushHandler)
        handler.state = types.SimpleNamespace()
        handler.headers = {}
        handler.responses = []
        handler.dispatched = []
        handler.chat = _FakeChat()
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler._source_for_request = lambda suffix="": f"android-app:{suffix}"
        handler._chat_for_contact = lambda _contact_id: handler.chat
        handler._enrich_user_links = lambda _text: types.SimpleNamespace(previews=[])
        handler._set_typing_for_contact = lambda *_args, **_kwargs: None
        handler._apples_members = lambda: [
            {"id": "astra", "display_name": "Astra", "mention": "@方小南", "can_reply": False},
            {"id": "kairos", "display_name": "Kairos", "mention": "@Kairos", "can_reply": True},
            {"id": "xiaoke", "display_name": "小克", "mention": "@小克", "can_reply": True},
            {"id": "kimi", "display_name": "卡拉米", "mention": "@卡拉米", "can_reply": True},
        ]
        handler._apples_self_id = lambda: "astra"

        def dispatch(rec, contact_id, targets, sender_name, **_kwargs):
            handler.dispatched.append(sorted(targets))
            return sorted(targets), []

        handler._dispatch_apples_mentions = dispatch
        self.handler = handler

    def send(self, text, metadata=None):
        body = {"text": text}
        if metadata is not None:
            body["metadata"] = metadata
        self.handler._handle_apples_chat_send(body, "apples")
        return self.handler.responses[-1]

    def test_plain_message_still_routes_by_text_mentions(self):
        status, payload = self.send("@Kairos 你看看")
        self.assertEqual(200, status)
        self.assertEqual(["kairos"], payload["routed"])

    def test_before_fix_shape_forward_body_mention_would_wake_kairos(self):
        # A forward without the forward marker (older App) keeps the old
        # text-grep behaviour: the @Kairos quoted in 小克's words routes Kairos.
        status, payload = self.send("[转发自小克]\n@Kairos 这个你来")
        self.assertEqual(["kairos"], payload["routed"])

    def test_forward_routes_only_by_the_users_note(self):
        text = "@卡拉米 你怎么看\n\n[转发自小克]\n@Kairos 这个你来"
        status, payload = self.send(text, {"forward": True, "forward_note": "@卡拉米 你怎么看"})
        self.assertEqual(200, status)
        self.assertEqual(["kimi"], payload["routed"])
        self.assertEqual([["kimi"]], self.handler.dispatched)
        self.assertEqual(["kimi"], self.handler.chat.records[-1]["mentions"])
        self.assertEqual(text, self.handler.chat.records[-1]["text"])

    def test_forward_without_a_note_wakes_nobody(self):
        status, payload = self.send("[转发自小克]\n@Kairos 这个你来", {"forward": True, "forward_note": ""})
        self.assertEqual(200, status)
        self.assertEqual([], payload["routed"])
        self.assertEqual([], self.handler.dispatched)
        self.assertEqual(1, len(self.handler.chat.records))

    def test_explicit_picker_ids_still_win_over_the_forward_note(self):
        status, payload = self.send(
            "看\n\n[转发自Kairos]\n@小克 原话",
            {"forward": True, "forward_note": "看", "mentioned_member_ids": ["kairos"]},
        )
        self.assertEqual(["kairos"], payload["routed"])


if __name__ == "__main__":
    unittest.main()
