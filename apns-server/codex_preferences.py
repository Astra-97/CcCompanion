"""Validated, private persistence for Kairos Codex model preferences."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any, Iterable


MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class CodexPreferenceError(ValueError):
    """A preference or catalog is malformed or unsupported."""


class CodexPreferencePersistenceError(RuntimeError):
    """The validated selection could not be persisted safely."""


@dataclass(frozen=True)
class CodexModelCapability:
    id: str
    display_name: str
    default_reasoning_effort: str
    supported_reasoning_efforts: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "default_reasoning_effort": self.default_reasoning_effort,
            "supported_reasoning_efforts": list(self.supported_reasoning_efforts),
        }


def _valid_model_id(value: Any) -> str:
    model = str(value or "").strip()
    if not MODEL_ID_RE.fullmatch(model):
        raise CodexPreferenceError("invalid Codex model id")
    return model


def _valid_effort(value: Any) -> str:
    effort = str(value or "").strip().lower()
    if not EFFORT_RE.fullmatch(effort):
        raise CodexPreferenceError("invalid Codex reasoning effort")
    return effort


def parse_codex_model_catalog(payload: Any) -> tuple[CodexModelCapability, ...]:
    """Parse the official ``model/list`` result without inventing fallbacks."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CodexPreferenceError("Codex app-server returned an invalid model catalog")
    models: list[CodexModelCapability] = []
    seen: set[str] = set()
    for raw in payload["data"]:
        if not isinstance(raw, dict) or raw.get("hidden") is True:
            continue
        try:
            model_id = _valid_model_id(raw.get("id") or raw.get("model"))
        except CodexPreferenceError:
            continue
        if model_id in seen:
            continue
        effort_items = raw.get("supportedReasoningEfforts")
        if not isinstance(effort_items, list):
            continue
        efforts: list[str] = []
        for item in effort_items:
            candidate = item.get("reasoningEffort") if isinstance(item, dict) else None
            try:
                effort = _valid_effort(candidate)
            except CodexPreferenceError:
                continue
            if effort not in efforts:
                efforts.append(effort)
        if not efforts:
            continue
        try:
            default_effort = _valid_effort(raw.get("defaultReasoningEffort"))
        except CodexPreferenceError:
            default_effort = ""
        if default_effort not in efforts:
            # Keep the absence explicit; clients can still render the real
            # supported list but must not treat a fabricated default as truth.
            default_effort = ""
        display_name = str(raw.get("displayName") or model_id).strip()[:200] or model_id
        models.append(CodexModelCapability(
            id=model_id,
            display_name=display_name,
            default_reasoning_effort=default_effort,
            supported_reasoning_efforts=tuple(efforts),
        ))
        seen.add(model_id)
    if not models:
        raise CodexPreferenceError("Codex app-server returned no selectable models")
    return tuple(models)


def validate_codex_selection(
    model: Any,
    reasoning_effort: Any,
    catalog: Iterable[CodexModelCapability],
) -> tuple[str, str]:
    model_id = _valid_model_id(model)
    effort = _valid_effort(reasoning_effort)
    selected = next((item for item in catalog if item.id == model_id), None)
    if selected is None:
        raise CodexPreferenceError("model is not present in the Codex app-server catalog")
    if effort not in selected.supported_reasoning_efforts:
        raise CodexPreferenceError("reasoning effort is not supported by the selected model")
    return model_id, effort


class CodexPreferenceStore:
    """Thread-safe versioned JSON store; every successful write is atomic 0600."""

    MAX_BYTES = 64 * 1024

    def __init__(self, path: str | Path, *, default_model: str, default_effort: str) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._selection = (
            _valid_model_id(default_model),
            _valid_effort(default_effort),
        )
        loaded = self._load()
        if loaded is not None:
            self._selection = loaded

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._selection

    def save_validated(self, model: str, effort: str) -> tuple[str, str]:
        candidate = (_valid_model_id(model), _valid_effort(effort))
        with self._lock:
            self._persist(candidate)
            self._selection = candidate
            return candidate

    def _load(self) -> tuple[str, str] | None:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return None
            if info.st_size > self.MAX_BYTES:
                return None
            # Repair an older overly-broad mode before reading its contents.
            if stat.S_IMODE(info.st_mode) != 0o600:
                self.path.chmod(0o600)
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1:
                return None
            return (
                _valid_model_id(value.get("model")),
                _valid_effort(value.get("reasoning_effort")),
            )
        except (CodexPreferenceError, json.JSONDecodeError, OSError, UnicodeError):
            return None

    def _persist(self, selection: tuple[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "version": 1,
            "model": selection[0],
            "reasoning_effort": selection[1],
        }, ensure_ascii=False, separators=(",", ":")) + "\n"
        temp = self.path.with_name(
            f".{self.path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        try:
            fd = os.open(temp, flags, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(payload)
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
            raise CodexPreferencePersistenceError(
                "unable to persist Codex preferences"
            ) from exc
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
