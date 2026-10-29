"""Client for pi (pi coding agent) in ``--mode rpc`` — 哥哥写手 (2026-10-29).

The writer contact ``gege`` runs pi on Tokyo as user ``pi`` through
``remote_stdio.RemoteStdio`` (ssh -T, its own remote process group, pidfile),
exactly like Kiro's ACP process.  pi speaks strict JSONL on stdin/stdout:

* commands  ``{"id", "type", ...}`` -> one ``{"type":"response","id",...}``
* events    session events without an id (``message_update``,
  ``agent_settled``, ...) and ``extension_ui_request`` records.

Everything the writer needs beyond plain prompting lives in pi's ``gege``
extension on Tokyo (archives, roll/undo, per-archive context settings, cache
heartbeat); it answers ``/gege <sub>`` prompts with one ``notify`` record
``"GEGE {json}"``.  This module only moves records: one operation at a time
(``self._op_lock``), events of that operation go to its queue.

Framing: records are split on LF only (bytes), never on U+2028/U+2029.
Nothing here logs prompt or reply text.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

logger = logging.getLogger("pi_rpc")

GEGE_NOTIFY_PREFIX = "GEGE "
_AUTH_RE = re.compile(
    r"no api key|api key found|/login|not logged in|log ?in to|unauthori[sz]ed|authentication|"
    r"invalid x-api-key|invalid api key|\b401\b|\b403\b|no model|no models available|credit|insufficient",
    re.IGNORECASE,
)


class PiRPCError(RuntimeError):
    """pi failed (process, protocol or provider)."""


class PiRPCAuthRequired(PiRPCError):
    """No usable model/credentials (not logged in, key missing, no credits)."""


class PiRPCBusy(PiRPCError):
    """pi is still streaming or compacting after the wait budget."""


class PiRPCCancelled(PiRPCError):
    def __init__(self, partial: str = "") -> None:
        super().__init__("cancelled")
        self.partial = partial


def classify_error(message: str) -> PiRPCError:
    text = str(message or "").strip() or "pi error"
    if _AUTH_RE.search(text):
        return PiRPCAuthRequired(text[:300])
    return PiRPCError(text[:300])


def message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class PiRPCClient:
    """One long-lived pi RPC process; started lazily, closed after idling."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        remote: Any = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        idle_close_sec: float = 3 * 3600,
        start_timeout: float = 60.0,
        command_timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.argv = [str(a) for a in argv]
        self.remote = remote
        self._popen_factory = popen_factory
        self.idle_close_sec = float(idle_close_sec)
        self.start_timeout = float(start_timeout)
        self.command_timeout = float(command_timeout)
        self._clock = clock
        self._proc: Any = None
        self._generation = 0
        self._state_lock = threading.RLock()
        self._op_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, "queue.Queue[dict[str, Any]]"] = {}
        self._sink: "queue.Queue[dict[str, Any] | None] | None" = None
        self._seq = 0
        self._last_used = clock()
        self._stderr_tail: list[str] = []
        self._reaper: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------ process
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _ensure(self) -> None:
        with self._state_lock:
            if self.alive():
                return
            self._close_locked()
            factory = self.remote.popen if self.remote is not None else self._popen_factory
            try:
                proc = factory(
                    list(self.argv),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    start_new_session=True,
                )
            except Exception as exc:
                raise PiRPCError(f"pi could not start: {type(exc).__name__}") from exc
            self._proc = proc
            self._generation += 1
            generation = self._generation
            self._stderr_tail = []
            threading.Thread(target=self._read_stdout, args=(proc, generation), name="pi-rpc-stdout", daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(proc,), name="pi-rpc-stderr", daemon=True).start()
            if self._reaper is None or not self._reaper.is_alive():
                self._reaper = threading.Thread(target=self._reap_idle, name="pi-rpc-idle", daemon=True)
                self._reaper.start()
        # First command proves the process answers (and loads the extension).
        try:
            self._command({"type": "get_state"}, timeout=self.start_timeout, ensure=False)
        except PiRPCError:
            tail = " | ".join(self._stderr_tail[-3:])
            self.close()
            raise PiRPCError(f"pi did not answer at startup{': ' + tail[:300] if tail else ''}") from None

    def _read_stdout(self, proc: Any, generation: int) -> None:
        buf = b""
        stream = proc.stdout
        try:
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    idx = buf.find(b"\n")
                    if idx < 0:
                        break
                    line, buf = buf[:idx], buf[idx + 1:]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        logger.warning("pi rpc: unparseable stdout record dropped")
                        continue
                    if isinstance(record, dict):
                        self._dispatch(record)
        except Exception:
            logger.debug("pi rpc stdout reader stopped", exc_info=True)
        finally:
            if generation == self._generation:
                sink = self._sink
                if sink is not None:
                    sink.put(None)
                for q in list(self._pending.values()):
                    q.put({"type": "response", "success": False, "error": "pi process exited"})

    def _read_stderr(self, proc: Any) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_tail = (self._stderr_tail + [line[:300]])[-20:]
        except Exception:
            pass

    def _dispatch(self, record: dict[str, Any]) -> None:
        if record.get("type") == "response":
            rid = str(record.get("id") or "")
            waiter = self._pending.get(rid)
            if waiter is not None:
                waiter.put(record)
                return
        sink = self._sink
        if sink is not None:
            sink.put(record)
        elif record.get("type") == "extension_error":
            logger.warning("pi extension error outside an operation: %s", str(record.get("error") or "")[:200])

    def _reap_idle(self) -> None:
        while not self._closed:
            time.sleep(60)
            if self._op_lock.acquire(blocking=False):
                try:
                    if self.alive() and self._clock() - self._last_used > self.idle_close_sec:
                        logger.info("pi rpc idle for %.0f s, closing", self._clock() - self._last_used)
                        self.close()
                finally:
                    self._op_lock.release()

    def close(self) -> None:
        with self._state_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._generation += 1
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # pi shuts down cleanly on stdin EOF
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        if self.remote is not None:
            try:
                if not self.remote.stop():
                    logger.warning("pi remote stop was not confirmed")
            except Exception:
                logger.warning("pi remote stop failed", exc_info=True)

    # ------------------------------------------------------------ protocol
    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise PiRPCError("pi process is not running")
        data = memoryview((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            with self._write_lock:
                while data:
                    written = proc.stdin.write(data)
                    data = data[written if written else len(data):]
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise PiRPCError("pi stdin closed") from exc

    def _next_id(self) -> str:
        self._seq += 1
        return f"ccc-{self._seq}"

    def _command(self, payload: dict[str, Any], *, timeout: float | None = None, ensure: bool = True) -> dict[str, Any]:
        if ensure:
            self._ensure()
        rid = self._next_id()
        waiter: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._pending[rid] = waiter
        try:
            self._send({**payload, "id": rid})
            try:
                return waiter.get(timeout=timeout or self.command_timeout)
            except queue.Empty:
                raise PiRPCError(f"pi did not answer {payload.get('type')}") from None
        finally:
            self._pending.pop(rid, None)

    def _wait_ready(self, budget: float) -> dict[str, Any]:
        """get_state until pi is neither streaming nor compacting."""
        deadline = self._clock() + budget
        while True:
            response = self._command({"type": "get_state"})
            if not response.get("success"):
                raise classify_error(str(response.get("error") or "get_state failed"))
            data = response.get("data") or {}
            if not data.get("isStreaming") and not data.get("isCompacting"):
                return data
            if self._clock() >= deadline:
                raise PiRPCBusy("pi is still busy")
            time.sleep(1.0)

    # ------------------------------------------------------------- public
    def state(self) -> dict[str, Any]:
        with self._op_lock:
            self._last_used = self._clock()
            response = self._command({"type": "get_state"})
            if not response.get("success"):
                raise classify_error(str(response.get("error") or "get_state failed"))
            return dict(response.get("data") or {})

    def gege(self, command: str, *, timeout: float = 600.0, ready_budget: float = 600.0) -> dict[str, Any]:
        """Run ``/gege <command>`` and return the extension's JSON answer."""
        with self._op_lock:
            self._last_used = self._clock()
            self._ensure()
            self._wait_ready(ready_budget)
            sink: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
            self._sink = sink
            try:
                response = self._command({"type": "prompt", "message": f"/gege {command}".strip()}, timeout=timeout)
                if not response.get("success"):
                    raise classify_error(str(response.get("error") or "command failed"))
                answer = self._find_notify(sink, wait=0.5)
                if answer is None:
                    raise PiRPCError("gege extension gave no answer")
                return answer
            finally:
                self._sink = None
                self._last_used = self._clock()

    @staticmethod
    def _find_notify(sink: "queue.Queue[dict[str, Any] | None]", *, wait: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + wait
        found: dict[str, Any] | None = None
        while True:
            remaining = deadline - time.monotonic()
            try:
                record = sink.get(timeout=max(0.0, remaining)) if found is None else sink.get_nowait()
            except queue.Empty:
                return found
            if record is None:
                return found
            if record.get("type") == "extension_ui_request" and record.get("method") == "notify":
                message = str(record.get("message") or "")
                if message.startswith(GEGE_NOTIFY_PREFIX):
                    try:
                        parsed = json.loads(message[len(GEGE_NOTIFY_PREFIX):])
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        found = parsed

    def prompt(
        self,
        text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
        timeout: float = 1800.0,
        ready_budget: float = 900.0,
    ) -> str:
        """One writing turn; returns the final assistant text."""
        with self._op_lock:
            self._last_used = self._clock()
            self._ensure()
            state = self._wait_ready(ready_budget)
            if not state.get("model"):
                raise PiRPCAuthRequired("no model configured")
            sink: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
            self._sink = sink
            try:
                return self._run_prompt(text, sink, on_delta, cancel_event, timeout)
            finally:
                self._sink = None
                self._last_used = self._clock()

    def _run_prompt(
        self,
        text: str,
        sink: "queue.Queue[dict[str, Any] | None]",
        on_delta: Callable[[str], None] | None,
        cancel_event: threading.Event | None,
        timeout: float,
    ) -> str:
        response = self._command({"type": "prompt", "message": text})
        if not response.get("success"):
            raise classify_error(str(response.get("error") or "prompt rejected"))
        if (response.get("data") or {}).get("disposition") == "handled":
            return ""
        deadline = self._clock() + timeout
        current = ""
        final = ""
        last_error = ""
        aborted_sent = False
        while True:
            if cancel_event is not None and cancel_event.is_set() and not aborted_sent:
                aborted_sent = True
                try:
                    self._send({"type": "abort", "id": self._next_id()})
                except PiRPCError:
                    raise PiRPCCancelled(current) from None
            if self._clock() >= deadline and not aborted_sent:
                aborted_sent = True
                last_error = "timeout"
                try:
                    self._send({"type": "abort", "id": self._next_id()})
                except PiRPCError:
                    break
            try:
                record = sink.get(timeout=0.5)
            except queue.Empty:
                continue
            if record is None:
                raise PiRPCError("pi process exited during the turn")
            kind = record.get("type")
            if kind == "message_start" and (record.get("message") or {}).get("role") == "assistant":
                current = ""
            elif kind == "message_update":
                event = record.get("assistantMessageEvent") or {}
                if event.get("type") == "text_delta":
                    current += str(event.get("delta") or "")
                    if on_delta is not None:
                        try:
                            on_delta(current)
                        except Exception:
                            logger.debug("pi on_delta failed", exc_info=True)
            elif kind == "message_end":
                message = record.get("message") or {}
                if message.get("role") == "assistant":
                    if message.get("stopReason") in ("error",):
                        last_error = str(message.get("errorMessage") or "provider error")
                    elif message.get("stopReason") == "aborted":
                        pass
                    else:
                        body = message_text(message).strip()
                        if body:
                            final = body
                            last_error = ""
            elif kind == "agent_settled":
                if record.get("aborted") or aborted_sent:
                    if last_error == "timeout":
                        raise PiRPCError("turn timed out")
                    raise PiRPCCancelled(final or current)
                if final:
                    return final
                if last_error:
                    raise classify_error(last_error)
                return ""
