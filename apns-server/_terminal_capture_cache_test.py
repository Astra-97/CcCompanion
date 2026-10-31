"""Remote terminal capture cache tests (2026-10-31, Tokyo terminals).

覆盖:
  1. 单飞: 同一 key 并发 poll 只跑一次 producer
  2. 后台刷新: viewer 在看时连续刷新, poll 直接拿缓存 (不等 ssh); 停止 poll 后很快停
  3. 输入优先: input_scope 期间刷新暂停, 结束后作废缓存 → 下一次 poll 一定是输入之后的画面
  4. producer 异常 → 503, 不卡死
  5. PushHandler 集成: kimi / kiro / kirocrew 走缓存 (假 state), 其它 session 不走;
     /tmux/send 等输入路径包 input_scope
"""
from __future__ import annotations

import threading
import time
import types
import unittest

import push
from terminal_capture_cache import TerminalCaptureCache


class SlowProducer:
    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.calls = 0
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0

    def __call__(self):
        with self.lock:
            self.calls += 1
            n = self.calls
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(self.delay)
        with self.lock:
            self.running -= 1
        return 200, {"ok": True, "content": f"frame {n}"}


class CacheTest(unittest.TestCase):
    def test_single_flight(self) -> None:
        cache = TerminalCaptureCache(background=False)
        prod = SlowProducer(0.3)
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(cache.get("kimi", 120, prod))) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(prod.calls, 1)
        self.assertEqual({r[1]["content"] for r in results}, {"frame 1"})
        stats = cache.stats()["kimi"]
        self.assertEqual(stats["requests"], 5)
        self.assertEqual(stats["request_fetches"], 1)
        # 一个新鲜帧之内再 poll 直接命中
        self.assertEqual(cache.get("kimi", 120, prod)[1]["content"], "frame 1")
        self.assertEqual(prod.calls, 1)
        # lines 不同是另一个 key
        cache.get("kimi", 50, prod)
        self.assertEqual(prod.calls, 2)

    def test_background_refresher_serves_instantly_then_stops(self) -> None:
        cache = TerminalCaptureCache(viewer_window_s=0.6, refresh_gap_s=0.05)
        prod = SlowProducer(0.2)
        cache.get("kiro", 120, prod)  # 冷: 自己跑一次
        time.sleep(0.6)
        started = time.monotonic()
        status, payload = cache.get("kiro", 120, prod)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(prod.calls, 2)  # 后台已经刷新过
        self.assertEqual(prod.max_running, 1)    # 后台和请求从不并发跑 producer
        time.sleep(1.2)
        self.assertEqual(cache.active_refreshers(), 0)
        calls = prod.calls
        time.sleep(0.4)
        self.assertEqual(prod.calls, calls)  # 没人看就不再碰远端

    def test_input_scope_pauses_and_invalidates(self) -> None:
        cache = TerminalCaptureCache(viewer_window_s=2.0, refresh_gap_s=0.02)
        frames = {"n": 0}
        typed = {"v": ""}

        def producer():
            frames["n"] += 1
            time.sleep(0.05)
            return 200, {"content": f"{typed['v']}#{frames['n']}"}

        cache.get("kimi", 120, producer)
        with cache.input_scope("kimi"):
            before = frames["n"]
            time.sleep(0.2)
            # 输入期间不起新的刷新 (最多收尾一个已经在跑的)
            self.assertLessEqual(frames["n"] - before, 1)
            typed["v"] = "ls"
        status, payload = cache.get("kimi", 120, producer)
        self.assertTrue(payload["content"].startswith("ls#"), payload)
        with cache.input_scope(None):  # 非远端目标: 什么也不做
            pass

    def test_producer_exception_is_503(self) -> None:
        cache = TerminalCaptureCache(background=False)

        def boom():
            raise RuntimeError("ssh died")

        status, payload = cache.get("kirocrew", 120, boom)
        self.assertEqual(status, 503)
        self.assertEqual(payload["target"], "kirocrew")
        self.assertEqual(cache.get("kirocrew", 120, lambda: (200, {"ok": True}))[0], 503)  # 新鲜期内仍是缓存
        cache.invalidate("kirocrew")
        self.assertEqual(cache.get("kirocrew", 120, lambda: (200, {"ok": True}))[0], 200)


class HandlerIntegrationTest(unittest.TestCase):
    def handler(self, path: str, cache: TerminalCaptureCache | None) -> push.PushHandler:
        h = object.__new__(push.PushHandler)
        h.path = path
        h.headers = {}
        h.state = types.SimpleNamespace(default_session="cctg", terminal_capture_cache=cache)
        h.responses = []
        h._send_json = lambda status, payload=None, **_kw: h.responses.append((status, payload))
        return h

    def test_remote_targets_go_through_cache(self) -> None:
        cache = TerminalCaptureCache(background=False)
        calls: list = []

        def direct(self, requested, lines):  # noqa: ANN001 - patched method
            calls.append((requested, lines, self.path))
            self._send_json(200, {"ok": True, "session": requested, "content": "x"})

        original = push.PushHandler._handle_tmux_capture_direct
        push.PushHandler._handle_tmux_capture_direct = direct
        try:
            for target in ("kimi", "kiro", "kirocrew"):
                h = self.handler(f"/tmux/capture?session={target}&lines=80", cache)
                h._handle_tmux_capture()
                self.assertEqual(h.responses, [(200, {"ok": True, "session": target, "content": "x"})])
                h2 = self.handler(f"/tmux/capture?session={target}&lines=80", cache)
                h2._handle_tmux_capture()  # 命中缓存
            self.assertEqual([c[0] for c in calls], ["kimi", "kiro", "kirocrew"])
            self.assertEqual(calls[0][2], "/tmux/capture?session=kimi&lines=80")  # 分离的 handler
            h = self.handler("/tmux/capture?session=cctg", cache)
            h._handle_tmux_capture()
            h._handle_tmux_capture()
            self.assertEqual([c[0] for c in calls][-2:], ["cctg", "cctg"])  # 本机终端不缓存
            # 没配缓存 (老 state / 测试替身) → 原路径
            h = self.handler("/tmux/capture?session=kimi", None)
            h._handle_tmux_capture()
            self.assertEqual(calls[-1][0], "kimi")
        finally:
            push.PushHandler._handle_tmux_capture_direct = original

    def test_input_scope_helper(self) -> None:
        cache = TerminalCaptureCache(background=False)
        h = self.handler("/tmux/send", cache)
        cache.get("kiro", 120, lambda: (200, {"v": 1}))
        with h._terminal_input_scope({"session": "Kiro"}):
            pass
        self.assertEqual(cache.get("kiro", 120, lambda: (200, {"v": 2}))[1], {"v": 2})
        with h._terminal_input_scope({"session": "cctg"}):
            pass
        with h._terminal_input_scope("junk"):
            pass


if __name__ == "__main__":
    unittest.main()
