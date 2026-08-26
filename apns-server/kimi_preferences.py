"""Private, closed-world preferences for the CcCompanion Kimi contact.

The Kimi Code global configuration is intentionally never edited by the App.
This file stores only the per-App selection which is then pinned on each ACP
session.  Values are constrained to aliases configured locally *and* to a
small audited allow-list; an Android caller can therefore not turn this into a
generic provider/model selector.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Any


KIMI_APP_MODELS = (
    "kimi-code/k3-256k",
    "kimi-code/k3",
    "kimi-code/kimi-for-coding",
    "kimi-code/kimi-for-coding-highspeed",
    "deepseek/v4-pro",
    "deepseek/v4-flash",
    "zhipu/glm-5.3",
    "zhipu/glm-5.3-flash",
)
KIMI_APP_EFFORTS = ("low", "high", "max")
KIMI_APP_DEFAULT_MODEL = "kimi-code/k3-256k"
KIMI_APP_DEFAULT_EFFORT = "high"
# These allowlisted aliases declare no support_efforts in the local Kimi
# config; the engine hard-fails any thinking effort submitted for them
# (v4-pro thinking is a boolean, v4-flash has no thinking at all).  The App
# picker still stores an effort value, but it is never forwarded for these.
KIMI_APP_EFFORTLESS_MODELS = (
    "deepseek/v4-pro",
    "deepseek/v4-flash",
)


def effective_kimi_effort(model: str, reasoning_effort: str) -> str:
    """Return the effort that may actually leave this process for ``model``.

    An empty string means "do not send a thinking field": kimi_web_client
    already omits blank values on both session create and prompt submit.
    """
    if str(model or "").strip() in KIMI_APP_EFFORTLESS_MODELS:
        return ""
    return str(reasoning_effort or "").strip().lower()


class KimiPreferenceError(ValueError):
    pass


class KimiPreferencePersistenceError(RuntimeError):
    pass


def configured_kimi_models(config_path: str | Path) -> tuple[str, ...]:
    """Return only audited aliases declared in the local Kimi config.

    A missing or malformed local config deliberately produces an empty list:
    callers then cannot select a model merely by naming it in an HTTP body.
    """
    try:
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:
            import tomli as tomllib  # Python 3.10 production compatibility

        path = Path(config_path).expanduser()
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > 512 * 1024:
            return ()
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, dict):
            return ()
        return tuple(model for model in KIMI_APP_MODELS if model in models)
    except Exception:
        return ()


class KimiPreferenceStore:
    """Thread-safe atomic 0600 preference store with a fixed allow-list."""

    MAX_BYTES = 16 * 1024

    def __init__(
        self,
        path: str | Path,
        *,
        config_path: str | Path = "/root/.kimi-code/config.toml",
        allowed_models: tuple[str, ...] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self.allowed_models = tuple(allowed_models) if allowed_models is not None else configured_kimi_models(config_path)
        if not self.allowed_models:
            # No selection is safe until the local Kimi config supplies one.
            # Retain the App default in memory so status remains well-formed;
            # validation refuses writes and ACP pinning will fail closed.
            self._selection = (KIMI_APP_DEFAULT_MODEL, KIMI_APP_DEFAULT_EFFORT)
            return
        default_model = (
            KIMI_APP_DEFAULT_MODEL
            if KIMI_APP_DEFAULT_MODEL in self.allowed_models
            else self.allowed_models[0]
        )
        self._selection = (default_model, KIMI_APP_DEFAULT_EFFORT)
        loaded = self._load()
        if loaded is not None:
            self._selection = loaded

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._selection

    def payload_models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": model,
                "supported_reasoning_efforts": (
                    [] if model in KIMI_APP_EFFORTLESS_MODELS else list(KIMI_APP_EFFORTS)
                ),
                "default_reasoning_effort": (
                    "" if model in KIMI_APP_EFFORTLESS_MODELS else KIMI_APP_DEFAULT_EFFORT
                ),
            }
            for model in self.allowed_models
        ]

    def validate(self, model: Any, reasoning_effort: Any) -> tuple[str, str]:
        selected_model = str(model or "").strip()
        selected_effort = str(reasoning_effort or "").strip().lower()
        if selected_model not in self.allowed_models:
            raise KimiPreferenceError("model is not in the local Kimi App allow-list")
        if selected_effort not in KIMI_APP_EFFORTS:
            raise KimiPreferenceError("reasoning effort is not supported by the Kimi App")
        return selected_model, selected_effort

    def save_validated(self, model: str, reasoning_effort: str) -> tuple[str, str]:
        selection = self.validate(model, reasoning_effort)
        with self._lock:
            self._persist(selection)
            self._selection = selection
            return selection

    def _load(self) -> tuple[str, str] | None:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > self.MAX_BYTES:
                return None
            if stat.S_IMODE(info.st_mode) != 0o600:
                self.path.chmod(0o600)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                return None
            return self.validate(raw.get("model"), raw.get("reasoning_effort"))
        except (KimiPreferenceError, json.JSONDecodeError, OSError, UnicodeError):
            return None

    def _persist(self, selection: tuple[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
        fd = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temp, flags, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(json.dumps({
                    "version": 1,
                    "model": selection[0],
                    "reasoning_effort": selection[1],
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            try:
                parent_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                pass
        except OSError as exc:
            raise KimiPreferencePersistenceError("unable to persist Kimi preferences") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
