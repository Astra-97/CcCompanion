"""Per-App model selection for the CcCompanion Kiro contact (kiro 切模型 2026-09-10).

Mirrors kimi_preferences.py but model-only: Kiro's ACP exposes
``session/set_model`` and a dynamic ``availableModels`` catalog instead of a
local config allowlist.  The catalog is owned by ``KiroACPClient`` (captured
from session/new|session/load, cached on disk); this store treats it as the
closed world: an Android caller can only pick an id Kiro itself offered.
The selection persists across restarts and is re-pinned on every ACP
prepare, because Kiro does not persist set_model across session/load.
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


class KiroPreferenceError(ValueError):
    pass


class KiroPreferencePersistenceError(RuntimeError):
    pass


class KiroPreferenceStore:
    """Thread-safe atomic 0600 model store validated against a live catalog."""

    MAX_BYTES = 16 * 1024

    def __init__(
        self,
        path: str | Path,
        *,
        catalog_loader: Callable[[], tuple[str, ...]] | None = None,
        default_model: str = KIRO_APP_DEFAULT_MODEL,
    ) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._catalog_loader = catalog_loader
        self._default_model = default_model
        self._selection = default_model
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

    def snapshot(self) -> str:
        with self._lock:
            return self._selection

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

    def save_validated(self, model: Any) -> str:
        selection = self.validate(model)
        with self._lock:
            self._persist(selection)
            self._selection = selection
            return selection

    def _load(self) -> str | None:
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
            return selected
        except (json.JSONDecodeError, OSError, UnicodeError):
            return None

    def _persist(self, selection: str) -> None:
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
                    "model": selection,
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
