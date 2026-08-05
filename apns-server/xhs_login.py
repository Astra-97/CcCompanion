"""One-time Android WebView -> xhs-cli cookie import bridge.

Cookie values stay in the request body and the fixed subprocess stdin.  This
module deliberately never places them in argv, environment variables, logs,
exceptions, or response bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Callable


XHS_LOGIN_URL = "https://www.xiaohongshu.com/login"
XHS_LOGIN_ORIGIN = "cccompanion-android-webview-v1"
DEFAULT_TTL_SECONDS = 300
DEFAULT_ALLOWED_CONTACTS = frozenset({"kairos"})
MAX_COOKIE_HEADER_BYTES = 24_000
MAX_COOKIE_VALUE_CHARS = 8_192
MAX_PENDING_SESSIONS = 16
DEFAULT_IMPORT_COMMAND = [
    "ssh",
    "memory-sg",
    "/home/ubuntu/xhs-login-bridge/import_cookies.py",
]

# Bound to the browser cookies xhs-cli currently consumes. Unknown fields are
# dropped rather than forwarded to the privileged remote helper.
COOKIE_ALLOWLIST = frozenset({
    "a1",
    "webId",
    "web_session",
    "web_session_sec",
    "gid",
    "xsecappid",
    "webBuild",
    "websectiga",
    "sec_poison_id",
    "loadts",
    "unread",
    "acw_tc",
    "abRequestId",
    "id_token",
    "ets",
    "x-rednote-datactry",
    "x-rednote-holderctry",
})
COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class XhsLoginError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PendingLogin:
    contact_id: str
    device_id: str
    origin: str
    expires_at: float


def _parse_cookie_header(raw: Any) -> dict[str, str]:
    if not isinstance(raw, str):
        raise XhsLoginError(400, "bad_cookie", "cookie header required")
    encoded = raw.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > MAX_COOKIE_HEADER_BYTES:
        raise XhsLoginError(413, "bad_cookie", "cookie header size invalid")

    cookies: dict[str, str] = {}
    for segment in raw.split(";"):
        item = segment.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not COOKIE_NAME_RE.fullmatch(name):
            raise XhsLoginError(400, "bad_cookie", "cookie header malformed")
        if name not in COOKIE_ALLOWLIST:
            continue
        if not value or len(value) > MAX_COOKIE_VALUE_CHARS or any(ord(ch) < 0x20 for ch in value):
            raise XhsLoginError(400, "bad_cookie", "cookie value invalid")
        cookies[name] = value

    missing = [name for name in ("a1", "webId") if not cookies.get(name)]
    if missing or not cookies.get("web_session"):
        raise XhsLoginError(422, "login_incomplete", "required login cookies are missing")
    return cookies


class XhsLoginManager:
    def __init__(
        self,
        *,
        import_command: list[str] | tuple[str, ...] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        allowed_contacts: set[str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        command = list(import_command or DEFAULT_IMPORT_COMMAND)
        if not command or len(command) > 16 or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("xhs import command must be a fixed non-empty argv list")
        self.import_command = tuple(command)
        self.ttl_seconds = max(60, min(int(ttl_seconds), 600))
        self.allowed_contacts = set(
            DEFAULT_ALLOWED_CONTACTS if allowed_contacts is None else allowed_contacts
        )
        self._runner = runner
        self._clock = clock
        self._pending: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validate_binding(contact_id: Any, device_id: Any, origin: Any) -> tuple[str, str, str]:
        contact = str(contact_id or "").strip().lower()
        device = str(device_id or "").strip()
        source = str(origin or "").strip()
        if not contact or not DEVICE_ID_RE.fullmatch(device):
            raise XhsLoginError(400, "bad_binding", "contact or device invalid")
        if source != XHS_LOGIN_ORIGIN:
            raise XhsLoginError(403, "bad_origin", "origin rejected")
        return contact, device, source

    def start(self, *, contact_id: Any, device_id: Any, origin: Any) -> dict[str, Any]:
        contact, device, source = self._validate_binding(contact_id, device_id, origin)
        if contact not in self.allowed_contacts:
            raise XhsLoginError(403, "contact_rejected", "contact rejected")
        now = self._clock()
        nonce = secrets.token_urlsafe(32)
        with self._lock:
            self._pending = {
                key: value for key, value in self._pending.items() if value.expires_at > now
            }
            # A device has only one usable capability for a contact at a time.
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if (value.contact_id, value.device_id) != (contact, device)
            }
            while len(self._pending) >= MAX_PENDING_SESSIONS:
                oldest = min(self._pending, key=lambda key: self._pending[key].expires_at)
                self._pending.pop(oldest, None)
            self._pending[nonce] = PendingLogin(contact, device, source, now + self.ttl_seconds)
        return {
            "ok": True,
            "nonce": nonce,
            "expires_in": self.ttl_seconds,
            "login_url": XHS_LOGIN_URL,
        }

    def import_cookies(
        self,
        *,
        nonce: Any,
        contact_id: Any,
        device_id: Any,
        origin: Any,
        cookie_header: Any,
    ) -> dict[str, Any]:
        contact, device, source = self._validate_binding(contact_id, device_id, origin)
        capability = str(nonce or "")
        if len(capability) < 32 or len(capability) > 128:
            raise XhsLoginError(400, "bad_nonce", "nonce invalid")
        cookies = _parse_cookie_header(cookie_header)
        now = self._clock()
        # Pop before privileged I/O: the capability is one-shot even when the
        # remote helper fails or two requests race.
        with self._lock:
            pending = self._pending.pop(capability, None)
        if pending is None:
            raise XhsLoginError(409, "nonce_used", "login session unavailable")
        if pending.expires_at <= now:
            raise XhsLoginError(410, "nonce_expired", "login session expired")
        if (pending.contact_id, pending.device_id, pending.origin) != (contact, device, source):
            raise XhsLoginError(403, "binding_mismatch", "login session binding mismatch")

        payload = json.dumps({"cookies": cookies}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            result = self._runner(
                list(self.import_command),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise XhsLoginError(502, "sync_failed", "cookie sync failed") from None
        if result.returncode != 0:
            raise XhsLoginError(502, "sync_failed", "cookie sync failed")
        try:
            response = json.loads((result.stdout or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise XhsLoginError(502, "sync_failed", "cookie sync failed") from None
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise XhsLoginError(502, "sync_failed", "cookie sync failed")
        return {"ok": True, "status": "stored"}
