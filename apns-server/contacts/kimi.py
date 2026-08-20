"""Kimi-specific registration, ingress policy and provider route adapters."""
from __future__ import annotations

from typing import Any


CONTACT = {
    "id": "kimi",
    "display_name": "Kimi",
    "provider": "kimi-web",
    "terminal_target": "",
    "capabilities": [
        "chat", "history", "draft", "busy", "stop", "kimi_model_preferences",
        "kimi_session_control", "kimi_memory_recall", "forward", "group_member",
        "group_reply",
    ],
    "group_mention": "@Kimi",
    "group_display_name": "Kimi",
    "group_color": "sage",
    "stop_fields": ["contact_id", "user_ts"],
}

ROUTE = {"send_handler": "kimi", "capabilities": CONTACT["capabilities"], "group_dispatcher": "kimi"}


def rejects_inbound(body: dict[str, Any]) -> bool:
    """Kimi is a plain-text Web contact; do this before attachment staging."""
    forbidden = (
        "attachment_id", "attachment_ids", "attachments", "attachment_path",
        "attachment_url", "attachment_type", "attachment_filename", "upload_id",
        "staged_attachment_ids", "location", "voice_mode", "voice_continuation",
        "voice_reply_token",
    )
    if any(body.get(field) for field in forbidden):
        return True
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return metadata is not None
    return (
        metadata.get("via") == "card"
        or bool(metadata.get("card"))
        or bool(metadata.get("card_title"))
        or any("card" in str(key).lower() and bool(value) for key, value in metadata.items())
    )


def send(handler: Any, body: dict[str, Any]) -> None:
    handler._handle_kimi_chat_send(body, "kimi")


def stop(handler: Any, body: dict[str, Any]) -> bool:
    handler._handle_kimi_chat_stop(str(body.get("user_ts") or "").strip())
    return True


GET_ROUTES = {
    "/kimi/status": "_handle_kimi_status",
    "/kimi/terminal/observer": "_handle_kimi_terminal_observer",
    "/kimi/preferences": "_handle_kimi_preferences_get",
    "/kimi/sessions": "_handle_kimi_sessions",
}

POST_ROUTES = {
    "/kimi/preferences": "_handle_kimi_preferences_post",
    "/kimi/new_session": "_handle_kimi_new_session",
    "/kimi/switch_session": "_handle_kimi_switch_session",
    "/kimi/forge": "_handle_kimi_forge",
}
