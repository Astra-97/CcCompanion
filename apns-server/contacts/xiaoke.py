"""XiaoKe-specific registration and private-turn policy."""
from __future__ import annotations

from typing import Any, Callable

CONTACT = {
    "id": "xiaoke",
    "display_name": "小克",
    "provider": "claude-code",
    "terminal_target": "",
    "capabilities": [
        "chat", "history", "draft", "busy", "stop", "attachments", "terminal",
        "forward", "group_member", "group_reply", "realtime", "ai_reading_continue",
    ],
    "group_display_name": "小克（螃蟹版）",
    "group_mention": "@小克",
    "group_color": "clay",
    "stop_fields": ["contact_id", "user_ts", "session"],
}

ROUTE = {"send_handler": "xiaoke", "capabilities": CONTACT["capabilities"], "group_dispatcher": "xiaoke"}


def send(handler: Any, body: dict[str, Any]) -> None:
    """Route a prepared private turn into the shared exact-turn pipeline."""
    handler._handle_xiaoke_chat_send(body)


def stop(handler: Any, body: dict[str, Any]) -> bool:
    """Route a semantic Stop into the shared exact-turn state machine."""
    handler._handle_xiaoke_chat_stop(body)
    return True


def clean_private_metadata(
    metadata: dict[str, Any],
    text: Any,
    *,
    normalize_health_context: Callable[[Any], Any],
    is_explicit_health_share: Callable[[Any, dict[str, Any]], bool],
) -> tuple[dict[str, Any], bool]:
    """Keep the health hint only for XiaoKe's explicit health-share turns.

    This is intentionally contact-local: no provider other than XiaoKe may
    receive the structured health context from the shared request envelope.
    The bool reports a normalized hint that was intentionally discarded so
    the shared logger can preserve its existing diagnostic without the module
    owning process logging configuration.
    """
    cleaned = dict(metadata)
    normalized = normalize_health_context(cleaned.get("health_context"))
    shared = is_explicit_health_share(text, cleaned)
    if normalized is None or not shared:
        cleaned.pop("health_context", None)
        return cleaned, normalized is not None
    cleaned["health_context"] = normalized
    return cleaned, False
