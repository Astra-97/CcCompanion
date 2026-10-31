"""Kiro Crew contact ``kirocrew`` (2026-10-31): 小克 through the Kiro Crew gateway.

方小南「先做一个 crew，然后功能和接口对齐 cli，稳定运行几天再看用哪个」: a
second, independent entrance next to the kiro-cli contact ``kiro`` (which is
untouched).  Turns go to one persistent Crew chat slot on Tokyo
(``kirocrew_client.py``).  Same ingress policy as Kiro: text plus staged
``attachment_ids`` only.  Chat commands (/new /stop /model /effort /status
/compact /help) are answered by push.py without a model call, except
/compact, which Crew runs itself.  Context forge (2026-10-31) follows kiro's
window-adaptive policy (auto at the hard threshold, a soft notice, the
agent's own ``[[KIRO_FORGE]]`` line, ``POST /kirocrew/forge``).

Disabled unless ``kirocrew_enabled = true`` in config.toml: while disabled
the contact has no history store, so /chat/contacts never lists it and
/chat/send answers 501.
"""
from __future__ import annotations

from typing import Any

from .kiro import rejects_inbound  # noqa: F401 -- same ingress policy as Kiro


CONTACT = {
    "id": "kirocrew",
    "display_name": "Crew",
    "provider": "kiro-crew",
    "terminal_target": "",
    "capabilities": [
        "chat", "history", "draft", "busy", "stop", "attachments", "realtime", "forward",
    ],
    "stop_fields": ["contact_id", "user_ts"],
}

ROUTE = {"send_handler": "kirocrew", "capabilities": CONTACT["capabilities"]}


def send(handler: Any, body: dict[str, Any]) -> None:
    handler._handle_kirocrew_chat_send(body, "kirocrew")


def stop(handler: Any, body: dict[str, Any]) -> bool:
    handler._handle_kirocrew_chat_stop(body)
    return True


GET_ROUTES = {
    "/kirocrew/preferences": "_handle_kirocrew_preferences_get",
}

POST_ROUTES = {
    "/kirocrew/new_session": "_handle_kirocrew_new_session",
    "/kirocrew/forge": "_handle_kirocrew_forge",
    "/kirocrew/preferences": "_handle_kirocrew_preferences_post",
}
