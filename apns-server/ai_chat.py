"""Xia Yizhou relay runtime with authoritative legacy-compatible history."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import shutil
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_session_relay_proxy import (
    AISessionRelayProxy,
    RelayBusyError,
    RelayError,
    RelayRequestUncertain,
    RelayRequestTerminal,
    build_authoritative_handoff,
    normalize_provider,
    validate_request_id,
    validate_loopback_url,
)
from xia_claude_channel import (
    XiaClaudeChannelClient,
    XiaChannelError,
    XiaChannelStale,
    XiaChannelUncertain,
    XiaChannelUnavailable,
    validate_channel_url,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG: dict[str, Any] = {
    "nickname": "夏以昼",
    "contact_id": "ai-custom",
    # chat_only relies on a restricted relay deployment. Full autonomous mode
    # has a separate explicit opt-in and is never implied by enabling relay.
    "relay_enabled": True,
    "relay_execution_mode": "chat_only",
    "relay_autonomous_tools_opt_in": False,
    "relay_url": "http://127.0.0.1:8900",
    "relay_models": {"claude": "", "codex": ""},
    # Rollout/rollback switch. Production remains on the existing -p relay
    # until the isolated channel service has passed its operator preflight.
    "claude_transport": "relay",
    "claude_channel_url": "http://127.0.0.1:8821",
    "claude_channel_token_file": "/var/lib/cc-xia-relay/channel-state/channel.token",
    "claude_channel_timeout_seconds": 900,
}


class AIChatManager:
    """Thread-safe relay manager; old ai_chat_history.jsonl remains authoritative."""

    def __init__(self, state_dir: str | Path):
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        # Deliberately separate from the retired private legacy config.
        # ai_chat_config.json is left untouched as private legacy state.
        self._config_path = self._state_dir / "ai_relay_config.json"
        self._history_path = self._state_dir / "ai_chat_history.jsonl"
        self._request_state_path = self._state_dir / "ai_relay_request_state.json"
        self._channel_route_state_path = self._state_dir / "ai_channel_route_state.json"
        self._channel_client_id_path = self._state_dir / "ai_channel_client_id"
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._route_lock = threading.RLock()
        self._config: dict[str, Any] = json.loads(json.dumps(_DEFAULT_CONFIG))
        self._relay = AISessionRelayProxy(self._state_dir)
        self._load_config()
        self._channel = self._make_channel_client()
        self._ensure_channel_route_state()
        self._recover_persona_transaction()

    # ---- config ----

    def _load_config(self) -> None:
        if self._config_path.exists():
            try:
                with self._config_path.open("r", encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    filtered = {k: v for k, v in stored.items() if k in _DEFAULT_CONFIG}
                    self._config.update(filtered)
            except Exception:
                logger.exception("ai_chat: failed to load config, using defaults")

    def _save_config(self) -> None:
        tmp = self._config_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._config_path)
        try:
            os.chmod(self._config_path, 0o600)
        except OSError:
            pass
        self._fsync_dir(self._state_dir)

    def relay_config_snapshot(self) -> dict[str, Any]:
        return dict(self._config)

    def configure_relay(self, partial: dict[str, Any]) -> dict[str, Any]:
        """Operator-only relay config helper; there is no remote config endpoint."""
        with self._route_lock:
            if self._send_lock.locked() or self._relay.turn_active:
                raise RelayBusyError("cannot change relay configuration while a turn is active")
            original = json.loads(json.dumps(self._config))
            candidate = json.loads(json.dumps(self._config))
            for k, v in partial.items():
                if k not in _DEFAULT_CONFIG:
                    continue
                if k == "nickname":
                    candidate[k] = str(v)[:100]
                elif k == "contact_id":
                    candidate[k] = re.sub(r"[^a-z0-9_-]", "", str(v).lower())[:50] or "ai-custom"
                elif k in ("relay_enabled", "relay_autonomous_tools_opt_in"):
                    candidate[k] = bool(v)
                elif k == "relay_execution_mode":
                    mode = str(v).strip().lower()
                    if mode not in {"chat_only", "autonomous"}:
                        raise ValueError("relay_execution_mode must be 'chat_only' or 'autonomous'")
                    candidate[k] = mode
                elif k == "relay_url":
                    candidate[k] = validate_loopback_url(v)
                elif k == "claude_transport":
                    transport = str(v).strip().lower()
                    if transport not in {"relay", "channel"}:
                        raise ValueError("claude_transport must be 'relay' or 'channel'")
                    candidate[k] = transport
                elif k == "claude_channel_url":
                    candidate[k] = validate_channel_url(v)
                elif k == "claude_channel_token_file":
                    candidate[k] = str(Path(str(v)).expanduser())
                elif k == "claude_channel_timeout_seconds":
                    candidate[k] = max(1, min(int(v), 1800))
                elif k == "relay_models" and isinstance(v, dict):
                    candidate[k] = {
                        provider: self._validate_model_id(v.get(provider, ""), allow_empty=True)
                        for provider in ("claude", "codex")
                    }
            old_transport = str(original.get("claude_transport") or "relay")
            new_transport = str(candidate.get("claude_transport") or "relay")
            if old_transport != new_transport:
                states = self._load_request_states()
                if any(
                    item.get("channel_lease") and item.get("status") in {"pending", "final_received"}
                    for item in states.values() if isinstance(item, dict)
                ):
                    raise RelayBusyError("cannot change Claude transport with an unresolved channel request")
            replacement_channel = self._make_channel_client(candidate)
            if old_transport != new_transport:
                route = self._load_channel_route_state()
                relay_status = self._relay.status(str(candidate.get("relay_url") or ""))
                fence = max(int(route.get("last_epoch") or 0), int(relay_status.get("epoch") or 0)) + 1
                route["last_epoch"] = fence
                route["relay_epoch"] = int(relay_status.get("epoch") or 0)
                route["needs_handoff"] = True
                # This conservative fence intentionally survives a later
                # config-file write failure; it cannot activate a transport,
                # and only forces a revoke/handoff on the still-active one.
                self._save_channel_route_state(route)
                revoke_client = self._channel if old_transport == "channel" else replacement_channel
                try:
                    revoke_client.revoke(epoch=fence, reason="Claude transport changed")
                except XiaChannelError:
                    logger.warning("ai_chat: transport-change channel revoke deferred")
            with self._lock:
                self._config = candidate
                try:
                    self._save_config()
                except Exception:
                    self._config = original
                    raise
                self._channel = replacement_channel
                if self._relay_ready:
                    self._relay.sync_persona(
                        self._compiled_persona(),
                        str(self._config.get("relay_execution_mode") or "chat_only"),
                    )
        return self.relay_config_snapshot()

    @property
    def enabled(self) -> bool:
        return self._relay_ready

    @property
    def contact_id(self) -> str:
        return str(self._config.get("contact_id") or "ai-custom")

    @property
    def nickname(self) -> str:
        return str(self._config.get("nickname") or "AI")

    @property
    def _relay_ready(self) -> bool:
        if not self._config.get("relay_enabled"):
            return False
        mode = str(self._config.get("relay_execution_mode") or "chat_only")
        return mode == "chat_only" or bool(self._config.get("relay_autonomous_tools_opt_in"))

    @property
    def _claude_uses_channel(self) -> bool:
        return str(self._config.get("claude_transport") or "relay") == "channel"

    def _make_channel_client(self, config: dict[str, Any] | None = None) -> XiaClaudeChannelClient:
        config = config or self._config
        return XiaClaudeChannelClient(
            str(config.get("claude_channel_url") or "http://127.0.0.1:8821"),
            token_file=str(config.get("claude_channel_token_file") or ""),
            timeout_seconds=min(30, int(config.get("claude_channel_timeout_seconds") or 900)),
        )

    def _channel_client_id(self) -> str:
        try:
            value = self._channel_client_id_path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"xia-backend-[a-f0-9]{32}", value):
                return value
        except Exception:
            pass
        value = "xia-backend-" + uuid.uuid4().hex
        self._atomic_private_write(self._channel_client_id_path, (value + "\n").encode("ascii"))
        return value

    def _load_channel_route_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._channel_route_state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {
                    "generation": max(1, int(value.get("generation") or 1)),
                    "stale": bool(value.get("stale", True)),
                    "needs_handoff": bool(value.get("needs_handoff", True)),
                    "last_epoch": max(0, int(value.get("last_epoch") or 0)),
                    "relay_epoch": int(value.get("relay_epoch", -1)),
                }
        except Exception:
            pass
        return {"generation": 1, "stale": True, "needs_handoff": True, "last_epoch": 0, "relay_epoch": -1}

    def _save_channel_route_state(self, state: dict[str, Any]) -> None:
        self._atomic_private_write(
            self._channel_route_state_path,
            json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    def _ensure_channel_route_state(self) -> None:
        if not self._channel_route_state_path.exists():
            self._save_channel_route_state(self._load_channel_route_state())

    def _mark_claude_generation_stale(self) -> None:
        state = self._load_channel_route_state()
        state["generation"] = max(1, int(state.get("generation") or 1)) + 1
        state["stale"] = True
        state["needs_handoff"] = True
        self._save_channel_route_state(state)

    def _ensure_persona_recovered(self) -> None:
        journal = self._persona_dir / ".apply-journal.json"
        if journal.exists():
            self._recover_persona_transaction()
        if journal.exists():
            raise RelayError(
                "persona transaction recovery is pending; relay chat and controls are temporarily unavailable"
            )

    def relay_provider_status(self) -> dict[str, Any]:
        with self._route_lock:
            self._ensure_persona_recovered()
        if not self._config.get("relay_enabled"):
            return {
                "ok": False,
                "mode": "relay",
                "enabled": False,
                "provider": "",
                "turn_active": self._send_lock.locked(),
                "error": "Xia relay is disabled",
            }
        mode = str(self._config.get("relay_execution_mode") or "chat_only")
        if mode == "autonomous" and not self._config.get("relay_autonomous_tools_opt_in"):
            return {
                "ok": False,
                "mode": "relay",
                "enabled": False,
                "provider": "",
                "turn_active": False,
                "execution_mode": mode,
                "error": "autonomous relay mode requires explicit tools opt-in",
            }
        status = self._relay.status(str(self._config.get("relay_url") or ""))
        status["turn_active"] = bool(status.get("turn_active") or self._send_lock.locked())
        channel_status: dict[str, Any] = {"transport": "relay", "ready": True}
        if self._claude_uses_channel:
            try:
                channel_status = {"transport": "channel", **self._channel.health()}
            except XiaChannelError as exc:
                channel_status = {"transport": "channel", "ready": False, "error": str(exc)}
            # Preserve the existing Android contract: its Claude availability
            # indicator must represent the selected transport, not the unused
            # -p relay CLI sitting beside an unavailable channel.
            status["claude_available"] = bool(channel_status.get("ready"))
        return {
            **status,
            "mode": "relay",
            "enabled": True,
            "execution_mode": mode,
            "current_model": self._selected_relay_model(str(status.get("provider") or "claude")),
            "claude_transport": str(self._config.get("claude_transport") or "relay"),
            "claude_channel": channel_status,
        }

    def switch_relay_provider(self, provider: str) -> dict[str, Any]:
        provider = normalize_provider(provider)
        if not self._relay_ready:
            raise RelayError("Xia relay is not ready")
        # Serialize the decision with turn admission. This makes 409 reliable
        # instead of racing the first bytes of a new chat request.
        with self._route_lock:
            self._ensure_persona_recovered()
            if self._send_lock.locked() or self._relay.turn_active:
                raise RelayBusyError("cannot switch provider while a turn is active")
            if provider == "claude" and self._claude_uses_channel:
                health = self._channel.health()
                if not health.get("ready"):
                    raise RelayError("Xia Claude channel is not ready")
            result = self._relay.switch_provider(
                str(self._config.get("relay_url") or ""), provider
            )
            if result.get("changed"):
                state = self._load_channel_route_state()
                state["needs_handoff"] = True
                state["last_epoch"] = max(int(state.get("last_epoch") or 0), int(result.get("epoch") or 0)) + 1
                state["relay_epoch"] = int(result.get("epoch") or 0)
                self._save_channel_route_state(state)
                if self._claude_uses_channel:
                    try:
                        self._channel.revoke(epoch=int(state["last_epoch"]), reason="provider changed")
                    except XiaChannelError:
                        # The next Claude admission repeats this epoch fence.
                        logger.warning("ai_chat: channel revoke deferred until next Claude turn")
        return {
            **result,
            "mode": "relay",
            "enabled": True,
            "execution_mode": str(self._config.get("relay_execution_mode") or "chat_only"),
            "current_model": self._selected_relay_model(provider),
            "claude_transport": str(self._config.get("claude_transport") or "relay"),
        }

    @staticmethod
    def _validate_model_id(value: Any, *, allow_empty: bool = False) -> str:
        model = str(value or "").strip()
        if not model and allow_empty:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            raise ValueError("model must be a simple model id (max 200 characters)")
        return model

    def _selected_relay_model(self, provider: str) -> str:
        models = self._config.get("relay_models")
        return str(models.get(provider, "")) if isinstance(models, dict) else ""

    def relay_model_status(self, provider: str | None = None) -> dict[str, Any]:
        with self._route_lock:
            self._ensure_persona_recovered()
        if not self._relay_ready:
            raise RelayError("relay is not enabled")
        provider = normalize_provider(provider or self._relay.status(
            str(self._config.get("relay_url") or "")
        ).get("provider"))
        selected = self._selected_relay_model(provider)
        dynamic = self._relay.list_models(str(self._config.get("relay_url") or ""), provider)
        choices = [{"id": "", "label": "CLI 默认", "source": "default"}]
        seen = {""}
        if provider == "claude":
            for alias, label in (
                ("fable", "Fable（CLI 别名）"),
                ("opus", "Opus（CLI 别名）"),
                ("sonnet", "Sonnet（CLI 别名）"),
            ):
                seen.add(alias)
                choices.append({"id": alias, "label": label, "source": "alias"})
        for item in dynamic.get("models", []):
            model_id = str(item.get("id") or "")
            if provider == "claude" and model_id == "default":
                continue
            if model_id and model_id not in seen:
                seen.add(model_id)
                choices.append({**item, "source": "relay"})
        if selected and selected not in seen:
            choices.append({"id": selected, "label": selected, "source": "configured"})
        return {
            "ok": True,
            "provider": provider,
            "current_model": selected,
            "models": choices,
            "dynamic": bool(dynamic.get("dynamic")),
            "custom_allowed": provider == "claude",
            "turn_active": self._send_lock.locked() or self._relay.turn_active,
        }

    def select_relay_model(self, provider: str, model: str) -> dict[str, Any]:
        provider = normalize_provider(provider)
        model = self._validate_model_id(model, allow_empty=True)
        if not self._relay_ready:
            raise RelayError("relay is not enabled")
        with self._route_lock:
            self._ensure_persona_recovered()
            if self._send_lock.locked() or self._relay.turn_active:
                raise RelayBusyError("cannot change model while a turn is active")
            status = self.relay_model_status(provider)
            allowed = {str(item.get("id") or "") for item in status["models"]}
            if provider == "codex" and model not in allowed:
                raise ValueError("Codex model must be selected from the relay model list")
            models = dict(self._config.get("relay_models") or {})
            changed = str(models.get(provider) or "") != model
            if provider == "claude" and changed:
                # Mark stale before the config commit: a failed config write may
                # cause one harmless fresh old-model session, never an old
                # session answering after a successful new-model selection.
                self._mark_claude_generation_stale()
            models[provider] = model
            self._config["relay_models"] = models
            self._save_config()
        return self.relay_model_status(provider)

    @property
    def _persona_dir(self) -> Path:
        return self._state_dir / "ai_persona"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _ensure_private_dir(cls, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    @classmethod
    def _atomic_private_write(cls, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        cls._fsync_dir(path.parent)

    def _remove_tree_durable(self, path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)
            self._fsync_dir(path.parent)

    def _unlink_durable(self, path: Path) -> None:
        if path.exists():
            path.unlink()
            self._fsync_dir(path.parent)

    def _load_persona_manifest(self) -> dict[str, Any]:
        path = self._persona_dir / "current" / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("files"), list):
                return value
        except Exception:
            pass
        return {"files": [], "custom_text": "", "updated_at": ""}

    def _compiled_persona_from_dir(self, root: Path) -> str:
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            return ""
        parts: list[str] = []
        files_dir = root / "files"
        for item in manifest.get("files", []):
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or "")
            name = str(item.get("filename") or "persona")
            if not re.fullmatch(r"[a-f0-9]{32}", file_id):
                continue
            try:
                text = (files_dir / f"{file_id}.txt").read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if text:
                parts.append(f"## Persona file: {name}\n\n{text}")
        custom = str(manifest.get("custom_text") or "").strip()
        if custom:
            parts.append("## Custom persona override (highest priority)\n\n" + custom)
        return "\n\n".join(parts)

    def _compiled_persona(self) -> str:
        return self._compiled_persona_from_dir(self._persona_dir / "current")

    def _recover_persona_transaction(self) -> None:
        journal = self._persona_dir / ".apply-journal.json"
        if not journal.exists():
            return
        try:
            record = json.loads(journal.read_text(encoding="utf-8"))
            txid = str(record.get("transaction_id") or "")
            phase = str(record.get("phase") or "")
            if not re.fullmatch(r"[a-f0-9]{32}", txid):
                raise ValueError("invalid persona transaction id")
            if not re.fullmatch(r"\.stage-[a-f0-9]{32}", str(record.get("stage") or "")):
                raise ValueError("invalid persona stage journal entry")
            if not re.fullmatch(r"\.backup-[a-f0-9]{32}", str(record.get("backup") or "")):
                raise ValueError("invalid persona backup journal entry")
            stage = self._persona_dir / str(record["stage"])
            backup = self._persona_dir / str(record["backup"])
            if stage.parent != self._persona_dir or backup.parent != self._persona_dir:
                raise ValueError("invalid persona journal path")
            current = self._persona_dir / "current"
            current_manifest = self._load_persona_manifest() if current.exists() else {}
            committed = phase in {"refresh_inflight", "refresh_committed", "local_committed"}
            committed = committed or current_manifest.get("transaction_id") == txid
            mode = str(self._config.get("relay_execution_mode") or "chat_only")
            if not committed:
                if not current.exists() and backup.exists():
                    os.replace(backup, current)
                    self._fsync_dir(self._persona_dir)
                self._relay.sync_persona(
                    self._compiled_persona(), mode,
                )
                self._remove_tree_durable(stage)
                self._remove_tree_durable(backup)
                self._unlink_durable(journal)
                return

            # refresh_inflight is intentionally treated as committed: a crash
            # can happen after the relay accepted refresh but before the next
            # journal write. Repeating refresh is safe and ensures a fresh
            # same-provider session even if the request never reached it.
            staged_root = stage if stage.exists() else current
            self._relay.sync_persona(self._compiled_persona_from_dir(staged_root), mode)
            if phase == "refresh_inflight":
                self._relay.refresh_sessions(str(self._config.get("relay_url") or ""))
            if stage.exists():
                if current.exists() and not backup.exists():
                    os.replace(current, backup)
                    self._fsync_dir(self._persona_dir)
                if not current.exists():
                    os.replace(stage, current)
                    self._fsync_dir(self._persona_dir)
            if current.exists():
                self._remove_tree_durable(stage)
                self._remove_tree_durable(backup)
                self._unlink_durable(journal)
        except Exception:
            logger.exception("ai_chat: persona transaction recovery failed")

    def persona_status(self) -> dict[str, Any]:
        manifest = self._load_persona_manifest()
        files = []
        for item in manifest.get("files", []):
            if isinstance(item, dict):
                files.append({
                    "id": str(item.get("id") or ""),
                    "filename": str(item.get("filename") or ""),
                    "size": int(item.get("size") or 0),
                })
        return {
            "ok": True,
            "files": files,
            "custom_text": str(manifest.get("custom_text") or ""),
            "updated_at": str(manifest.get("updated_at") or ""),
            "total_size": sum(item["size"] for item in files)
                + len(str(manifest.get("custom_text") or "").encode("utf-8")),
            "turn_active": self._send_lock.locked() or self._relay.turn_active,
        }

    @staticmethod
    def _validate_persona_text(filename: str, text: str) -> tuple[str, bytes]:
        clean_name = Path(filename).name[:200]
        if Path(clean_name).suffix.lower() not in {".md", ".txt", ".yaml", ".yml"}:
            raise ValueError("persona files must be .md, .txt, .yaml, or .yml")
        # YAML persona files are treated only as UTF-8 plain text. They are
        # never parsed or executed; reject binary control bytes before the
        # unchanged byte-budget and atomic composition path sees the content.
        if any(
            char not in "\t\n\r" and unicodedata.category(char) == "Cc"
            for char in text
        ):
            raise ValueError("persona file contains binary data")
        data = text.encode("utf-8")
        if not data or len(data) > 256 * 1024:
            raise ValueError("each persona file must be 1 byte to 256 KiB")
        return clean_name, data

    def apply_persona_composition(
        self,
        files: Any,
        custom_text: Any,
        *,
        _fault: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(files, list):
            raise ValueError("files must be an ordered list")
        custom = str(custom_text or "")
        custom_bytes = custom.encode("utf-8")
        if len(custom_bytes) > 512 * 1024:
            raise ValueError("custom persona text exceeds 512 KiB")
        current = self._load_persona_manifest()
        current_by_id = {
            str(item.get("id")): item for item in current.get("files", [])
            if isinstance(item, dict) and item.get("id")
        }
        prepared: list[tuple[dict[str, Any], bytes]] = []
        total = len(custom_bytes)
        for raw in files:  # no count cap; byte limits bound resource use
            if not isinstance(raw, dict):
                raise ValueError("each persona file must be an object")
            existing_id = str(raw.get("id") or "")
            if existing_id:
                old = current_by_id.get(existing_id)
                if old is None or not re.fullmatch(r"[a-f0-9]{32}", existing_id):
                    raise ValueError("unknown persona file id")
                source = self._persona_dir / "current" / "files" / f"{existing_id}.txt"
                data = source.read_bytes()
                meta = {"id": existing_id, "filename": str(old.get("filename") or "persona.txt"), "size": len(data)}
            else:
                name, data = self._validate_persona_text(
                    str(raw.get("filename") or "persona.md"), str(raw.get("content") or "")
                )
                meta = {"id": uuid.uuid4().hex, "filename": name, "size": len(data)}
            total += len(data)
            if total > 2 * 1024 * 1024:
                raise ValueError("combined persona text exceeds 2 MiB")
            prepared.append((meta, data))

        with self._route_lock:
            if self._send_lock.locked() or self._relay.turn_active:
                raise RelayBusyError("cannot apply persona while a turn is active")
            self._ensure_persona_recovered()
            self._ensure_private_dir(self._persona_dir)
            txid = uuid.uuid4().hex
            stage = self._persona_dir / f".stage-{txid}"
            backup = self._persona_dir / f".backup-{uuid.uuid4().hex}"
            current_dir = self._persona_dir / "current"
            old_compiled = self._compiled_persona()
            journal = self._persona_dir / ".apply-journal.json"
            mode = str(self._config.get("relay_execution_mode") or "chat_only")
            external_commit = False

            def checkpoint(name: str) -> None:
                if _fault is not None:
                    _fault(name)

            def write_journal(phase: str) -> None:
                self._atomic_private_write(
                    journal,
                    json.dumps({
                        "version": 1,
                        "transaction_id": txid,
                        "phase": phase,
                        "stage": stage.name,
                        "backup": backup.name,
                    }, sort_keys=True).encode("utf-8"),
                )

            def finish_local_commit() -> None:
                current_txid = ""
                if current_dir.exists():
                    try:
                        current_txid = str(json.loads(
                            (current_dir / "manifest.json").read_text(encoding="utf-8")
                        ).get("transaction_id") or "")
                    except Exception:
                        pass
                if current_txid != txid and stage.exists():
                    if current_dir.exists() and not backup.exists():
                        os.replace(current_dir, backup)
                        self._fsync_dir(self._persona_dir)
                    if not current_dir.exists():
                        os.replace(stage, current_dir)
                        self._fsync_dir(self._persona_dir)
                write_journal("local_committed")

            try:
                (stage / "files").mkdir(parents=True)
                os.chmod(stage, 0o700)
                os.chmod(stage / "files", 0o700)
                self._fsync_dir(self._persona_dir)
                manifest_files = []
                for meta, data in prepared:
                    self._atomic_private_write(stage / "files" / f"{meta['id']}.txt", data)
                    manifest_files.append(meta)
                manifest = {
                    "files": manifest_files,
                    "custom_text": custom,
                    "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "transaction_id": txid,
                }
                self._atomic_private_write(
                    stage / "manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                )
                self._fsync_dir(stage / "files")
                self._fsync_dir(stage)
                write_journal("prepared")
                checkpoint("after_journal")
                compiled_parts = [f"## Persona file: {meta['filename']}\n\n{data.decode('utf-8').strip()}" for meta, data in prepared]
                if custom.strip():
                    compiled_parts.append("## Custom persona override (highest priority)\n\n" + custom.strip())
                compiled = "\n\n".join(compiled_parts)
                self._relay.sync_persona(compiled, mode)
                write_journal("workspace_synced")
                checkpoint("after_workspace_sync")

                # A long-lived Claude TUI cannot reliably hot-reload its
                # project persona. Invalidate it before the persona refresh
                # commit; rollback may cause one harmless fresh old-persona
                # session, but success can never leave the old session live.
                write_journal("channel_stale_inflight")
                self._mark_claude_generation_stale()
                write_journal("channel_stale_committed")

                # The relay refresh is the external commit boundary. The
                # active manifest still points at the old persona here.
                write_journal("refresh_inflight")
                self._relay.refresh_sessions(str(self._config.get("relay_url") or ""))
                external_commit = True
                write_journal("refresh_committed")
                checkpoint("after_refresh_commit")

                if current_dir.exists():
                    os.replace(current_dir, backup)
                    self._fsync_dir(self._persona_dir)
                checkpoint("after_backup_rename")
                os.replace(stage, current_dir)
                self._fsync_dir(self._persona_dir)
                write_journal("local_committed")
            except Exception:
                if external_commit:
                    # Never claim failure or restore the old persona after the
                    # relay has discarded its old sessions. Complete the local
                    # pointer swap and leave a recoverable journal on failure.
                    logger.exception("ai_chat: post-refresh persona commit repair")
                    finish_local_commit()
                else:
                    if not current_dir.exists() and backup.exists():
                        os.replace(backup, current_dir)
                        self._fsync_dir(self._persona_dir)
                    self._remove_tree_durable(stage)
                    self._remove_tree_durable(backup)
                    self._relay.sync_persona(old_compiled, mode)
                    self._unlink_durable(journal)
                    raise

            # Cleanup is post-commit maintenance. Any failure leaves a journal
            # for startup recovery but must not turn a successful apply into a
            # reported failure or roll the relay workspace back.
            backup_clean = False
            try:
                checkpoint("before_backup_cleanup")
                self._remove_tree_durable(backup)
                backup_clean = True
            except Exception:
                logger.exception("ai_chat: persona backup cleanup deferred")
            if backup_clean:
                try:
                    checkpoint("before_journal_unlink")
                    self._unlink_durable(journal)
                except Exception:
                    logger.exception("ai_chat: persona journal cleanup deferred")
        return self.persona_status()

    # ---- history ----

    def _load_request_states(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self._request_state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {
                    str(key): item for key, item in value.items()
                    if isinstance(item, dict)
                }
        except Exception:
            pass
        return {}

    def _set_request_state(
        self, client_message_id: str, status: str, *, visible_output: bool = False, error: str = "",
        **metadata: Any,
    ) -> None:
        if not client_message_id:
            return
        with self._lock:
            states = self._load_request_states()
            existing = states.get(client_message_id, {})
            states[client_message_id] = {
                **existing,
                "status": status,
                "visible_output": bool(visible_output),
                "error": str(error)[:500],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            for key in ("channel_epoch", "channel_lease", "channel_generation"):
                if key in metadata:
                    states[client_message_id][key] = metadata[key]
            if len(states) > 2000:
                states = dict(list(states.items())[-2000:])
            self._atomic_private_write(
                self._request_state_path,
                json.dumps(states, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )

    def _append_history(self, role: str, text: str, thinking: str = "", **extra: Any) -> str:
        """Append a message to the JSONL history file.  Returns the ISO ts."""
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        rec = {
            "ts": ts,
            "role": role,
            "text": text,
            "contact_id": self.contact_id,
        }
        if thinking:
            rec["thinking"] = thinking
        for key in (
            "client_message_id",
            "attachment_url",
            "attachment_type",
            "attachment_filename",
            "image",
            "files",
            "provider",
            "tools",
        ):
            value = extra.get(key)
            if value:
                rec[key] = value
        with self._lock:
            with self._history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return ts

    def _find_client_message_result(self, client_message_id: str) -> dict[str, Any] | None:
        if not client_message_id:
            return None
        records = self.read_history(limit=1000)
        user_index = -1
        for i, rec in enumerate(records):
            if rec.get("role") == "user" and rec.get("client_message_id") == client_message_id:
                user_index = i
        if user_index < 0:
            return None
        for rec in records[user_index + 1:]:
            if rec.get("role") == "assistant" and rec.get("client_message_id") == client_message_id:
                result = {
                    "ok": True,
                    "duplicate": True,
                    "reply": rec.get("text", ""),
                    "ts": rec.get("ts", ""),
                    "provider": "claude" if rec.get("provider") == "claude-channel" else rec.get("provider", ""),
                    "transport": "channel" if rec.get("provider") == "claude-channel" else "relay",
                }
                if rec.get("thinking"):
                    result["thinking"] = rec.get("thinking", "")
                return result
        state = self._load_request_states().get(client_message_id, {})
        status = str(state.get("status") or "")
        visible = bool(state.get("visible_output"))
        if status == "final_received" and state.get("channel_lease"):
            # Channel results are durable outside this process. A backend
            # crash after result receipt but before assistant-history append
            # must retrieve the cached result with the same exact grant.
            return {"_retry": True, "user_ts": records[user_index].get("ts", "")}
        if status in {"pending", "failed"} and not visible:
            # The send lock guarantees no active request can reach here. A
            # pending record is therefore stale after a process interruption.
            # Retrying is best-effort UX, not proof that the isolated engine
            # never accepted the earlier request.
            return {"_retry": True, "user_ts": records[user_index].get("ts", "")}
        if status == "failed" or visible:
            return {
                "ok": False,
                "duplicate": True,
                "terminal": True,
                "retryable": False,
                "error": str(state.get("error") or "previous relay attempt failed after visible output"),
            }
        return {"_retry": True, "user_ts": records[user_index].get("ts", "")}

    def read_history(self, since: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Return history records, optionally filtered by *since* timestamp."""
        if not self._history_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._lock:
            with self._history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = rec.get("ts", "")
                    if since and ts <= since:
                        continue
                    out.append(rec)
        limit = max(1, min(int(limit), 10000))
        return out[-limit:]

    # ---- relay calls ----

    def send_message(self, user_text: str, client_message_id: str = "") -> dict[str, Any]:
        """Send *user_text*, call the AI API, store both sides, return result dict.
        Serialized per-session to prevent interleaving."""
        if not self._relay_ready:
            return {"ok": False, "error": "Xia relay is not ready"}

        with self._route_lock:
            try:
                self._ensure_persona_recovered()
            except RelayError as exc:
                return {"ok": False, "error": str(exc), "retryable": True}
            self._send_lock.acquire()
        try:
            return self._send_message_locked(user_text, client_message_id=client_message_id)
        finally:
            self._send_lock.release()

    def send_attachment(
        self,
        user_text: str,
        attachment_url: str,
        attachment_type: str,
        attachment_filename: str,
        local_path: str,
    ) -> dict[str, Any]:
        """Reject new files until the restricted relay has a safe byte bridge."""
        return {
            "ok": False,
            "unsupported": True,
            "error": "夏以昼的隔离会话目前只支持文字；新图片和文件尚未接入安全附件桥",
        }

    def _send_message_locked(self, user_text: str, client_message_id: str = "") -> dict[str, Any]:
        return self._send_message_relay_locked(
            user_text, lambda _event: None, client_message_id=client_message_id
        )

    def send_message_stream(self, text: str, emit: Any, client_message_id: str = "") -> dict[str, Any]:
        """Send a message and emit newline-JSON stream events while the reply arrives."""
        text = text.strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        if not self._relay_ready:
            return {"ok": False, "error": "Xia relay is not ready"}
        with self._route_lock:
            try:
                self._ensure_persona_recovered()
            except RelayError as exc:
                return {"ok": False, "error": str(exc), "retryable": True}
            self._send_lock.acquire()
        try:
            return self._send_message_stream_locked(text, emit, client_message_id=client_message_id)
        finally:
            self._send_lock.release()

    def _send_message_stream_locked(self, user_text: str, emit: Any, client_message_id: str = "") -> dict[str, Any]:
        return self._send_message_relay_locked(
            user_text, emit, client_message_id=client_message_id
        )

    def _send_message_relay_locked(
        self,
        user_text: str,
        emit: Any,
        *,
        client_message_id: str = "",
        history_user_text: str | None = None,
        history_user_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drive the isolated relay while retaining local history authority."""
        if not client_message_id:
            client_message_id = "server_" + uuid.uuid4().hex
        client_message_id = validate_request_id(client_message_id)
        duplicate = self._find_client_message_result(client_message_id)
        retry_existing = bool(duplicate and duplicate.get("_retry"))
        if duplicate is not None and not retry_existing:
            if duplicate.get("ok") and duplicate.get("reply") and duplicate.get("transport") != "channel":
                emit({"type": "delta", "text": duplicate.get("reply", "")})
                if duplicate.get("thinking"):
                    emit({"type": "thinking_delta", "text": duplicate.get("thinking", "")})
            return duplicate

        prior_history = self.read_history(limit=200)
        if retry_existing and client_message_id:
            prior_history = [
                record for record in prior_history
                if not (
                    record.get("role") == "user"
                    and record.get("client_message_id") == client_message_id
                )
            ]
        handoff = build_authoritative_handoff(prior_history)
        execution_mode = str(self._config.get("relay_execution_mode") or "chat_only")
        self._relay.sync_persona(
            self._compiled_persona(), execution_mode
        )
        relay_url = str(self._config.get("relay_url") or "")
        status = self._relay.status(relay_url)
        provider = normalize_provider(status.get("provider"))
        selected_model = self._selected_relay_model(provider)

        self._set_request_state(client_message_id, "pending", visible_output=False)
        if retry_existing:
            user_ts = str(duplicate.get("user_ts") or "")
        else:
            user_ts = self._append_history(
                "user",
                history_user_text if history_user_text is not None else user_text,
                client_message_id=client_message_id,
                **(history_user_extra or {}),
            )
            emit({
                "type": "user",
                "ts": user_ts,
                "text": history_user_text if history_user_text is not None else user_text,
            })
        activities_by_id: dict[str, dict[str, Any]] = {}
        visible_output = False

        if provider == "claude" and self._claude_uses_channel:
            return self._send_message_channel_admitted(
                user_text=user_text,
                client_message_id=client_message_id,
                user_ts=user_ts,
                handoff=handoff,
                selected_model=selected_model,
                retry_existing=retry_existing,
                emit=emit,
            )

        def relay_emit(event: dict[str, Any]) -> None:
            nonlocal visible_output
            if not visible_output and event.get("type") in {"delta", "thinking_delta", "activity"}:
                visible_output = True
                self._set_request_state(client_message_id, "pending", visible_output=True)
            activity = event.get("activity")
            if isinstance(activity, dict):
                activity_id = str(activity.get("id") or "")
                if activity_id:
                    activities_by_id[activity_id] = {**activities_by_id.get(activity_id, {}), **activity}
            emit(event)

        try:
            final = self._relay.stream_turn(
                relay_url,
                provider=provider,
                text=user_text,
                handoff=handoff,
                execution_mode=execution_mode,
                model=selected_model,
                request_id=client_message_id,
                emit=relay_emit,
            )
            # Close the replay window as soon as the proxy returns an
            # authoritative done. If the process dies before history append,
            # the same client ID becomes terminal instead of replaying a
            # model turn whose upstream side effects are unknowable.
            visible_output = True
            self._set_request_state(
                client_message_id,
                "final_received",
                visible_output=True,
                error="relay final received but history commit is incomplete",
            )
        except RelayRequestUncertain as exc:
            logger.warning("ai_chat: relay request is terminal-ambiguous id=%s", client_message_id[:32])
            message = "上一轮已被隔离会话接收，但完成状态不确定；请把内容作为一条新消息发送"
            self._set_request_state(
                client_message_id, "failed", visible_output=True, error=message
            )
            return {
                "ok": False,
                "error": message,
                "code": exc.code,
                "ts": user_ts,
                "terminal": True,
                "retryable": False,
            }
        except RelayRequestTerminal as exc:
            logger.warning("ai_chat: relay request ended without usable final id=%s", client_message_id[:32])
            message = "上一轮已经结束，但没有可恢复的完整回复；请把内容作为一条新消息发送"
            self._set_request_state(
                client_message_id, "failed", visible_output=True, error=message
            )
            return {
                "ok": False,
                "error": message,
                "code": exc.code,
                "ts": user_ts,
                "terminal": True,
                "retryable": False,
            }
        except Exception as exc:
            logger.exception("ai_chat: relay stream failed")
            self._set_request_state(
                client_message_id, "failed", visible_output=visible_output, error=str(exc)
            )
            return {
                "ok": False,
                "error": str(exc),
                "ts": user_ts,
                "terminal": visible_output,
                "retryable": not visible_output,
            }

        reply_text = str(final.get("reply") or "").strip()
        thinking = str(final.get("thinking") or "")
        final_activities = final.get("activities")
        if isinstance(final_activities, list):
            for activity in final_activities:
                if isinstance(activity, dict) and activity.get("id"):
                    activity = dict(activity)
                    activity.setdefault(
                        "name", str(activity.get("title") or activity.get("kind") or "tool")
                    )
                    activity_id = str(activity["id"])
                    activities_by_id[activity_id] = {
                        **activities_by_id.get(activity_id, {}), **activity
                    }
        if not reply_text:
            error = str(final.get("error") or "relay returned an empty final reply")
            self._set_request_state(
                client_message_id, "failed", visible_output=visible_output, error=error
            )
            return {
                "ok": False,
                "error": error,
                "ts": user_ts,
                "terminal": visible_output,
                "retryable": not visible_output,
            }
        tools = json.dumps(list(activities_by_id.values()), ensure_ascii=False) if activities_by_id else ""
        try:
            reply_ts = self._append_history(
                "assistant",
                reply_text,
                thinking=thinking,
                client_message_id=client_message_id,
                provider=provider,
                tools=tools,
            )
        except Exception as exc:
            logger.exception("ai_chat: relay final history commit failed")
            self._set_request_state(
                client_message_id, "failed", visible_output=True, error=str(exc)
            )
            return {
                "ok": False,
                "error": "relay final was received but could not be committed to history",
                "ts": user_ts,
                "terminal": True,
                "retryable": False,
            }
        result = {
            "ok": True,
            "reply": reply_text,
            "ts": reply_ts,
            "provider": provider,
            "activities": list(activities_by_id.values()),
        }
        if thinking:
            result["thinking"] = thinking
        if final.get("error"):
            result["warning"] = str(final["error"])
        self._set_request_state(client_message_id, "completed", visible_output=True)
        return result

    def _send_message_channel_admitted(
        self,
        *,
        user_text: str,
        client_message_id: str,
        user_ts: str,
        handoff: str,
        selected_model: str,
        retry_existing: bool,
        emit: Any,
    ) -> dict[str, Any]:
        """Wait for one durable channel result; intentionally emit no deltas."""
        route = self._load_channel_route_state()
        generation = max(1, int(route.get("generation") or 1))
        states = self._load_request_states()
        previous = states.get(client_message_id, {}) if retry_existing else {}
        lease = str(previous.get("channel_lease") or uuid.uuid4().hex)
        epoch = int(previous.get("channel_epoch") or 0)
        saved_relay_epoch = int(route.get("relay_epoch", -1))
        current_relay_epoch = saved_relay_epoch
        if not epoch:
            status = self._relay.status(str(self._config.get("relay_url") or ""))
            current_relay_epoch = int(status.get("epoch") or 0)
            epoch = max(0, current_relay_epoch, int(route.get("last_epoch") or 0))
        previous_generation = int(previous.get("channel_generation") or generation)
        if retry_existing and previous_generation != generation:
            message = "Claude 会话世代已更新，上一轮结果无法安全重放；请作为新消息发送"
            self._set_request_state(client_message_id, "failed", visible_output=True, error=message)
            return {"ok": False, "error": message, "code": "request_uncertain", "terminal": True, "retryable": False, "ts": user_ts}

        self._set_request_state(
            client_message_id, "pending", visible_output=False,
            channel_epoch=epoch, channel_lease=lease, channel_generation=generation,
        )
        try:
            # Repeating the fence on every Claude admission safely catches a
            # provider switch that happened while the channel was offline.
            self._channel.revoke(epoch=epoch, reason="Claude turn admission")
            ready = self._channel.ensure_generation(
                generation=generation,
                model=selected_model,
                timeout_seconds=min(180, int(self._config.get("claude_channel_timeout_seconds") or 900)),
                on_wait=lambda: emit({"type": "keepalive"}),
            )
            include_handoff = bool(
                route.get("needs_handoff") or route.get("stale") or ready.get("fresh")
                or current_relay_epoch != saved_relay_epoch
            )

            def mark_admitted() -> None:
                # Consume the one-shot handoff at durable channel admission,
                # not after local history append. A backend crash can retrieve
                # this request without injecting the same history into the
                # already-running native session a second time.
                route["stale"] = False
                route["needs_handoff"] = False
                route["last_epoch"] = epoch
                route["relay_epoch"] = current_relay_epoch
                self._save_channel_route_state(route)

            final = self._channel.send_and_wait(
                request_id=client_message_id,
                client_id=self._channel_client_id(),
                epoch=epoch,
                lease=lease,
                generation=generation,
                text=user_text,
                handoff=handoff if include_handoff else "",
                timeout_seconds=int(self._config.get("claude_channel_timeout_seconds") or 900),
                on_admitted=mark_admitted,
                on_wait=lambda: emit({"type": "keepalive"}),
            )
            reply_text = str(final.get("reply") or "").strip()
            if not reply_text:
                raise XiaChannelUncertain("channel completed without a usable final reply")
            # The channel ledger is now authoritative for this request. Close
            # the replay window before local history append.
            self._set_request_state(
                client_message_id, "final_received", visible_output=True,
                error="channel final received but history commit is incomplete",
            )
            reply_ts = self._append_history(
                "assistant", reply_text, client_message_id=client_message_id,
                provider="claude-channel",
            )
            self._set_request_state(client_message_id, "completed", visible_output=True)
            return {
                "ok": True, "reply": reply_text, "ts": reply_ts,
                "provider": "claude", "activities": [],
            }
        except (XiaChannelUncertain, XiaChannelStale) as exc:
            self._mark_claude_generation_stale()
            message = "Claude 长期会话已接收这一轮，但完成状态无法确认；请作为一条新消息发送"
            self._set_request_state(client_message_id, "failed", visible_output=True, error=message)
            return {
                "ok": False, "error": message, "code": getattr(exc, "code", "request_uncertain"),
                "ts": user_ts, "terminal": True, "retryable": False,
            }
        except XiaChannelUnavailable as exc:
            self._set_request_state(client_message_id, "failed", visible_output=False, error=str(exc))
            return {"ok": False, "error": str(exc), "ts": user_ts, "terminal": False, "retryable": True}
        except Exception as exc:
            logger.exception("ai_chat: Claude channel turn failed")
            # Once submit could have happened, generic channel failures are
            # terminal. The channel client uses Unavailable only for failures
            # before an HTTP response, so conservatively do not replay here.
            self._set_request_state(client_message_id, "failed", visible_output=True, error=str(exc))
            return {"ok": False, "error": str(exc), "ts": user_ts, "terminal": True, "retryable": False}
