"""Authenticated loopback client for Xia Yizhou's isolated Claude channel.

The channel owns no CcCompanion credentials and never writes chat history.
This client is the only bridge between the authoritative backend manager and
the durable request/result ledger maintained beside the dedicated Claude TUI.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class XiaChannelError(RuntimeError):
    code = "channel_error"


class XiaChannelUnavailable(XiaChannelError):
    code = "channel_unavailable"


class XiaChannelUncertain(XiaChannelError):
    code = "request_uncertain"


class XiaChannelStale(XiaChannelError):
    code = "channel_stale"


class XiaChannelNotFound(XiaChannelError):
    code = "not_found"


def validate_channel_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("claude channel URL must use HTTP loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("claude channel URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("claude channel URL must not contain a path")
    return url


def validate_channel_token_file(value: str | Path) -> Path:
    path = Path(value).expanduser()
    try:
        info = path.lstat()
    except Exception as exc:
        raise XiaChannelUnavailable("claude channel token file is unavailable") from exc
    if stat.S_ISLNK(info.st_mode):
        raise XiaChannelUnavailable("claude channel token file must not be a symlink")
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise XiaChannelUnavailable("claude channel token file must be a private regular file")
    return path


class XiaClaudeChannelClient:
    def __init__(
        self,
        base_url: str,
        *,
        token_file: str | Path | None = None,
        token: str = "",
        timeout_seconds: float = 10.0,
    ):
        self.base_url = validate_channel_url(base_url)
        self.token_file = Path(token_file).expanduser() if token_file else None
        self._token = str(token or "")
        self.timeout_seconds = max(0.2, min(float(timeout_seconds), 900.0))
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _read_token(self) -> str:
        token = self._token
        if self.token_file is not None:
            path = validate_channel_token_file(self.token_file)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                        raise XiaChannelUnavailable("claude channel token file became unsafe")
                    token = os.read(fd, 4097).decode("utf-8").strip()
                finally:
                    os.close(fd)
            except XiaChannelError:
                raise
            except Exception as exc:
                raise XiaChannelUnavailable("claude channel token is unavailable") from exc
        if not token or len(token) > 4096 or "\n" in token or "\r" in token:
            raise XiaChannelUnavailable("claude channel token is unavailable")
        return token

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Token": self._read_token(),
                "User-Agent": "cccompanion-xia-channel/1",
            },
        )
        try:
            with self._opener.open(request, timeout=timeout or self.timeout_seconds) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")[:1000]
            try:
                detail = json.loads(raw)
            except Exception:
                detail = {"error": raw}
            code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
            message = str(detail.get("error") or f"channel HTTP {exc.code}") if isinstance(detail, dict) else f"channel HTTP {exc.code}"
            if code == "request_uncertain":
                raise XiaChannelUncertain(message) from exc
            if exc.code == 404 or code == "not_found":
                raise XiaChannelNotFound(message) from exc
            if code in {"stale", "generation_stale", "revoked"}:
                raise XiaChannelStale(message) from exc
            raise XiaChannelError(message) from exc
        except XiaChannelError:
            raise
        except Exception as exc:
            raise XiaChannelUnavailable(f"claude channel unavailable: {exc}") from exc
        if not isinstance(value, dict):
            raise XiaChannelError("claude channel returned invalid JSON")
        return value

    def health(self) -> dict[str, Any]:
        value = self._request("/health")
        return {
            "ok": bool(value.get("ok")),
            "ready": bool(value.get("ready")),
            "mcp_connected": bool(value.get("mcp_connected")),
            "generation": max(0, int(value.get("generation") or 0)),
            "session_id": str(value.get("session_id") or ""),
            "model": str(value.get("model") or ""),
            "requires_fresh": bool(value.get("requires_fresh")),
        }

    def revoke(self, *, epoch: int, reason: str = "provider changed") -> dict[str, Any]:
        return self._request("/revoke", method="POST", body={"epoch": max(0, int(epoch)), "reason": reason[:200]})

    def ensure_generation(
        self, *, generation: int, model: str, timeout_seconds: float = 90.0,
        on_wait: Any = None,
    ) -> dict[str, Any]:
        generation = max(1, int(generation))
        last_wait_notice = float("-inf")

        def signal_wait() -> None:
            nonlocal last_wait_notice
            current = time.monotonic()
            if on_wait is not None and current - last_wait_notice >= 15.0:
                on_wait()
                last_wait_notice = current

        health = self.health()
        if health["ready"] and health["generation"] == generation and health["model"] == model and not health.get("requires_fresh"):
            return {**health, "fresh": False}
        self._request(
            "/rotate",
            method="POST",
            body={"generation": generation, "model": model},
        )
        signal_wait()
        deadline = time.monotonic() + max(1.0, min(float(timeout_seconds), 300.0))
        last_error = "channel did not become ready"
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                health = self.health()
            except XiaChannelError as exc:
                last_error = str(exc)
                signal_wait()
                continue
            if health["ready"] and health["generation"] == generation and health["model"] == model and not health.get("requires_fresh"):
                return {**health, "fresh": True}
            last_error = "channel generation is not ready"
            signal_wait()
        raise XiaChannelUnavailable(last_error)

    def submit(
        self,
        *,
        request_id: str,
        client_id: str,
        epoch: int,
        lease: str,
        generation: int,
        text: str,
        handoff: str = "",
    ) -> dict[str, Any]:
        for name, value in (("request_id", request_id), ("client_id", client_id), ("lease", lease)):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", str(value or "")):
                raise ValueError(f"invalid {name}")
        return self._request(
            "/messages",
            method="POST",
            body={
                "request_id": request_id,
                "client_id": client_id,
                "provider": "claude",
                "contact_id": "ai-custom",
                "epoch": max(0, int(epoch)),
                "lease": lease,
                "generation": max(1, int(generation)),
                "text": str(text),
                "handoff": str(handoff),
            },
        )

    def result(
        self,
        *,
        request_id: str,
        client_id: str,
        epoch: int,
        lease: str,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        return self._request(
            "/result", method="POST",
            body={
                "request_id": request_id, "client_id": client_id,
                "epoch": max(0, int(epoch)), "lease": lease,
                "wait_ms": max(0, min(int(wait_seconds * 1000), 25_000)),
            },
            timeout=max(self.timeout_seconds, wait_seconds + 2.0),
        )

    def send_and_wait(
        self, *, timeout_seconds: float = 900.0,
        on_admitted: Any = None,
        on_wait: Any = None,
        **payload: Any,
    ) -> dict[str, Any]:
        last_wait_notice = float("-inf")

        def signal_wait() -> None:
            nonlocal last_wait_notice
            current = time.monotonic()
            if on_wait is not None and current - last_wait_notice >= 15.0:
                on_wait()
                last_wait_notice = current

        try:
            admitted = self.submit(**payload)
        except XiaChannelUnavailable:
            # The connection may have dropped after the durable ledger write.
            # Query the same exact grant before deciding it was pre-admission.
            try:
                admitted = self.result(
                    request_id=payload["request_id"], client_id=payload["client_id"],
                    epoch=payload["epoch"], lease=payload["lease"], wait_seconds=0,
                )
            except XiaChannelNotFound as exc:
                raise XiaChannelUnavailable("channel request was not admitted") from exc
            except XiaChannelUnavailable as exc:
                raise XiaChannelUncertain("channel admission could not be determined") from exc
        if on_admitted is not None:
            on_admitted()
        if str(admitted.get("status") or "") == "completed":
            return admitted
        deadline = time.monotonic() + max(1.0, min(float(timeout_seconds), 1800.0))
        while time.monotonic() < deadline:
            try:
                value = self.result(
                    request_id=payload["request_id"], client_id=payload["client_id"],
                    epoch=payload["epoch"], lease=payload["lease"], wait_seconds=min(20.0, deadline - time.monotonic()),
                )
            except XiaChannelUnavailable:
                signal_wait()
                time.sleep(0.25)
                continue
            status = str(value.get("status") or "")
            if status == "completed":
                return value
            if status == "uncertain":
                raise XiaChannelUncertain(str(value.get("error") or "channel request completion is uncertain"))
            if status in {"revoked", "stale"}:
                raise XiaChannelStale(str(value.get("error") or "channel request is stale"))
            signal_wait()
        raise XiaChannelUncertain("channel request timed out after admission")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the private auth header beyond the validated endpoint."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None
