"""One-time Android WebView -> X (Twitter) cookie import bridge.

Mirrors the xhs_login.py discipline: cookie values stay in the request body
and the fixed subprocess stdin.  This module deliberately never places them in
argv, environment variables, logs, exceptions, or response bodies.

The consumer is twitter-cli on the Tokyo box (x-login-bridge repo).  The
remote importer verifies the candidate cookies with a real ``twitter status``
call *before* storing them (so a fake/failed login leaves no file behind);
after it reports success this manager immediately re-runs the status probe
end to end and only then reports ``stored``.

``needs_login()`` is the server-side card gate probe: ``xcli status --json``
on Tokyo performs a real authenticated request.  Probe failures and unknown
shapes fail closed (no card), matching the XHS/JD gates.
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


X_LOGIN_URL = "https://x.com/i/flow/login"
X_LOGIN_ORIGIN = "cccompanion-android-webview-v1"
X_ACCOUNT = "ggg1446850"
DEFAULT_TTL_SECONDS = 300
DEFAULT_ALLOWED_CONTACTS = frozenset({"xiaoke"})
MAX_COOKIE_HEADER_BYTES = 12_000
MAX_COOKIE_VALUE_CHARS = 4_096
MAX_PENDING_SESSIONS = 16
_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "tokyo"]
DEFAULT_IMPORT_COMMAND = [*_SSH, "/home/ubuntu/x-login-bridge/import_cookies.py"]
DEFAULT_STATUS_COMMAND = [*_SSH, "/home/ubuntu/x-login-bridge/xcli", "status", "--json"]
# X anti-bot is strict and every probe is a real authenticated request, so the
# gate caches longer than the XHS one.  A successful import resets it.
DEFAULT_STATUS_CACHE_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 20

# Bound to the cookies twitter-cli / x.com use for an account session.
# Unknown fields are dropped rather than forwarded to the remote helper.
COOKIE_ALLOWLIST = frozenset({
    "auth_token",
    "ct0",
    "twid",
    "kdt",
    "att",
    "guest_id",
    "guest_id_ads",
    "guest_id_marketing",
    "personalization_id",
    "lang",
    "_twitter_sess",
})
REQUIRED_COOKIES = ("auth_token", "ct0")
COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class XLoginError(RuntimeError):
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
        raise XLoginError(400, "bad_cookie", "cookie header required")
    encoded = raw.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > MAX_COOKIE_HEADER_BYTES:
        raise XLoginError(413, "bad_cookie", "cookie header size invalid")

    cookies: dict[str, str] = {}
    for segment in raw.split(";"):
        item = segment.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not COOKIE_NAME_RE.fullmatch(name):
            raise XLoginError(400, "bad_cookie", "cookie header malformed")
        if name not in COOKIE_ALLOWLIST:
            continue
        if not value or len(value) > MAX_COOKIE_VALUE_CHARS or any(ord(ch) < 0x20 for ch in value):
            raise XLoginError(400, "bad_cookie", "cookie value invalid")
        cookies[name] = value

    if any(not cookies.get(name) for name in REQUIRED_COOKIES):
        raise XLoginError(422, "login_incomplete", "required login cookies are missing")
    return cookies


def classify_status_doc(returncode: int, stdout: bytes | None) -> str:
    """Map ``twitter status --json`` output to "ok" / "needs_login" / "unknown"."""
    try:
        doc = json.loads((stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unknown"
    if not isinstance(doc, dict):
        return "unknown"
    if doc.get("ok") is True:
        if returncode != 0:
            return "unknown"
        data = doc.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("authenticated") is not True or not isinstance(user, dict):
            return "needs_login"
        name = str(user.get("screenName") or user.get("username") or "").strip()
        return "ok" if name else "needs_login"
    if doc.get("ok") is False:
        error = doc.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        # not_authenticated covers a missing cookie file and an expired /
        # revoked session (HTTP 401/403 from X).  Rate limits, network errors
        # and anything else are "unknown" and never authorize the card.
        return "needs_login" if code == "not_authenticated" else "unknown"
    return "unknown"


class XLoginManager:
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
            raise ValueError("x import command must be a fixed non-empty argv list")
        self.import_command = tuple(command)
        probe = list(status_command or DEFAULT_STATUS_COMMAND)
        if not probe or len(probe) > 16 or any(not isinstance(item, str) or not item for item in probe):
            raise ValueError("x status command must be a fixed non-empty argv list")
        self.status_command = tuple(probe)
        self.ttl_seconds = max(60, min(int(ttl_seconds), 600))
        self.allowed_contacts = set(
            DEFAULT_ALLOWED_CONTACTS if allowed_contacts is None else allowed_contacts
        )
        self.status_cache_seconds = max(0, min(int(status_cache_seconds), 3600))
        self._runner = runner
        self._clock = clock
        self._pending: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()
        self._needs_login_cache: tuple[float, bool] | None = None

    def needs_login(self) -> bool:
        """True only while Tokyo's twitter-cli reports the X session is unusable.

        Never raises, never exposes cookie material, caches the probe so one
        ssh+X request serves many chat turns.  Probe errors fail closed.
        """
        now = self._clock()
        cached = self._needs_login_cache
        if cached is not None and cached[0] > now:
            return cached[1]
        value = self.probe_state() == "needs_login"
        self._needs_login_cache = (now + self.status_cache_seconds, value)
        return value

    def probe_state(self) -> str:
        try:
            result = self._runner(
                list(self.status_command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return classify_status_doc(result.returncode, result.stdout)

    @staticmethod
    def _validate_binding(contact_id: Any, device_id: Any, origin: Any) -> tuple[str, str, str]:
        contact = str(contact_id or "").strip().lower()
        device = str(device_id or "").strip()
        source = str(origin or "").strip()
        if not contact or not DEVICE_ID_RE.fullmatch(device):
            raise XLoginError(400, "bad_binding", "contact or device invalid")
        if source != X_LOGIN_ORIGIN:
            raise XLoginError(403, "bad_origin", "origin rejected")
        return contact, device, source

    def start(self, *, contact_id: Any, device_id: Any, origin: Any) -> dict[str, Any]:
        contact, device, source = self._validate_binding(contact_id, device_id, origin)
        if contact not in self.allowed_contacts:
            raise XLoginError(403, "contact_rejected", "contact rejected")
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
            "login_url": X_LOGIN_URL,
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
            raise XLoginError(400, "bad_nonce", "nonce invalid")
        cookies = _parse_cookie_header(cookie_header)
        now = self._clock()
        # Pop before privileged I/O: the capability is one-shot even when the
        # remote helper fails or two requests race.
        with self._lock:
            pending = self._pending.pop(capability, None)
        if pending is None:
            raise XLoginError(409, "nonce_used", "login session unavailable")
        if pending.expires_at <= now:
            raise XLoginError(410, "nonce_expired", "login session expired")
        if (pending.contact_id, pending.device_id, pending.origin) != (contact, device, source):
            raise XLoginError(403, "binding_mismatch", "login session binding mismatch")

        payload = json.dumps({"cookies": cookies}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            result = self._runner(
                list(self.import_command),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise XLoginError(502, "sync_failed", "cookie sync failed") from None
        try:
            response = json.loads((result.stdout or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise XLoginError(502, "sync_failed", "cookie sync failed") from None
        status = response.get("status") if isinstance(response, dict) else None
        if not isinstance(response, dict) or response.get("ok") is not True or result.returncode != 0:
            # The importer verified the cookies against X before storing and
            # stored nothing on any of these branches.
            if status == "verify_failed":
                raise XLoginError(422, "verify_failed", "x rejected the imported login")
            if status == "wrong_account":
                raise XLoginError(422, "wrong_account", "login is not the expected x account")
            if status == "verify_unavailable":
                raise XLoginError(502, "verify_unavailable", "x login could not be verified")
            raise XLoginError(502, "sync_failed", "cookie sync failed")

        # Stored: run the live probe once more, end to end, before reporting.
        self._needs_login_cache = None
        state = self.probe_state()
        if state == "needs_login":
            self._needs_login_cache = (self._clock() + self.status_cache_seconds, True)
            raise XLoginError(422, "verify_failed", "x rejected the imported login")
        if state != "ok":
            raise XLoginError(502, "verify_unavailable", "x login could not be verified")
        self._needs_login_cache = (self._clock() + self.status_cache_seconds, False)
        return {"ok": True, "status": "stored", "account": str(response.get("screen_name") or X_ACCOUNT)}
