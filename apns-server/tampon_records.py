"""Private, append-safe tampon wear record domain and JSON store.

This deliberately does not share the generic health-records file: a tampon
wear has an open/close lifecycle and replacement must be one atomic mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SIZES = frozenset({"MINI", "REGULAR", "HEAVY", "SUPER"})
AMOUNTS = frozenset({"LIGHT", "MEDIUM", "HEAVY", "FULL"})
_ACTIONS = frozenset({"start", "replace", "remove"})
_SERVER_OWNED_FIELDS = frozenset({
    "actor", "source", "night", "status", "id", "record_id",
    "created_by", "createdBy", "started_by", "startedBy",
    "closed_by", "closedBy", "sync_state", "syncState",
})


class TamponRecordValidationError(ValueError):
    """The native client supplied an invalid tampon record action."""


class TamponRecordConflictError(ValueError):
    """The client acted on stale open-record state or reused an operation id."""


def _text(value: Any, field: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise TamponRecordValidationError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise TamponRecordValidationError(f"{field} is invalid")
    return result


def _field(body: Mapping[str, Any], snake: str, camel: str | None = None) -> Any:
    if snake in body:
        return body[snake]
    return body.get(camel) if camel else None


def _choice(value: Any, field: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise TamponRecordValidationError(f"{field} is required")
    result = value.strip().upper()
    if result not in choices:
        raise TamponRecordValidationError(f"{field} is invalid")
    return result


def _instant(value: Any, field: str) -> datetime:
    raw = _text(value, field, maximum=80)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TamponRecordValidationError(f"{field} must be an ISO-8601 instant with offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TamponRecordValidationError(f"{field} must include an offset")
    return parsed.astimezone(timezone.utc)


def _at_ms(body: Mapping[str, Any], action: str) -> int:
    """Read frozen ``at_ms`` first, retaining ISO aliases for old callers."""
    raw = _field(body, "at_ms", "atMs")
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise TamponRecordValidationError("at_ms must be a non-negative integer")
        try:
            datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise TamponRecordValidationError("at_ms is out of range") from exc
        return raw
    legacy_key = "started_at" if action == "start" else "ended_at"
    legacy_alias = "startedAt" if action == "start" else "endedAt"
    value = _field(body, legacy_key, legacy_alias)
    if value is None:
        value = _field(body, "occurred_at", "occurredAt")
    return int(_instant(value, legacy_key).timestamp() * 1000)


def _zone(value: Any) -> str:
    zone = _text(value, "zone_id", maximum=80)
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise TamponRecordValidationError("zone_id must be an IANA time zone") from exc
    return zone


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_action(body: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a public action and make its idempotency input canonical.

    ``actor`` and ``source`` are intentionally absent: native callers cannot
    choose a different identity by smuggling those keys into the JSON body.
    """

    prohibited = _SERVER_OWNED_FIELDS.intersection(body)
    if prohibited:
        raise TamponRecordValidationError(f"client must not set {sorted(prohibited)[0]}")
    action = _text(body.get("action"), "action", maximum=20).lower()
    if action not in _ACTIONS:
        raise TamponRecordValidationError("action must be start, replace, or remove")
    operation_id = _text(_field(body, "operation_id", "operationId"), "operation_id")
    result: dict[str, Any] = {
        "action": action,
        "operation_id": operation_id,
        "at_ms": _at_ms(body, action),
        # ``zone_id`` is the device-declared IANA zone at action time. It is
        # retained for audit; the server, not the client, derives night policy.
        "zone_id": _zone(
            _field(body, "zone_id", "zoneId")
            if _field(body, "zone_id", "zoneId") is not None
            else _field(body, "start_zone", "startZone")
        ),
    }
    expected_open_id = _field(body, "expected_open_id", "expectedOpenId")
    if action == "start":
        if expected_open_id is not None:
            raise TamponRecordValidationError("expected_open_id is not allowed for start")
    else:
        if expected_open_id is None:
            raise TamponRecordValidationError("expected_open_id is required for replace and remove")
        result["expected_open_id"] = _text(expected_open_id, "expected_open_id")
    if action == "start":
        result["size"] = _choice(body.get("size"), "size", SIZES)
    elif action == "replace":
        result["size"] = _choice(
            _field(body, "next_size", "nextSize")
            if _field(body, "next_size", "nextSize") is not None
            else _field(body, "new_size", "newSize")
            if _field(body, "new_size", "newSize") is not None else body.get("size"),
            "next_size",
            SIZES,
        )
        result["amount"] = _choice(body.get("amount"), "amount", AMOUNTS)
    else:
        result["amount"] = _choice(body.get("amount"), "amount", AMOUNTS)
    return result


def _fingerprint(action: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in action.items() if key != "operation_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _night(started_at_ms: int, ended_at_ms: int, zone_id: str) -> bool:
    start = datetime.fromtimestamp(started_at_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(ended_at_ms / 1000, tz=timezone.utc)
    zone = ZoneInfo(zone_id)
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    return (
        local_start.hour >= 22
        and local_end.date() > local_start.date()
        and end - start >= timedelta(hours=4)
    )


def _open_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    open_records = [record for record in records if record.get("status") == "OPEN"]
    if len(open_records) > 1:
        raise RuntimeError("tampon record state contains more than one open record")
    return open_records[0] if open_records else None


def _close(record: dict[str, Any], *, ended_at_ms: int, amount: str) -> None:
    if ended_at_ms < record["started_at_ms"]:
        raise TamponRecordValidationError("at_ms must not be before started_at_ms")
    record.update(
        status="CLOSED",
        ended_at_ms=ended_at_ms,
        amount=amount,
        closed_by="ASTRA",
        night=_night(record["started_at_ms"], ended_at_ms, record["zone_id"]),
    )


def _new_record(*, size: str, started_at_ms: int, zone_id: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "status": "OPEN",
        "size": size,
        "started_at_ms": started_at_ms,
        "zone_id": zone_id,
        "created_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
        "source": "android",
        "created_by": "ASTRA",
    }


def apply_action(state: Mapping[str, Any], body: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Return ``(new_state, response, deduplicated)`` without touching disk."""

    action = _canonical_action(body)
    fingerprint = _fingerprint(action)
    next_state = {
        "version": 1,
        "records": deepcopy(state.get("records", [])) if isinstance(state.get("records"), list) else [],
        "operations": deepcopy(state.get("operations", {})) if isinstance(state.get("operations"), dict) else {},
    }
    prior = next_state["operations"].get(action["operation_id"])
    if isinstance(prior, Mapping):
        if prior.get("fingerprint") != fingerprint:
            raise TamponRecordConflictError("operation_id already belongs to another action")
        response = prior.get("response")
        if not isinstance(response, Mapping):
            raise RuntimeError("tampon operation state is invalid")
        return next_state, deepcopy(dict(response)), True

    records: list[dict[str, Any]] = next_state["records"]
    open_record = _open_record(records)
    expected = action.get("expected_open_id")
    if expected is not None and (open_record is None or open_record.get("id") != expected):
        raise TamponRecordConflictError("expected_open_id does not match the current open record")

    response: dict[str, Any]
    if action["action"] == "start":
        if open_record is not None:
            raise TamponRecordConflictError("an open tampon record already exists")
        record = _new_record(size=action["size"], started_at_ms=action["at_ms"], zone_id=action["zone_id"])
        records.append(record)
        response = {"ok": True, "action": "start", "record": deepcopy(record)}
    elif action["action"] == "remove":
        if open_record is None:
            raise TamponRecordConflictError("no open tampon record")
        _close(open_record, ended_at_ms=action["at_ms"], amount=action["amount"])
        response = {"ok": True, "action": "remove", "closed_record": deepcopy(open_record)}
    else:
        if open_record is None:
            raise TamponRecordConflictError("no open tampon record")
        _close(open_record, ended_at_ms=action["at_ms"], amount=action["amount"])
        record = _new_record(size=action["size"], started_at_ms=action["at_ms"], zone_id=action["zone_id"])
        records.append(record)
        response = {
            "ok": True,
            "action": "replace",
            "closed_record": deepcopy(open_record),
            "record": deepcopy(record),
        }

    next_state["operations"][action["operation_id"]] = {
        "fingerprint": fingerprint,
        "response": deepcopy(response),
    }
    return next_state, response, False


class TamponRecordStore:
    """File-backed action store. One lock covers load, transform and save."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Path):
        self.path = path
        key = str(path.resolve())
        with self._locks_guard:
            self.lock = self._locks.setdefault(key, threading.Lock())

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "records": [], "operations": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("tampon record state cannot be read") from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError("tampon record state is invalid")
        return dict(raw)

    def _save_unlocked(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _harden_existing_unlocked(self) -> None:
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def apply(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        with self.lock:
            self._harden_existing_unlocked()
            state = self._load_unlocked()
            next_state, response, deduplicated = apply_action(state, body)
            if not deduplicated:
                self._save_unlocked(next_state)
            return response, deduplicated

    def snapshot(self, *, limit: int = 100) -> dict[str, Any]:
        with self.lock:
            self._harden_existing_unlocked()
            state = self._load_unlocked()
            records = state.get("records", [])
            if not isinstance(records, list):
                raise RuntimeError("tampon record state is invalid")
            copied = [deepcopy(record) for record in records if isinstance(record, dict)]
            copied.sort(key=lambda record: (int(record.get("started_at_ms", 0)), str(record.get("id", ""))), reverse=True)
            _open_record(copied)
            return {
                "schema": "tampon_records.v1",
                "records": copied[:limit],
                "server_now_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
            }
