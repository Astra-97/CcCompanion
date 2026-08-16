"""读 vault 里几个 todo md 文件 parse 成结构化 todo 列表"""
from __future__ import annotations

import os
import re
import shutil
import fcntl
import json
import tempfile
from datetime import date, datetime
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
from zoneinfo import ZoneInfo

VAULT = Path(os.path.expanduser("~/Documents/星原"))

TODO_SOURCES = [
    {
        "section": "进行中",
        "path": VAULT / "眠的小家/AI的记忆/日常/进行中事项.md",
    },
    {
        "section": "工作",
        "path": VAULT / "工作/工作待办/总览.md",
    },
    {
        "section": "生活",
        "path": VAULT / "生活/生活待办/个人inbox待办.md",
    },
    {
        "section": "AI",
        "path": VAULT / "眠的小家/AI的记忆/日常/AI协作迭代记录.md",
    },
    {
        "section": "项目",
        "path": VAULT / "工作/工作待办/项目.md",
    },
]

# 匹配: - [ ] / - [x] / - [X] / - [❓] 行
TODO_RE = re.compile(r"^\s*-\s*\[([\sxX❓✓])\]\s*(.+?)\s*$", re.UNICODE)
# 匹配子项 actor [Cc] / [User]
ACTOR_RE = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

PRIORITY_RE = re.compile(r'(?:!p([123])|(?<![a-zA-Z])#p([123]))(?![a-zA-Z0-9])', re.IGNORECASE)
DUEDATE_RE = re.compile(r'@(\d{4}-\d{2}-\d{2})|📅\s*(\d{4}-\d{2}-\d{2})')
TAG_RE = re.compile(r'(?<![a-zA-Z])#([a-zA-Z一-鿿][a-zA-Z0-9一-鿿_-]*)(?![a-zA-Z0-9])')

ALLOWED_PATHS = {src["path"].resolve(): src["section"] for src in TODO_SOURCES}
BACKUP_DIR = Path("~/CcCompanion/apns-server/tokens/todos_backup").expanduser()
_WRITE_LOCK = Lock()

# The home-screen todo view is a projection of XiaoKe's schedule, not a
# second Markdown-backed task list.  The default and lock protocol match
# /root/ccbot-lite/ops/scripts/schedule-ctl and schedule-remind.
DEFAULT_SCHEDULE_PATH = Path("/root/schedule/events.json")


class ScheduleTodoStoreError(RuntimeError):
    """Schedule data cannot be safely read or modified."""


def collect_all(
    schedule_path: str | Path | None = None,
    *,
    today: date | None = None,
) -> list[dict]:
    """Return today's and future schedule events in the legacy todo shape."""
    path = _schedule_path(schedule_path)
    with _schedule_lock(path, create_parent=False):
        events = _load_schedule(path)
    local_today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    events = [event for event in events if date.fromisoformat(event["date"]) >= local_today]
    items = [_schedule_item(event) for event in sorted(events, key=_schedule_sort_key)]
    return [{
        "section": "日程",
        "source": "schedule",
        "items": items,
        "count": len(items),
        "pending": sum(1 for item in items if not item["done"]),
    }]


def parse_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    current_heading = ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    for line_idx, raw in enumerate(text.splitlines()):
        line = raw.rstrip()
        if line.startswith("##"):
            current_heading = line.lstrip("#").strip()
            continue
        m = TODO_RE.match(line)
        if not m:
            continue
        status_char = m.group(1)
        body = m.group(2).strip()
        done = status_char.lower() == "x" or status_char == "✓"
        unsure = status_char == "❓"

        raw_text = body  # save before actor strip

        # actor parse
        actor = None
        am = ACTOR_RE.match(body)
        if am:
            actor = am.group(1)
            body = am.group(2).strip()

        # metadata extraction
        raw_body = body  # body after actor strip, before metadata strip

        pm = PRIORITY_RE.search(body)
        priority = None
        if pm:
            priority = int(pm.group(1) or pm.group(2))

        dm = DUEDATE_RE.search(body)
        due_date = None
        if dm:
            due_date = dm.group(1) or dm.group(2)

        tags = []
        for tm in TAG_RE.finditer(body):
            tag = tm.group(1)
            if not re.match(r'^p[123]$', tag, re.IGNORECASE):
                tags.append(tag)

        # strip metadata tokens from display text
        display = body
        display = PRIORITY_RE.sub("", display)
        display = DUEDATE_RE.sub("", display)
        display = TAG_RE.sub(lambda m2: "" if not re.match(r'^p[123]$', m2.group(1), re.IGNORECASE) else m2.group(0), display)
        display = display.strip()

        item: dict = {
            "text": display if display else body,
            "done": done,
            "unsure": unsure,
            "actor": actor,
            "heading": current_heading,
            "rawText": raw_body,
            "lineIndex": line_idx,
        }
        if priority is not None:
            item["priority"] = priority
        if due_date is not None:
            item["dueDate"] = due_date
        if tags:
            item["tags"] = tags
        items.append(item)
    return items


def toggle(
    rel_path: str,
    heading: str,
    text: str,
    expected_done: bool | None = None,
    file_mtime: float | None = None,
    line_index: int | None = None,
    *,
    event_id: str | None = None,
    schedule_path: str | Path | None = None,
) -> dict:
    """Toggle one schedule event by its stable id.

    The legacy Markdown locator parameters remain in the signature for caller
    compatibility but are deliberately never used to modify Markdown files.
    """
    del rel_path, heading, text, file_mtime, line_index
    if not isinstance(event_id, str) or not event_id.strip():
        return {"ok": False, "error": "schedule_event_id_required"}
    if not isinstance(expected_done, bool):
        return {"ok": False, "error": "expected_done_required"}
    path = _schedule_path(schedule_path)
    try:
        with _schedule_lock(path, create_parent=False):
            events = _load_schedule(path)
            event = next((item for item in events if item["id"] == event_id), None)
            if event is None:
                return {"ok": False, "error": "event_not_found"}
            if event["done"] != expected_done:
                return {"ok": False, "error": "race_detected"}
            event["done"] = not expected_done
            _save_schedule(path, events)
            return {
                "ok": True,
                "new_done": event["done"],
                "event_id": event_id,
                "title": event["title"],
            }
    except ScheduleTodoStoreError:
        return {"ok": False, "error": "schedule_unavailable"}


def add(
    rel_path: str,
    heading: str,
    text: str,
    actor: str | None = None,
    after_text: str | None = None,
) -> dict:
    abs_path = _resolve_path(rel_path)
    if abs_path is None:
        return {"ok": False, "error": "path_not_allowed"}

    with _WRITE_LOCK:
        if not abs_path.exists():
            return {"ok": False, "error": "file_missing"}

        lines = abs_path.read_text(encoding="utf-8").splitlines(keepends=False)
        block = _heading_block(lines, heading)
        if block is None:
            return {"ok": False, "error": "heading_not_found"}
        heading_idx, next_heading_idx = block

        body = f"[{actor}] {text}" if actor else text
        new_line = f"- [ ] {body}"
        if after_text:
            insert_after = _locate_line(lines, heading, after_text)
            if insert_after is None:
                return {"ok": False, "error": "after_text_not_found"}
            if isinstance(insert_after, str):
                return {"ok": False, "error": insert_after}
            lines.insert(insert_after + 1, new_line)
        else:
            insert_at = next_heading_idx
            while insert_at > heading_idx + 1 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            lines.insert(insert_at, new_line)

        _backup(abs_path)
        _atomic_write(abs_path, lines)
        return {"ok": True, "added_text": body, "file_mtime": abs_path.stat().st_mtime}


def edit(rel_path: str, heading: str, text: str, new_text: str) -> dict:
    abs_path = _resolve_path(rel_path)
    if abs_path is None:
        return {"ok": False, "error": "path_not_allowed"}

    with _WRITE_LOCK:
        if not abs_path.exists():
            return {"ok": False, "error": "file_missing"}

        lines = abs_path.read_text(encoding="utf-8").splitlines(keepends=False)
        target_idx = _locate_line(lines, heading, text)
        if target_idx is None:
            return {"ok": False, "error": "line_not_found"}
        if isinstance(target_idx, str):
            return {"ok": False, "error": target_idx}

        m = TODO_RE.match(lines[target_idx])
        if not m:
            return {"ok": False, "error": "regex_fail"}
        cur_body = m.group(2).strip()
        am = ACTOR_RE.match(cur_body)
        new_body = f"[{am.group(1)}] {new_text}" if am else new_text
        lines[target_idx] = lines[target_idx][:m.start(2)] + new_body + lines[target_idx][m.end(2):]

        _backup(abs_path)
        _atomic_write(abs_path, lines)
        return {
            "ok": True,
            "old_text": text,
            "new_text": new_text,
            "file_mtime": abs_path.stat().st_mtime,
        }


def _backup(path: Path) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    shutil.copy2(path, BACKUP_DIR / f"{path.name}_{ts}.bak")


def _resolve_path(rel_path: str) -> Path | None:
    if not rel_path or os.path.isabs(rel_path) or ".." in Path(rel_path).parts:
        return None
    candidate = (VAULT / rel_path).resolve()
    if candidate in ALLOWED_PATHS:
        return candidate
    return None


def _mtime_changed(path: Path, file_mtime: float | None) -> bool:
    if file_mtime is None:
        return False
    return abs(path.stat().st_mtime - float(file_mtime)) > 0.01


def _heading_block(lines: list[str], heading: str) -> tuple[int, int] | None:
    heading_idx = None
    for i, line in enumerate(lines):
        if line.startswith("##") and line.lstrip("#").strip() == heading:
            heading_idx = i
            break
    if heading_idx is None:
        return None
    next_heading_idx = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        if lines[j].startswith("##"):
            next_heading_idx = j
            break
    return heading_idx, next_heading_idx


def _locate_line(lines: list[str], heading: str, text: str) -> int | str | None:
    block = _heading_block(lines, heading)
    if block is None:
        return None
    heading_idx, next_heading_idx = block
    matches: list[int] = []
    target = _compare_text(text)
    for i in range(heading_idx + 1, next_heading_idx):
        m = TODO_RE.match(lines[i])
        if not m:
            continue
        if _compare_text(m.group(2)) == target:
            matches.append(i)
    if not matches:
        return None
    if len(matches) > 1:
        return "ambiguous_match"
    return matches[0]


def _compare_text(text: str) -> str:
    body = text.strip()
    am = ACTOR_RE.match(body)
    return am.group(2).strip() if am else body


def _atomic_write(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _schedule_path(override: str | Path | None) -> Path:
    raw = override or os.environ.get("CC_COMPANION_TODOS_SCHEDULE_PATH") or DEFAULT_SCHEDULE_PATH
    return Path(raw).expanduser()


@contextmanager
def _schedule_lock(path: Path, *, create_parent: bool) -> Iterator[None]:
    """Use schedule-ctl's exact ``<schedule dir>/.lock`` protocol."""
    parent = path.parent
    if create_parent:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise ScheduleTodoStoreError("schedule directory unavailable") from exc
    elif not parent.exists():
        yield
        return
    try:
        with _WRITE_LOCK:
            fd = os.open(parent / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "r+") as lock_file:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file, fcntl.LOCK_UN)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
    except ScheduleTodoStoreError:
        raise
    except OSError as exc:
        raise ScheduleTodoStoreError("schedule lock unavailable") from exc


def _load_schedule(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            events = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise ScheduleTodoStoreError("schedule data unavailable") from exc
    _validate_schedule(events)
    return events


def _save_schedule(path: Path, events: list[dict[str, Any]]) -> None:
    _validate_schedule(events)
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".events.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(events, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise ScheduleTodoStoreError("schedule write failed") from exc
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


def _validate_schedule(events: Any) -> None:
    if not isinstance(events, list):
        raise ScheduleTodoStoreError("schedule data invalid")
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ScheduleTodoStoreError("schedule data invalid")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise ScheduleTodoStoreError("schedule data invalid")
        seen.add(event_id)
        if not isinstance(event.get("title"), str) or not event["title"].strip():
            raise ScheduleTodoStoreError("schedule data invalid")
        try:
            date.fromisoformat(event["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleTodoStoreError("schedule data invalid") from exc
        event_time = event.get("time", object())
        if event_time is not None:
            if not isinstance(event_time, str):
                raise ScheduleTodoStoreError("schedule data invalid")
            try:
                normalized = datetime.strptime(event_time, "%H:%M").strftime("%H:%M")
            except ValueError as exc:
                raise ScheduleTodoStoreError("schedule data invalid") from exc
            if normalized != event_time:
                raise ScheduleTodoStoreError("schedule data invalid")
        remind = event.get("remind_minutes_before")
        if remind is not None and (not isinstance(remind, int) or isinstance(remind, bool) or remind < 0):
            raise ScheduleTodoStoreError("schedule data invalid")
        if not isinstance(event.get("done"), bool) or not isinstance(event.get("reminded"), bool):
            raise ScheduleTodoStoreError("schedule data invalid")
        if event.get("note") is not None and not isinstance(event.get("note"), str):
            raise ScheduleTodoStoreError("schedule data invalid")


def _schedule_item(event: dict[str, Any]) -> dict:
    item: dict[str, Any] = {
        "text": event["title"],
        "done": event["done"],
        "unsure": False,
        "actor": None,
        "heading": "日程",
        "rawText": event["title"],
        "lineIndex": None,
        "dueDate": event["date"],
        "event_id": event["id"],
        "time": event["time"],
        "note": event.get("note") or "",
        "remind_minutes_before": event.get("remind_minutes_before"),
    }
    return item


def _schedule_sort_key(event: dict[str, Any]) -> tuple[str, str]:
    return str(event["date"]), str(event["time"] or "00:00")
