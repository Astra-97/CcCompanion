"""哈基米 (``hajiki``, 2026-11-01): Gemini 4 Argon via pi on Tokyo.

Was a read-only archive since the group days; 方小南 brought her back with
Gemini 4 Argon on Vertex AI, running in pi's RPC mode as user ``pi`` on Tokyo
(``/home/pi/hajiki``, same harness as 哥哥写手 ``gege``).  Text only, app only:
no attachments, no WeChat mirror, no proactive turns, no heartbeat — pi is
called only when she sends a message.
"""
from __future__ import annotations

from typing import Any

CONTACT = {
    "id": "hajiki",
    "display_name": "哈基米",
    "provider": "pi-rpc",
    "terminal_target": "",
    "capabilities": ["chat", "history", "draft", "busy", "stop", "realtime"],
    "stop_fields": ["contact_id", "user_ts"],
}

ROUTE = {"send_handler": "hajiki", "capabilities": CONTACT["capabilities"]}


def send(handler: Any, body: dict[str, Any]) -> None:
    handler._handle_hajiki_chat_send(body)


def stop(handler: Any, body: dict[str, Any]) -> bool:
    handler._handle_hajiki_chat_stop(body)
    return True


GET_ROUTES: dict[str, str] = {}

POST_ROUTES = {
    "/hajiki/restart": "_handle_hajiki_restart",
}
