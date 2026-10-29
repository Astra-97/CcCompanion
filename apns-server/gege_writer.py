"""哥哥写手 (contact ``gege``, 2026-10-29): text commands and reply wording.

Pure helpers for push.py's gege pipeline.  The writer itself is pi on Tokyo
(``pi_rpc.PiRPCClient``); archives / roll / undo / context settings are pi's
``gege`` extension, reached as ``/gege <sub>``.  Here:

* ``parse_command``  — 方小南's text (app or WeChat, ［💬微信］ prefix already
  stripped) -> ``(command, argument)`` or ``None`` for a normal writing turn.
  WeChat menu buttons of app 1000007 arrive as the same texts (/roll, /undo,
  /list), so buttons and typed commands share one path.
* ``format_*``       — plain-text answers (WeChat shows no Markdown; one blank
  line = one bubble).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

CONTACT_ID = "gege"
DISPLAY_NAME = "哥哥写手"
WECOM_PREFIX = "［💬微信］"

AUTH_REQUIRED_TEXT = (
    "哥哥写手还没接上写作账号（后端的登录或密钥还没配好），这条我先记下了。\n\n"
    "等小克把账号接上，再发一次「继续」或者重新发要求就能写。"
)
NOT_CONFIGURED_TEXT = "哥哥写手的后台还没配置好（服务器上没有设置 gege_ssh_host），这条先记下了，等小克接上。"
BUSY_TEXT = "哥哥写手那边还在整理上下文，等一两分钟再发一次就好。"
ERROR_TEXT = "这次没写出来（写作后端出错了），原话已经记下。过一会儿发「/roll」或者再发一次要求就能重写。"
EMPTY_TEXT = "这次没有写出内容，发「/roll」可以重写一次。"

HELP_TEXT = (
    "哥哥写手的命令（直接发这些字就行）：\n\n"
    "/new 标题 —— 新开一篇（上一篇自动存档）\n"
    "/list —— 列出所有存档，回编号就能切换\n"
    "/open 编号 —— 切换到某一篇\n"
    "/roll —— 不满意上一段，用同样的要求重写（旧版本留在存档里）\n"
    "/undo —— 撤回上一轮（你的那句和那段文都从当前篇拿掉）\n"
    "/rename 标题 —— 给当前篇改名\n"
    "/info —— 当前篇的信息和上下文设置\n\n"
    "/set 窗口 300k、/set 保留 100k、/set 总结 20k —— 改当前篇的上下文：写满「窗口」时，最近「保留」那么多原文不动，更早的压成小总结；小总结攒过「总结」就合成大总结\n"
    "/set 长篇 —— 一键 300k 窗口 / 100k 保留；/set 默认 —— 恢复默认"
)

_NEW_RE = re.compile(r"^(?:/new|/新开|新开一篇|新开)(?:\s+(?P<title>.+))?$", re.S)
_OPEN_RE = re.compile(r"^(?:/open|/打开|打开|切换到?|/切换)\s*第?(?P<n>\d{1,4})篇?$")
_SET_RE = re.compile(r"^/set\s+(?P<rest>.+)$", re.S)
_RENAME_RE = re.compile(r"^(?:/rename|改名)\s+(?P<title>.+)$", re.S)
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

_SIMPLE = {
    "/list": "list", "/ls": "list", "/存档": "list", "存档": "list", "列出存档": "list", "所有存档": "list",
    "/roll": "roll", "/重roll": "roll", "重roll": "roll", "重新roll": "roll", "重写": "roll", "重写这段": "roll",
    "/undo": "undo", "/撤回": "undo", "撤回": "undo", "撤回上一轮": "undo",
    "/info": "info", "/status": "info", "/状态": "info",
    "/help": "help", "/帮助": "help", "帮助": "help", "/命令": "help",
}


def strip_source_prefix(text: str) -> str:
    value = str(text or "").strip()
    while value.startswith(WECOM_PREFIX):
        value = value[len(WECOM_PREFIX):].strip()
    return value


def parse_command(text: str, *, list_pending: bool = False) -> tuple[str, str] | None:
    """Command for a whole message, or None (= write)."""
    value = strip_source_prefix(text).translate(_FULLWIDTH_DIGITS)
    if not value or len(value) > 200:
        return None
    lowered = value.lower()
    if lowered in _SIMPLE:
        return _SIMPLE[lowered], ""
    match = _NEW_RE.match(value)
    if match:
        return "new", re.sub(r"\s+", " ", (match.group("title") or "").strip())[:60]
    match = _OPEN_RE.match(value)
    if match:
        return "open", str(int(match.group("n")))
    match = _SET_RE.match(value)
    if match:
        return "set", re.sub(r"\s+", " ", match.group("rest").strip())[:80]
    match = _RENAME_RE.match(value)
    if match:
        return "rename", re.sub(r"\s+", " ", match.group("title").strip())[:60]
    if list_pending and re.fullmatch(r"\d{1,4}", value):
        return "open", str(int(value))
    return None


def _k(tokens: Any) -> str:
    try:
        value = int(tokens)
    except (TypeError, ValueError):
        return "?"
    if value >= 10_000:
        return f"{round(value / 1000)}k"
    if value >= 1000:
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(value)


def _bj(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (moment.astimezone(timezone(timedelta(hours=8)))).strftime("%m-%d %H:%M")


def archive_title(archive: dict[str, Any]) -> str:
    name = str(archive.get("name") or "").strip()
    if name:
        return f"《{name}》"
    first = str(archive.get("first") or "").strip()
    return f"《{first[:16]}…》" if first else "《未命名》"


def settings_line(settings: dict[str, Any]) -> str:
    settings = settings or {}
    return (
        f"窗口 {_k(settings.get('window'))}，保留原文 {_k(settings.get('keep'))}，"
        f"小总结上限 {_k(settings.get('summary'))}"
    )


def format_list(archives: list[dict[str, Any]]) -> str:
    if not archives:
        return "还没有存档。发「/new 标题」开第一篇。"
    lines = ["存档（回编号切换）："]
    for item in archives[-30:]:
        mark = " ← 当前" if item.get("current") else ""
        turns = max(0, int(item.get("messages") or 0))
        lines.append(f"{item.get('n')}. {archive_title(item)} {_bj(item.get('modified') or '')}，{turns} 条{mark}")
    if len(archives) > 30:
        lines.insert(1, f"（共 {len(archives)} 篇，只列最近 30 篇）")
    return "\n".join(lines)


def _tail(archive: dict[str, Any]) -> str:
    tail = str(archive.get("last_tail") or "").strip()
    if not tail:
        return ""
    tail = re.sub(r"\n\s*\n", "\n", tail)
    return f"\n\n上次写到：\n…{tail[-120:]}"


def format_result(result: dict[str, Any]) -> str:
    """Reply text for a /gege command answer (not for roll's regenerate)."""
    cmd = str(result.get("cmd") or "")
    archive = result.get("archive") if isinstance(result.get("archive"), dict) else {}
    if not result.get("ok"):
        error = str(result.get("error") or "")
        if cmd == "open" and error == "no_such_archive":
            return f"没有这个编号（现在共 {result.get('count', 0)} 篇），发「/list」看看。"
        if cmd == "roll" and error in ("nothing_to_roll", "no_reply_yet"):
            return "当前这篇还没有可以重写的段落。"
        if cmd == "undo" and error == "nothing_to_undo":
            return "当前这篇已经没有可以撤回的了。"
        if cmd == "set":
            if error == "bad_args":
                return "格式不对。例子：/set 窗口 300k、/set 保留 100k、/set 总结 20k、/set 长篇、/set 默认\n\n现在是：" + settings_line(result.get("settings") or {})
            return f"没改成：{error}\n\n现在是：" + settings_line(result.get("settings") or {})
        if cmd == "rename":
            return "格式：/rename 新标题"
        return f"没办成（{error or '未知原因'}）。"
    if cmd == "new":
        return f"新开了一篇{archive_title(archive) if archive.get('name') else ''}，上一篇已经存好。想写什么？\n\n（{settings_line(archive.get('settings') or {})}）"
    if cmd == "open":
        prefix = "本来就在这篇" if result.get("already") else "已切换到"
        return f"{prefix}{archive_title(archive)}，共 {archive.get('turns', 0)} 轮。{_tail(archive)}"
    if cmd == "undo":
        said = str(result.get("text") or "").strip().replace("\n", " ")
        said = strip_source_prefix(said)
        return f"撤回了上一轮（你说的「{said[:30]}{'…' if len(said) > 30 else ''}」和那段文），存档里还留着旧版本。{_tail(archive)}"
    if cmd == "set":
        return f"当前篇已改为：{settings_line(result.get('settings') or {})}"
    if cmd == "rename":
        return f"当前篇改名为{archive_title(archive)}。"
    if cmd == "info":
        used = archive.get("context_tokens")
        used_text = f"，现在上下文约 {_k(used)} token" if used else ""
        model = str(archive.get("model") or "未配置")
        return (
            f"当前篇{archive_title(archive)}，共 {archive.get('turns', 0)} 轮{used_text}。\n"
            f"{settings_line(archive.get('settings') or {})}\n模型：{model}"
        )
    return "好了。"
