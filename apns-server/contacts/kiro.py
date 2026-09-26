"""Kiro-specific registration and ingress policy (kiro 桥接 2026-09-09).

Phase 1: text chat + durable ACP session continuity.

kiro 切模型 (2026-09-10): /kiro/preferences GET/POST — 模型目录查询与切换，
契约对照 /kimi/preferences（无 effort 维度）。

kiro 对齐 CC (2026-09-26): Stop（/chat/stop → ACP session/cancel，契约同
Kimi：contact_id + user_ts）与附件（与 Kimi 同款，只接受 staged
``attachment_ids``）。旧式附件路径/URL、位置、语音与卡片形状仍在入口拒绝。
"""
from __future__ import annotations

from typing import Any


CONTACT = {
    "id": "kiro",
    "display_name": "Kiro",
    "provider": "kiro-acp",
    "terminal_target": "",
    "capabilities": [
        "chat", "history", "draft", "busy", "stop", "attachments", "realtime",
        "kiro_model_preferences",
    ],
    "stop_fields": ["contact_id", "user_ts"],
}

ROUTE = {"send_handler": "kiro", "capabilities": CONTACT["capabilities"]}


def rejects_inbound(body: dict[str, Any]) -> bool:
    """Allow opaque staged IDs, while rejecting every legacy attachment shape."""
    forbidden = (
        "attachment_id", "attachments", "attachment_path",
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


def stop(handler: Any, body: dict[str, Any]) -> bool:
    handler._handle_kiro_chat_stop(str(body.get("user_ts") or "").strip())
    return True


GET_ROUTES = {
    "/kiro/preferences": "_handle_kiro_preferences_get",
}

POST_ROUTES = {
    "/kiro/new_session": "_handle_kiro_new_session",
    "/kiro/preferences": "_handle_kiro_preferences_post",
}
