"""Apples (苹果幼稚园) 群 per-member 未读游标 (2026-09-25 第二阶段).

apples 历史存在 ChatHistory (chat_history_apples.jsonl), 记录以 ts 为键
(没有 workgroup 那样的 grp_ id), 所以游标直接按 ts 字符串存。

游标语义:
- 每个 AI 成员 (kimi/kairos/xiaoke) 一个游标, 值为它"已读到"的那条消息 ts。
- 游标在**派发时**推进到本次注入的最新一条未读 (不是回复落库时),
  回复生成期间新到的消息自然保持未读, 下次触发再注入, 不静默丢失。
- 首次启用 (store 里没有任何游标) 视为全部已读: 初始化所有成员游标到
  当前最新 ts, 本次不注入未读 — 避免上线第一天把历史全倒给 AI。
- 游标缺失/失效 (指向比最新记录还新的 ts, 即目标被删或数据异常) 回退最近 4 条。
- 未读超过 50 条取最近 50 条, 由调用方在注入文本首行明示省略条数。

持久化: apples_read_cursors.json, .tmp + replace 原子写, 重启不丢。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

UNREAD_CAP = 50
UNREAD_FALLBACK = 4

APPLE_AI_MEMBERS = ("kimi", "kairos", "xiaoke")


def split_unread(
    records: list[dict[str, Any]],
    cursor_ts: str | None,
    *,
    cap: int = UNREAD_CAP,
    fallback: int = UNREAD_FALLBACK,
) -> tuple[list[dict[str, Any]], int, bool]:
    """按 ts 游标切出未读。records 必须按 ts 升序 (ChatHistory.tail/read_since 输出)。

    返回 (unread, omitted, used_fallback):
    - 游标有效 (<= 最新记录 ts): 未读 = 游标之后的消息, 超 cap 取最近 cap 条,
      omitted = 被省略的较早未读条数。
    - 游标缺失/为空/比最新记录还新 (目标被删/异常): 回退最近 fallback 条,
      used_fallback=True。
    """
    if not records:
        return [], 0, False
    cursor = str(cursor_ts or "")
    if cursor:
        latest_ts = str(records[-1].get("ts") or "")
        if cursor <= latest_ts:
            unread = [r for r in records if str(r.get("ts") or "") > cursor]
            omitted = max(0, len(unread) - cap)
            return (unread[-cap:] if cap > 0 else []), omitted, False
    return records[-fallback:], 0, True


def format_unread_lines(
    records: list[dict[str, Any]],
    *,
    member_id: str,
    trigger_ts: str = "",
    name_for=None,
) -> list[str]:
    """把未读记录格式化成注入行 "[HH:MM] 名字: 文本"。

    过滤: 成员自己发的 (自己的回复不算自己的未读)、hidden_in_ui (重新发言
    覆盖的旧版)、触发消息本身 (调用方会单独附上, 不重复)。
    """
    lines: list[str] = []
    for rec in records:
        if str(rec.get("sender_id") or "").strip().lower() == member_id:
            continue
        if rec.get("hidden_in_ui"):
            continue
        if trigger_ts and str(rec.get("ts") or "") == trigger_ts:
            continue
        name = str(rec.get("sender_name") or "").strip()
        if not name:
            sender_id = str(rec.get("sender_id") or "").strip()
            name = name_for(sender_id) if callable(name_for) else (sender_id or "成员")
        ts = str(rec.get("ts") or "")[11:16]
        text = str(rec.get("text") or "").strip().replace("\n", " ")
        if not text and rec.get("attachment_filename"):
            text = f"[附件 {rec['attachment_filename']}]"
        if not text and rec.get("location"):
            text = "[位置]"
        if len(text) > 180:
            text = text[:177] + "..."
        lines.append(f"[{ts}] {name}: {text}")
    return lines


class ApplesCursorStore:
    """per-member 已读游标 (按 ts), json 文件原子写持久化。"""

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path).expanduser()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"initialized": False, "cursors": {}}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("initialized", False)
                if not isinstance(data.get("cursors"), dict):
                    data["cursors"] = {}
                return data
        except Exception:
            pass
        return {"initialized": False, "cursors": {}}

    def _save_locked(self) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.state_path)

    def is_initialized(self) -> bool:
        return bool(self._state.get("initialized"))

    def initialize(self, latest_ts: str) -> None:
        """首次启用: 全部成员游标推到当前最新, 视为全部已读。"""
        with self._lock:
            if self._state.get("initialized"):
                return
            cursors = self._state.setdefault("cursors", {})
            for member_id in APPLE_AI_MEMBERS:
                cursors.setdefault(member_id, latest_ts)
            self._state["initialized"] = True
            self._save_locked()

    def get_cursor(self, member_id: str) -> str:
        with self._lock:
            return str(self._state.get("cursors", {}).get(member_id) or "")

    def set_cursor(self, member_id: str, ts: str) -> None:
        if not ts:
            return
        with self._lock:
            cursors = self._state.setdefault("cursors", {})
            # 单调推进, 绝不让游标倒退 (并发派发/乱序回复兜底)
            if str(cursors.get(member_id) or "") <= ts:
                cursors[member_id] = ts
                self._save_locked()
