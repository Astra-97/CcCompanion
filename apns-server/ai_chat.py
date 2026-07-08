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

    def _append_history(self, role: str, text: str, thinking: str = "", **extra: Any) -> str:
        """Append a message to the JSONL history file.  Returns the ISO ts."""
        ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        rec = {
            "ts": ts,
            "role": role,
            "text": text,
            "contact_id": self.contact_id,
        }
        if thinking:
            rec["thinking"] = thinking
        for key in (
            "client_message_id",
            "attachment_url",
            "attachment_type",
            "attachment_filename",
            "image",
            "files",
        ):
            value = extra.get(key)
            if value:
                rec[key] = value
        with self._lock:
            with self._history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return ts

    def _find_client_message_result(self, client_message_id: str) -> dict[str, Any] | None:
        if not client_message_id:
            return None
        records = self.read_history(limit=1000)
        user_index = -1
        for i, rec in enumerate(records):
            if rec.get("role") == "user" and rec.get("client_message_id") == client_message_id:
                user_index = i
        if user_index < 0:
            return None
        for rec in records[user_index + 1:]:
            if rec.get("role") == "assistant" and rec.get("client_message_id") == client_message_id:
                result = {
                    "ok": True,
                    "duplicate": True,
                    "reply": rec.get("text", ""),
                    "ts": rec.get("ts", ""),
                }
                if rec.get("thinking"):
                    result["thinking"] = rec.get("thinking", "")
                return result
        return {"ok": False, "duplicate": True, "error": "duplicate client message already in progress"}

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

    # ---- models discovery ----

    def fetch_models(self, api_url: str = "", api_key: str = "") -> dict[str, Any]:
        """Fetch available models from an OpenAI-compatible /models endpoint."""
        url = api_url or self._config.get("api_url", "")
        key = api_key or self._config.get("api_key", "")
        if not url or not key:
            return {"ok": False, "error": "api_url and api_key required"}
        self._validate_url(url)
        models_url = url.split("/chat/completions")[0].rstrip("/") + "/models"
        req = urllib.request.Request(
            models_url,
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "ai-chat/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        data = body.get("data", body.get("models", []))
        if isinstance(data, list):
            model_ids = [m.get("id", "") if isinstance(m, dict) else str(m) for m in data]
            model_ids = [m for m in model_ids if m]
            return {"ok": True, "models": sorted(model_ids)}
        return {"ok": True, "models": []}

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

    def send_message(self, user_text: str, client_message_id: str = "") -> dict[str, Any]:
        """Send *user_text*, call the AI API, store both sides, return result dict.
        Serialized per-session to prevent interleaving."""
        if not self.enabled:
            return {"ok": False, "error": "ai chat is not enabled"}

        with self._send_lock:
            return self._send_message_locked(user_text, client_message_id=client_message_id)

    def send_attachment(
        self,
        user_text: str,
        attachment_url: str,
        attachment_type: str,
        attachment_filename: str,
        local_path: str,
    ) -> dict[str, Any]:
        """Store an uploaded attachment in AI chat history and notify the AI."""
        if not self.enabled:
            return {"ok": False, "error": "ai chat is not enabled"}

        display_text = user_text.strip() or f"[用户发了{'图片' if attachment_type == 'image' else '文件'}: {attachment_filename}]"
        prompt = display_text
        prompt += (
            f"\n\n[用户上传了{'图片' if attachment_type == 'image' else '文件'}: {attachment_filename}]"
            f"\n附件 URL: {attachment_url}"
        )
        if local_path:
            prompt += f"\n服务端本地路径: {local_path}"

        with self._send_lock:
            return self._send_attachment_locked(
                display_text=display_text,
                prompt=prompt,
                attachment_url=attachment_url,
                attachment_type=attachment_type,
                attachment_filename=attachment_filename,
            )

    def _send_message_locked(self, user_text: str, client_message_id: str = "") -> dict[str, Any]:

        api_url = self._config.get("api_url", "")
        api_key = self._config.get("api_key", "")
        model = self._config.get("model", "")
        system_prompt = self._config.get("system_prompt", "")
        max_ctx = int(self._config.get("max_context_messages", 20))

        if not api_url or not api_key or not model:
            return {"ok": False, "error": "ai chat not configured (missing api_url / api_key / model)"}

        duplicate = self._find_client_message_result(client_message_id)
        if duplicate is not None:
            return duplicate

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
        user_ts = self._append_history("user", user_text, client_message_id=client_message_id)

        # Call API
        try:
            reply_text, thinking = self._call_api(api_url, api_key, model, messages)
        except Exception as e:
            logger.exception("ai_chat: API call failed")
            return {"ok": False, "error": str(e), "ts": user_ts}

        # Store assistant reply
        reply_ts = self._append_history("assistant", reply_text, thinking=thinking, client_message_id=client_message_id)

        result = {"ok": True, "reply": reply_text, "ts": reply_ts}
        if thinking:
            result["thinking"] = thinking
        return result

    def send_message_stream(self, text: str, emit: Any, client_message_id: str = "") -> dict[str, Any]:
        """Send a message and emit newline-JSON stream events while the reply arrives."""
        text = text.strip()
        if not text:
            return {"ok": False, "error": "empty message"}
        with self._send_lock:
            return self._send_message_stream_locked(text, emit, client_message_id=client_message_id)

    def _send_message_stream_locked(self, user_text: str, emit: Any, client_message_id: str = "") -> dict[str, Any]:
        api_url = self._config.get("api_url", "")
        api_key = self._config.get("api_key", "")
        model = self._config.get("model", "")
        system_prompt = self._config.get("system_prompt", "")
        max_ctx = int(self._config.get("max_context_messages", 20))

        if not api_url or not api_key or not model:
            return {"ok": False, "error": "ai chat not configured (missing api_url / api_key / model)"}

        duplicate = self._find_client_message_result(client_message_id)
        if duplicate is not None:
            if duplicate.get("ok") and duplicate.get("reply"):
                emit({"type": "delta", "text": duplicate.get("reply", "")})
                if duplicate.get("thinking"):
                    emit({"type": "thinking_delta", "text": duplicate.get("thinking", "")})
            return duplicate

        memories = self._fetch_memories(user_text)

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

        user_ts = self._append_history("user", user_text, client_message_id=client_message_id)
        emit({"type": "user", "ts": user_ts, "text": user_text})

        try:
            reply_text, thinking = self._call_api_stream(api_url, api_key, model, messages, emit)
        except Exception as e:
            logger.exception("ai_chat: streaming API call failed")
            return {"ok": False, "error": str(e), "ts": user_ts}

        reply_ts = self._append_history("assistant", reply_text, thinking=thinking, client_message_id=client_message_id)
        result = {"ok": True, "reply": reply_text, "ts": reply_ts}
        if thinking:
            result["thinking"] = thinking
        return result

    def _send_attachment_locked(
        self,
        display_text: str,
        prompt: str,
        attachment_url: str,
        attachment_type: str,
        attachment_filename: str,
    ) -> dict[str, Any]:
        api_url = self._config.get("api_url", "")
        api_key = self._config.get("api_key", "")
        model = self._config.get("model", "")
        system_prompt = self._config.get("system_prompt", "")
        max_ctx = int(self._config.get("max_context_messages", 20))

        if not api_url or not api_key or not model:
            return {"ok": False, "error": "ai chat not configured (missing api_url / api_key / model)"}

        memories = self._fetch_memories(prompt)

        messages: list[dict[str, str]] = []
        if system_prompt:
            now = datetime.now(timezone.utc).astimezone()
            time_block = f"\n\n## 当前时间\n{now.strftime('%Y年%m月%d日 %H:%M %A')}"
            mem_block = ""
            if memories:
                mem_block = "\n\n## 相关记忆\n" + "\n---\n".join(memories)
            messages.append({"role": "system", "content": system_prompt + time_block + mem_block})
        messages.extend(self._recent_messages(max_ctx))
        messages.append({"role": "user", "content": prompt})

        user_ts = self._append_history(
            "user",
            display_text,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
            attachment_filename=attachment_filename,
        )

        try:
            reply_text, thinking = self._call_api(api_url, api_key, model, messages)
        except Exception as e:
            logger.exception("ai_chat: attachment API call failed")
            return {"ok": False, "error": str(e), "ts": user_ts}

        reply_ts = self._append_history("assistant", reply_text, thinking=thinking)

        result = {"ok": True, "reply": reply_text, "ts": reply_ts}
        if thinking:
            result["thinking"] = thinking
        return result

    def _call_api(
        self,
        api_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
    ) -> str:
        """POST to an OpenAI-compatible chat/completions endpoint.  Returns reply text."""
        def _chat_completions_url(url: str) -> str:
            stripped = url.rstrip("/")
            if stripped.endswith("/chat/completions"):
                return stripped
            return stripped + "/chat/completions"

        def _preview(raw: bytes, limit: int = 300) -> str:
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", text).strip()
            return text[:limit]

        payload_obj: dict[str, Any] = {"model": model, "messages": messages}
        if (urlparse(api_url).hostname or "").endswith("openrouter.ai"):
            payload_obj["reasoning"] = {"enabled": True, "exclude": False}
            payload_obj["include_reasoning"] = True
        payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            _chat_completions_url(api_url),
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.getcode()
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as e:
                    preview = _preview(raw)
                    logger.warning(
                        "ai_chat: API non-JSON response status=%s content_type=%r bytes=%d preview=%r",
                        status,
                        content_type,
                        len(raw),
                        preview,
                    )
                    detail = preview or "<empty body>"
                    raise RuntimeError(
                        f"API returned non-JSON response (HTTP {status}, {content_type or 'unknown content type'}, "
                        f"{len(raw)} bytes): {detail}"
                    ) from e
        except urllib.error.HTTPError as e:
            raw = e.read()
            content_type = e.headers.get("Content-Type", "") if e.headers else ""
            preview = _preview(raw)
            logger.warning(
                "ai_chat: API HTTP %d content_type=%r bytes=%d preview=%r",
                e.code,
                content_type,
                len(raw),
                preview,
            )
            detail = f": {preview}" if preview else ""
            raise RuntimeError(f"API returned HTTP {e.code}{detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"API request failed: {e.reason}") from e

        choices = body.get("choices")
        if not choices or not isinstance(choices, list):
            raise RuntimeError(f"unexpected API response shape: {json.dumps(body, ensure_ascii=False)[:300]}")
        message = choices[0].get("message", {})
        content = str(message.get("content", "")).strip()
        if not content:
            raise RuntimeError("模型返回了空回复")
        thinking = str(
            message.get("reasoning")
            or message.get("reasoning_content")
            or message.get("thoughts")
            or ""
        ).strip()
        if not thinking:
            reasoning_details = message.get("reasoning_details")
            if isinstance(reasoning_details, list):
                parts: list[str] = []
                for item in reasoning_details:
                    if not isinstance(item, dict):
                        continue
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                thinking = "\n".join(parts).strip()
        return content, thinking

    def _call_api_stream(
        self,
        api_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        emit: Any,
    ) -> tuple[str, str]:
        """POST to chat/completions with stream=true and emit incremental chunks."""
        def _chat_completions_url(url: str) -> str:
            stripped = url.rstrip("/")
            if stripped.endswith("/chat/completions"):
                return stripped
            return stripped + "/chat/completions"

        def _preview(raw: bytes, limit: int = 300) -> str:
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", text).strip()
            return text[:limit]

        payload_obj: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if (urlparse(api_url).hostname or "").endswith("openrouter.ai"):
            payload_obj["reasoning"] = {"enabled": True, "exclude": False}
            payload_obj["include_reasoning"] = True
        payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            _chat_completions_url(api_url),
            data=payload,
            headers={
                "Accept": "text/event-stream, application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        reply_parts: list[str] = []
        thinking_parts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type and "application/x-ndjson" not in content_type:
                    raw = resp.read()
                    preview = _preview(raw)
                    logger.warning(
                        "ai_chat: API stream non-stream response status=%s content_type=%r bytes=%d preview=%r",
                        resp.getcode(),
                        content_type,
                        len(raw),
                        preview,
                    )
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError as e:
                        detail = preview or "<empty body>"
                        raise RuntimeError(
                            f"API returned non-stream response (HTTP {resp.getcode()}, "
                            f"{content_type or 'unknown content type'}, {len(raw)} bytes): {detail}"
                        ) from e
                    choices = body.get("choices")
                    if not choices or not isinstance(choices, list):
                        raise RuntimeError(f"unexpected API response shape: {json.dumps(body, ensure_ascii=False)[:300]}")
                    message = choices[0].get("message", {})
                    content = str(message.get("content", "")).strip()
                    thinking = str(message.get("reasoning") or message.get("reasoning_content") or "").strip()
                    if content:
                        emit({"type": "delta", "text": content})
                    if thinking:
                        emit({"type": "thinking_delta", "text": thinking})
                    if not content:
                        raise RuntimeError("模型返回了空回复")
                    return content, thinking

                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("ai_chat: ignored non-json stream line: %r", line[:200])
                        continue
                    choices = chunk.get("choices")
                    if not choices or not isinstance(choices, list):
                        continue
                    delta = choices[0].get("delta") or {}
                    content_piece = delta.get("content")
                    if content_piece:
                        text = str(content_piece)
                        reply_parts.append(text)
                        emit({"type": "delta", "text": text})
                    thinking_piece = (
                        delta.get("reasoning")
                        or delta.get("reasoning_content")
                        or delta.get("thoughts")
                    )
                    if thinking_piece:
                        text = str(thinking_piece)
                        thinking_parts.append(text)
                        emit({"type": "thinking_delta", "text": text})
                    reasoning_details = delta.get("reasoning_details")
                    if isinstance(reasoning_details, list):
                        for item in reasoning_details:
                            if not isinstance(item, dict):
                                continue
                            detail_text = item.get("text") or item.get("content")
                            if detail_text:
                                text = str(detail_text)
                                thinking_parts.append(text)
                                emit({"type": "thinking_delta", "text": text})
        except urllib.error.HTTPError as e:
            raw = e.read()
            content_type = e.headers.get("Content-Type", "") if e.headers else ""
            preview = _preview(raw)
            logger.warning(
                "ai_chat: API stream HTTP %d content_type=%r bytes=%d preview=%r",
                e.code,
                content_type,
                len(raw),
                preview,
            )
            detail = f": {preview}" if preview else ""
            raise RuntimeError(f"API returned HTTP {e.code}{detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"API request failed: {e.reason}") from e

        reply_text = "".join(reply_parts).strip()
        thinking = "".join(thinking_parts).strip()
        if not reply_text:
            raise RuntimeError("模型返回了空回复")
        return reply_text, thinking
