"""Kairos-specific registration and provider route adapters."""
from __future__ import annotations

from typing import Any


CONTACT = {
    "id": "kairos",
    "display_name": "Kairos",
    "provider": "codex-app-server",
    "terminal_target": "kairos",
    "capabilities": [
        "chat", "history", "draft", "busy", "stop", "attachments", "terminal",
        "model_preferences", "session_control", "memory_recall", "forward",
        "group_member", "group_reply",
    ],
    "group_mention": "@Kairos",
    "group_color": "gold",
    "stop_fields": ["contact_id", "user_ts"],
}

ROUTE = {"send_handler": "kairos", "capabilities": CONTACT["capabilities"], "group_dispatcher": "kairos"}


def send(handler: Any, body: dict[str, Any]) -> None:
    """Hand the already-authenticated, staged private turn to Kairos."""
    handler._handle_kairos_chat_send(body, "kairos")


def stop(handler: Any, body: dict[str, Any]) -> bool:
    user_ts = str(body.get("user_ts") or "").strip()
    if not user_ts:
        handler._send_json(400, {"ok": False, "error": "user_ts is required to stop a Kairos turn"})
        return True
    handler._handle_codex_abort({
        "contact_id": "kairos",
        "user_ts": user_ts,
        "cancel_pending": True,
    })
    return True


GET_ROUTES = {
    "/codex/status": "_handle_codex_status",
    "/codex/preferences": "_handle_codex_preferences_get",
    "/codex/sessions": "_handle_codex_sessions",
}

POST_ROUTES = {
    "/codex/preferences": "_handle_codex_preferences_post",
    "/codex/abort": "_handle_codex_abort",
    "/codex/new_session": "_handle_codex_new_session",
    "/codex/switch": "_handle_codex_switch",
    "/codex/forge": "_handle_codex_forge",
}
