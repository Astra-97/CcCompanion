import types
import unittest

from contacts import (
    chat_contact_directory,
    clean_xiaoke_private_metadata,
    default_contact_routes,
    dispatch_contact_get,
    dispatch_contact_post,
    dispatch_contact_send,
    dispatch_contact_stop,
)
from contacts.kimi import rejects_inbound


class FakeHandler:
    def __init__(self):
        self.calls = []
        self.responses = []

    def _handle_kairos_chat_send(self, body, contact_id):
        self.calls.append(("kairos-send", body, contact_id))

    def _handle_kimi_chat_send(self, body, contact_id):
        self.calls.append(("kimi-send", body, contact_id))

    def _handle_codex_abort(self, body):
        self.calls.append(("kairos-stop", body))

    def _handle_kimi_chat_stop(self, user_ts):
        self.calls.append(("kimi-stop", user_ts))

    def _send_json(self, status, payload):
        self.responses.append((status, payload))

    def __getattr__(self, name):
        if name.startswith("_handle_"):
            return lambda *args: self.calls.append((name, *args))
        raise AttributeError(name)


class ContactRegistryTest(unittest.TestCase):
    def test_default_routes_are_fresh_and_contact_owned(self):
        first = default_contact_routes()
        second = default_contact_routes()
        self.assertEqual({"xiaoke", "kairos", "kimi"}, {
            key for key, route in first.items() if route.get("group_dispatcher")
        })
        first["kairos"]["send_handler"] = "changed"
        self.assertEqual("kairos", second["kairos"]["send_handler"])
        first["kairos"]["capabilities"].append("mutated")
        self.assertNotIn("mutated", second["kairos"]["capabilities"])

    def test_private_send_and_stop_use_exact_contact_adapters(self):
        handler = FakeHandler()
        self.assertTrue(dispatch_contact_send(handler, "kairos", {"text": "hi"}))
        self.assertTrue(dispatch_contact_send(handler, "kimi", {"text": "hi"}))
        self.assertTrue(dispatch_contact_send(handler, "xiaoke", {"text": "hi"}))
        self.assertTrue(dispatch_contact_stop(handler, {"contact_id": "kairos", "user_ts": "u1"}))
        self.assertTrue(dispatch_contact_stop(handler, {"contact_id": "kimi", "user_ts": "u2"}))
        self.assertTrue(dispatch_contact_stop(handler, {"contact_id": "xiaoke", "user_ts": "u3", "session": "s"}))
        self.assertEqual("kairos", handler.calls[0][2])
        self.assertEqual("kimi", handler.calls[1][2])
        self.assertEqual(("_handle_xiaoke_chat_send", {"text": "hi"}), handler.calls[2])
        self.assertEqual("u1", handler.calls[3][1]["user_ts"])
        self.assertEqual(("kimi-stop", "u2"), handler.calls[4])
        self.assertEqual(("_handle_xiaoke_chat_stop", {"contact_id": "xiaoke", "user_ts": "u3", "session": "s"}), handler.calls[5])

    def test_provider_endpoint_tables_do_not_catch_unrelated_paths(self):
        handler = FakeHandler()
        self.assertTrue(dispatch_contact_get(handler, "/codex/status"))
        self.assertTrue(dispatch_contact_get(handler, "/kimi/preferences"))
        self.assertTrue(dispatch_contact_post(handler, "/codex/forge", {"x": 1}))
        self.assertTrue(dispatch_contact_post(handler, "/kimi/forge", {"x": 2}))
        self.assertFalse(dispatch_contact_get(handler, "/settings"))
        self.assertFalse(dispatch_contact_post(handler, "/chat/send", {}))

    def test_kimi_ingress_policy_remains_before_attachment_staging(self):
        self.assertFalse(rejects_inbound({"text": "plain text"}))
        self.assertTrue(rejects_inbound({"text": "x", "attachment_ids": ["a"]}))
        self.assertTrue(rejects_inbound({"text": "x", "metadata": {"via": "card"}}))

    def test_xiaoke_health_context_is_contact_local(self):
        metadata, dropped = clean_xiaoke_private_metadata(
            {"health_context": {"period": True}, "keep": "yes"},
            "ordinary chat",
            normalize_health_context=lambda value: value,
            is_explicit_health_share=lambda _text, _metadata: False,
        )
        self.assertTrue(dropped)
        self.assertNotIn("health_context", metadata)
        metadata, dropped = clean_xiaoke_private_metadata(
            {"health_context": {"period": True}},
            "health share",
            normalize_health_context=lambda value: {**value, "normalized": True},
            is_explicit_health_share=lambda _text, _metadata: True,
        )
        self.assertFalse(dropped)
        self.assertTrue(metadata["health_context"]["normalized"])

    def test_directory_is_route_capability_authoritative(self):
        state = types.SimpleNamespace(
            contact_chats={"xiaoke": object(), "kairos": object(), "kimi": object()},
            contact_catalog=[],
            contact_routes=default_contact_routes(),
        )
        state.contact_routes["kimi"] = {"capabilities": ["history", "chat", "stop"]}
        contacts = {item["id"]: item for item in chat_contact_directory(state)}
        self.assertTrue(contacts["kimi"]["read_only"])
        self.assertFalse(contacts["kimi"]["stop"]["supported"])
        self.assertEqual("kairos", contacts["kairos"]["terminal_target"])

    def test_missing_route_table_uses_the_legacy_capability_floor(self):
        state = types.SimpleNamespace(
            contact_chats={"xiaoke": object(), "kairos": object(), "kimi": object()},
            contact_catalog=[],
        )
        contacts = {item["id"]: item for item in chat_contact_directory(state)}
        self.assertNotIn("model_preferences", contacts["kairos"]["capabilities"])
        self.assertNotIn("session_control", contacts["kairos"]["capabilities"])
        self.assertNotIn("kimi_model_preferences", contacts["kimi"]["capabilities"])
        self.assertEqual(["contact_id", "user_ts"], contacts["kairos"]["stop"]["required_fields"])


if __name__ == "__main__":
    unittest.main()
