"""Call screen-share mailbox: lets 小克 grab one frame of the phone screen.

Three processes cooperate through small JSON files under
``state/screen-capture/`` (no sockets, nothing reachable from outside):

1. ``call-screenshot`` (CLI, :mod:`call_screenshot`) drops ``requests/<id>.json``
   and waits for ``results/<id>.json``.
2. ``voice_call_ws.py`` polls ``requests/``.  If a live call advertised the
   ``screen_capture_v1`` capability *and* reports an active MediaProjection
   grant (``screen_share_state`` frame) it writes ``pending/<id>.json`` and sends
   ``{"type": "screen_capture_request", "request_id": id}`` down the call
   socket; otherwise it answers ``no_active_call`` / ``not_authorized`` at once.
3. The App captures one frame and uploads it to push.py
   ``POST /voice-call/screen-capture?request_id=<id>``.  push.py only accepts
   ids with a fresh ``pending`` marker (one shot), stores the JPEG in the
   attachments dir and writes the result with the absolute path.

Old clients never advertise the capability, so they never see the frame.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

CAPABILITY = "screen_capture_v1"
DEFAULT_ROOT = Path(__file__).resolve().parent / "state" / "screen-capture"
REQUEST_TTL_SEC = 15.0
PENDING_TTL_SEC = 20.0
RESULT_TTL_SEC = 120.0
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

ERROR_MESSAGES = {
    "no_active_call": "现在没有进行中的通话（或 App 版本太旧，不支持屏幕共享）",
    "not_authorized": "通话中但她没有开屏幕共享授权",
    "timeout": "等了太久没收到截图（手机可能锁屏/切后台被限制/网络慢）",
    "capture_failed": "手机端抓屏失败",
    "upload_failed": "手机端上传截图失败",
    "send_failed": "抓屏指令没发到手机",
}


def is_valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


class Mailbox:
    def __init__(
        self,
        root: Path | str = DEFAULT_ROOT,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self._clock = clock

    def _dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write(self, directory: str, request_id: str, payload: dict[str, Any]) -> None:
        target_dir = self._dir(directory)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=target_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, target_dir / f"{request_id}.json")
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _read(self, directory: str, request_id: str) -> Optional[dict[str, Any]]:
        try:
            data = json.loads((self.root / directory / f"{request_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _remove(self, directory: str, request_id: str) -> bool:
        try:
            (self.root / directory / f"{request_id}.json").unlink()
            return True
        except FileNotFoundError:
            return False

    # -- CLI side ------------------------------------------------------------

    def new_request(self) -> str:
        request_id = uuid.uuid4().hex
        self._write("requests", request_id, {"id": request_id, "created_at": self._clock()})
        return request_id

    def poll_result(self, request_id: str) -> Optional[dict[str, Any]]:
        result = self._read("results", request_id)
        if result is not None:
            self._remove("results", request_id)
        return result

    def abandon(self, request_id: str) -> None:
        self._remove("requests", request_id)
        self._remove("pending", request_id)

    # -- voice WS side -------------------------------------------------------

    def take_requests(self) -> list[str]:
        """Consume queued requests (oldest first), dropping expired ones."""
        directory = self._dir("requests")
        now = self._clock()
        taken: list[tuple[float, str]] = []
        for path in directory.glob("*.json"):
            request_id = path.stem
            if not is_valid_request_id(request_id):
                continue
            data = self._read("requests", request_id) or {}
            created = data.get("created_at")
            self._remove("requests", request_id)
            if not isinstance(created, (int, float)) or now - created > REQUEST_TTL_SEC:
                continue
            taken.append((float(created), request_id))
        return [rid for _, rid in sorted(taken)]

    def mark_pending(self, request_id: str) -> None:
        self._write("pending", request_id, {"id": request_id, "sent_at": self._clock()})

    # -- push.py upload side -------------------------------------------------

    def claim_pending(self, request_id: str) -> bool:
        """One-shot: True only for a fresh, not yet used pending marker."""
        if not is_valid_request_id(request_id):
            return False
        data = self._read("pending", request_id)
        if data is None or not self._remove("pending", request_id):
            return False
        sent = data.get("sent_at")
        return isinstance(sent, (int, float)) and self._clock() - sent <= PENDING_TTL_SEC

    # -- shared --------------------------------------------------------------

    def write_result(self, request_id: str, payload: dict[str, Any]) -> None:
        if not is_valid_request_id(request_id):
            return
        self._write("results", request_id, {**payload, "at": self._clock()})

    def write_error(self, request_id: str, code: str, detail: str = "") -> None:
        payload: dict[str, Any] = {"ok": False, "error": code}
        if detail:
            payload["detail"] = detail[:200]
        self.write_result(request_id, payload)

    def sweep(self) -> None:
        """Drop stale pending markers/results nobody will ever read."""
        now = self._clock()
        for name, ttl, key in (("pending", PENDING_TTL_SEC, "sent_at"), ("results", RESULT_TTL_SEC, "at")):
            for path in self._dir(name).glob("*.json"):
                data = self._read(name, path.stem) or {}
                stamp = data.get(key)
                if not isinstance(stamp, (int, float)) or now - stamp > ttl:
                    try:
                        path.unlink()
                    except OSError:
                        pass
