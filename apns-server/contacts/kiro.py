"""Kiro-specific registration and ingress policy (kiro 桥接 2026-09-09).

Phase 1: text chat + durable ACP session continuity only.  Attachments,
voice, cards, model switching, stop and forge are later phases and are
rejected at the ingress boundary instead of being silently dropped.
"""
from __future__ import annotations

from typing import Any


CONTACT = {
    "id": "kiro",
    "display_name": "Kiro",
    "provider": "kiro-acp",
    "terminal_target": "",
    "capabilities": ["chat", "history", "draft", "busy", "realtime"],
    "stop_fields": [],
}

ROUTE = {"send_handler": "kiro", "capabilities": CONTACT["capabilities"]}


def rejects_inbound(body: dict[str, Any]) -> bool:
    """Phase 1 is text-only: reject every attachment/voice/card shape."""
    forbidden = (
        "attachment_id", "attachments", "attachment_ids", "attachment_path",
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
    handler._handle_kiro_chat_send(body, "kiro")


POST_ROUTES = {
    "/kiro/new_session": "_handle_kiro_new_session",
}
