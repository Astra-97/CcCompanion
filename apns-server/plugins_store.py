"""插件系统服务端 MVP (2026-09-25 Astra 拍板).

目录布局:
- 内置插件 (入库):        apns-server/plugins-builtin/<id>/  manifest.json + 静态文件
- 运行时插件/数据 (gitignore): apns-server/plugins/<id>/      同名 id 覆盖内置;
                          数据文档存 plugins/<id>/data/<doc>.json
解析顺序: 数据目录优先, 内置目录回落。目录名以 "_" 或 "." 开头的一律不算插件
(_template 骨架因此不会被列出也不会被托管)。

manifest.json 字段: id (必须等于目录名) / name / version / description / entry
(入口 html, 默认 index.html)。

KV 协议 (单文档, 每插件隔离):
- GET  /plugins/<id>/data/<doc>  -> {"ok", "doc", "version", "body", "updated_at"}
- PUT  /plugins/<id>/data/<doc>  必须带 If-Match: <int>:
  - If-Match: 0  仅当文档不存在时创建 (新建语义);
  - If-Match: N  仅当当前 version == N 时写入, 成功后 version = N+1;
  - 版本不符 -> 409 {"error": "version_conflict", "version", "body"} 调用方重读合并重试;
  - 缺/坏 If-Match -> 428。
- 落盘 {"version": int, "body": <任意 JSON>, "updated_at": iso}, 全局锁 + .tmp/replace 原子写。

scoped token (插件直连 fetch 的备用通道, 主链路是 App 原生 bridge 代发):
- 签发: POST /plugins/<id>/token, 仅在 shared_secret 强鉴权 (native pairing 闸门,
  fail-closed) 后调用, web session cookie 无权签发;
- 格式: p1.<b64url(payload)>.<hmac-sha256-hex>, payload = {"v", "pid", "exp", "n"},
  以 shared_secret 为 key 无状态签名 (HMAC 单向, 不泄露 secret 本身);
- 校验: 仅在 /plugins/<pid>/data/* 的 GET/PUT 处理器里检查, pid 必须匹配,
  因此对任何其他端点天然无效。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)

PLUGIN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
DOC_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# 插件静态托管允许的格式; 与 CSP default-src 'self' 配套 (禁远程脚本/样式/字体)。
STATIC_SUFFIXES = {
    ".html", ".htm", ".js", ".mjs", ".css", ".json", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".txt", ".md", ".woff", ".woff2",
}
STATIC_MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

MAX_DOC_BODY_BYTES = 256 * 1024
TOKEN_TTL_DEFAULT_SECONDS = 7 * 86400
TOKEN_TTL_MAX_SECONDS = 30 * 86400
_TOKEN_SIGN_CONTEXT = "cc-plugin-scope"


class PluginStore:
    """插件清单 / 静态文件解析 / 单文档 KV / scoped token。线程安全。"""

    def __init__(self, data_dir: str | Path, builtin_dir: str | Path | None = None):
        self.data_dir = Path(data_dir).expanduser()
        self.builtin_dir = Path(builtin_dir).expanduser() if builtin_dir else None
        self._lock = threading.Lock()

    # ---------- 清单 ----------

    def _plugin_root(self, plugin_id: str) -> Path | None:
        """数据目录优先, 内置目录回落; 无 manifest 的目录不算插件。"""
        if not PLUGIN_ID_RE.fullmatch(str(plugin_id or "")):
            return None
        for base in (self.data_dir, self.builtin_dir):
            if base is None:
                continue
            root = base / plugin_id
            try:
                if root.is_dir() and (root / "manifest.json").is_file():
                    return root
            except OSError:
                continue
        return None

    def _read_manifest(self, root: Path) -> dict[str, Any] | None:
        try:
            data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        plugin_id = str(data.get("id") or "")
        if plugin_id != root.name or not PLUGIN_ID_RE.fullmatch(plugin_id):
            return None
        entry = str(data.get("entry") or "index.html")
        if not self._safe_static_relpath(entry):
            entry = "index.html"
        return {
            "id": plugin_id,
            "name": str(data.get("name") or plugin_id),
            "version": str(data.get("version") or "0.0.0"),
            "description": str(data.get("description") or ""),
            "entry": entry,
        }

    def load_manifest(self, plugin_id: str) -> dict[str, Any] | None:
        root = self._plugin_root(plugin_id)
        if root is None:
            return None
        manifest = self._read_manifest(root)
        if manifest is not None:
            manifest["builtin"] = self.builtin_dir is not None and root.parent == self.builtin_dir
        return manifest

    def list_plugins(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for base, builtin_flag in ((self.data_dir, False), (self.builtin_dir, True)):
            if base is None:
                continue
            try:
                children = sorted(base.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(("_", ".")) or child.name in seen:
                    continue
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                manifest = self._read_manifest(child)
                if manifest is None:
                    continue
                manifest["builtin"] = builtin_flag
                seen[child.name] = manifest
        return list(seen.values())

    # ---------- 静态文件 ----------

    @staticmethod
    def _safe_static_relpath(rel_path: str) -> bool:
        if not rel_path or "\\" in rel_path:
            return False
        parts = rel_path.split("/")
        return all(part not in {"", ".", ".."} for part in parts)

    def resolve_static(self, plugin_id: str, rel_path: str) -> Path | None:
        """解析插件静态文件, 路径穿越/符号链接/非常规文件一律 None。"""
        root = self._plugin_root(plugin_id)
        if root is None:
            return None
        rel = unquote(str(rel_path or ""))
        if not rel:
            manifest = self._read_manifest(root)
            rel = str((manifest or {}).get("entry") or "index.html")
        if not self._safe_static_relpath(rel):
            return None
        try:
            base = root.resolve(strict=True)
            candidate = base.joinpath(*rel.split("/"))
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return None
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(base):
                return None
            if resolved.suffix.lower() not in STATIC_SUFFIXES:
                return None
            return resolved
        except (OSError, ValueError):
            return None

    @staticmethod
    def static_mime(path: Path) -> str:
        return STATIC_MIME_MAP.get(path.suffix.lower(), "application/octet-stream")

    # ---------- 单文档 KV ----------

    def _doc_path(self, plugin_id: str, doc: str) -> Path | None:
        if not PLUGIN_ID_RE.fullmatch(str(plugin_id or "")):
            return None
        if not DOC_NAME_RE.fullmatch(str(doc or "")):
            return None
        if self._plugin_root(plugin_id) is None:
            return None
        path = self.data_dir / plugin_id / "data" / f"{doc}.json"
        try:
            resolved_data = self.data_dir.resolve()
            if not path.resolve().is_relative_to(resolved_data):
                return None
        except (OSError, ValueError):
            return None
        return path

    @staticmethod
    def _read_doc_file(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("plugin kv doc unreadable, treated as missing: %s", path)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("version"), int):
            return None
        return data

    def read_doc(self, plugin_id: str, doc: str) -> dict[str, Any] | None:
        """返回 {"version", "body", "updated_at"}; 文档或插件不存在返回 None。"""
        path = self._doc_path(plugin_id, doc)
        if path is None:
            return None
        with self._lock:
            record = self._read_doc_file(path)
        if record is None:
            return None
        return {
            "version": record["version"],
            "body": record.get("body"),
            "updated_at": str(record.get("updated_at") or ""),
        }

    def write_doc(
        self,
        plugin_id: str,
        doc: str,
        body: Any,
        expected_version: int,
    ) -> tuple[str, dict[str, Any]]:
        """乐观并发写。返回 (status, info):

        - ("ok", {"version": N+1})
        - ("conflict", {"version": 当前版本, "body": 当前内容}) 调用方重读合并重试
        - ("not_found", {}) 插件或文档名非法
        """
        path = self._doc_path(plugin_id, doc)
        if path is None:
            return "not_found", {}
        with self._lock:
            current = self._read_doc_file(path)
            cur_version = int(current["version"]) if current else 0
            if int(expected_version) != cur_version:
                return "conflict", {
                    "version": cur_version,
                    "body": (current or {}).get("body"),
                }
            new_version = cur_version + 1
            record = {
                "version": new_version,
                "body": body,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
            tmp.replace(path)
        return "ok", {"version": new_version}

    # ---------- scoped token ----------

    def mint_scoped_token(
        self,
        plugin_id: str,
        secret: str,
        ttl_seconds: int | None = None,
        *,
        now: float | None = None,
    ) -> tuple[str, int]:
        """签发插件级 token。返回 (token, expires_at_epoch)。调用方负责强鉴权闸门。"""
        if self._plugin_root(plugin_id) is None:
            raise ValueError(f"unknown plugin: {plugin_id}")
        if not secret:
            raise ValueError("shared_secret required to mint plugin tokens")
        ttl = int(ttl_seconds or TOKEN_TTL_DEFAULT_SECONDS)
        ttl = max(60, min(ttl, TOKEN_TTL_MAX_SECONDS))
        exp = int(now if now is not None else time.time()) + ttl
        payload = {"v": 1, "pid": plugin_id, "exp": exp, "n": secrets.token_hex(8)}
        raw = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        sig = hmac.new(
            str(secret).encode("utf-8"),
            f"{_TOKEN_SIGN_CONTEXT}.{raw}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"p1.{raw}.{sig}", exp

    def verify_scoped_token(
        self,
        token: str,
        plugin_id: str,
        secret: str,
        *,
        now: float | None = None,
    ) -> bool:
        """校验 scoped token: 签名/格式/过期/pid 匹配, 全部 fail-closed。"""
        if not token or not secret or not plugin_id:
            return False
        parts = str(token).split(".")
        if len(parts) != 3 or parts[0] != "p1":
            return False
        raw, sig = parts[1], parts[2]
        expected = hmac.new(
            str(secret).encode("utf-8"),
            f"{_TOKEN_SIGN_CONTEXT}.{raw}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            padded = raw + "=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, binascii.Error, UnicodeError):
            return False
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return False
        if str(payload.get("pid") or "") != str(plugin_id):
            return False
        try:
            exp = int(payload.get("exp"))
        except (TypeError, ValueError):
            return False
        return exp >= int(now if now is not None else time.time())
