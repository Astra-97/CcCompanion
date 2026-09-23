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
        # 2026-09-23 Astra 授权：私聊可自己浏览书架、挑任意一本续读。
        "ai_reading_browse",
        "voice_message",
    ],
    "group_display_name": "小克（螃蟹版）",
    "group_mention": "@小克",
    "group_color": "clay",
    "stop_fields": ["contact_id", "user_ts", "session"],
}

ROUTE = {"send_handler": "xiaoke", "capabilities": CONTACT["capabilities"], "group_dispatcher": "xiaoke"}

# 小克控制台 (2026-09-15)：/xiaoke/ REST 路由，对齐 /kimi/ 控制台契约族。
# 处理器本体在 push.py（复用 _run_toolbot_command 白名单命令面）；本表只做
# 路径 → 方法名的注册，dispatch 由 contacts.registry 统一走。
GET_ROUTES = {
    "/xiaoke/status": "_handle_xiaoke_status",
    "/xiaoke/preferences": "_handle_xiaoke_preferences_get",
    "/xiaoke/sessions": "_handle_xiaoke_sessions",
}

POST_ROUTES = {
    "/xiaoke/preferences": "_handle_xiaoke_preferences_post",
    "/xiaoke/new_session": "_handle_xiaoke_new_session",
    "/xiaoke/switch_session": "_handle_xiaoke_switch_session",
    "/xiaoke/forge": "_handle_xiaoke_forge",
}


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
