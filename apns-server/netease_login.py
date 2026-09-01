"""One-time Android WebView -> NetEase Cloud Music cookie import bridge.

Mirrors the xhs_login.py discipline: cookie values stay in the request body
and the two 0600 credential files this module owns.  They are deliberately
never placed in argv, environment variables of other processes, logs,
exceptions, or response bodies.

Both NetEase projects read their cookie once at startup, so a successful
import ends with a fixed ``systemctl restart`` of the three netease-*
services.  Those services are unrelated to cc-companion.service and may be
restarted at any time.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable


NETEASE_LOGIN_URL = "https://music.163.com/"
NETEASE_LOGIN_ORIGIN = "cccompanion-android-webview-v1"
DEFAULT_TTL_SECONDS = 300
DEFAULT_ALLOWED_CONTACTS = frozenset({"kairos", "kimi"})
MAX_COOKIE_HEADER_BYTES = 8_000
MAX_PENDING_SESSIONS = 16
DEFAULT_CRED_FILE = Path("/root/netease-music/anko3o/server/.netease_cred")
DEFAULT_VAEL_ENV_FILE = Path("/root/netease-music/env/vael-mcp.env")
DEFAULT_RESTART_COMMAND = [
    "systemctl",
    "restart",
    "netease-vael-mcp.service",
    "netease-music-server.service",
    "netease-music-mcp.service",
]

# Only the two account cookies the deployments actually consume are accepted;
# unknown fields are dropped rather than persisted to a credential file.
COOKIE_ALLOWLIST = frozenset({"MUSIC_U", "__csrf"})
COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Values must additionally be safe inside a double-quoted systemd env-file
# line: no whitespace, quotes, backslashes, dollar signs, or control chars.
COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~%+/=-]{8,8192}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class NeteaseLoginError(RuntimeError):
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
        raise NeteaseLoginError(400, "bad_cookie", "cookie header required")
    encoded = raw.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > MAX_COOKIE_HEADER_BYTES:
        raise NeteaseLoginError(413, "bad_cookie", "cookie header size invalid")

    cookies: dict[str, str] = {}
    for segment in raw.split(";"):
        item = segment.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not COOKIE_NAME_RE.fullmatch(name):
            raise NeteaseLoginError(400, "bad_cookie", "cookie header malformed")
        if name not in COOKIE_ALLOWLIST:
            continue
        if not COOKIE_VALUE_RE.fullmatch(value):
            raise NeteaseLoginError(400, "bad_cookie", "cookie value invalid")
        cookies[name] = value

    if not cookies.get("MUSIC_U"):
        raise NeteaseLoginError(422, "login_incomplete", "required login cookies are missing")
    return cookies


def _atomic_write_0600(path: Path, content: str) -> None:
    """Replace ``path`` atomically with 0600 content owned by this process."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _render_vael_env(existing: str, cookie_header: str, csrf: str) -> str:
    """Return the env file with NETEASE_COOKIE/NETEASE_CSRF replaced in place.

    Unrelated lines (NETEASE_READONLY and future additions) pass through
    verbatim; missing keys are appended.
    """
    replacements = {
        "NETEASE_COOKIE": f'NETEASE_COOKIE="{cookie_header}"',
        "NETEASE_CSRF": f'NETEASE_CSRF="{csrf}"',
    }
    seen: set[str] = set()
    out: list[str] = []
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in replacements:
            out.append(replacements[key])
            seen.add(key)
        else:
            out.append(line)
    for key, rendered in replacements.items():
        if key not in seen:
            out.append(rendered)
    return "\n".join(out).strip("\n") + "\n"


class NeteaseLoginManager:
    def __init__(
        self,
        *,
        cred_file: str | Path = DEFAULT_CRED_FILE,
        vael_env_file: str | Path = DEFAULT_VAEL_ENV_FILE,
        restart_command: list[str] | tuple[str, ...] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        allowed_contacts: set[str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        command = list(restart_command or DEFAULT_RESTART_COMMAND)
        if not command or len(command) > 16 or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("netease restart command must be a fixed non-empty argv list")
        self.restart_command = tuple(command)
        self.cred_file = Path(cred_file)
        self.vael_env_file = Path(vael_env_file)
        self.ttl_seconds = max(60, min(int(ttl_seconds), 600))
        self.allowed_contacts = set(
            DEFAULT_ALLOWED_CONTACTS if allowed_contacts is None else allowed_contacts
        )
        self._runner = runner
        self._clock = clock
        self._pending: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()

    def needs_login(self) -> bool:
        """True while the Anko3o credential file lacks a MUSIC_U line.

        This is the server-side gate for offering the login card; it never
        raises and never exposes the cookie value.
        """
        try:
            for line in self.cred_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("MUSIC_U=") and line.split("=", 1)[1].strip():
                    return False
        except OSError:
            pass
        return True

    @staticmethod
    def _validate_binding(contact_id: Any, device_id: Any, origin: Any) -> tuple[str, str, str]:
        contact = str(contact_id or "").strip().lower()
        device = str(device_id or "").strip()
        source = str(origin or "").strip()
        if not contact or not DEVICE_ID_RE.fullmatch(device):
            raise NeteaseLoginError(400, "bad_binding", "contact or device invalid")
        if source != NETEASE_LOGIN_ORIGIN:
            raise NeteaseLoginError(403, "bad_origin", "origin rejected")
        return contact, device, source

    def start(self, *, contact_id: Any, device_id: Any, origin: Any) -> dict[str, Any]:
        contact, device, source = self._validate_binding(contact_id, device_id, origin)
        if contact not in self.allowed_contacts:
            raise NeteaseLoginError(403, "contact_rejected", "contact rejected")
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
            "login_url": NETEASE_LOGIN_URL,
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
            raise NeteaseLoginError(400, "bad_nonce", "nonce invalid")
        cookies = _parse_cookie_header(cookie_header)
        now = self._clock()
        # Pop before privileged I/O: the capability is one-shot even when the
        # file write or service restart fails or two requests race.
        with self._lock:
            pending = self._pending.pop(capability, None)
        if pending is None:
            raise NeteaseLoginError(409, "nonce_used", "login session unavailable")
        if pending.expires_at <= now:
            raise NeteaseLoginError(410, "nonce_expired", "login session expired")
        if (pending.contact_id, pending.device_id, pending.origin) != (contact, device, source):
            raise NeteaseLoginError(403, "binding_mismatch", "login session binding mismatch")

        music_u = cookies["MUSIC_U"]
        csrf = cookies.get("__csrf", "")
        vael_cookie = f"MUSIC_U={music_u};__csrf={csrf}" if csrf else f"MUSIC_U={music_u}"
        try:
            _atomic_write_0600(self.cred_file, f"MUSIC_U={music_u}\n")
            try:
                existing = self.vael_env_file.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            _atomic_write_0600(self.vael_env_file, _render_vael_env(existing, vael_cookie, csrf))
        except OSError:
            raise NeteaseLoginError(502, "sync_failed", "cookie sync failed") from None

        try:
            result = self._runner(
                list(self.restart_command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise NeteaseLoginError(502, "sync_failed", "cookie sync failed") from None
        if result.returncode != 0:
            raise NeteaseLoginError(502, "sync_failed", "cookie sync failed")
        return {"ok": True, "status": "stored"}
