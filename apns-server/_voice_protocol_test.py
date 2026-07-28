#!/usr/bin/env python3
"""Regression tests for the voice-call WS protocol contract (v1.9.99).

Run: .venv/bin/python _voice_protocol_test.py

Covers the capability handshake, which is the load-bearing part: an APK built
before a frame type existed must keep seeing exactly the stream it was written
against. If `thinking` frames ever start leaking to clients that did not ask
for them, an old build logs an unknown type on every tick — and a stricter one
could disconnect.

Also pins `fatal` on error frames. Before it existed, one failed ASR looked
identical to "this call is over" and the Android client hung up on itself.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import voice_call_ws as V  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  {status}  {name}{(' ' + detail) if not cond and detail else ''}")
    if not cond:
        failures.append(name)


class FakeWs:
    """Collects the JSON frames the server would have sent."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            return
        self.sent.append(json.loads(payload))

    def types(self) -> list[str]:
        return [f.get("type") for f in self.sent]


NEW_CLIENT = frozenset({"thinking_frames", "canned_audio", "app_ping"})
OLD_CLIENT = frozenset()


print("test_1: thinking frames only reach clients that asked for them")


async def _t1():
    # A "slow LLM" long enough for several ticks.
    tick = V.THINKING_TICK_SEC

    new_ws, old_ws = FakeWs(), FakeWs()
    for ws, caps in ((new_ws, NEW_CLIENT), (old_ws, OLD_CLIENT)):
        async with V.thinking_ticker(ws, caps, asyncio.Event()):
            await asyncio.sleep(tick * 2.4)
    return new_ws, old_ws


new_ws, old_ws = asyncio.run(_t1())
check(
    "capable client gets a thinking drip",
    new_ws.types().count("thinking") >= 3,
    f"(got {new_ws.types()})",
)
check(
    "first tick is immediate (no dead air before the drip starts)",
    new_ws.sent and new_ws.sent[0].get("elapsed_ms", 9999) < 100,
    f"(first={new_ws.sent[:1]})",
)
check(
    "elapsed_ms increases",
    len(new_ws.sent) >= 2
    and new_ws.sent[-1]["elapsed_ms"] > new_ws.sent[0]["elapsed_ms"],
)
check(
    "legacy client sees nothing new on the wire",
    old_ws.sent == [],
    f"(got {old_ws.types()})",
)


print("test_2: the ticker stops when the turn is interrupted")


async def _t2():
    ws = FakeWs()
    interrupt = asyncio.Event()
    async with V.thinking_ticker(ws, NEW_CLIENT, interrupt):
        await asyncio.sleep(V.THINKING_TICK_SEC * 0.5)
        interrupt.set()
        await asyncio.sleep(V.THINKING_TICK_SEC * 2.2)
    return ws


ws2 = asyncio.run(_t2())
check(
    "no further thinking frames after interrupt",
    len(ws2.sent) <= 1,
    f"(got {len(ws2.sent)} frames)",
)


print("test_3: the ticker is torn down with its context")


async def _t3():
    ws = FakeWs()
    async with V.thinking_ticker(ws, NEW_CLIENT, asyncio.Event()):
        await asyncio.sleep(V.THINKING_TICK_SEC * 0.3)
    before = len(ws.sent)
    await asyncio.sleep(V.THINKING_TICK_SEC * 2.0)
    return before, len(ws.sent)


before, after = asyncio.run(_t3())
check("no leaked task keeps sending after exit", before == after, f"({before} -> {after})")


print("test_4: error severity")


async def _t4():
    ws = FakeWs()
    await V.send_error(ws, "asr failed: whatever")
    await V.send_error(ws, "voice call contact not supported: nobody", fatal=True)
    return ws


ws4 = asyncio.run(_t4())
check("per-turn error is not fatal", ws4.sent[0]["fatal"] is False, f"({ws4.sent[0]})")
check("unsupported contact is fatal", ws4.sent[1]["fatal"] is True, f"({ws4.sent[1]})")
check(
    "error frames keep the legacy shape (type/msg)",
    all(f["type"] == "error" and "msg" in f for f in ws4.sent),
)


print("test_5: advertised server capabilities cover what the client can ask for")
check(
    "thinking_frames advertised",
    "thinking_frames" in V.SERVER_CAPABILITIES,
)
check("app_ping advertised", "app_ping" in V.SERVER_CAPABILITIES)
check("error_severity advertised", "error_severity" in V.SERVER_CAPABILITIES)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("all voice protocol tests passed")
