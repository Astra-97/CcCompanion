"""Shared contact registry; each named contact owns its own module."""
from __future__ import annotations

import re
from typing import Any

from . import kairos, kimi, xiaoke


_CONTACT_MODULES = {"xiaoke": xiaoke, "kairos": kairos, "kimi": kimi}
_OTHER_CONTACTS = (
    {
        "id": "hajiki", "display_name": "哈基米", "provider": "contact-local",
        "terminal_target": "", "capabilities": ["history"], "stop_fields": [],
    },
    {
        "id": "apples", "display_name": "苹果幼稚园", "provider": "group-router",
        "terminal_target": "", "capabilities": ["chat", "history", "draft", "busy", "attachments", "forward", "group_chat"], "stop_fields": [],
    },
    {
        "id": "toolbot", "display_name": "小克·工具版", "provider": "task-observer",
        "terminal_target": "", "capabilities": ["history"], "stop_fields": [],
    },
)
_OTHER_ROUTES = {
    "hajiki": {"capabilities": ["history"]},
    "apples": {"send_handler": "apples", "capabilities": _OTHER_CONTACTS[1]["capabilities"]},
    "toolbot": {"capabilities": ["history"]},
}
_LEGACY_FALLBACK_ROUTES = {
    # Focused handler fixtures created before ServerState.contact_routes used
    # this conservative directory.  Keep it byte-for-byte capability
    # compatible: absence of the registration table must never grant the
    # newer Kairos/Kimi control capabilities merely because the server code
    # was upgraded.
    "xiaoke": {"send_handler": "xiaoke", "capabilities": ["chat", "history", "draft", "busy", "stop", "attachments", "terminal", "forward", "group_member", "group_reply"], "group_dispatcher": "xiaoke"},
    "kairos": {"send_handler": "kairos", "capabilities": ["chat", "history", "draft", "busy", "stop", "attachments", "terminal", "forward", "group_member", "group_reply"], "group_dispatcher": "kairos"},
    "kimi": {"send_handler": "kimi", "capabilities": ["chat", "history", "draft", "busy", "stop", "forward", "group_member", "group_reply"], "group_dispatcher": "kimi"},
    "hajiki": {"capabilities": ["history"]},
    "apples": {"send_handler": "apples", "capabilities": ["chat", "history", "draft", "busy", "attachments", "forward", "group_chat"]},
    "toolbot": {"capabilities": ["history"]},
}
_SEND_HANDLERS = frozenset({"xiaoke", "kairos", "kimi", "apples"})
_GROUP_DISPATCHERS = frozenset({"xiaoke", "kairos", "kimi"})


def default_contact_routes() -> dict[str, dict[str, Any]]:
    """Return fresh state-owned registrations; callers may safely mutate them."""
    def copy_route(route: dict[str, Any]) -> dict[str, Any]:
        return {
            key: list(value) if isinstance(value, list) else value
            for key, value in route.items()
        }

    routes = {contact_id: copy_route(module.ROUTE) for contact_id, module in _CONTACT_MODULES.items()}
    routes.update({contact_id: copy_route(route) for contact_id, route in _OTHER_ROUTES.items()})
    return routes


def _default_definitions() -> list[dict[str, Any]]:
    return [dict(module.CONTACT) for module in _CONTACT_MODULES.values()] + [dict(item) for item in _OTHER_CONTACTS]


def _copy_routes(routes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        contact_id: {
            key: list(value) if isinstance(value, list) else value
            for key, value in route.items()
        }
        for contact_id, route in routes.items()
    }


def chat_contact_directory(state: Any) -> list[dict[str, Any]]:
    """Build the safe UI directory from contact-owned registrations."""
    definitions = _default_definitions()
    configured = getattr(state, "contact_catalog", None)
    if isinstance(configured, (list, tuple)):
        definitions.extend(item for item in configured if isinstance(item, dict))
    routes = getattr(state, "contact_routes", None)
    if not isinstance(routes, dict):
        routes = _copy_routes(_LEGACY_FALLBACK_ROUTES)

    known_ids: set[str] = set()
    contacts: list[dict[str, Any]] = []
    for definition in definitions:
        contact_id = str(definition.get("id") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", contact_id) or contact_id in known_ids:
            continue
        route = routes.get(contact_id)
        if not isinstance(route, dict):
            continue
        raw_capabilities = route.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raw_capabilities = []
        capabilities = [
            str(value).strip().lower() for value in raw_capabilities
            if isinstance(value, str)
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value.strip().lower())
        ]
        capabilities = list(dict.fromkeys(capabilities))
        contact_chats = getattr(state, "contact_chats", None)
        if isinstance(contact_chats, dict) and contact_id not in contact_chats:
            continue
        handler = str(route.get("send_handler") or "").strip().lower()
        if handler not in _SEND_HANDLERS or handler != contact_id:
            capabilities = [cap for cap in capabilities if cap not in {"chat", "draft", "busy", "stop", "attachments", "terminal", "forward", "group_chat"}]
        group_dispatcher = str(route.get("group_dispatcher") or "").strip().lower()
        if group_dispatcher not in _GROUP_DISPATCHERS or group_dispatcher != contact_id:
            capabilities = [cap for cap in capabilities if cap != "group_reply"]
        display_name = re.sub(r"[\x00-\x1f\x7f]", "", str(definition.get("display_name") or contact_id)).strip()[:80]
        if not display_name:
            continue
        known_ids.add(contact_id)
        contact = {
            "id": contact_id,
            "display_name": display_name,
            "provider": re.sub(r"[\x00-\x1f\x7f]", "", str(definition.get("provider") or "contact")).strip()[:80],
            "capabilities": capabilities,
            "read_only": "chat" not in capabilities,
            "terminal_target": re.sub(r"[\x00-\x1f\x7f]", "", str(definition.get("terminal_target") or "")).strip()[:80],
            "stop": {"supported": "stop" in capabilities, "endpoint": "/chat/stop" if "stop" in capabilities else "", "required_fields": [
                field.strip().lower() for field in (definition.get("stop_fields") if isinstance(definition.get("stop_fields"), list) else [])
                if isinstance(field, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", field.strip().lower())
            ]},
        }
        for key in ("group_display_name", "group_mention", "group_color"):
            value = re.sub(r"[\x00-\x1f\x7f]", "", str(definition.get(key) or "")).strip()
            if value:
                contact[key] = value[:80]
        contacts.append(contact)
    return contacts


def dispatch_contact_send(handler: Any, contact_id: str, body: dict[str, Any]) -> bool:
    module = _CONTACT_MODULES.get(contact_id)
    callback = getattr(module, "send", None) if module is not None else None
    if callback is None:
        return False
    callback(handler, body)
    return True


def dispatch_contact_stop(handler: Any, body: dict[str, Any]) -> bool:
    module = _CONTACT_MODULES.get(str(body.get("contact_id") or "").strip())
    callback = getattr(module, "stop", None) if module is not None else None
    return bool(callback and callback(handler, body))


def _dispatch_route(handler: Any, path: str, routes_name: str, body: dict[str, Any] | None = None) -> bool:
    for module in _CONTACT_MODULES.values():
        callback_name = getattr(module, routes_name, {}).get(path)
        if not callback_name:
            continue
        callback = getattr(handler, callback_name)
        if body is None:
            callback()
        else:
            callback(body)
        return True
    return False


def dispatch_contact_get(handler: Any, path: str) -> bool:
    return _dispatch_route(handler, path, "GET_ROUTES")


def dispatch_contact_post(handler: Any, path: str, body: dict[str, Any]) -> bool:
    return _dispatch_route(handler, path, "POST_ROUTES", body)
