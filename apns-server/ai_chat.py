"""
AI Chat module — manage chat sessions with a custom AI character
via any OpenAI-compatible chat completions API.

Config:  state/ai_chat_config.json
History: state/ai_chat_history.jsonl   (one JSON line per message)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG: dict[str, Any] = {
    "api_url": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "",
    "model": "deepseek-chat",
    "system_prompt": "",
    "nickname": "AI",
    "contact_id": "ai-custom",
    "max_context_messages": 20,
    "enabled": False,
    "memory_enabled": True,
    "memory_mcp_url": "https://memory.xiaonancaleb.xyz/mcp",
    "memory_category": "xiayizhou",
    "memory_max_results": 5,
}


class AIChatManager:
    """Thread-safe AI chat manager with JSONL history and OpenAI-compat API calls."""

    def __init__(self, state_dir: str | Path):
        self._state_dir = Path(state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._config_path = self._state_dir / "ai_chat_config.json"
        self._history_path = self._state_dir / "ai_chat_history.jsonl"
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._config: dict[str, Any] = dict(_DEFAULT_CONFIG)
        self._load_config()

    # ---- config ----

    def _load_config(self) -> None:
        if self._config_path.exists():
            try:
                with self._config_path.open("r", encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    filtered = {k: v for k, v in stored.items() if k in _DEFAULT_CONFIG}
                    for k in ("api_url", "memory_mcp_url"):
                        url = filtered.get(k, "")
                        if url and urlparse(str(url)).scheme != "https":
                            filtered.pop(k, None)
                    self._config.update(filtered)
            except Exception:
                logger.exception("ai_chat: failed to load config, using defaults")

    def _save_config(self) -> None:
        tmp = self._config_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
        tmp.replace(self._config_path)
        try:
            os.chmod(self._config_path, 0o600)
        except OSError:
            pass

    def get_config(self, mask_key: bool = True) -> dict[str, Any]:
        """Return config dict.  When *mask_key* is True the api_key is masked."""
        cfg = dict(self._config)
        if mask_key and cfg.get("api_key"):
            key = cfg["api_key"]
            if len(key) > 8:
                cfg["api_key"] = key[:4] + "****" + key[-4:]
            else:
                cfg["api_key"] = "****"
        return cfg

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("https",):
            raise ValueError(f"api_url must use https, got {parsed.scheme!r}")
        if not parsed.hostname:
            raise ValueError("api_url has no hostname")
        return url

    def update_config(self, partial: dict[str, Any]) -> dict[str, Any]:
        """Merge *partial* into current config and persist.  Returns masked config."""
        with self._lock:
            for k, v in partial.items():
                if k not in _DEFAULT_CONFIG:
                    continue
                if k == "api_url":
                    self._validate_url(str(v))
                    self._config[k] = str(v)
                elif k == "api_key":
                    self._config[k] = str(v)
                elif k == "model":
                    self._config[k] = str(v)[:200]
                elif k == "system_prompt":
                    self._config[k] = str(v)[:50000]
                elif k == "nickname":
                    self._config[k] = str(v)[:100]
                elif k == "contact_id":
                    self._config[k] = re.sub(r"[^a-z0-9_-]", "", str(v).lower())[:50] or "ai-custom"
                elif k == "max_context_messages":
                    self._config[k] = max(1, min(int(v), 200))
                elif k == "memory_max_results":
                    self._config[k] = max(0, min(int(v), 20))
                elif k in ("enabled", "memory_enabled"):
                    self._config[k] = bool(v)
                elif k == "memory_mcp_url":
                    self._validate_url(str(v))
                    self._config[k] = str(v)
                elif k == "memory_category":
                    self._config[k] = str(v)[:100]
            self._save_config()
        return self.get_config(mask_key=True)

    def set_system_prompt(self, prompt: str) -> dict[str, Any]:
        return self.update_config({"system_prompt": prompt})

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled"))

    @property
    def contact_id(self) -> str:
        return str(self._config.get("contact_id") or "ai-custom")

    @property
    def nickname(self) -> str:
        return str(self._config.get("nickname") or "AI")

    # ---- history ----

    def _append_history(self, role: str, text: str) -> str:
        """Append a message to the JSONL history file.  Returns the ISO ts."""
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        rec = {
            "ts": ts,
            "role": role,
            "text": text,
            "contact_id": self.contact_id,
        }
        with self._lock:
            with self._history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return ts

    def read_history(self, since: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Return history records, optionally filtered by *since* timestamp."""
        if not self._history_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._lock:
            with self._history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = rec.get("ts", "")
                    if since and ts <= since:
                        continue
                    out.append(rec)
        limit = max(1, min(int(limit), 10000))
        return out[-limit:]

    def _recent_messages(self, n: int) -> list[dict[str, str]]:
        """Return the last *n* messages formatted for the OpenAI messages array."""
        records = self.read_history(limit=n)
        return [{"role": r["role"], "content": r["text"]} for r in records]

    # ---- memory (via memory-mcp) ----

    def _fetch_memories(self, query: str) -> list[str]:
        """Semantic-search the memory-mcp for relevant memories. Returns list of text snippets."""
        if not self._config.get("memory_enabled"):
            return []
        mcp_url = self._config.get("memory_mcp_url", "")
        category = self._config.get("memory_category", "xiayizhou")
        limit = int(self._config.get("memory_max_results", 5))
        if not mcp_url:
            return []
        try:
            _mcp_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "ai-chat/1.0",
            }
            init_payload = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ai-chat", "version": "1.0"},
                },
            }).encode("utf-8")
            req = urllib.request.Request(mcp_url, data=init_payload, headers=_mcp_headers)
            with urllib.request.urlopen(req, timeout=10) as init_resp:
                init_resp.read()

            search_payload = json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "semantic_search",
                    "arguments": {"query": query, "limit": limit},
                },
            }).encode("utf-8")
            req2 = urllib.request.Request(mcp_url, data=search_payload, headers=_mcp_headers)
            with urllib.request.urlopen(req2, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body.get("result", {}).get("content", [])
            if not content:
                return []
            raw_text = content[0].get("text", "")
            memories_list = json.loads(raw_text) if raw_text.startswith("[") else []
            results = []
            for mem in memories_list:
                cat = mem.get("category", "")
                if category and cat != category:
                    continue
                text = mem.get("content", "")[:500]
                if text:
                    results.append(text)
            return results[:limit]
        except Exception:
            logger.debug("ai_chat: memory fetch failed", exc_info=True)
            return []

    # ---- API call ----

    def send_message(self, user_text: str) -> dict[str, Any]:
        """Send *user_text*, call the AI API, store both sides, return result dict.
        Serialized per-session to prevent interleaving."""
        if not self.enabled:
            return {"ok": False, "error": "ai chat is not enabled"}

        with self._send_lock:
            return self._send_message_locked(user_text)

    def _send_message_locked(self, user_text: str) -> dict[str, Any]:

        api_url = self._config.get("api_url", "")
        api_key = self._config.get("api_key", "")
        model = self._config.get("model", "")
        system_prompt = self._config.get("system_prompt", "")
        max_ctx = int(self._config.get("max_context_messages", 20))

        if not api_url or not api_key or not model:
            return {"ok": False, "error": "ai chat not configured (missing api_url / api_key / model)"}

        # Fetch relevant memories
        memories = self._fetch_memories(user_text)

        # Build messages array
        messages: list[dict[str, str]] = []
        if system_prompt:
            now = datetime.now(timezone.utc).astimezone()
            time_block = f"\n\n## 当前时间\n{now.strftime('%Y年%m月%d日 %H:%M %A')}"
            mem_block = ""
            if memories:
                mem_block = "\n\n## 相关记忆\n" + "\n---\n".join(memories)
            messages.append({"role": "system", "content": system_prompt + time_block + mem_block})
        messages.extend(self._recent_messages(max_ctx))
        messages.append({"role": "user", "content": user_text})

        # Store user message
        user_ts = self._append_history("user", user_text)

        # Call API
        try:
            reply_text = self._call_api(api_url, api_key, model, messages)
        except Exception as e:
            logger.exception("ai_chat: API call failed")
            return {"ok": False, "error": str(e), "ts": user_ts}

        # Store assistant reply
        reply_ts = self._append_history("assistant", reply_text)

        return {"ok": True, "reply": reply_text, "ts": reply_ts}

    def _call_api(
        self,
        api_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
    ) -> str:
        """POST to an OpenAI-compatible chat/completions endpoint.  Returns reply text."""
        payload = json.dumps({"model": model, "messages": messages}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            logger.warning("ai_chat: API HTTP %d", e.code)
            raise RuntimeError(f"API returned HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"API request failed: {e.reason}") from e

        choices = body.get("choices")
        if not choices or not isinstance(choices, list):
            raise RuntimeError(f"unexpected API response shape: {json.dumps(body, ensure_ascii=False)[:300]}")
        message = choices[0].get("message", {})
        content = str(message.get("content", "")).strip()
        if not content:
            raise RuntimeError("模型返回了空回复")
        return content
