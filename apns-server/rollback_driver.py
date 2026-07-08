"""Rollback driver — forge-style truncate + restart + tmux re-inject.

2026-07-08 app 长按消息「重roll/回滚到这里」功能的服务端核心（v2）。

v1 曾驱动 Claude Code 原生双 Esc Rewind 选择器，但实测（2.1.198）证实：
通过 development channel（CC Companion channel transport / telegram plugin）
进来的用户消息在 jsonl 里是 isMeta=true / promptSource=system，Rewind
选择器**不会列出它们**——而方小南的消息几乎全部走 channel。所以 v2 改为
forge 式回滚（方案 2026-07-08 方小南拍板）：

1. 定位：解析 session jsonl（/root/.claude/projects/-root/<sid>.jsonl），
   沿 parentUuid 链取「当前活跃分支」（rewind/forge 会 fork，废弃分支还留
   在文件里），user_record_ts 优先 + 文本兜底定位目标用户消息。
2. 上锁：扫描目标之后将被撤销的 assistant 消息，含不可逆副作用（TG 消息、
   后续轮次的 companion reply、git push / gh release / 外部 curl POST）
   则拒绝。
3. 截断：参照 /usr/local/bin/forge-reload 的机制，把活跃分支截到目标消息
   **之前**（目标本身也截掉，等会儿重新注入），只保留 user/assistant 事件，
   生成新 session id、重建 parentUuid 链，写新 jsonl + session 子目录，
   更新 /root/.ccbot/current-session。截断前先备份原 jsonl
   （<name>.bak-rollback-<ts>，同目录），截断点校验不过就中止、不杀 claude。
4. 重启：systemctl restart claude-tg.service（claude-telegram-start.sh 会
   --resume current-session 指向的 forged session），然后套用
   forge-reload-claude 的自动确认逻辑按掉两个启动页（development-channels
   确认页、resume-mode 选择页选 "Resume full session as-is"），等 tmux pane
   出现 statusline 的 "Ctx" 判定 ready。
5. 重注入：tmux load-buffer + paste-buffer（bracketed paste）把她的原话
   （带 [YYYY-MM-DD HH:MM:SS] 时间前缀模拟 channel 格式）敲进终端，capture
   校验输入框内容后 Enter 提交。注入后消息成为 typed prompt，模型重新生成，
   回复照常走 mcp reply 回 app。

整个 3-5 链路几十秒，由 push.py 的后台线程执行（HTTP 先行返回），
DRIVER_LOCK 覆盖全流程。失败日志落 /var/log/cc-rollback.log。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("rollback")

# 同一时刻只允许一个回滚在跑（截断→重启→注入不可交叉），
# push.py 在派后台线程前 acquire，线程结束才 release——覆盖整个异步流程。
DRIVER_LOCK = threading.Lock()

DEFAULT_SESSION_ID_FILE = Path("/root/.ccbot/current-session")
DEFAULT_PROJECT_DIR = Path("/root/.claude/projects/-root")

# 生产 live 会话：cctg 由 claude-tg.service 管理（ExecStop kill-session +
# ExecStart tmux new-session），重启走 systemctl 才能保住服务托管关系。
PRODUCTION_TMUX_SESSION = "cctg"
DEFAULT_RESTART_CMD = ["systemctl", "restart", "claude-tg.service"]

# forge-reload 同款：只保留 user/assistant 事件，其余（file-history-snapshot、
# queue-operation、attachment、last-prompt、permission-mode…）CC 启动时重建。
KEEP_TYPES = {"user", "assistant"}

READY_TIMEOUT = 120.0       # 重启后等 ready 的硬上限（forge 脚本是 45s，放宽）
INJECT_SETTLE = 3.0         # ready 后再静置几秒才注入
LOG_FILE = Path("/var/log/cc-rollback.log")

_file_log_ready = False


def _ensure_file_logging() -> None:
    """把 rollback logger 挂一个 /var/log/cc-rollback.log 的 FileHandler。
    best-effort：没权限就算了（单测 / 非 root 环境）。"""
    global _file_log_ready
    if _file_log_ready:
        return
    try:
        h = logging.FileHandler(LOG_FILE, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)
        if logger.level in (logging.NOTSET, logging.WARNING):
            logger.setLevel(logging.INFO)
        _file_log_ready = True
    except Exception:
        _file_log_ready = True  # 别每次都重试


class RollbackRefused(Exception):
    """副作用上锁 / 目标不可回滚 — reason 直接给用户看。"""

    def __init__(self, reason: str, code: str = "refused"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class RollbackError(Exception):
    """驱动失败（截断校验不过、重启超时、注入校验失败等）— reason 给用户看。"""

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
    """Rewind/forge 会 fork 会话：新分支 append 在文件尾、parentUuid 指向
    分叉点，废弃分支的行还留在文件里。从最后一个带 uuid 的条目沿 parentUuid
    回溯，得到当前活跃分支（按时间正序返回）。"""
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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _squash(s: str) -> str:
    """去掉所有空白——tmux pane 在换行处会插空格/断行，逐字符比对时
    两边都 squash 掉空白才稳（CJK 尤其）。"""
    return re.sub(r"\s+", "", s or "")


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
    ts_best = -1
    text_best = -1
    for i, e in enumerate(branch):
        if not is_user_prompt(e):
            continue
        content = entry_text(e)
        if ts_needle and ts_needle in content:
            ts_best = i
            continue
        if text_needle and text_needle in _norm(content):
            text_best = i
    best = ts_best if ts_best >= 0 else text_best
    if best < 0:
        raise RollbackRefused(
            "在当前 Claude 会话里找不到这条消息——可能已经被 forge/新开窗口滚出上下文了，回滚不了。",
            code="not_found",
        )
    return best


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


# ─────────────────────────── 截断（纯函数） ───────────────────────────

def build_truncated_events(
    branch: list[dict[str, Any]],
    target_index: int,
    new_sid: str,
) -> list[dict[str, Any]]:
    """把活跃分支截到目标用户消息之前，重建为新 session 的事件列表。

    - 目标消息本身不保留（之后由 tmux 重新注入成 typed prompt）。
    - 只保留 user/assistant 事件（forge-reload 同款，其余 CC 会重建）。
    - 深拷贝后重写 sessionId、重建 parentUuid 链（首条 None）。

    截断点校验（红线）：
    - 目标必须是用户发言且不是分支第一条（截空了没法 resume）。
    - branch[target_index].parentUuid 必须指向 branch[target_index-1]
      （活跃分支按构造应满足，显式验证防御脏数据）。
    - 若目标的直接 parent 是 user/assistant，则截完最后一条的原 uuid 必须
      等于目标的 parentUuid（等价校验：parent 是非保留类型时，最后一条必须
      是 branch[:target_index] 里最后一个 user/assistant 事件）。
    任一校验不过：抛异常中止，调用方保证此时还没动 claude。
    """
    if not 0 <= target_index < len(branch):
        raise RollbackError("截断点越界（内部错误）。", code="bad_index")
    target = branch[target_index]
    if not is_user_prompt(target):
        raise RollbackRefused("这条消息不是可回滚的用户发言。", code="not_rewindable")
    if target_index == 0:
        raise RollbackRefused(
            "这已经是当前会话的第一条消息，前面没有可保留的上下文，回滚不了。",
            code="target_is_first",
        )

    parent_entry = branch[target_index - 1]
    target_parent_uuid = target.get("parentUuid")
    if not isinstance(target_parent_uuid, str) or parent_entry.get("uuid") != target_parent_uuid:
        raise RollbackError(
            "截断点校验失败：目标消息的 parentUuid 和活跃分支对不上，中止（没动任何东西）。",
            code="truncate_verify_failed",
        )

    kept_src = [
        e for e in branch[:target_index]
        if e.get("type") in KEEP_TYPES and isinstance(e.get("uuid"), str) and e.get("uuid")
    ]
    if not kept_src:
        raise RollbackRefused(
            "截到这条消息之前就没有可保留的对话内容了，回滚不了。",
            code="nothing_left",
        )

    # 等价校验：截完最后一条必须与目标的 parent 衔接。
    if parent_entry.get("type") in KEEP_TYPES:
        if kept_src[-1].get("uuid") != target_parent_uuid:
            raise RollbackError(
                "截断点校验失败：截断后最后一条消息不是目标消息的 parent，中止（没动任何东西）。",
                code="truncate_verify_failed",
            )
    else:
        # parent 是被丢弃的非保留类型（system 等）：最后一条保留事件必须是
        # branch[:target_index] 里最后一个 user/assistant（构造即如此，防御性验证）。
        last_keep = None
        for e in branch[:target_index]:
            if e.get("type") in KEEP_TYPES:
                last_keep = e
        if last_keep is not kept_src[-1]:
            raise RollbackError(
                "截断点校验失败：保留事件与活跃分支不一致，中止（没动任何东西）。",
                code="truncate_verify_failed",
            )
        logger.info(
            "truncate: target parent is non-keep type=%s, splicing chain at uuid=%s",
            parent_entry.get("type"), kept_src[-1].get("uuid"),
        )

    kept = [copy.deepcopy(e) for e in kept_src]
    prev_uuid: str | None = None
    for ev in kept:
        ev["sessionId"] = new_sid
        ev["parentUuid"] = prev_uuid
        prev_uuid = ev["uuid"]
    return kept


def verify_truncated_file(
    path: Path,
    new_sid: str,
    expected_count: int,
    expected_last_uuid: str,
) -> None:
    """写完 forged jsonl 后回读校验：行数、链条连续、sessionId、末条 uuid。"""
    events = read_session_entries(path)
    if len(events) != expected_count:
        raise RollbackError(
            f"forged session 回读校验失败：期望 {expected_count} 条，实际 {len(events)} 条。",
            code="forge_verify_failed",
        )
    prev: str | None = None
    for ev in events:
        if ev.get("sessionId") != new_sid:
            raise RollbackError("forged session 回读校验失败：sessionId 不一致。", code="forge_verify_failed")
        if ev.get("parentUuid") != prev:
            raise RollbackError("forged session 回读校验失败：parentUuid 链断裂。", code="forge_verify_failed")
        prev = ev.get("uuid")
    if prev != expected_last_uuid:
        raise RollbackError("forged session 回读校验失败：末条 uuid 不符。", code="forge_verify_failed")


def format_injection(text: str, user_ts: str = "") -> str:
    """注入格式：保持原文，带 [YYYY-MM-DD HH:MM:SS] 时间前缀模拟 channel 格式。
    ts 解析不了就不带前缀。"""
    prefix = ""
    ts = (user_ts or "").strip()
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            prefix = dt.strftime("[%Y-%m-%d %H:%M:%S] ")
        except Exception:
            prefix = ""
    return prefix + text


# ─────────────────────────── tmux / 系统操作 ───────────────────────────

def _tmux(*args: str, timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout,
    )


def capture_pane(tmux_session: str) -> str | None:
    """capture 失败（session 正在重启不存在）返回 None，调用方自行容忍。"""
    try:
        p = _tmux("capture-pane", "-t", tmux_session, "-p")
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def assert_not_busy(tmux_session: str) -> None:
    pane = capture_pane(tmux_session)
    if pane is None:
        raise RollbackError(f"读不到 tmux 会话 {tmux_session} 的画面，回滚中止。", code="no_pane")
    if "esc to interrupt" in pane:
        raise RollbackRefused("小克正在生成回复，等这条说完再回滚。", code="busy")


def backup_session_jsonl(jsonl_path: Path) -> Path:
    """截断前强制备份原 jsonl（同目录 .bak-rollback-<ts>，不带 .jsonl 后缀
    结尾所以不会被 session 扫描 glob 到）。失败即中止。"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = jsonl_path.with_name(f"{jsonl_path.name}.bak-rollback-{ts}")
    try:
        shutil.copy2(jsonl_path, backup)
    except Exception as e:
        raise RollbackError(f"备份原 session 失败（{e}），中止，什么都没动。", code="backup_failed")
    return backup


# ─────────────────────────── 计划 / 执行 ───────────────────────────

@dataclass
class RollbackPlan:
    tmux_session: str
    jsonl_path: Path
    branch: list[dict[str, Any]]
    target_index: int
    user_ts: str
    user_text: str
    restart_cmd: list[str] = field(default_factory=lambda: list(DEFAULT_RESTART_CMD))
    session_id_file: Path = DEFAULT_SESSION_ID_FILE
    session_dir: Path = DEFAULT_PROJECT_DIR


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


def prepare_rollback(
    *,
    tmux_session: str,
    user_record_ts: str = "",
    raw_text: str = "",
    jsonl_path: str | Path | None = None,
    restart_cmd: list[str] | None = None,
    session_id_file: str | Path = DEFAULT_SESSION_ID_FILE,
    session_dir: str | Path = DEFAULT_PROJECT_DIR,
) -> RollbackPlan:
    """同步预检：定位 + 截断点校验（干跑）+ 副作用上锁 + busy 检测。
    全部通过才返回 plan；不动任何文件、不碰 claude。"""
    _ensure_file_logging()
    if not user_record_ts and not raw_text.strip():
        raise RollbackError("缺少目标消息标识。", code="bad_target")
    if restart_cmd is None:
        if tmux_session != PRODUCTION_TMUX_SESSION:
            raise RollbackRefused(
                f"回滚只支持主会话 {PRODUCTION_TMUX_SESSION}（当前 {tmux_session}）。",
                code="unsupported_session",
            )
        restart_cmd = list(DEFAULT_RESTART_CMD)

    path = Path(jsonl_path) if jsonl_path else resolve_current_jsonl(session_id_file, session_dir)
    branch = active_branch(read_session_entries(path))
    if not branch:
        raise RollbackError("session jsonl 是空的。", code="empty_session")
    idx = find_target_index(branch, user_record_ts=user_record_ts, raw_text=raw_text)
    reasons = scan_side_effects(branch, idx)
    if reasons:
        raise RollbackRefused("不能回滚：" + "；".join(reasons) + "。", code="side_effects")
    # 截断干跑：校验不过在这里就报，别等杀了 claude 才发现
    build_truncated_events(branch, idx, "dry-run")
    assert_not_busy(tmux_session)
    return RollbackPlan(
        tmux_session=tmux_session,
        jsonl_path=path,
        branch=branch,
        target_index=idx,
        user_ts=user_record_ts,
        user_text=entry_text(branch[idx]) if not raw_text.strip() else raw_text,
        restart_cmd=restart_cmd,
        session_id_file=Path(session_id_file),
        session_dir=Path(session_dir),
    )


def forge_truncated_session(plan: RollbackPlan) -> dict[str, Any]:
    """备份 → 生成 forged jsonl → 回读校验 → 建 session 子目录 →
    切 current-session 指针。全程不碰 claude 进程；任何一步失败都在切换
    指针之前抛异常，live 会话保持原状。"""
    backup = backup_session_jsonl(plan.jsonl_path)
    new_sid = str(uuid_mod.uuid4())
    kept = build_truncated_events(plan.branch, plan.target_index, new_sid)

    new_path = plan.session_dir / f"{new_sid}.jsonl"
    tmp_path = plan.session_dir / f"{new_sid}.jsonl.forging"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            for ev in kept:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.replace(tmp_path, new_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise RollbackError(f"写 forged session 失败（{e}），中止，原会话未动。", code="forge_write_failed")

    verify_truncated_file(new_path, new_sid, len(kept), str(kept[-1].get("uuid")))

    # CC 期望的 session 子目录（subagents / tool-results）
    subdir = plan.session_dir / new_sid
    (subdir / "subagents").mkdir(parents=True, exist_ok=True)
    (subdir / "tool-results").mkdir(parents=True, exist_ok=True)

    old_sid = ""
    try:
        old_sid = plan.session_id_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    plan.session_id_file.parent.mkdir(parents=True, exist_ok=True)
    plan.session_id_file.write_text(new_sid + "\n", encoding="utf-8")

    info = {
        "new_sid": new_sid,
        "new_path": str(new_path),
        "backup_path": str(backup),
        "old_sid": old_sid,
        "kept_events": len(kept),
        "dropped_events": len(plan.branch) - plan.target_index,
    }
    logger.info("rollback forge ok: %s", info)
    return info


def restart_claude(restart_cmd: list[str]) -> None:
    try:
        subprocess.run(restart_cmd, capture_output=True, text=True, timeout=60, check=True)
    except Exception as e:
        raise RollbackError(
            f"重启 claude 服务失败（{e}）。session 指针已指向截断后的会话，"
            "服务恢复后会自动 resume 到回滚点，但这条消息需要手动重发。",
            code="restart_failed",
        )


def wait_claude_ready(tmux_session: str, timeout: float = READY_TIMEOUT) -> None:
    """等重启后的 claude 就绪（forge-reload-claude auto_confirm_startup_pages
    的 Python 版）：容忍 pane 暂时不存在；按掉 development-channels 确认页；
    resume 选择页把光标挪到 "Resume full session as-is" 再 Enter；看到
    statusline 的 "Ctx" 即 ready。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = capture_pane(tmux_session)
        if not pane:
            time.sleep(2)
            continue
        if "Ctx" in pane:
            logger.info("rollback restart: normal UI reached (session=%s)", tmux_session)
            return
        low = pane.lower()
        if "development channel" in low:
            logger.info("rollback restart: confirming development-channels page")
            _tmux("send-keys", "-t", tmux_session, "Enter")
            time.sleep(2)
            continue
        if "resume full session" in low:
            cursor_line = ""
            for ln in pane.splitlines():
                if "❯" in ln:
                    cursor_line = ln
                    break
            if "full session" in cursor_line.lower():
                logger.info("rollback restart: selecting 'Resume full session as-is'")
                _tmux("send-keys", "-t", tmux_session, "Enter")
            else:
                _tmux("send-keys", "-t", tmux_session, "Down")
            time.sleep(1)
            continue
        time.sleep(2)
    raise RollbackError(
        "重启后等 claude 就绪超时。会话已回滚到目标消息之前，这条消息需要手动重发。",
        code="ready_timeout",
    )


def inject_prompt(tmux_session: str, text: str, *, settle: float = INJECT_SETTLE) -> None:
    """tmux bracketed-paste 注入原话并提交。粘贴后 capture 校验输入框内容
    （squash 空白后前缀比对），校验不过就不按 Enter——此时会话已回滚成功，
    只是没自动重发。"""
    time.sleep(settle)
    # 就绪后不该在生成中；防御性再等一下
    for _ in range(15):
        pane = capture_pane(tmux_session)
        if pane is not None and "esc to interrupt" not in pane:
            break
        time.sleep(2)

    buf = f"ccrollback-{os.getpid()}"
    try:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buf, "-"],
            input=text.encode("utf-8"), timeout=5, check=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-p", "-d", "-b", buf, "-t", tmux_session],
            capture_output=True, timeout=5, check=True,
        )
    except Exception as e:
        raise RollbackError(
            f"tmux 注入消息失败（{e}）。会话已回滚，这条消息需要手动重发。",
            code="inject_failed",
        )
    time.sleep(1.2)
    pane = capture_pane(tmux_session) or ""
    probe = _squash(text)[:16]
    if probe and probe not in _squash(pane):
        raise RollbackError(
            "注入后输入框校验失败，没有自动重发。会话已回滚到目标消息之前，可以手动再发一次这条消息。",
            code="resubmit_verify_failed",
        )
    _tmux("send-keys", "-t", tmux_session, "Enter")
    logger.info("rollback inject ok (session=%s, %d chars)", tmux_session, len(text))


def execute_rollback(
    plan: RollbackPlan,
    *,
    after_truncate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """完整执行：截断 →（回调，push.py 在这里 mark_regenerated）→ 重启 →
    等就绪 → 重注入。调用方必须已持有 DRIVER_LOCK 并负责 release。
    抛 RollbackError/RollbackRefused，reason 可直接给用户看。"""
    _ensure_file_logging()
    # 截断/杀进程前最后一次 busy 检测（预检到现在可能过了几秒）
    assert_not_busy(plan.tmux_session)
    info = forge_truncated_session(plan)
    if after_truncate is not None:
        try:
            after_truncate(info)
        except Exception:
            logger.exception("rollback after_truncate callback failed (continuing)")
    restart_claude(plan.restart_cmd)
    wait_claude_ready(plan.tmux_session)
    injected = format_injection(plan.user_text, plan.user_ts)
    inject_prompt(plan.tmux_session, injected)
    info["injected_text"] = injected
    logger.info("rollback complete: session=%s new_sid=%s", plan.tmux_session, info["new_sid"])
    return info


# ─────────────────────────── CLI（测试用） ───────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import argparse

    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="forge-style rollback driver (test CLI)")
    ap.add_argument("--tmux", required=True, help="tmux session name (NEVER cctg while testing)")
    ap.add_argument("--jsonl", required=True, help="session jsonl path")
    ap.add_argument("--text", default="", help="target user message raw text")
    ap.add_argument("--ts", default="", help="target user_record_ts")
    ap.add_argument("--restart-cmd", default="", help="shell command to restart claude (required for non-cctg)")
    ap.add_argument("--session-id-file", default=str(DEFAULT_SESSION_ID_FILE))
    ap.add_argument("--session-dir", default=str(DEFAULT_PROJECT_DIR))
    ap.add_argument("--dry-run", action="store_true", help="locate + lock scan + truncate dry-run only")
    args = ap.parse_args()

    branch_ = active_branch(read_session_entries(args.jsonl))
    idx_ = find_target_index(branch_, user_record_ts=args.ts, raw_text=args.text)
    print(f"target index={idx_} text={entry_text(branch_[idx_])[:80]!r}")
    reasons_ = scan_side_effects(branch_, idx_)
    print(f"side effects: {reasons_ or 'none'}")
    kept_ = build_truncated_events(branch_, idx_, "dry-run")
    print(f"truncate: keep {len(kept_)} events, drop {len(branch_) - idx_} (incl. target)")
    if args.dry_run:
        raise SystemExit(0)
    plan_ = prepare_rollback(
        tmux_session=args.tmux,
        jsonl_path=args.jsonl,
        user_record_ts=args.ts,
        raw_text=args.text,
        restart_cmd=(["bash", "-c", args.restart_cmd] if args.restart_cmd else None),
        session_id_file=args.session_id_file,
        session_dir=args.session_dir,
    )
    if not DRIVER_LOCK.acquire(blocking=False):
        raise SystemExit("another rollback in progress")
    try:
        out = execute_rollback(plan_)
    finally:
        DRIVER_LOCK.release()
    print("OK", json.dumps(out, ensure_ascii=False, indent=2))
