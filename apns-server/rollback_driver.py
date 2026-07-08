"""Rollback driver — drives Claude Code's native double-Esc Rewind selector.

2026-07-08 app 长按消息「重roll/回滚到这里」功能的服务端核心：
给定目标用户消息（chat_history 的 user record ts + 原文），在当前 live
Claude Code 会话（tmux session, 默认 cctg）里：

1. 解析 session jsonl（/root/.claude/projects/-root/<sid>.jsonl），沿
   parentUuid 链取「当前活跃分支」（rewind 会 fork，废弃分支还留在文件里，
   线性扫描会数错），定位目标用户消息。
2. 副作用上锁：扫描目标之后将被撤销的 assistant 消息，含以下工具调用则
   拒绝执行：mcp__plugin_telegram*（别人已看到的 TG 消息）、后续轮次的
   mcp__companion__reply*（会撤掉后面对话已发出的回复；目标自己那一轮的
   reply 是重roll本体，允许撤）、Bash 且命令含 git push / gh release /
   curl POST 到外部域名。
3. tmux send-keys Escape Escape 打开 Rewind 选择器（实测 2.1.198：光标
   起始在底部 "(current)"，每按一次 Up 往更早的用户消息移动一行），逐步
   Up + capture-pane 校验 ❯ 光标行文本与目标消息前缀比对，匹配才 Enter；
   Enter 后还有一个确认页（"Confirm you want to restore ..." + │ 引用行）
   再校验一次才最终 Enter。恢复完成后消息文本进输入框，再 Enter 重新提交。
   任何一步校验失败都按 Esc 退出并报错，不盲按。

⚠️ 实测已证实（2026-07-08, Claude Code 2.1.198）：通过 development
channel（CC Companion channel transport / telegram plugin）进来的用户
消息在 jsonl 里是 isMeta=true / promptSource=system，Rewind 选择器
**不会列出它们**——只有 typed prompt（终端键入 / tmux paste 注入）才是
可回滚锚点。channel 消息作为目标时本模块直接拒绝并给出用户可读原因。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("rollback")

# 同一时刻只允许一个回滚在驱动 tmux（多次按键序列不可交叉）。
DRIVER_LOCK = threading.Lock()

DEFAULT_SESSION_ID_FILE = Path("/root/.ccbot/current-session")
DEFAULT_PROJECT_DIR = Path("/root/.claude/projects/-root")

# 选择器逐行扫描的硬上限（每步都有 capture 校验，纯粹防死循环）。
MAX_UP_STEPS = 150


class RollbackRefused(Exception):
    """副作用上锁 / 目标不可回滚 — reason 直接给用户看。"""

    def __init__(self, reason: str, code: str = "refused"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class RollbackError(Exception):
    """驱动失败（选择器解析不上、校验不过等）— reason 给用户看。"""

    def __init__(self, reason: str, code: str = "error"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


# ─────────────────────────── session jsonl 解析 ───────────────────────────

def read_session_entries(jsonl_path: str | Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                entries.append(d)
    return entries


def active_branch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewind 会 fork 会话：新分支 append 在文件尾、parentUuid 指向分叉点，
    废弃分支的行还留在文件里。从最后一个带 uuid 的条目沿 parentUuid 回溯，
    得到当前活跃分支（按时间正序返回）。"""
    by_uuid: dict[str, dict[str, Any]] = {}
    last: dict[str, Any] | None = None
    for e in entries:
        u = e.get("uuid")
        if isinstance(u, str) and u:
            by_uuid[u] = e
            last = e
    branch: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur = last
    while cur is not None:
        u = cur.get("uuid")
        if not isinstance(u, str) or u in seen:
            break
        seen.add(u)
        branch.append(cur)
        pu = cur.get("parentUuid")
        cur = by_uuid.get(pu) if isinstance(pu, str) else None
    branch.reverse()
    return branch


def entry_text(entry: dict[str, Any]) -> str:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
        return "\n".join(parts)
    return ""


def is_user_prompt(entry: dict[str, Any]) -> bool:
    """一条「用户发言」（含 channel 注入的），排除 tool_result / 命令条目。"""
    if entry.get("type") != "user":
        return False
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, list):
        # tool_result 回包不算用户发言
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
    text = entry_text(entry)
    if not text.strip():
        return False
    if text.lstrip().startswith("<command-name>"):
        return False
    return True


def is_rewind_anchor(entry: dict[str, Any]) -> bool:
    """Rewind 选择器里会出现的条目。实测（2.1.198）：只有 typed prompt
    （promptSource=="typed"，非 isMeta）会被列出；channel 注入（isMeta=true,
    promptSource=="system"）、task-notification、nudge 都不会出现。"""
    if not is_user_prompt(entry):
        return False
    if entry.get("isMeta"):
        return False
    return entry.get("promptSource") == "typed"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def find_target_index(
    branch: list[dict[str, Any]],
    *,
    user_record_ts: str = "",
    raw_text: str = "",
) -> int:
    """在活跃分支里定位目标用户消息，返回 branch 下标。

    优先用 user_record_ts 精确匹配（channel 注入的消息 metadata_json 里带
    user_record_ts=chat_history 的 ts；HTML 实体转义过所以用包含匹配）；
    找不到再用消息原文做 normalized 包含匹配，取最后（最新）一个命中。"""
    ts_needle = (user_record_ts or "").strip()
    text_needle = _norm(raw_text)
    best = -1
    for i, e in enumerate(branch):
        if not is_user_prompt(e):
            continue
        content = entry_text(e)
        if ts_needle and ts_needle in content:
            best = i
            continue
        if text_needle and text_needle in _norm(content):
            best = i
    if best < 0:
        raise RollbackRefused(
            "在当前 Claude 会话里找不到这条消息——可能已经被 forge/新开窗口滚出上下文了，回滚不了。",
            code="not_found",
        )
    return best


def ups_to_target(branch: list[dict[str, Any]], target_index: int) -> int:
    """Rewind 选择器光标从底部 "(current)" 起，每按一次 Up 移到更近一条
    锚点消息。目标是「倒数第 N 条锚点」→ 按 N 次 Up。"""
    target = branch[target_index]
    if not is_rewind_anchor(target):
        if target.get("isMeta") or target.get("promptSource") == "system":
            raise RollbackRefused(
                "这条消息是走 channel 通道进入 Claude 的，Claude Code 原生回滚选择器看不到它，"
                "目前只支持回滚以键盘/tmux 注入方式发进去的消息。",
                code="not_rewindable_channel",
            )
        raise RollbackRefused("这条消息不是可回滚的用户输入类型。", code="not_rewindable")
    n = 1
    for e in branch[target_index + 1:]:
        if is_rewind_anchor(e):
            n += 1
    return n


# ─────────────────────────── 副作用上锁 ───────────────────────────

_RISKY_BASH = [
    (re.compile(r"\bgit\s+push\b"), "git push"),
    (re.compile(r"\bgh\s+release\b"), "gh release"),
]
_CURL_POST = re.compile(r"\bcurl\b[^\n|;&]*?(?:-X\s*POST|--data\b|--data-\w+|-d\s|-F\s|--form\b)", re.IGNORECASE)
_CURL_EXTERNAL_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0)[\w.-]+")


def _bash_command_risky(command: str) -> str | None:
    for pat, label in _RISKY_BASH:
        if pat.search(command):
            return label
    if _CURL_POST.search(command) and _CURL_EXTERNAL_URL.search(command):
        return "curl POST 到外部地址"
    return None


def scan_side_effects(branch: list[dict[str, Any]], target_index: int) -> list[str]:
    """扫描目标之后将被撤销的 assistant 消息里的危险工具调用。

    返回拒绝原因列表（空 = 放行）。规则：
    - mcp__plugin_telegram*：任何轮次都拒绝（TG 那头别人已经看到了）。
    - mcp__companion__reply / reply_done：目标自己那一轮允许（重roll撤的
      就是这条回复），之后轮次拒绝（会连带撤掉后面对话已发出的回复）。
    - Bash 含 git push / gh release / curl POST 外部域名：任何轮次拒绝。
    """
    reasons: list[str] = []
    later_turn = False  # False = 目标自己的回复轮，True = 后续用户消息的轮次
    for e in branch[target_index + 1:]:
        if is_user_prompt(e):
            later_turn = True
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            name = str(b.get("name") or "")
            if name.startswith("mcp__plugin_telegram"):
                reasons.append("要撤销的回复里有已经发出去的 Telegram 消息，撤了对面也收不回")
            elif name in ("mcp__companion__reply", "mcp__companion__reply_done"):
                if later_turn:
                    reasons.append("回滚区间里还有后面几轮对话已发出的回复，会一起被撤掉")
            elif name == "Bash":
                inp = b.get("input")
                cmd = str(inp.get("command") or "") if isinstance(inp, dict) else ""
                label = _bash_command_risky(cmd)
                if label:
                    reasons.append(f"要撤销的回复里执行过不可逆命令（{label}），回滚会造成状态错乱")
    # 去重保序
    seen: set[str] = set()
    out = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ─────────────────────────── tmux pane 解析 ───────────────────────────

def parse_rewind_pane(pane: str) -> dict[str, Any]:
    """解析 Rewind 选择器的 capture-pane 文本。

    实测布局（80 列）：
        Rewind
        Restore the code and/or conversation to the point before…
         ↑ 2 more above
          第三条测试消息 樱桃
          No code changes
        ❯ (current)
    """
    lines = pane.splitlines()
    info: dict[str, Any] = {
        "in_rewind": False,
        "nothing": False,
        "cursor_text": "",
        "more_above": False,
        "confirm": False,
        "confirm_text": "",
    }
    for ln in lines:
        s = ln.strip()
        if s == "Rewind":
            info["in_rewind"] = True
        if "Nothing to rewind" in s:
            info["nothing"] = True
        if "more above" in s and "↑" in s:
            info["more_above"] = True
        if "Confirm you want to restore" in s:
            info["confirm"] = True
    if info["in_rewind"]:
        # 光标行：最后一条以 ❯ 开头的行（Rewind 页里输入框不显示，
        # 不会跟 prompt 的 ❯ 混淆——只在 in_rewind 时解析）。
        for ln in lines:
            s = ln.strip()
            if s.startswith("❯"):
                info["cursor_text"] = s[1:].strip()
    if info["confirm"]:
        quoted = [ln.strip()[1:].strip() for ln in lines if ln.strip().startswith("│")]
        # 第一行是消息文本，第二行是 "(54s ago)" 之类
        if quoted:
            info["confirm_text"] = quoted[0]
    return info


def texts_match(visible: str, expected: str, min_overlap: int = 4) -> bool:
    """选择器行是单行截断展示——归一化后可见文本应是完整消息的前缀。"""
    v = _norm(visible).rstrip("…").rstrip()
    e = _norm(expected)
    if not v or not e:
        return False
    if len(v) < min_overlap and len(e) >= min_overlap:
        return False
    return e.startswith(v) or v.startswith(e)


# ─────────────────────────── tmux 驱动 ───────────────────────────

class TmuxRewindDriver:
    def __init__(self, tmux_session: str, *, step_delay: float = 0.15, page_delay: float = 0.8):
        self.session = tmux_session
        self.step_delay = step_delay
        self.page_delay = page_delay

    def _send(self, *keys: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session, *keys],
            capture_output=True, text=True, timeout=5, check=True,
        )

    def _capture(self) -> str:
        p = subprocess.run(
            ["tmux", "capture-pane", "-t", self.session, "-p"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return p.stdout

    def _abort_ui(self, times: int = 2) -> None:
        """校验失败时退出 Rewind UI，不留半开状态。"""
        try:
            for _ in range(times):
                self._send("Escape")
                time.sleep(0.3)
        except Exception:
            logger.warning("rollback abort_ui failed", exc_info=True)

    def run(self, expected_text: str, expected_ups: int) -> dict[str, Any]:
        """执行完整回滚链路。expected_text 是 jsonl 里目标 typed prompt 的
        完整文本（选择器行 / 确认页 / 输入框都拿它做前缀比对）。"""
        pane = self._capture()
        if "esc to interrupt" in pane:
            raise RollbackRefused("小克正在生成回复，等这条说完再回滚。", code="busy")

        # 打开 Rewind
        self._send("Escape", "Escape")
        time.sleep(self.page_delay)
        pane = self._capture()
        info = parse_rewind_pane(pane)
        if info["nothing"]:
            self._abort_ui(1)
            raise RollbackRefused(
                "Claude Code 说没有可回滚的位置（这个会话里没有可回滚的用户消息）。",
                code="nothing_to_rewind",
            )
        if not info["in_rewind"]:
            self._abort_ui(1)
            raise RollbackError("双击 Esc 没有打开回滚选择器，已放弃（会话可能正忙）。", code="no_selector")

        # 逐步 Up + 校验
        found = False
        ups = 0
        prev_cursor = info["cursor_text"]
        max_steps = min(MAX_UP_STEPS, expected_ups + 30)
        while ups < max_steps:
            self._send("Up")
            ups += 1
            time.sleep(self.step_delay)
            pane = self._capture()
            info = parse_rewind_pane(pane)
            cur = info["cursor_text"]
            if texts_match(cur, expected_text):
                found = True
                break
            if cur == prev_cursor and not info["more_above"]:
                break  # 到顶了
            prev_cursor = cur
        if not found:
            self._abort_ui(1)
            raise RollbackError(
                "在回滚选择器里没找到目标消息（走到顶也没匹配上），已按 Esc 取消，什么都没动。",
                code="target_not_in_selector",
            )
        if ups != expected_ups:
            logger.info("rollback ups mismatch: expected=%d actual=%d (以文本匹配为准)", expected_ups, ups)

        # 确认页
        self._send("Enter")
        time.sleep(self.page_delay)
        pane = self._capture()
        info = parse_rewind_pane(pane)
        if not info["confirm"] or not texts_match(info["confirm_text"], expected_text):
            self._abort_ui(2)
            raise RollbackError("回滚确认页校验失败，已按 Esc 取消，什么都没动。", code="confirm_mismatch")

        # 确认恢复 → 消息文本进输入框
        self._send("Enter")
        time.sleep(self.page_delay)
        pane = self._capture()
        tail = _norm("\n".join(pane.splitlines()[-14:]))
        probe = _norm(expected_text)[:20]
        if probe and probe not in tail:
            # 恢复已经发生（fork 已完成），但输入框内容对不上——清掉输入框，
            # 不盲目提交。此时会话已回退，只是没有重新提交。
            self._abort_ui(1)
            raise RollbackError(
                "回滚已执行，但输入框里的消息校验失败，没有自动重发。可以手动再发一次这条消息。",
                code="resubmit_verify_failed",
            )

        # 重新提交
        self._send("Enter")
        return {"ups": ups}


# ─────────────────────────── 对外入口 ───────────────────────────

def resolve_current_jsonl(
    session_id_file: str | Path = DEFAULT_SESSION_ID_FILE,
    project_dir: str | Path = DEFAULT_PROJECT_DIR,
) -> Path:
    sid = Path(session_id_file).read_text(encoding="utf-8").strip()
    if not sid:
        raise RollbackError("读不到当前 Claude session id。", code="no_session_id")
    p = Path(project_dir) / f"{sid}.jsonl"
    if not p.exists():
        raise RollbackError(f"当前 session jsonl 不存在：{p.name}", code="no_jsonl")
    return p


def rollback_to_user_message(
    *,
    tmux_session: str,
    jsonl_path: str | Path | None = None,
    user_record_ts: str = "",
    raw_text: str = "",
    step_delay: float = 0.15,
) -> dict[str, Any]:
    """完整流程：定位 → 上锁扫描 → 驱动 tmux。成功返回 info dict，
    失败抛 RollbackRefused（给用户看的原因）/ RollbackError。"""
    if not user_record_ts and not raw_text.strip():
        raise RollbackError("缺少目标消息标识。", code="bad_target")
    path = Path(jsonl_path) if jsonl_path else resolve_current_jsonl()
    branch = active_branch(read_session_entries(path))
    if not branch:
        raise RollbackError("session jsonl 是空的。", code="empty_session")
    idx = find_target_index(branch, user_record_ts=user_record_ts, raw_text=raw_text)
    reasons = scan_side_effects(branch, idx)
    if reasons:
        raise RollbackRefused("不能回滚：" + "；".join(reasons) + "。", code="side_effects")
    ups = ups_to_target(branch, idx)
    target_text = entry_text(branch[idx])

    if not DRIVER_LOCK.acquire(blocking=False):
        raise RollbackRefused("已经有一个回滚正在执行，等它跑完。", code="in_progress")
    try:
        driver = TmuxRewindDriver(tmux_session, step_delay=step_delay)
        info = driver.run(target_text, ups)
    finally:
        DRIVER_LOCK.release()
    info.update({"target_index": idx, "expected_ups": ups})
    logger.info("rollback ok session=%s ups=%s target=%r", tmux_session, info.get("ups"), target_text[:60])
    return info


# ─────────────────────────── CLI（测试用） ───────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Claude Code rewind rollback driver (test CLI)")
    ap.add_argument("--tmux", required=True, help="tmux session name (NEVER cctg while testing)")
    ap.add_argument("--jsonl", required=True, help="session jsonl path")
    ap.add_argument("--text", default="", help="target user message raw text")
    ap.add_argument("--ts", default="", help="target user_record_ts")
    ap.add_argument("--dry-run", action="store_true", help="locate + lock scan only, no tmux keys")
    args = ap.parse_args()

    branch_ = active_branch(read_session_entries(args.jsonl))
    idx_ = find_target_index(branch_, user_record_ts=args.ts, raw_text=args.text)
    print(f"target index={idx_} text={entry_text(branch_[idx_])[:80]!r}")
    reasons_ = scan_side_effects(branch_, idx_)
    print(f"side effects: {reasons_ or 'none'}")
    if not reasons_:
        print(f"ups needed: {ups_to_target(branch_, idx_)}")
    if args.dry_run:
        raise SystemExit(0)
    out = rollback_to_user_message(
        tmux_session=args.tmux, jsonl_path=args.jsonl,
        user_record_ts=args.ts, raw_text=args.text,
    )
    print("OK", out)
