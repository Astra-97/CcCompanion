"""Kiro Crew contact ``kirocrew`` (2026-10-31): client for the Crew gateway on Tokyo.

方小南 is trying Kiro Crew side by side with the kiro-cli contact ``kiro`` for a
few days.  The gateway (``kirocrew.service`` on Tokyo, user kiro) binds
127.0.0.1:5476 only; every call goes through ``kirocrew_remote_bridge.py``,
which this module runs remotely as ``python3 -c <source> <port> <op>`` over the
same ssh login as Kiro (``ssh -l kiro tokyo``, ControlMaster).  The bridge reads
the gateway's per-start internal secret on Tokyo, so no secret ever reaches 216.

One fixed Crew chat slot (default ``ccc-kirocrew``) is the contact's persistent
session: Crew keeps the kiro-cli ACP session behind it alive (the slot is in
``session.idle_exempt_keys``) and replays its history after a gateway restart.
``reset()`` (``/new`` in the chat) starts a fresh conversation in that slot.

No message text, reply text or secret is ever logged here.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BRIDGE_PATH = Path(__file__).with_name("kirocrew_remote_bridge.py")
DEFAULT_PORT = 5476
DEFAULT_AGENT = "xiaoke-persona"
DEFAULT_SLOT = "ccc-kirocrew"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_SLOT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


class KiroCrewError(RuntimeError):
    """Crew answered with an error (message is display-safe, never reply text)."""


class KiroCrewUnavailable(KiroCrewError):
    """Gateway down / ssh failed."""


class KiroCrewBusy(KiroCrewError):
    """The Crew slot is already running a turn."""


class KiroCrewAuthRequired(KiroCrewError):
    """kiro-cli on Tokyo is not signed in."""


class KiroCrewQuotaExceeded(KiroCrewError):
    """Kiro credits exhausted."""


class KiroCrewCancelled(KiroCrewError):
    def __init__(self, partial: str = "") -> None:
        super().__init__("cancelled")
        self.partial = partial


@dataclass
class KiroCrewTurn:
    text: str
    tools: list[str] = field(default_factory=list)
    context_pct: float | None = None
    context_used_tokens: int | None = None
    context_window_tokens: int | None = None
    errors: list[str] = field(default_factory=list)


def _classify_error(status: int, body: Any) -> KiroCrewError:
    text = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
    lower = text.lower()
    if status == 409 and ("busy" in lower or "slot_busy" in lower):
        return KiroCrewBusy("slot busy")
    if "kiro_prerequisite_required" in lower or "sign-in" in lower or "sign in" in lower or "not signed" in lower:
        return KiroCrewAuthRequired("kiro-cli sign-in required")
    if "quota" in lower or "insufficient credit" in lower or "out of credits" in lower or "limit reached" in lower:
        return KiroCrewQuotaExceeded("quota exceeded")
    if status == 0:
        return KiroCrewUnavailable("gateway unreachable")
    return KiroCrewError(f"gateway error {status}")


class KiroCrewClient:
    """Talks to one Crew slot through the remote bridge."""

    def __init__(
        self,
        remote: Any,
        *,
        port: int = DEFAULT_PORT,
        agent: str = DEFAULT_AGENT,
        slot: str = DEFAULT_SLOT,
        python: str = "python3",
        popen: Callable[..., Any] = subprocess.Popen,
        bridge_source: str | None = None,
        call_timeout: float = 30.0,
        turn_timeout: float = 4 * 3600.0,
    ) -> None:
        if not _SLOT_RE.fullmatch(slot):
            raise ValueError("invalid crew slot")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", agent):
            raise ValueError("invalid crew agent")
        self.remote = remote
        self.port = int(port)
        self.agent = agent
        self.slot = slot
        self.python = python
        self._popen = popen
        self._bridge_source = bridge_source
        self.call_timeout = float(call_timeout)
        self.turn_timeout = float(turn_timeout)
        self._lock = threading.Lock()
        self._proc: Any = None

    # ── transport ─────────────────────────────────────────────────────────
    def _source(self) -> str:
        if self._bridge_source is None:
            self._bridge_source = BRIDGE_PATH.read_text(encoding="utf-8")
        return self._bridge_source

    def _argv(self, op: str) -> list[str]:
        return self.remote.remote_argv([self.python, "-c", self._source(), str(self.port), op], multiplex=True)

    def _call(self, op: str, request: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        limit = float(timeout or self.call_timeout)
        try:
            result = self.remote.run(
                [self.python, "-c", self._source(), str(self.port), op],
                input=json.dumps({**request, "timeout": limit}, ensure_ascii=False),
                timeout=limit + 15,
            )
        except Exception as exc:
            raise KiroCrewUnavailable(f"ssh failed: {type(exc).__name__}") from exc
        lines = [line for line in str(getattr(result, "stdout", "") or "").splitlines() if line.strip()]
        if getattr(result, "returncode", 1) != 0 or not lines:
            raise KiroCrewUnavailable("bridge failed")
        try:
            payload = json.loads(lines[-1])
        except ValueError as exc:
            raise KiroCrewUnavailable("bridge output unreadable") from exc
        return payload if isinstance(payload, dict) else {"status": 0, "body": ""}

    def get(self, path: str) -> tuple[int, Any]:
        payload = self._call("get", {"path": path})
        return int(payload.get("status") or 0), payload.get("body")

    def post(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        payload = self._call("post", {"path": path, "body": body or {}})
        return int(payload.get("status") or 0), payload.get("body")

    def patch(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        payload = self._call("patch", {"path": path, "body": body or {}})
        return int(payload.get("status") or 0), payload.get("body")

    def delete(self, path: str) -> tuple[int, Any]:
        payload = self._call("delete", {"path": path})
        return int(payload.get("status") or 0), payload.get("body")

    # ── control ───────────────────────────────────────────────────────────
    def health(self) -> bool:
        try:
            status, body = self.get("/api/health")
        except KiroCrewError:
            return False
        return status == 200 and isinstance(body, dict) and bool(body.get("ok"))

    def slot_info(self) -> dict[str, Any]:
        """Slot detail without the transcript ({} when the slot does not exist yet)."""
        status, body = self.get(f"/api/chat/slots/{self.slot}")
        if status != 200 or not isinstance(body, dict):
            return {}
        return {key: value for key, value in body.items() if key != "messages"}

    def slot_summary(self) -> dict[str, Any]:
        """Model / effort row from the slot list ({} when absent)."""
        status, body = self.get("/api/chat/slots")
        if status != 200 or not isinstance(body, list):
            return {}
        for row in body:
            if isinstance(row, dict) and row.get("key") == self.slot:
                return {
                    key: row.get(key)
                    for key in ("model", "served_model", "reasoning_effort", "running", "compacting", "agent")
                }
        return {}

    SLOT_TITLE = "CC Companion · 方小南"

    def ensure_slot(self) -> None:
        """Create the contact's slot with our agent and give it a fixed title.

        A titled slot never runs Crew's auto-title, which would otherwise
        spend a background model turn (and keep a ``kirocrew-lite`` kiro-cli
        resident) for a title nobody sees.  Creating the slot up front also
        lets the app's model / effort apply before the very first turn.
        """
        if getattr(self, "_slot_ready", False):
            return
        info = self.slot_info()
        if not info:
            status, body = self.post("/api/chat/slots", {"name": self.slot, "agent": self.agent, "agent_kind": "template"})
            if status not in (200, 201):
                raise _classify_error(status, body)
        if not info or str(info.get("title") or "") != self.SLOT_TITLE:
            status, body = self.patch(f"/api/chat/slots/{self.slot}/title", {"title": self.SLOT_TITLE})
            if status != 200:
                raise _classify_error(status, body)
        self._slot_ready = True

    def stop(self) -> bool:
        try:
            status, _body = self.post(f"/api/chat/slots/{self.slot}/stop")
        except KiroCrewError:
            return False
        return status == 200

    def reset(self) -> None:
        """Fresh conversation in the contact's slot (``/new``)."""
        status, body = self.post(f"/api/chat/slots/{self.slot}/reset-conversation")
        self._slot_ready = False  # re-check the title on the next turn
        if status == 404:
            return  # nothing to reset yet; the next turn creates the slot
        if status != 200:
            raise _classify_error(status, body)

    def set_model(self, model: str) -> None:
        if not _MODEL_RE.fullmatch(str(model or "")):
            raise ValueError("invalid model")
        status, body = self.post(f"/api/chat/slots/{self.slot}/model", {"model": model})
        if status != 200:
            raise _classify_error(status, body)

    def set_effort(self, effort: str) -> None:
        if effort and effort not in EFFORTS:
            raise ValueError("invalid effort")
        status, body = self.post(f"/api/chat/slots/{self.slot}/reasoning-effort", {"reasoning_effort": effort})
        if status != 200:
            raise _classify_error(status, body)

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    # ── turn ──────────────────────────────────────────────────────────────
    def prompt(
        self,
        text: str,
        *,
        on_update: Callable[[str], None] | None = None,
        on_tool: Callable[[list[str]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> KiroCrewTurn:
        """Run one turn in the slot; returns the whole reply.

        Crew emits the reply as segments (one ``assistant`` row per stretch of
        text between tool calls); the visible reply is the segments joined by a
        blank line, which is also what ``on_update`` receives while streaming.
        """
        request = json.dumps({"agent": self.agent, "slot": self.slot, "text": str(text or ""),
                              "timeout": self.turn_timeout}, ensure_ascii=False)
        try:
            proc = self._popen(
                self._argv("chat"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except Exception as exc:
            raise KiroCrewUnavailable(f"ssh failed: {type(exc).__name__}") from exc
        with self._lock:
            self._proc = proc
        lines: "queue.Queue[str | None]" = queue.Queue()

        def pump() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(request)
                proc.stdin.close()
            except Exception:
                pass
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    lines.put(raw)
            except Exception:
                pass
            finally:
                lines.put(None)

        threading.Thread(target=pump, name="kirocrew-turn-reader", daemon=True).start()
        segments: list[str] = []
        current = ""
        tools: list[str] = []
        turn = KiroCrewTurn(text="")
        terminal: dict[str, Any] | None = None
        cancelled = False
        cancel_deadline = 0.0
        deadline = time.monotonic() + self.turn_timeout + 60

        def visible() -> str:
            return "\n\n".join(part for part in [*segments, current] if part.strip())

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set() and not cancelled:
                    cancelled = True
                    cancel_deadline = time.monotonic() + 30
                    threading.Thread(target=self.stop, name="kirocrew-stop", daemon=True).start()
                now = time.monotonic()
                if now > deadline or (cancelled and now > cancel_deadline):
                    break
                try:
                    raw = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if raw is None:
                    break
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                kind = row.get("t")
                if kind == "d":
                    current += str(row.get("c") or "")
                    if on_update is not None:
                        on_update(visible())
                elif kind == "final":
                    final = str(row.get("c") or "")
                    segments.append(final if final.strip() else current)
                    current = ""
                    if on_update is not None:
                        on_update(visible())
                elif kind == "ev":
                    etype = str(row.get("type") or "")
                    content = str(row.get("c") or "")
                    if etype == "context_usage":
                        try:
                            usage = json.loads(content)
                            turn.context_pct = float(usage.get("pct"))
                            turn.context_used_tokens = int(usage.get("used_tokens"))
                            turn.context_window_tokens = int(usage.get("window_tokens"))
                        except (ValueError, TypeError, AttributeError):
                            pass
                    elif etype == "tool":
                        label = re.sub(r"^\W+\s*", "", content.strip()).splitlines()[0][:120] if content.strip() else "工具调用"
                        tools.append(label or "工具调用")
                        if on_tool is not None:
                            on_tool(list(tools))
                    elif etype in {"error", "system_error", "turn_error"} and content.strip():
                        turn.errors.append(content.strip()[:300])
                elif kind in {"done", "err"}:
                    terminal = row
                    break
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            for stream in (getattr(proc, "stdout", None), getattr(proc, "stdin", None)):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        turn.text = visible().strip()
        turn.tools = tools
        if cancelled:
            raise KiroCrewCancelled(turn.text)
        if terminal is None:
            raise KiroCrewUnavailable("turn stream ended without a result")
        if terminal.get("t") == "err":
            raise _classify_error(int(terminal.get("status") or 0), terminal.get("body"))
        if not turn.text and turn.errors:
            raise _classify_error(500, " ".join(turn.errors))
        return turn


_MD_IMAGE_RE = re.compile(r"!\[[^\]\n]{0,200}\]\((/[^)\s]{1,1024}\.(?:png|jpe?g|gif|webp))\)", re.IGNORECASE)


def markdown_images_to_markers(text: str) -> str:
    """Crew's own convention for a picture is ``![alt](/abs/path.png)``; turn
    local absolute paths into the app's ``[[CCC_IMAGE:...]]`` marker."""
    return _MD_IMAGE_RE.sub(lambda m: f"[[CCC_IMAGE:{m.group(1)}]]", str(text or ""))


class KiroCrewPreferences:
    """Model / effort pinned from the app for the crew contact, plus the
    conversation epoch (bumped by ``/new``; names the recall-dedup session)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.model, self.effort, self.epoch = self._load()

    def _load(self) -> tuple[str, str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", "", 0
        if not isinstance(data, dict):
            return "", "", 0
        model = str(data.get("model") or "")
        effort = str(data.get("effort") or "")
        try:
            epoch = max(0, int(data.get("epoch") or 0))
        except (TypeError, ValueError):
            epoch = 0
        return (model if _MODEL_RE.fullmatch(model) else ""), (effort if effort in EFFORTS else ""), epoch

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return self.model, self.effort

    def _write(self, model: str, effort: str, epoch: int) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": model, "effort": effort, "epoch": epoch}), encoding="utf-8")
        tmp.replace(self.path)
        self.model, self.effort, self.epoch = model, effort, epoch

    def save(self, *, model: str | None = None, effort: str | None = None) -> tuple[str, str]:
        with self._lock:
            new_model = self.model if model is None else str(model)
            new_effort = self.effort if effort is None else str(effort)
            if new_model and not _MODEL_RE.fullmatch(new_model):
                raise ValueError("invalid model")
            if new_effort and new_effort not in EFFORTS:
                raise ValueError("invalid effort")
            self._write(new_model, new_effort, self.epoch)
            return new_model, new_effort

    def bump_epoch(self) -> int:
        with self._lock:
            self._write(self.model, self.effort, self.epoch + 1)
            return self.epoch
