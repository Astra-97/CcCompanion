"""Per-App model + effort selection for the CcCompanion Kiro contact (kiro 切模型 2026-09-10).

Mirrors kimi_preferences.py: Kiro's ACP exposes ``session/set_model`` and a
dynamic ``availableModels`` catalog instead of a local config allowlist.  The
catalog is owned by ``KiroACPClient`` (captured from session/new|session/load,
cached on disk); this store treats it as the closed world: an Android caller
can only pick an id Kiro itself offered.  The selection persists across
restarts and is re-pinned on every ACP prepare, because Kiro does not persist
set_model across session/load.

kiro 推理强度 (2026-09-10): the effort dimension is validated against a closed
five-level set (low/medium/high/xhigh/max).  Kiro's own options list is
model-conditional and empty on every model this account is offered, so it
cannot act as the allowlist; the pin is applied via the
``_kiro.dev/commands/execute`` TuiCommand bridge at every prepare and simply
no-ops on models without effort support.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Any, Callable


KIRO_APP_DEFAULT_MODEL = "auto"
KIRO_APP_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# kiro 推理强度 (2026-09-10): 默认空串 = 不 pin 不重放，跟随 Kiro 自己的每模型
# 默认档位。只有用户显式选择过才持久化并参与 prepare 重放——避免将来 thinking
# 模型上线时在无用户动作的情况下覆写 Kiro 默认。
KIRO_APP_DEFAULT_EFFORT = ""


class KiroPreferenceError(ValueError):
    pass


class KiroPreferencePersistenceError(RuntimeError):
    pass


class KiroPreferenceStore:
    """Thread-safe atomic 0600 model+effort store validated against a live catalog."""

    MAX_BYTES = 16 * 1024

    def __init__(
        self,
        path: str | Path,
        *,
        catalog_loader: Callable[[], tuple[str, ...]] | None = None,
        default_model: str = KIRO_APP_DEFAULT_MODEL,
        default_effort: str = KIRO_APP_DEFAULT_EFFORT,
    ) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._catalog_loader = catalog_loader
        self._default_model = default_model
        self._default_effort = default_effort
        self._selection = (default_model, default_effort)
        loaded = self._load()
        if loaded is not None:
            self._selection = loaded

    def catalog(self) -> tuple[str, ...]:
        loader = self._catalog_loader
        if loader is None:
            return ()
        try:
            return tuple(loader())
        except Exception:
            return ()

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self._selection

    def snapshot_model(self) -> str:
        return self.snapshot()[0]

    def snapshot_effort(self) -> str:
        return self.snapshot()[1]

    def validate(self, model: Any) -> str:
        selected = str(model or "").strip()
        ids = self.catalog()
        if not ids:
            # Fail closed like the Kimi store: without a catalog no selection
            # is safe, because any string would reach session/set_model.
            raise KiroPreferenceError("kiro model catalog is unavailable")
        if selected not in ids:
            raise KiroPreferenceError("model is not in the Kiro available catalog")
        return selected

    def validate_effort(self, effort: Any) -> str:
        selected = str(effort or "").strip().lower()
        if selected not in KIRO_APP_EFFORTS:
            raise KiroPreferenceError("effort is not in the Kiro supported levels")
        return selected

    def save_validated(self, model: Any = None, effort: Any = None) -> tuple[str, str]:
        """Validate and persist; ``None`` keeps the current dimension."""
        with self._lock:
            current_model, current_effort = self._selection
            next_model = self.validate(model) if model is not None else current_model
            next_effort = self.validate_effort(effort) if effort is not None else current_effort
            self._persist((next_model, next_effort))
            self._selection = (next_model, next_effort)
            return self._selection

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
            selected = str(raw.get("model") or "").strip()
            if not selected:
                return None
            ids = self.catalog()
            # Keep a persisted value the catalog cannot confirm yet (process
            # down, cache absent); drop one the live catalog disproves.
            if ids and selected not in ids:
                return None
            # kiro 推理强度 (2026-09-10): a missing/unknown effort (file from
            # before this dimension existed, or hand-edited) falls back to the
            # default instead of discarding the whole record.
            try:
                effort = self.validate_effort(raw.get("effort"))
            except KiroPreferenceError:
                effort = self._default_effort
            return selected, effort
        except (json.JSONDecodeError, OSError, UnicodeError):
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
                    "effort": selection[1],
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
            raise KiroPreferencePersistenceError("unable to persist Kiro preferences") from exc
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
