"""App 记忆库备份的「附带文件」白名单 (phone-backup-files.json)。

Android app 每轮记忆库备份 (每天首次打开 app 的自动增量 / 设置页手动全量) 会先
GET /phone-backup/files 拿清单, 再按 name 逐个 GET /phone-backup/file?name=...,
整份覆盖写进手机 Documents/CcCompanion/memory-backup/<name>。

配置文件就在 push.py 旁边, 每次请求重新读, 改清单不用重启服务, 也不用重编 app::

    [
      {"name": "persona-xiaoke.md", "path": "/root/.claude/output-styles/xiaoke.md"},
      {"name": "RESTORE.md", "path": "/root/vps-xiaoke-config/RESTORE.md"}
    ]

- name: 手机上的文件名。字母数字开头, 只含字母数字 . _ -, <=128 字符;
  不能和记忆库三件套 (memories.jsonl / messages.jsonl / manifest.json) 重名, 不能以 .tmp 结尾。
- path: 216 上的绝对路径, 必须是普通文件 (软链会被解析后再判断), 单个 <= 8 MB。
- 客户端只能按 name 要文件, 永远不接受路径参数。坏条目和重名条目跳过, 不拖垮整份清单。
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "phone-backup-files.json"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 64
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RESERVED_NAMES = frozenset({"memories.jsonl", "messages.jsonl", "manifest.json"})


@dataclass(frozen=True)
class BackupFileEntry:
    name: str
    path: Path


def valid_name(name: Any) -> bool:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return False
    if name in RESERVED_NAMES or name.lower().endswith(".tmp"):
        return False
    return name not in {".", ".."}


def load_entries(config_path: Path | None = None) -> list[BackupFileEntry]:
    """读白名单; 文件缺失或不是 JSON 数组时返回空表。"""
    path = config_path or CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    entries: list[BackupFileEntry] = []
    seen: set[str] = set()
    for item in raw[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        file_path = item.get("path")
        if not valid_name(name) or name in seen:
            continue
        if not isinstance(file_path, str) or not file_path.startswith("/"):
            continue
        seen.add(name)
        entries.append(BackupFileEntry(name=name, path=Path(file_path)))
    return entries


def find_entry(name: str, config_path: Path | None = None) -> BackupFileEntry | None:
    if not valid_name(name):
        return None
    for entry in load_entries(config_path):
        if entry.name == name:
            return entry
    return None


def read_entry(entry: BackupFileEntry) -> tuple[bytes, float]:
    """读一个白名单文件; 不是普通文件抛 FileNotFoundError, 超限抛 ValueError。"""
    st = entry.path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise FileNotFoundError(str(entry.path))
    if st.st_size > MAX_FILE_BYTES:
        raise ValueError("too large")
    data = entry.path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("too large")
    return data, st.st_mtime


def iso_utc(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def describe(entry: BackupFileEntry) -> dict[str, Any]:
    """清单里的一行: name / bytes / sha256 / updatedAt; 读不到时 available=false。"""
    try:
        data, mtime = read_entry(entry)
    except FileNotFoundError:
        return {"name": entry.name, "available": False, "error": "not found"}
    except ValueError:
        return {"name": entry.name, "available": False, "error": "too large"}
    except OSError:
        return {"name": entry.name, "available": False, "error": "unreadable"}
    return {
        "name": entry.name,
        "available": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "updatedAt": iso_utc(mtime),
    }
