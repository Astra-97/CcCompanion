#!/usr/bin/env python3
"""agent_task_watch — Kimi Code CLI 后台任务「跨会话完成通知」守望者。

背景: CcCompanion (push.py) 在上下文超阈值时会 forge 一个全新的 Kimi 会话。
Kimi 的后台子代理任务绑死在派出它的会话上 (任务存档在
~/.kimi-code/sessions/wd_<ws>_<hash>/session_<uuid>/agents/*/tasks/<taskId>.json),
会话被换掉后, 旧会话里完成的任务通知永远送不到新会话。

本脚本独立运行 (cron / systemd timer, 建议每 5 分钟), 不改动 push.py:
  1. 扫描所有 kimi 会话的 tasks/*.json, 找出终态 (completed/failed/stopped/
     killed/timed_out) 且尚未送达的 kind=agent 子代理任务
     (kind=process/bash 的后台命令琐碎且量大, 一律不推);
  2. 通过 CcCompanion 现成的回环推送入口 POST /chat/append
     (contact_id=kimi, role=assistant) 把结果推给 Astra 的 App
     (写 Kimi 聊天历史 + APNs banner, 由 push.py 内部 _send_chat_notification 发出);
  3. 用本地 delivered 记录文件去重, 失败不标记、下轮静默重试, 每轮限量防刷。

首次部署先跑一次 --prime: 把现存全部终态任务标记为已送达 (不推送),
避免上线时把积压的几十条旧任务一次性刷给用户。

凭证: 从 config.toml [server].shared_secret 读取 (缺失时回退 ~/.ots/secret),
只用于 X-Auth-Token 请求头, 绝不打印。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# killed/timed_out 也是终态: 被杀或超时的子代理同样不会有人汇报, 值得通知。
TERMINAL_STATUSES = {"completed", "failed", "stopped", "killed", "timed_out"}

# 只推 kind=agent 的子代理任务; kind=process/bash 的后台命令琐碎且量大, 不打扰。
NOTIFY_KINDS = {"agent"}

DEFAULT_SESSIONS_ROOT = Path.home() / ".kimi-code" / "sessions"
DEFAULT_REPORTS_DIR = Path.home() / ".kimi-code" / "task-reports"
DEFAULT_STATE_DIR = Path.home() / ".kimi-code" / "agent-task-watch"
DEFAULT_CONFIG = Path("/root/CcCompanion/apns-server/config.toml")
DEFAULT_SECRET_FILE = Path.home() / ".ots" / "secret"
DEFAULT_ENDPOINT = "http://127.0.0.1:8291/chat/append"
DEFAULT_POINTERS = [
    Path("/root/CcCompanion/apns-server/tokens/kimi_acp_session.json"),
    Path("/root/CcCompanion/apns-server/tokens/kimi_web_session.json"),
]

MAX_BODY_CHARS = 480
BANNER_HINT_CHARS = 80  # push.py 里 banner 正文只取前 80 字, 重要信息放最前


def log(state_dir: Path, msg: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n"
    try:
        with (state_dir / "watch.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def load_secret(config_path: Path, secret_file: Path) -> str:
    """从 config.toml 提取 [server].shared_secret; 失败回退 ~/.ots/secret。不打印。"""
    try:
        text = config_path.read_text(encoding="utf-8")
        m = re.search(r'^\s*shared_secret\s*=\s*["\']([^"\']+)["\']', text, re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except OSError:
        pass
    try:
        s = secret_file.read_text(encoding="utf-8").strip()
        if s:
            return s
    except OSError:
        pass
    return ""


def load_active_session_ids(pointer_paths: list[Path]) -> set[str]:
    """读取 CcCompanion 的 kimi 会话指针, 返回当前仍活跃 (未被 forge 遗弃) 的会话集合。"""
    active: set[str] = set()
    for p in pointer_paths:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            sid = str(payload.get("session_id") or "").strip()
            if sid:
                active.add(sid)
        except (OSError, ValueError):
            continue
    return active


def load_delivered(delivered_file: Path) -> dict:
    try:
        data = json.loads(delivered_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_delivered(delivered_file: Path, delivered: dict) -> None:
    delivered_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = delivered_file.with_name(f".{delivered_file.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(delivered, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(delivered_file))


def scan_terminal_tasks(sessions_root: Path, max_age_days: float) -> list[dict]:
    """返回终态任务列表 (按 endedAt 升序), 每条带 session/agent/task 上下文。"""
    found: list[dict] = []
    cutoff_ms = (time.time() - max_age_days * 86400) * 1000
    for task_file in sessions_root.glob("wd_*/session_*/agents/*/tasks/*.json"):
        try:
            task = json.loads(task_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(task, dict):
            continue
        if str(task.get("kind") or "") not in NOTIFY_KINDS:
            continue
        status = str(task.get("status") or "")
        if status not in TERMINAL_STATUSES:
            continue
        ended = task.get("endedAt")
        if not isinstance(ended, (int, float)):
            ended = task_file.stat().st_mtime * 1000
        if ended < cutoff_ms:
            continue
        parts = task_file.parts
        # .../wd_<ws>/session_<uuid>/agents/<agent>/tasks/<task>.json
        session_id = parts[-5] if len(parts) >= 5 else ""
        agent_id = str(task.get("agentId") or (parts[-3] if len(parts) >= 3 else ""))
        found.append({
            "key": f"{session_id}/{task.get('taskId') or task_file.stem}",
            "task_id": str(task.get("taskId") or task_file.stem),
            "session_id": session_id,
            "agent_id": agent_id,
            "status": status,
            "description": str(task.get("description") or "").strip(),
            "started_at": task.get("startedAt"),
            "ended_at": ended,
            "stop_reason": str(task.get("stopReason") or task.get("stop_reason") or ""),
        })
    found.sort(key=lambda t: t["ended_at"])
    return found


def report_highlight(reports_dir: Path, task: dict, limit: int = 160) -> str:
    """优先取 task-reports/<taskId>.md 首段 / index.jsonl 结论, 都没有则空串。"""
    if reports_dir.is_dir():
        for cand in (reports_dir / f"{task['task_id']}.md",):
            try:
                if cand.is_file():
                    for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                        line = line.strip().lstrip("#").strip()
                        if line:
                            return line[:limit]
            except OSError:
                pass
        index = reports_dir / "index.jsonl"
        try:
            if index.is_file():
                for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
                    if task["task_id"] not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    concl = str(rec.get("conclusion") or "").strip()
                    if concl:
                        return concl[:limit]
        except OSError:
            pass
    return ""


def format_duration(started, ended) -> str:
    if not isinstance(started, (int, float)) or not isinstance(ended, (int, float)):
        return ""
    secs = max(0, int((ended - started) / 1000))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


def build_message(task: dict, orphaned: bool, reports_dir: Path) -> str:
    status_cn = {"completed": "完成", "failed": "失败", "stopped": "已停止",
                 "killed": "已停止", "timed_out": "超时终止"}.get(
        task["status"], task["status"])
    session_state = "会话已被替换(通知本已丢失)" if orphaned else "会话仍活跃"
    head = f"[Kimi后台任务{status_cn}] {task['description'][:120] or task['task_id']}"
    parts = [head]
    detail = f"状态 {task['status']}"
    dur = format_duration(task["started_at"], task["ended_at"])
    if dur:
        detail += f" · 耗时 {dur}"
    if task["stop_reason"]:
        detail += f" · {task['stop_reason'][:60]}"
    parts.append(detail)
    parts.append(f"会话 {task['session_id'][:20]}… · {session_state} · agent {task['agent_id']}")
    highlight = report_highlight(reports_dir, task)
    if highlight:
        parts.append(f"报告要点: {highlight}")
    msg = "\n".join(parts)
    return msg[:MAX_BODY_CHARS]


def push_via_chat_append(endpoint: str, secret: str, text: str, timeout: float = 8.0) -> bool:
    """POST /chat/append (contact_id=kimi, role=assistant)。

    push.py 侧效果: 写入 Kimi 聊天历史 + 异步 APNs banner
    (_send_chat_notification, 标题 "Cc", 正文取前 80 字)。
    """
    payload = json.dumps({
        "contact_id": "kimi",
        "role": "assistant",
        "text": text,
        "source": "agent-task-watch",
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "X-Auth-Token": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048)
            if resp.status != 200:
                return False
            try:
                return bool(json.loads(body).get("ok"))
            except ValueError:
                return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Kimi 后台任务跨会话完成通知守望者")
    ap.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS_ROOT)
    ap.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--delivered-file", type=Path, default=None,
                    help="默认 <state-dir>/delivered.json")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET_FILE)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--pointer", type=Path, action="append", default=None,
                    help="活跃会话指针文件, 可重复; 默认读 CcCompanion 两个 kimi 指针")
    ap.add_argument("--max-age-days", type=float, default=14.0,
                    help="只通知最近 N 天内结束的任务 (默认 14, 防首轮刷屏)")
    ap.add_argument("--max-pushes", type=int, default=5,
                    help="每轮最多推送条数, 剩余的留下轮 (默认 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只扫描和输出去重判定, 不真的推送、不更新 delivered 记录")
    ap.add_argument("--prime", action="store_true",
                    help="首轮预热: 把现有全部终态任务标记为已送达 (不推送), 避免上线刷屏")
    args = ap.parse_args()

    state_dir: Path = args.state_dir
    delivered_file = args.delivered_file or (state_dir / "delivered.json")
    pointer_paths = args.pointer if args.pointer else DEFAULT_POINTERS

    # 防 cron 重叠: 非阻塞锁, 抢不到就静默退出
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(state_dir / "watch.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    # 首轮预热: 不看年龄上限, 把所有现存终态任务标记为已送达, 一条都不推。
    if args.prime:
        backlog = scan_terminal_tasks(args.sessions_root, max_age_days=36500.0)
        delivered = load_delivered(delivered_file)
        now = int(time.time())
        for t in backlog:
            delivered.setdefault(t["key"], {
                "status": t["status"],
                "delivered_at": now,
                "primed": True,
            })
        save_delivered(delivered_file, delivered)
        log(state_dir, f"primed: {len(backlog)} terminal task(s) marked delivered, 0 pushed")
        print(f"primed {len(backlog)} terminal task(s) into {delivered_file}")
        return 0

    tasks = scan_terminal_tasks(args.sessions_root, args.max_age_days)
    delivered = load_delivered(delivered_file)
    active_sessions = load_active_session_ids(pointer_paths)

    pending = [t for t in tasks if t["key"] not in delivered]
    if not pending:
        log(state_dir, f"scan ok: {len(tasks)} terminal, 0 pending")
        return 0

    secret = "" if args.dry_run else load_secret(args.config, args.secret_file)
    if not args.dry_run and not secret:
        # 无凭证: 不标记 delivered, 下轮重试
        log(state_dir, f"no shared_secret available; {len(pending)} pending deferred")
        return 0

    pushed, failed = 0, 0
    for task in pending[: max(args.max_pushes, 1)]:
        orphaned = task["session_id"] not in active_sessions
        msg = build_message(task, orphaned, args.reports_dir)
        if args.dry_run:
            print(f"DRY-RUN would push ({'orphaned' if orphaned else 'active'}):\n{msg}\n---")
            pushed += 1
            continue
        if push_via_chat_append(args.endpoint, secret, msg):
            delivered[task["key"]] = {
                "status": task["status"],
                "delivered_at": int(time.time()),
                "orphaned": orphaned,
            }
            pushed += 1
        else:
            failed += 1  # 不标记, 下轮静默重试

    if not args.dry_run and pushed:
        save_delivered(delivered_file, delivered)
    remaining = len(pending) - pushed - failed
    log(state_dir,
        f"scan ok: {len(tasks)} terminal, {len(pending)} pending, "
        f"pushed={pushed} failed={failed} remaining={remaining} dry_run={args.dry_run}")
    if remaining > 0:
        print(f"note: {remaining} pending task(s) deferred to next run (max-pushes)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 守望者绝不能炸到调用方 / 线上服务
        try:
            log(DEFAULT_STATE_DIR, f"fatal: {exc!r}")
        except Exception:
            pass
        sys.exit(0)
