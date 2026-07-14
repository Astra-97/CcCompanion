"""Loopback-only adapter for the optional Xia Yizhou dual-engine relay.

The public Android client never talks to ai-session-relay directly.  This
adapter keeps that trusted, tool-capable process on loopback, maps its NDJSON
wire format to CcCompanion's small stream contract, and keeps the existing
``ai_chat_history.jsonl`` as the authoritative conversation archive.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import threading
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


VALID_PROVIDERS = frozenset({"claude", "codex"})
_MAX_HANDOFF_CHARS = 24_000


class RelayError(RuntimeError):
    pass


class RelayBusyError(RelayError):
    pass


class RelayRequestUncertain(RelayError):
    """Relay durably admitted the ID but cannot recover its final response."""

    code = "request_uncertain"


class RelayRequestTerminal(RelayError):
    """Relay completed the ID but has no usable final response to replay."""

    code = "request_terminal"


def normalize_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider == "cc":
        provider = "claude"
    if provider not in VALID_PROVIDERS:
        raise ValueError("provider must be 'claude' or 'codex'")
    return provider


def validate_loopback_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("relay_url must use http or https")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("relay_url must point to loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("relay_url must not contain credentials, query, or fragment")
    return url


def validate_request_id(value: Any) -> str:
    request_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", request_id):
        raise ValueError("request_id must be 1-200 simple ASCII characters")
    return request_id


def build_authoritative_handoff(records: Iterable[dict[str, Any]]) -> str:
    lines = [
        "[Authoritative Xia Yizhou conversation history from CcCompanion. ",
        "Continue naturally. Do not mention or repeat this handoff.]",
    ]
    for record in records:
        role = record.get("role")
        text = str(record.get("text") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        lines.append(("User: " if role == "user" else "Assistant: ") + text)
    rendered = "\n".join(lines)
    if len(rendered) <= _MAX_HANDOFF_CHARS:
        return rendered
    # Retain the instruction and the newest part of the actual conversation.
    header = "\n".join(lines[:2]) + "\n"
    return header + rendered[-(_MAX_HANDOFF_CHARS - len(header)):]


def map_relay_event(event: dict[str, Any], expected_provider: str) -> dict[str, Any] | None:
    """Map one upstream relay event to a display-safe CcCompanion event."""
    expected_provider = normalize_provider(expected_provider)
    if event.get("done"):
        # Codex marks this as a public reasoning summary. Claude's similarly
        # named field can contain reconstructed raw chain-of-thought and must
        # not cross the backend boundary.
        thinking = (
            str(event.get("codex_thinking_full") or "")
            if expected_provider == "codex" else ""
        )
        return {
            "type": "done",
            "reply": str(event.get("full") or ""),
            "thinking": thinking,
            "provider": str(event.get("provider") or ""),
            "activities": event.get("activities") if isinstance(event.get("activities"), list) else [],
            "error": str(event.get("error") or ""),
            "code": str(event.get("code") or ""),
            "request_status": str(event.get("request_status") or ""),
            "retryable": event.get("retryable") if isinstance(event.get("retryable"), bool) else None,
        }
    delta = event.get("delta")
    if isinstance(delta, str) and delta:
        return {"type": "delta", "text": delta}
    thinking = event.get("codex_thinking_delta")
    if expected_provider == "codex" and isinstance(thinking, str) and thinking:
        return {"type": "thinking_delta", "text": thinking}
    activity = event.get("activity")
    if isinstance(activity, dict):
        normalized_activity = dict(activity)
        normalized_activity.setdefault("name", str(activity.get("title") or activity.get("kind") or "tool"))
        return {"type": "activity", "activity": normalized_activity}
    steps = (event.get("tool_activity") or {}).get("steps") if isinstance(event.get("tool_activity"), dict) else None
    if isinstance(steps, list) and steps:
        return {
            "type": "activity",
            "activity": {
                "id": "tool:" + "|".join(str(step) for step in steps),
                "kind": "tool",
                "title": " · ".join(str(step) for step in steps),
                "status": "running",
            },
        }
    # Commentary is deliberately not exposed as hidden reasoning. It is public
    # progress text, so present it as an activity record instead of chat text.
    commentary = event.get("commentary_delta")
    if isinstance(commentary, str) and commentary.strip():
        return {
            "type": "activity",
            "activity": {
                "id": "commentary:current",
                "kind": "commentary",
                "title": commentary.strip()[:500],
                "status": "running",
            },
        }
    return None


class AISessionRelayProxy:
    _WORKSPACE_OWNER = "cc-xia-relay"

    def __init__(
        self,
        state_dir: str | Path,
        *,
        workspace_owner: str | None = _WORKSPACE_OWNER,
    ):
        if workspace_owner not in {None, self._WORKSPACE_OWNER}:
            raise ValueError("workspace_owner may only be 'cc-xia-relay' or None")
        self.state_dir = Path(state_dir)
        self.workspace = self.state_dir / "ai_relay_workspace"
        self._proxy_state_path = self.state_dir / "ai_relay_proxy_state.json"
        self._workspace_owner_name = workspace_owner
        self._workspace_identity: tuple[int, int] | None = None
        self._resolve_workspace_identity()
        self._guard = threading.Lock()
        self._operation_lock = threading.Lock()
        self._turn_active = False

    def _resolve_workspace_identity(self) -> None:
        if self._workspace_owner_name is not None and self._workspace_identity is None:
            try:
                account = pwd.getpwnam(self._workspace_owner_name)
                self._workspace_identity = (account.pw_uid, account.pw_gid)
            except KeyError:
                # Development/test hosts do not have the deployment user.
                # Keep current ownership for now and retry lazily on every
                # write so a later-created deployment account is picked up.
                self._workspace_identity = None

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """Persist directory-entry changes made by replace/unlink."""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _ensure_private_dir(cls, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    def _set_workspace_owner(self, path: Path) -> None:
        self._resolve_workspace_identity()
        if self._workspace_identity is not None:
            os.chown(path, *self._workspace_identity)

    @property
    def turn_active(self) -> bool:
        with self._guard:
            return self._turn_active

    def _set_turn_active(self, value: bool) -> None:
        with self._guard:
            self._turn_active = value

    def sync_persona(
        self,
        system_prompt: str,
        execution_mode: str = "chat_only",
        *,
        _fault: Callable[[str], None] | None = None,
    ) -> None:
        """Atomically mirror only persona instructions; never copy API config."""
        self._ensure_private_dir(self.workspace)
        self._set_workspace_owner(self.workspace)
        prompt = str(system_prompt or "").strip()
        content = (
            "# Xia Yizhou isolated companion workspace\n\n"
            "This workspace belongs only to the ai-custom contact. Do not inspect, "
            "resume, alter, or message the Kairos or Xiaoke sessions.\n\n"
            f"Execution mode: {execution_mode}. In chat_only mode, do not invoke tools, "
            "MCP servers, shell commands, or modify files.\n\n"
            "## Persona and conversation instructions\n\n"
            + (prompt or "Continue as Xia Yizhou using the authoritative chat handoff.")
            + "\n"
        )
        targets = [self.workspace / name for name in ("CLAUDE.md", "AGENTS.md")]
        previous = {target: target.read_bytes() if target.exists() else None for target in targets}
        staged: dict[Path, Path] = {}
        for target in targets:
            tmp = target.with_suffix(target.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            self._set_workspace_owner(tmp)
            staged[target] = tmp
        try:
            for index, (target, tmp) in enumerate(staged.items()):
                os.replace(tmp, target)
                os.chmod(target, 0o600)
                self._fsync_dir(self.workspace)
                if index == 0 and _fault is not None:
                    _fault("after_first_workspace_replace")
        except Exception:
            for target, old_bytes in previous.items():
                if old_bytes is None:
                    target.unlink(missing_ok=True)
                    self._fsync_dir(self.workspace)
                else:
                    rollback = target.with_suffix(target.suffix + ".rollback")
                    with rollback.open("wb") as handle:
                        handle.write(old_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(rollback, 0o600)
                    self._set_workspace_owner(rollback)
                    os.replace(rollback, target)
                    self._fsync_dir(self.workspace)
            raise
        finally:
            for tmp in staged.values():
                tmp.unlink(missing_ok=True)
            self._fsync_dir(self.workspace)

    def _load_proxy_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._proxy_state_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except Exception:
            pass
        return {"initialized": False, "pending_switch": False}

    def _save_proxy_state(self, value: dict[str, Any]) -> None:
        tmp = self._proxy_state_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._proxy_state_path)
        os.chmod(self._proxy_state_path, 0o600)
        self._fsync_dir(self.state_dir)

    def refresh_sessions(self, base_url: str) -> dict[str, Any]:
        """Discard only Xia's relay sessions so new instructions are reloaded."""
        base_url = validate_loopback_url(base_url)
        if not self._operation_lock.acquire(blocking=False):
            raise RelayBusyError("cannot refresh relay sessions while a turn is active")
        try:
            with self._guard:
                if self._turn_active:
                    raise RelayBusyError("cannot refresh relay sessions while a turn is active")
            result = self._json_request(
                base_url + "/refresh", "POST", {"reason": "persona_updated"}
            )
            if not result.get("ok"):
                raise RelayError(str(result.get("error") or "relay session refresh failed"))
            state = self._load_proxy_state()
            state.update({"initialized": False, "pending_switch": True})
            self._save_proxy_state(state)
            return {
                "ok": True,
                "epoch": max(0, int(result.get("epoch", 0))),
                "provider": normalize_provider(result.get("provider") or state.get("provider") or "claude"),
            }
        finally:
            self._operation_lock.release()

    @staticmethod
    def _json_request(url: str, method: str = "GET", body: dict[str, Any] | None = None, timeout: int = 10) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "User-Agent": "cccompanion-ai-relay/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RelayError(f"relay HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RelayError(f"relay unavailable: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RelayError("relay returned invalid JSON")
        return parsed

    def status(self, base_url: str) -> dict[str, Any]:
        base_url = validate_loopback_url(base_url)
        health = self._json_request(base_url + "/health")
        provider = self._json_request(base_url + "/provider")
        current = normalize_provider(provider.get("provider"))
        return {
            "ok": bool(health.get("ok", True)),
            "provider": current,
            "epoch": max(0, int(provider.get("epoch", 0))),
            "turn_active": self.turn_active,
            "claude_available": bool(health.get("claude_cli")),
            "codex_available": bool(health.get("codex_cli")),
        }

    def list_models(self, base_url: str, provider: str) -> dict[str, Any]:
        """Use an optional restricted-relay model endpoint when available."""
        base_url = validate_loopback_url(base_url)
        provider = normalize_provider(provider)
        query = urllib.parse.urlencode({"provider": provider})
        try:
            result = self._json_request(base_url + "/models?" + query)
        except RelayError:
            result = {"models": [], "dynamic": False}
        raw_models = result.get("models")
        models: list[dict[str, str]] = []
        if isinstance(raw_models, list):
            for item in raw_models[:200]:
                if isinstance(item, str):
                    model_id, label = item.strip(), item.strip()
                elif isinstance(item, dict):
                    model_id = str(item.get("id") or item.get("model") or "").strip()
                    label = str(item.get("label") or item.get("displayName") or model_id).strip()
                else:
                    continue
                if model_id:
                    models.append({"id": model_id[:200], "label": label[:200] or model_id[:200]})
        return {"models": models, "dynamic": bool(result.get("dynamic", models) or models)}

    def switch_provider(self, base_url: str, provider: str) -> dict[str, Any]:
        base_url = validate_loopback_url(base_url)
        provider = normalize_provider(provider)
        if not self._operation_lock.acquire(blocking=False):
            raise RelayBusyError("cannot switch provider while a turn is active")
        try:
            with self._guard:
                if self._turn_active:
                    raise RelayBusyError("cannot switch provider while a turn is active")
            health = self._json_request(base_url + "/health")
            availability_key = "claude_cli" if provider == "claude" else "codex_cli"
            if not health.get(availability_key):
                raise RelayError(f"{provider} CLI is not available to the isolated relay")
            result = self._json_request(base_url + "/provider", "POST", {"provider": provider})
            state = self._load_proxy_state()
            if result.get("changed"):
                state["pending_switch"] = True
            self._save_proxy_state(state)
            return {
                "ok": True,
                "provider": normalize_provider(result.get("provider") or provider),
                "epoch": max(0, int(result.get("epoch", 0))),
                "changed": bool(result.get("changed")),
                "turn_active": False,
            }
        finally:
            self._operation_lock.release()

    def _prime_first_handoff(self, base_url: str, provider: str) -> None:
        """Make the relay's first turn a real switch so it consumes handoff."""
        opposite = "codex" if provider == "claude" else "claude"
        self._json_request(base_url + "/provider", "POST", {"provider": opposite})
        self._json_request(base_url + "/provider", "POST", {"provider": provider})

    def stream_turn(
        self,
        base_url: str,
        *,
        provider: str,
        text: str,
        handoff: str,
        execution_mode: str = "chat_only",
        model: str = "",
        request_id: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        base_url = validate_loopback_url(base_url)
        provider = normalize_provider(provider)
        request_id = validate_request_id(request_id)
        if not self._operation_lock.acquire(blocking=False):
            raise RelayBusyError("a relay operation is already active")
        with self._guard:
            if self._turn_active:
                self._operation_lock.release()
                raise RelayBusyError("a relay turn is already active")
            self._turn_active = True
        state = self._load_proxy_state()
        try:
            if not state.get("initialized"):
                self._prime_first_handoff(base_url, provider)
                state["pending_switch"] = True
                self._save_proxy_state(state)

            # Always include authoritative history. The upstream consumes it
            # only for a changed/pending switch, but unconditional inclusion
            # survives relay restarts and out-of-band operator switches.
            payload: dict[str, Any] = {
                "text": text,
                "provider": provider,
                "handoff": handoff[-_MAX_HANDOFF_CHARS:],
                "execution_mode": execution_mode,
                "request_id": request_id,
            }
            if model:
                payload["model"] = model
            request = urllib.request.Request(
                base_url + "/chat_stream",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "cccompanion-ai-relay/1"},
            )
            final_event: dict[str, Any] | None = None
            try:
                with urllib.request.urlopen(request, timeout=900) as response:
                    for raw_line in response:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            upstream = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(upstream, dict):
                            continue
                        mapped = map_relay_event(upstream, provider)
                        if mapped is None:
                            continue
                        if mapped.get("type") == "done":
                            final_event = mapped
                        else:
                            emit(mapped)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code == 409:
                    try:
                        error_body = json.loads(detail)
                    except Exception:
                        error_body = {}
                    if isinstance(error_body, dict) and error_body.get("code") == "request_uncertain":
                        raise RelayRequestUncertain(
                            "relay previously admitted this request but its final state is uncertain"
                        ) from exc
                raise RelayError(f"relay HTTP {exc.code}: {detail}") from exc
            except RelayError:
                raise
            except Exception as exc:
                raise RelayError(f"relay stream failed: {exc}") from exc

            if final_event is None:
                raise RelayError("relay stream ended without authoritative done")
            request_status = str(final_event.get("request_status") or "")
            if request_status == "uncertain" or final_event.get("code") == "request_uncertain":
                raise RelayRequestUncertain(
                    "relay previously admitted this request but its final state is uncertain"
                )
            if request_status == "completed" and not final_event.get("reply"):
                raise RelayRequestTerminal(
                    str(final_event.get("error") or "relay completed without a usable final response")
                )
            if final_event.get("retryable") is False and final_event.get("error") and not final_event.get("reply"):
                raise RelayRequestTerminal(str(final_event["error"]))
            if final_event.get("error") and not final_event.get("reply"):
                raise RelayError(str(final_event["error"]))
            state = self._load_proxy_state()
            state.update({
                "initialized": True,
                "pending_switch": False,
                "provider": provider,
            })
            self._save_proxy_state(state)
            return final_event
        finally:
            self._set_turn_active(False)
            self._operation_lock.release()
