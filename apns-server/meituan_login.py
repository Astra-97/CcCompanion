"""One-time Android WebView -> Meituan main-site cookie import bridge.

Mirrors the xhs_login.py / netease_login.py discipline: cookie values stay in
the request body and the fixed subprocess stdin.  This module deliberately
never places them in argv, environment variables, logs, exceptions, or
response bodies.

The Meituan consumer is the long-lived Chrome on memory-sg (CDP 9225); the
remote helper injects the allowlisted cookies via ``Network.setCookie``.

This card covers the Meituan main site (i.meituan.com): the login URL is the
mobile "我的" page, which redirects logged-out sessions to the passport
mobile SMS login page (useraccount/ilogin).  The waimai card lives in
mt_waimai_login.py; the two login states are probed independently even
though both ride the same .meituan.com cookie jar.

``needs_login()`` is the server-side card gate probe: it runs meituan-mcp's
``meituan_tools.py status_main`` on memory-sg (the i.meituan.com account
gate, verified live 2026-09-19 in both login states) and caches the result
briefly.  Probe failures and unknown status values fail closed (no card),
matching the NetEase gate's exception handling in push.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Callable


# 主站移动版「我的」页：未登录自动 302 到 passport ilogin 短信登录页
# （2026-09-19 隔离 browser context 实测），登录后回到账号页。
MEITUAN_LOGIN_URL = "https://i.meituan.com/mttouch/page/account"
MEITUAN_LOGIN_ORIGIN = "cccompanion-android-webview-v1"
DEFAULT_TTL_SECONDS = 300
DEFAULT_ALLOWED_CONTACTS = frozenset({"kairos", "kimi"})
MAX_COOKIE_HEADER_BYTES = 16_000

logger = logging.getLogger("cc-apns-server")
MAX_COOKIE_VALUE_CHARS = 8_192
MAX_PENDING_SESSIONS = 16
# 固定尾参 "main"：远端注入端改用主站判据（i.meituan.com）复验。
DEFAULT_IMPORT_COMMAND = [
    "ssh",
    "memory-sg",
    "/home/ubuntu/taobao-login/.venv/bin/python",
    "/home/ubuntu/meituan-login/import_cookies.py",
    "main",
]
DEFAULT_STATUS_COMMAND = [
    "ssh",
    "memory-sg",
    "/home/ubuntu/taobao-login/.venv/bin/python",
    "/home/ubuntu/meituan-mcp/meituan_tools.py",
    "status_main",
]
# The status probe drives a real CDP navigation; keep it rare.
DEFAULT_STATUS_CACHE_SECONDS = 300

# Known Meituan H5 cookie names (session + device fingerprint + locality).
# Unknown fields are dropped rather than forwarded to the privileged remote
# helper.  ``token`` is the account session cookie sms_meituan.py treats as
# the login-success signal.
# 2026-09-25 放宽：主站「我的」页只放行 12 个时复验不过（verify_failed），
# 补齐登录后 mttouch/passport 现场实际存在的名字（实测自 CDP cookie 罐）。
COOKIE_ALLOWLIST = frozenset({
    "token",
    "u",
    "uuid",
    "iuuid",
    "openh5_uuid",
    "_lxsdk_cuid",
    "_lxsdk",
    "_lxsdk_s",
    "_lx_utm",
    "_hc.v",
    "JSESSIONID",
    "IJSESSIONID",
    "cityid",
    "ci",
    "cityname",
    "lng",
    "lat",
    "latlng",
    "logan_session_token",
    "logintype",
    "mt_c_token",
    "isid",
    "isIframe",
    "WEBDFPID",
    "au_trace_key_net",
    "swim_line",
    "oops",
    "utm_source",
    "utm_source_rg",
    "wm_order_channel",
})
REQUIRED_COOKIES = ("token",)
COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class MeituanLoginError(RuntimeError):
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
        raise MeituanLoginError(400, "bad_cookie", "cookie header required")
    encoded = raw.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > MAX_COOKIE_HEADER_BYTES:
        raise MeituanLoginError(413, "bad_cookie", "cookie header size invalid")

    cookies: dict[str, str] = {}
    for segment in raw.split(";"):
        item = segment.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not COOKIE_NAME_RE.fullmatch(name):
            raise MeituanLoginError(400, "bad_cookie", "cookie header malformed")
        if name not in COOKIE_ALLOWLIST:
            continue
        if not value or len(value) > MAX_COOKIE_VALUE_CHARS or any(ord(ch) < 0x20 for ch in value):
            raise MeituanLoginError(400, "bad_cookie", "cookie value invalid")
        cookies[name] = value

    if any(not cookies.get(name) for name in REQUIRED_COOKIES):
        raise MeituanLoginError(422, "login_incomplete", "required login cookies are missing")
    return cookies


class MeituanLoginManager:
    def __init__(
        self,
        *,
        import_command: list[str] | tuple[str, ...] | None = None,
        status_command: list[str] | tuple[str, ...] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        allowed_contacts: set[str] | None = None,
        status_cache_seconds: int = DEFAULT_STATUS_CACHE_SECONDS,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        command = list(import_command or DEFAULT_IMPORT_COMMAND)
        if not command or len(command) > 16 or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("meituan import command must be a fixed non-empty argv list")
        self.import_command = tuple(command)
        probe = list(status_command or DEFAULT_STATUS_COMMAND)
        if not probe or len(probe) > 16 or any(not isinstance(item, str) or not item for item in probe):
            raise ValueError("meituan status command must be a fixed non-empty argv list")
        self.status_command = tuple(probe)
        self.ttl_seconds = max(60, min(int(ttl_seconds), 600))
        self.allowed_contacts = set(
            DEFAULT_ALLOWED_CONTACTS if allowed_contacts is None else allowed_contacts
        )
        self.status_cache_seconds = max(0, min(int(status_cache_seconds), 1800))
        self._runner = runner
        self._clock = clock
        self._pending: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()
        self._needs_login_cache: tuple[float, bool] | None = None

    def needs_login(self) -> bool:
        """True while memory-sg reports the Meituan session is not logged in.

        This is the server-side gate for offering the login card; it never
        raises and never exposes cookie material.  The probe drives a real
        CDP navigation, so the result is cached; probe errors and unknown
        status values fail closed (no card).
        """
        now = self._clock()
        cached = self._needs_login_cache
        if cached is not None and cached[0] > now:
            return cached[1]
        value = self._probe_needs_login()
        self._needs_login_cache = (now + self.status_cache_seconds, value)
        return value

    def _probe_needs_login(self) -> bool:
        try:
            result = self._runner(
                list(self.status_command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=150,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            lines = (result.stdout or b"").decode("utf-8").strip().splitlines()
            doc = json.loads(lines[-1]) if lines else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(doc, dict):
            return False
        # Only meituan_tools' explicit not-logged-in states authorize the card.
        return doc.get("status") in ("waiting", "needs_verification")

    @staticmethod
    def _validate_binding(contact_id: Any, device_id: Any, origin: Any) -> tuple[str, str, str]:
        contact = str(contact_id or "").strip().lower()
        device = str(device_id or "").strip()
        source = str(origin or "").strip()
        if not contact or not DEVICE_ID_RE.fullmatch(device):
            raise MeituanLoginError(400, "bad_binding", "contact or device invalid")
        if source != MEITUAN_LOGIN_ORIGIN:
            raise MeituanLoginError(403, "bad_origin", "origin rejected")
        return contact, device, source

    def start(self, *, contact_id: Any, device_id: Any, origin: Any) -> dict[str, Any]:
        contact, device, source = self._validate_binding(contact_id, device_id, origin)
        if contact not in self.allowed_contacts:
            raise MeituanLoginError(403, "contact_rejected", "contact rejected")
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
            "login_url": MEITUAN_LOGIN_URL,
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
            raise MeituanLoginError(400, "bad_nonce", "nonce invalid")
        cookies = _parse_cookie_header(cookie_header)
        now = self._clock()
        # Pop before privileged I/O: the capability is one-shot even when the
        # remote helper fails or two requests race.
        with self._lock:
            pending = self._pending.pop(capability, None)
        if pending is None:
            raise MeituanLoginError(409, "nonce_used", "login session unavailable")
        if pending.expires_at <= now:
            raise MeituanLoginError(410, "nonce_expired", "login session expired")
        if (pending.contact_id, pending.device_id, pending.origin) != (contact, device, source):
            raise MeituanLoginError(403, "binding_mismatch", "login session binding mismatch")

        payload = json.dumps({"cookies": cookies}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            result = self._runner(
                list(self.import_command),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=150,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("meituan cookie import failed: runner error", exc_info=True)
            raise MeituanLoginError(502, "sync_failed", "cookie sync failed") from None
        # The remote helper only ever emits cookie *names* and status codes,
        # so its stdout/stderr are safe to log for diagnosis (values never
        # leave the request body / subprocess stdin).
        try:
            response = json.loads((result.stdout or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = None
        if result.returncode != 0 or not isinstance(response, dict) or response.get("ok") is not True:
            logger.warning(
                "meituan cookie import failed: rc=%s remote=%r stderr=%r",
                result.returncode,
                response,
                (result.stderr or b"").decode("utf-8", errors="replace")[:500],
            )
            raise MeituanLoginError(502, "sync_failed", "cookie sync failed")
        # A successful import flips the card gate on the next probe.
        self._needs_login_cache = None
        return {"ok": True, "status": "stored"}
