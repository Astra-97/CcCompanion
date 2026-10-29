"""哥哥写手 (``gege``, 2026-10-29): writing-only contact backed by pi on Tokyo.

Text only (no attachments / voice / cards); the main entrance is the 企业微信
app 「哥哥写手」 (wecom-channel route ``companion:gege``), the app chat works the
same way.  Commands (/new /list /open /roll /undo /set /rename /info /help)
are handled in push.py ``_gege_process`` without a model call.
"""
from __future__ import annotations

from typing import Any

CONTACT = {
    "id": "gege",
    "display_name": "哥哥写手",
    "provider": "pi-rpc",
    "terminal_target": "",
    "capabilities": ["chat", "history", "draft", "busy", "stop", "realtime"],
    "stop_fields": ["contact_id", "user_ts"],
}

ROUTE = {"send_handler": "gege", "capabilities": CONTACT["capabilities"]}


def send(handler: Any, body: dict[str, Any]) -> None:
    handler._handle_gege_chat_send(body, "gege")


def stop(handler: Any, body: dict[str, Any]) -> bool:
    handler._handle_gege_chat_stop(body)
    return True


GET_ROUTES: dict[str, str] = {}

POST_ROUTES = {
    "/gege/restart": "_handle_gege_restart",
}
