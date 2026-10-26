"""企业微信 ↔ CC Companion 联系人桥（2026-10-26：卡拉米 kimi、kairos）。

入站：/root/wecom-channel 的回调服务按解密后的 AgentID 分流（WECOM_ROUTES），
companion 路由的消息 POST 到本服务 ``/wecom/inbound``（X-Wecom-Webhook-Secret，
0600 文件，fail-closed）。本服务把它当成方小南在 app 里给该联系人发了一条
``［💬微信］原文``：同入历史、同走该联系人的发送管线，metadata 带
``source=wecom`` + ``wecom={msg_id, agent_id, msg_type}``。卡拉米的图片由回调
服务下载到私有 media 目录，本服务校验并复制到附件库后复用 Kimi 图片附件输入。

出站：联系人这一轮的最终回复落历史时，*只有*它回答的那条用户消息带 wecom
标记，才写一个 outbox 文件到 wecom-channel 的 state/outbox/；由回调服务用
该联系人自己的应用（AgentID + Secret）发回微信。判定只看「这一轮回答的是哪条
用户消息」，与模型自觉无关；app 里正常聊的消息没有标记，永远只回 app。

小铃铛（主动推送，2026-10-26）：``send_bell(contact_id, text)`` 读同一个共享
密钥 POST 到回调服务的 ``/wecom/bell``，不等方小南先发消息就往她微信推文字。
收件人永远是白名单第一个 UserID（不能指定别人、不能批量），鉴权失败 fail-closed。

本模块只放纯函数与文件 I/O，便于单测；路由与管线接线在 push.py。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_PREFIX = "［💬微信］"
META_KEY = "wecom"
META_SOURCE = "wecom"

# 已接好「微信入站 + 最终回复回微信」的联系人。新增联系人要先在它自己的
# 发送管线里接上 outbox 钩子（push.py 的 _mirror_reply_to_wecom），再加进来。
INBOUND_CONTACTS = frozenset({"kimi", "kairos"})
_APP_LABEL = {"kimi": ("wecom_karami_app", "卡拉米"), "kairos": ("wecom_kairos_app", "kairos")}

SECRET_FILE_ENV = "CC_COMPANION_WECOM_WEBHOOK_SECRET_FILE"
DEFAULT_SECRET_FILE = Path("/root/.secrets/wecom_companion_webhook_secret")
OUTBOX_DIR_ENV = "CC_COMPANION_WECOM_OUTBOX_DIR"
DEFAULT_OUTBOX_DIR = Path("/root/wecom-channel/state/outbox")
MEDIA_DIR_ENV = "CC_COMPANION_WECOM_MEDIA_DIR"
DEFAULT_MEDIA_DIR = Path("/root/wecom-channel/state/media")
BELL_URL_ENV = "CC_COMPANION_WECOM_BELL_URL"
DEFAULT_BELL_URL = "http://127.0.0.1:8792/wecom/bell"

REQUEST_LIMIT = 16 * 1024
TEXT_LIMIT = 4000
OUTBOX_TEXT_LIMIT = 20000
IMAGE_MAX_BYTES = 30 * 1024 * 1024

_AGENT_RE = re.compile(r"^\d{1,12}$")
_MSG_TYPE_RE = re.compile(r"^[a-z_]{1,24}$")


def read_secret_file(path: Path) -> str:
    """Owner-only regular file, no symlink, bounded size; '' otherwise."""
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return ""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            raw = os.read(fd, 4096 + 1)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            return ""
        value = raw.decode("utf-8").strip()
        return value if 16 <= len(value) <= 4096 else ""
    except (OSError, UnicodeError):
        return ""


def webhook_secret() -> str:
    raw = os.environ.get(SECRET_FILE_ENV, "").strip()
    return read_secret_file(Path(raw).expanduser() if raw else DEFAULT_SECRET_FILE)


def outbox_dir() -> Path:
    raw = os.environ.get(OUTBOX_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_OUTBOX_DIR


def media_dir() -> Path:
    raw = os.environ.get(MEDIA_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_MEDIA_DIR


def bell_url() -> str:
    raw = os.environ.get(BELL_URL_ENV, "").strip()
    return raw if raw else DEFAULT_BELL_URL


def send_bell(contact_id: str, text: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """小铃铛：主动推一条文字到方小南的微信（经 wecom-channel /wecom/bell）。

    只发文字、只能发给有 companion 路由的联系人，收件人由回调服务固定为白名单
    第一个 UserID。共享密钥文件缺失或不是 0600 时 fail-closed（不发、抛错）。
    返回回调服务的 JSON；任何失败都抛 RuntimeError（消息里不含密钥）。
    """
    contact = str(contact_id or "").strip()
    if contact not in INBOUND_CONTACTS:
        raise ValueError(f"contact_not_supported: {contact or '(empty)'}")
    body_text = str(text or "").strip()
    if not body_text:
        raise ValueError("empty bell text")
    # 注意：超时 / 连接错误只代表「结果未知」——回调服务可能仍在排队发送，
    # 消息也许已经送达。失败后不要盲目重试，先确认微信侧再决定。
    secret = webhook_secret()
    if not secret:
        raise RuntimeError("wecom webhook secret file missing or not 0600; bell refused")
    payload = json.dumps({"contact": contact, "text": body_text[:OUTBOX_TEXT_LIMIT]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        bell_url(),
        data=payload,
        headers={"Content-Type": "application/json", "X-Wecom-Webhook-Secret": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("error") or "")
        except (OSError, ValueError, AttributeError):
            pass
        raise RuntimeError(f"bell HTTP {exc.code}: {detail or 'rejected'}") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"bell request failed: {type(exc).__name__}") from None
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("bell rejected: unexpected response")
    return result


def parse_inbound(payload: Any) -> tuple[dict[str, Any] | None, str]:
    """Validate a /wecom/inbound body -> (clean, "") or (None, error)."""
    if not isinstance(payload, dict):
        return None, "json object required"
    contact_id = str(payload.get("contact_id") or "").strip()
    if contact_id not in INBOUND_CONTACTS:
        return None, "contact_not_supported"
    agent_id = str(payload.get("agent_id") or "").strip()
    if not _AGENT_RE.match(agent_id):
        return None, "agent_id required"
    raw_id = payload.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
        return None, "id required"
    msg_type = str(payload.get("msg_type") or "text").strip().lower()
    if not _MSG_TYPE_RE.match(msg_type):
        msg_type = "unknown"
    media = payload.get("media")
    clean_media = None
    if contact_id == "kimi" and msg_type == "image" and isinstance(media, dict) and media.get("status") == "ok":
        raw_path = media.get("path")
        if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096 or "\0" in raw_path:
            return None, "invalid media path"
        clean_media = {"path": raw_path}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        if clean_media is None:
            return None, "text required"
        text = "（发来一张图片）"
    clean = {
        "contact_id": contact_id,
        "agent_id": agent_id,
        "msg_id": str(raw_id).strip()[:128],
        "msg_type": msg_type,
        "text": text.strip()[:TEXT_LIMIT],
    }
    if clean_media is not None:
        clean["media"] = clean_media
    return clean, ""


def stage_image(clean: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Copy one callback-downloaded image into the private attachment store."""
    media = clean.get("media")
    if clean.get("contact_id") != "kimi" or clean.get("msg_type") != "image" or not isinstance(media, dict):
        raise ValueError("not a Kimi image message")
    source = Path(str(media.get("path") or ""))
    if not source.is_absolute():
        raise ValueError("media path must be absolute")
    source_info = source.lstat()
    root = media_dir().resolve(strict=True)
    resolved = source.resolve(strict=True)
    if resolved.parent != root or stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(source_info.st_mode):
        raise ValueError("media path is outside the WeCom image store")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    tmp: Path | None = None
    try:
        opened = os.fstat(source_fd)
        if (
            opened.st_dev != source_info.st_dev
            or opened.st_ino != source_info.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > IMAGE_MAX_BYTES
        ):
            raise ValueError("invalid image size or file type")
        header = os.read(source_fd, 12)
        if header.startswith(b"\xff\xd8\xff"):
            extension, media_type = ".jpg", "image/jpeg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            extension, media_type = ".png", "image/png"
        elif header.startswith((b"GIF87a", b"GIF89a")):
            extension, media_type = ".gif", "image/gif"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            extension, media_type = ".webp", "image/webp"
        else:
            raise ValueError("unsupported image bytes")
        target_dir = destination.resolve(strict=True)
        stored_name = f"wecom-{secrets.token_hex(16)}{extension}"
        final = target_dir / stored_name
        tmp = target_dir / f".{stored_name}.tmp-{os.getpid()}"
        target_fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(target_fd, "wb") as handle:
            handle.write(header)
            remaining = opened.st_size - len(header)
            while remaining:
                chunk = os.read(source_fd, min(remaining, 1024 * 1024))
                if not chunk:
                    raise OSError("image changed while being copied")
                handle.write(chunk)
                remaining -= len(chunk)
            if os.read(source_fd, 1):
                raise OSError("image changed while being copied")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final)
        tmp = None
    finally:
        os.close(source_fd)
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    return {
        "attachment_url": f"/attachments/{stored_name}",
        "filename": f"wecom-image{extension}",
        "type": "image",
        "media_type": media_type,
        "size": opened.st_size,
        "stored_path": str(final),
    }


def inbound_body(clean: dict[str, Any], attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The internal send body handed to the contact's pipeline."""
    body = {
        "text": f"{USER_PREFIX}{clean['text']}",
        "metadata": {
            "source": META_SOURCE,
            META_KEY: {
                "msg_id": clean["msg_id"],
                "agent_id": clean["agent_id"],
                "msg_type": clean["msg_type"],
            },
        },
    }
    if attachments:
        body["_pwa_staged_attachments"] = attachments
    return body


def origin(record: Any) -> dict[str, Any] | None:
    """The wecom marker of a *user* history record, or None (= app message)."""
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("source") != META_SOURCE:
        return None
    marker = metadata.get(META_KEY)
    if not isinstance(marker, dict) or not _AGENT_RE.match(str(marker.get("agent_id") or "")):
        return None
    return marker


def prompt_entry_lines(contact_id: str) -> list[str]:
    """Replaces the app-entry lines of a contact's prompt for a WeChat turn."""
    entry, label = _APP_LABEL.get(contact_id, (f"wecom_{contact_id}_app", contact_id))
    return [
        f"入口: {entry}（个人微信 → 企业微信「{label}」应用）",
        f"contact_id: {contact_id}",
        "",
        "Astra 这条是在个人微信里通过企业微信插件发给你的（消息开头的［💬微信］是来源标记，不是她打的字）。"
        "你的回复会同时进 app 和她的微信：微信只显示纯文本，不要用 Markdown（不要 **加粗**、标题、表格、代码块）；"
        "空一行 = 微信里另起一个气泡，通常 1-4 个气泡。请直接回复她，不要提到后台路由。",
    ]


def write_outbox(
    *,
    contact_id: str,
    marker: dict[str, Any],
    text: str,
    user_ts: str = "",
    reply_ts: str = "",
    directory: Path | None = None,
) -> Path:
    """Atomically drop one reply item for the wecom-channel callback service."""
    body = str(text or "").strip()
    if not body:
        raise ValueError("empty reply")
    target = directory or outbox_dir()
    if not target.is_dir():
        raise FileNotFoundError(f"wecom outbox directory missing: {target}")
    now = time.time()
    item = {
        "kind": "reply",
        "contact_id": contact_id,
        "agent_id": str(marker.get("agent_id") or ""),
        "in_reply_to": str(marker.get("msg_id") or ""),
        "user_ts": str(user_ts or ""),
        "reply_ts": str(reply_ts or ""),
        "text": body[:OUTBOX_TEXT_LIMIT],
        "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    name = f"{int(now * 1000):014d}-{secrets.token_hex(4)}.json"
    final = target / name
    tmp = target / f"{name}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.write(fd, (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, final)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return final
