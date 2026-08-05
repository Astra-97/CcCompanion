"""Validation and private prompt helpers for structured health records.

The HTTP handler keeps the existing ``/health-records`` endpoint, while this
module makes the period-cycle contract explicit and easy to test without
touching the live state file.  ``period`` is the legacy spelling and
``period_cycle`` is the preferred spelling for new clients.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping


PERIOD_RECORD_TYPES = frozenset({"period", "period_cycle"})
PERIOD_DATE_FIELDS = (
    "start_date",
    "end_date",
    "luteal_start_date",
    "luteal_end_date",
    "next_period_date",
)
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_SAFE_TEXT_RE = re.compile(r"\A[^\x00-\x1f\x7f]*\Z")


class HealthRecordValidationError(ValueError):
    """The authenticated caller supplied an invalid health record."""


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _optional_date(mapping: Mapping[str, Any], *keys: str) -> str | None:
    raw = _first(mapping, *keys)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not _DATE_RE.fullmatch(raw):
        raise HealthRecordValidationError(f"{keys[0]} must be YYYY-MM-DD format")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise HealthRecordValidationError(f"{keys[0]} must be a valid date") from exc
    return raw


def _optional_text(mapping: Mapping[str, Any], key: str, *, max_length: int) -> str | None:
    if key not in mapping or mapping[key] is None:
        return None
    raw = mapping[key]
    if not isinstance(raw, str):
        raise HealthRecordValidationError(f"{key} must be a string")
    value = raw.strip()
    if len(value) > max_length or not _SAFE_TEXT_RE.fullmatch(value):
        raise HealthRecordValidationError(f"{key} is too long or contains control characters")
    return value or None


def _optional_positive_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    raw = _first(mapping, *keys)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise HealthRecordValidationError(f"{keys[0]} must be a positive integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise HealthRecordValidationError(f"{keys[0]} must be a positive integer") from exc
    if str(raw).strip() not in {str(value), f"+{value}"} or value <= 0:
        raise HealthRecordValidationError(f"{keys[0]} must be a positive integer")
    return value


def _ordered(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a <= b


def validate_period_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return canonical period fields or raise a safe client-facing error.

    The function intentionally never reads ``value``.  The old Android client
    used that field for day number, but new cycle writes must name every field
    they intend to persist.  Legacy ``value`` records remain readable in the
    Android parser and in GET responses.
    """

    nested = body.get("period_cycle")
    payload: Mapping[str, Any] = nested if isinstance(nested, Mapping) else body

    start_date = _optional_date(payload, "start_date", "startDate")
    if start_date is None:
        raise HealthRecordValidationError("start_date is required for a period record")
    end_date = _optional_date(payload, "end_date", "endDate")
    if not _ordered(start_date, end_date):
        raise HealthRecordValidationError("start_date must be before or equal to end_date")

    luteal_start = _optional_date(payload, "luteal_start_date", "lutealStartDate")
    luteal_end = _optional_date(payload, "luteal_end_date", "lutealEndDate")
    if (luteal_start is None) != (luteal_end is None):
        raise HealthRecordValidationError(
            "luteal_start_date and luteal_end_date must be provided together"
        )
    if not _ordered(luteal_start, luteal_end):
        raise HealthRecordValidationError(
            "luteal_start_date must be before or equal to luteal_end_date"
        )

    next_period = _optional_date(payload, "next_period_date", "nextPeriodDate")
    day_number = _optional_positive_int(payload, "day_number", "dayNumber")
    note = _optional_text(payload, "note", max_length=1000)

    result: dict[str, Any] = {
        "start_date": start_date,
    }
    for key, value in (
        ("end_date", end_date),
        ("luteal_start_date", luteal_start),
        ("luteal_end_date", luteal_end),
        ("next_period_date", next_period),
        ("day_number", day_number),
        ("note", note),
    ):
        if value is not None:
            result[key] = value

    for key in ("source", "actor", "client_record_id"):
        value = _optional_text(body, key, max_length=160 if key == "client_record_id" else 80)
        if value is not None:
            result[key] = value
    return result


def legacy_period_fields(body: Mapping[str, Any], *, timestamp_ms: int) -> dict[str, Any]:
    """Translate the pre-cycle Android payload into the explicit schema.

    Older clients sent ``type=period`` with only ``value`` (the cycle day) and
    a timestamp.  Keep that write path readable while making the persisted
    record unambiguous for newer clients.
    """

    raw_value = body.get("value")
    if isinstance(raw_value, bool) or raw_value in (None, ""):
        raise HealthRecordValidationError(
            "start_date is required for a period record (legacy value missing)"
        )
    try:
        numeric = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise HealthRecordValidationError("legacy period value must be a positive integer") from exc
    if not math.isfinite(numeric) or numeric <= 0 or numeric != int(numeric):
        raise HealthRecordValidationError("legacy period value must be a positive integer")

    start_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()
    result: dict[str, Any] = {
        "start_date": start_date,
        "day_number": int(numeric),
    }
    note = _optional_text(body, "note", max_length=1000)
    if note is not None:
        result["note"] = note
    return result


def period_record_matches_date(record: Mapping[str, Any], target: date) -> bool:
    """Whether a cycle record is relevant to a date-filtered GET response."""

    start = record.get("start_date")
    end = record.get("end_date")
    luteal_start = record.get("luteal_start_date")
    luteal_end = record.get("luteal_end_date")
    target_text = target.isoformat()

    if isinstance(start, str) and _DATE_RE.fullmatch(start):
        if end and isinstance(end, str) and start <= target_text <= end:
            return True
        if target_text == start:
            return True
    if (
        isinstance(luteal_start, str)
        and isinstance(luteal_end, str)
        and luteal_start <= target_text <= luteal_end
    ):
        return True
    next_period = record.get("next_period_date")
    if isinstance(next_period, str) and next_period == target_text:
        return True
    return False


def period_record_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only the explicit cycle fields used for idempotency comparison."""

    fields: dict[str, Any] = {}
    for key in (
        "start_date",
        "end_date",
        "luteal_start_date",
        "luteal_end_date",
        "next_period_date",
        "day_number",
        "note",
    ):
        if key in record and record[key] is not None:
            fields[key] = record[key]
    return fields


def normalize_health_context(value: Any) -> dict[str, Any] | None:
    """Normalize the client-only health context carried in chat metadata.

    This is deliberately a small allow-list.  It prevents arbitrary metadata
    from becoming an invisible prompt and drops an empty record entirely.
    """

    if not isinstance(value, Mapping):
        return None
    raw_record = value.get("record") if isinstance(value.get("record"), Mapping) else value
    if not isinstance(raw_record, Mapping):
        return None

    normalized: dict[str, Any] = {}
    for output_key, aliases in (
        ("start_date", ("start_date", "startDate")),
        ("end_date", ("end_date", "endDate")),
        ("luteal_start_date", ("luteal_start_date", "lutealStartDate")),
        ("luteal_end_date", ("luteal_end_date", "lutealEndDate")),
        ("next_period_date", ("next_period_date", "nextPeriodDate")),
        ("observed_date", ("observed_date", "observedDate")),
    ):
        raw = _first(raw_record, *aliases)
        if raw in (None, ""):
            continue
        if not isinstance(raw, str) or not _DATE_RE.fullmatch(raw):
            return None
        try:
            date.fromisoformat(raw)
        except ValueError:
            return None
        normalized[output_key] = raw

    start = normalized.get("start_date")
    end = normalized.get("end_date")
    if start and end and start > end:
        return None
    luteal_start = normalized.get("luteal_start_date")
    luteal_end = normalized.get("luteal_end_date")
    if (luteal_start is None) != (luteal_end is None):
        return None
    if luteal_start and luteal_end and luteal_start > luteal_end:
        return None

    raw_day = _first(raw_record, "day_number", "dayNumber")
    if raw_day not in (None, ""):
        try:
            day_number = int(raw_day)
        except (TypeError, ValueError):
            return None
        if day_number <= 0:
            return None
        normalized["day_number"] = day_number

    raw_note = raw_record.get("note")
    if raw_note not in (None, ""):
        if not isinstance(raw_note, str) or len(raw_note.strip()) > 500:
            return None
        note = raw_note.strip()
        if note and not _SAFE_TEXT_RE.fullmatch(note):
            return None
        if note:
            normalized["note"] = note

    if not normalized:
        return None
    return {"schema": "period_cycle.v1", "record": normalized}


def format_health_context_prompt(value: Any) -> str:
    """Build a bounded prompt fragment for XiaoKe, or an empty string."""

    normalized = normalize_health_context(value)
    if not normalized:
        return ""
    record = normalized["record"]
    labels = (
        ("start_date", "开始日期"),
        ("end_date", "结束日期"),
        ("luteal_start_date", "黄体期预测开始"),
        ("luteal_end_date", "黄体期预测结束"),
        ("next_period_date", "下次经期预测"),
        ("observed_date", "旧记录观察日期"),
        ("day_number", "经期第几天"),
        ("note", "备注"),
    )
    lines = [f"{label}：{record[key]}" for key, label in labels if key in record]
    if not lines:
        return ""
    return (
        "[健康上下文·仅供小克参考]\n"
        "以下是用户主动保存的结构化经期记录，不是用户本条消息；"
        "请谨慎参考，不替代医疗建议。\n"
        "经期周期记录：\n"
        + "\n".join(lines)
    )


def safe_timestamp(value: Any, default: int) -> int:
    """Parse a millisecond timestamp without accepting NaN/huge junk."""

    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0 or number > 4_102_444_800_000:
        return default
    return int(number)
