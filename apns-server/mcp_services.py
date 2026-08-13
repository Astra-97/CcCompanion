"""Server-owned configuration for the small, allow-listed food-order MCPs.

Tokens deliberately live in files shared by the Kairos and XiaoKe runtimes,
never in the Android client or normal application state.  This module returns
only configuration/health metadata and must not log caller-supplied tokens.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import stat
import pwd
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MCP_TEST_TIMEOUT_SECONDS = 8
MCP_TEST_RESPONSE_LIMIT = 64 * 1024

PROVIDERS: dict[str, dict[str, str]] = {
    "luckin": {
        "name": "瑞幸咖啡",
        "endpoint": "https://gwmcp.lkcoffee.com/order/user/mcp",
        "canonical_token_path": "/var/lib/cc-xia-relay/channel-state/mcp-tokens/luckin.token",
        # Read-only migration source.  The relay cannot read root's HOME, so
        # this is never the bridge's effective configuration.
        "legacy_token_path": "/root/.my-coffee/LUCKIN_MCP_TOKEN",
        "env_path": "LUCKIN_MCP_TOKEN_PATH",
    },
    "mcdonalds": {
        "name": "麦当劳",
        "endpoint": "https://mcp.mcd.cn",
        "canonical_token_path": "/var/lib/cc-xia-relay/channel-state/mcp-tokens/mcdonalds.token",
        "legacy_token_path": "/root/.my-coffee/MCDONALDS_MCP_TOKEN",
        "env_path": "MCDONALDS_MCP_TOKEN_PATH",
    },
}


class McpServiceError(ValueError):
    """A deliberately safe message suitable for the authenticated UI."""


class McpServiceStore:
    def __init__(self, metadata_path: Path | str):
        self.metadata_path = Path(metadata_path)
        # Token+metadata changes are a single logical update.  The atomic
        # file writes protect crashes; this lock prevents concurrent requests
        # from publishing contradictory status metadata.
        self._lock = threading.RLock()

    def _token_path(self, provider_id: str) -> Path:
        provider = PROVIDERS[provider_id]
        return Path(os.environ.get(provider["env_path"], provider["canonical_token_path"]))

    def _legacy_token_path(self, provider_id: str) -> Path | None:
        # Test/deployment environment overrides are already intentionally
        # canonical; never infer or chown an arbitrary sibling path.
        if os.environ.get(PROVIDERS[provider_id]["env_path"]):
            return None
        return Path(PROVIDERS[provider_id]["legacy_token_path"])

    @staticmethod
    def _read_token(path: Path) -> str:
        try:
            # Do not accept a directory, special device, or unbounded token.
            if not path.is_file() or path.stat().st_size > 16 * 1024:
                return ""
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    def _load_metadata(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _save_metadata(self, metadata: dict[str, dict[str, Any]]) -> None:
        self.metadata_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._atomic_write(self.metadata_path, json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(raw_path, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(raw_path)
            except FileNotFoundError:
                pass

    def _atomic_write_token(self, provider_id: str, path: Path, text: str) -> None:
        """Write one token while preserving the canonical relay ownership.

        Environment-overridden paths are intentionally ordinary 0600 server
        files; provisioning must never unexpectedly chown a test/deployment
        override.  Only the fixed canonical runtime paths get the relay owner.
        """
        if os.environ.get(PROVIDERS[provider_id]["env_path"]):
            self._atomic_write(path, text)
            return
        expected = Path(PROVIDERS[provider_id]["canonical_token_path"])
        if path != expected:
            raise McpServiceError("共享 MCP 令牌路径无效")
        try:
            account = pwd.getpwnam("cc-xia-relay")
            parent_info = path.parent.lstat()
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or path.parent.is_symlink()
                or parent_info.st_uid != account.pw_uid
                or stat.S_IMODE(parent_info.st_mode) != 0o700
            ):
                raise McpServiceError("共享 MCP 运行时尚未完成部署")
            fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
            try:
                os.fchmod(fd, 0o600)
                os.fchown(fd, account.pw_uid, account.pw_gid)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(raw_path, path)
                os.chmod(path, 0o600)
            finally:
                try:
                    os.unlink(raw_path)
                except FileNotFoundError:
                    pass
        except McpServiceError:
            raise
        except (KeyError, OSError):
            raise McpServiceError("无法保存共享 MCP 令牌") from None

    @staticmethod
    def _safe_message(message: Any) -> str:
        # Never reflect upstream bodies, URLs, or request tokens into the app.
        value = str(message or "连接失败").replace("\n", " ").strip()
        return value[:120] or "连接失败"

    def status(self) -> dict[str, Any]:
        with self._lock:
            metadata = self._load_metadata()
            providers = []
            for provider_id, provider in PROVIDERS.items():
                last = metadata.get(provider_id, {})
                if not isinstance(last, dict):
                    last = {}
                canonical = bool(self._read_token(self._token_path(provider_id)))
                legacy = bool(self._read_token(self._legacy_token_path(provider_id))) if self._legacy_token_path(provider_id) else False
                configured = canonical or legacy
                # A stale success is not a live connection.  The UI calls it
                # "checked" rather than treating it as a credential verdict.
                providers.append({
                    "id": provider_id,
                    "name": provider["name"],
                    "endpoint": provider["endpoint"],
                    "configured": configured,
                    "configuration_source": "shared" if canonical else ("legacy_migration_pending" if legacy else "none"),
                    "health": last.get("health") if configured else "not_configured",
                    "message": (
                        self._safe_message(last.get("message", "")) if canonical
                        else ("已发现旧令牌，待迁移到共享运行时" if legacy else "未配置")
                    ),
                    "last_checked": int(last.get("last_checked", 0) or 0) if configured else 0,
                })
            return {"ok": True, "providers": providers, "runtime": self._runtime_status()}

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._update_locked(body)

    def _update_locked(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise McpServiceError("请求格式无效")
        if set(body) - {"provider_id", "action", "token", "confirm_clear"}:
            raise McpServiceError("请求包含不支持的字段")
        provider_id = body.get("provider_id")
        action = body.get("action")
        if provider_id not in PROVIDERS or action not in {"save", "test", "save_and_test", "clear"}:
            raise McpServiceError("不支持的 MCP 服务或操作")
        if not isinstance(provider_id, str) or not isinstance(action, str):
            raise McpServiceError("请求格式无效")
        token = body.get("token", "")
        if token is None:
            token = ""
        if not isinstance(token, str) or len(token.strip()) > 8192 or any(ord(c) < 32 for c in token):
            raise McpServiceError("令牌格式无效")
        token = token.strip()
        path = self._token_path(provider_id)

        if action == "clear":
            if body.get("confirm_clear") is not True:
                raise McpServiceError("请确认清除该服务的服务器端令牌")
            try:
                path.unlink(missing_ok=True)
                legacy_path = self._legacy_token_path(provider_id)
                if legacy_path is not None:
                    legacy_path.unlink(missing_ok=True)
            except OSError:
                raise McpServiceError("无法清除服务器端令牌") from None
            metadata = self._load_metadata()
            metadata.pop(provider_id, None)
            self._save_metadata(metadata)
            return self.status()

        # An empty input is intentionally a no-op for save/test, preventing a
        # password field which was left blank from erasing a shared token.
        if token:
            if not os.environ.get(PROVIDERS[provider_id]["env_path"]) and not path.parent.is_dir():
                raise McpServiceError("共享 MCP 运行时尚未完成部署")
            self._atomic_write_token(provider_id, path, token + "\n")
        elif not self._read_token(path):
            raise McpServiceError("请先输入该服务的令牌")

        if action in {"test", "save_and_test"}:
            health, message = self._test_provider(provider_id, self._read_token(path))
            metadata = self._load_metadata()
            metadata[provider_id] = {
                "health": health,
                "message": message,
                "last_checked": int(time.time()),
            }
            self._save_metadata(metadata)
        return self.status()

    @staticmethod
    def _contains_bridge_config(path: Path, provider_id: str) -> bool:
        """Read only non-secret configuration and require the fixed bridge."""
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            entry = payload.get("mcp_servers", {}).get(provider_id)
        except (OSError, UnicodeError, ValueError, TypeError):
            return False
        return entry == {
            "command": "/usr/local/libexec/cc-companion-mcp-bridge",
            "args": [provider_id],
        }

    @staticmethod
    def _contains_claude_bridge_config(path: Path, provider_id: str) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entry = payload.get("mcpServers", {}).get(provider_id, {})
            return entry == {
                "command": "/usr/local/libexec/cc-companion-mcp-bridge",
                "args": [provider_id],
            }
        except (OSError, ValueError, TypeError):
            return False

    def _runtime_status(self) -> dict[str, Any]:
        """Truthful discovery only; installation/reload is an explicit op."""
        bridge = Path(os.environ.get("CC_MCP_BRIDGE_PATH", "/usr/local/libexec/cc-companion-mcp-bridge"))
        codex_config = Path(os.environ.get("CODEX_HOME", "/root/.codex")) / "config.toml"
        claude_template = Path(os.environ.get("CC_XIA_MCP_TEMPLATE", "/opt/cc-xia-claude-channel/.mcp.json.in"))
        claude_active = Path(os.environ.get("CC_XIA_ACTIVE_MCP_CONFIG", "/var/lib/cc-xia-relay/channel-state/runtime/.mcp.json"))
        try:
            bridge_info = bridge.lstat()
            bridge_installed = (
                stat.S_ISREG(bridge_info.st_mode)
                and not bridge.is_symlink()
                and bridge_info.st_uid == 0
                and not (stat.S_IMODE(bridge_info.st_mode) & 0o022)
                and bool(bridge_info.st_mode & stat.S_IXUSR)
            )
        except OSError:
            bridge_installed = False
        codex_registered = all(self._contains_bridge_config(codex_config, key) for key in PROVIDERS)
        xia_template_registered = all(self._contains_claude_bridge_config(claude_template, key) for key in PROVIDERS)
        xia_active_registered = all(self._contains_claude_bridge_config(claude_active, key) for key in PROVIDERS)
        # Test-only override; production is deliberately the fixed canonical
        # relay runtime path, never an API/client supplied location.
        token_root = Path(os.environ.get("CC_MCP_TOKEN_ROOT", "/var/lib/cc-xia-relay/channel-state/mcp-tokens"))
        try:
            relay_uid = pwd.getpwnam("cc-xia-relay").pw_uid
            token_root_info = token_root.lstat()
            shared_token_root_ready = (
                stat.S_ISDIR(token_root_info.st_mode)
                and not token_root.is_symlink()
                and token_root_info.st_uid == relay_uid
                and stat.S_IMODE(token_root_info.st_mode) == 0o700
            )
        except (KeyError, OSError):
            relay_uid = -1
            shared_token_root_ready = False
        configured_canonical = [
            self._token_path(provider_id)
            for provider_id in PROVIDERS
            if self._read_token(self._token_path(provider_id))
        ]
        def relay_can_read(path: Path) -> bool:
            try:
                info = path.lstat()
                return stat.S_ISREG(info.st_mode) and info.st_uid == relay_uid and stat.S_IMODE(info.st_mode) == 0o600
            except OSError:
                return False
        xiaoke_can_read_configured_tokens = shared_token_root_ready and all(relay_can_read(path) for path in configured_canonical)
        return {
            "bridge_installed": bridge_installed,
            "codex_registered": codex_registered,
            "xiaoke_template_registered": xia_template_registered,
            "xiaoke_active_registered": xia_active_registered,
            # Token writes are dynamic because the bridge rereads its token
            # file per JSON-RPC request; discovery changes require a new turn.
            "shared_token_root_ready": shared_token_root_ready,
            "xiaoke_can_read_configured_tokens": xiaoke_can_read_configured_tokens,
            "activation": "ready" if bridge_installed and codex_registered and xia_active_registered and shared_token_root_ready and xiaoke_can_read_configured_tokens else "pending_activation",
            "migration_pending": any(
                not self._read_token(self._token_path(provider_id))
                and bool(self._legacy_token_path(provider_id))
                and bool(self._read_token(self._legacy_token_path(provider_id)))
                for provider_id in PROVIDERS
            ),
        }

    def _test_provider(self, provider_id: str, token: str) -> tuple[str, str]:
        if not token:
            return "failed", "未配置令牌"
        endpoint = PROVIDERS[provider_id]["endpoint"]
        payload = json.dumps({"jsonrpc": "2.0", "id": "cc-mcp-check", "method": "tools/list"}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
                "User-Agent": "CcCompanion-MCP-Check/1",
            },
        )
        try:
            # Never follow a redirect with a bearer credential, even if an
            # official endpoint is misconfigured upstream later.
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=MCP_TEST_TIMEOUT_SECONDS) as response:
                raw = response.read(MCP_TEST_RESPONSE_LIMIT + 1)
                if len(raw) > MCP_TEST_RESPONSE_LIMIT:
                    return "failed", "服务响应过大"
                return self._parse_test_response(raw, response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                return "failed", "令牌被服务拒绝"
            return "failed", "服务暂时不可用"
        except (urllib.error.URLError, TimeoutError, OSError):
            return "failed", "连接超时或不可达"

    @staticmethod
    def _parse_test_response(raw: bytes, content_type: str) -> tuple[str, str]:
        text = raw.decode("utf-8", errors="replace").strip()
        candidates = [text]
        if "text/event-stream" in content_type.lower():
            candidates = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        for candidate in candidates:
            try:
                message = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("result"), dict):
                tools = message["result"].get("tools")
                if isinstance(tools, list):
                    return "connected", "连接成功"
        return "failed", "服务未返回有效的工具清单"
