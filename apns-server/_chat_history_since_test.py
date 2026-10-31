"""/chat/history?since= 旧游标快路径 (2026-10-31).

App 的 since 游标停在几天前时, 以前每次轮询都整文件扫描 (77 MB ≈1.1 s)。
尾部缓存 (最后 1000 条) 里已有 ≥ limit 条匹配时, 结果与全量扫描逐条相同, 不再碰文件。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chat_history import ChatHistory


def _ts(i: int) -> str:
    day, rest = divmod(i, 1000)
    return f"2026-10-{10 + day:02d}T{rest // 60 % 24:02d}:{rest % 60:02d}:00.000+00:00"


class SinceFastPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "h.jsonl"
        self.records = []
        for i in range(3000):
            rec = {"ts": _ts(i), "role": "assistant", "text": f"m{i}"}
            if i % 7 == 0:
                rec["hidden_in_ui"] = True
            self.records.append(rec)
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.records), encoding="utf-8")
        self.chat = ChatHistory(self.path)

    def brute(self, since: str, limit: int, include_hidden: bool = False) -> list:
        out = [r for r in self.records if r["ts"] > since and (include_hidden or not r.get("hidden_in_ui"))]
        return out[-limit:]

    def test_stale_cursor_served_from_cache(self) -> None:
        since = _ts(10)  # 远早于尾部缓存
        self.chat.read_since(since_ts=None, limit=1)  # 预热缓存
        with mock.patch.object(Path, "open", side_effect=AssertionError("full scan")):
            got = self.chat.read_since(since_ts=since, limit=500)
            got_hidden = self.chat.read_since(since_ts=since, limit=50, include_hidden=True)
        self.assertEqual(got, self.brute(since, 500))
        self.assertEqual(got_hidden, self.brute(since, 50, include_hidden=True))

    def test_large_limit_still_scans(self) -> None:
        since = _ts(10)
        self.assertEqual(self.chat.read_since(since_ts=since, limit=2500), self.brute(since, 2500))

    def test_recent_cursor_and_before(self) -> None:
        since = _ts(2990)
        self.assertEqual(self.chat.read_since(since_ts=since, limit=500), self.brute(since, 500))
        self.assertEqual(self.chat.read_since(since_ts=_ts(2999), limit=500), [])
        before = self.chat.read_since(before_ts=_ts(100), limit=5)
        expected = [x for x in self.records[:100] if not x.get("hidden_in_ui")][-5:]
        self.assertEqual(before, expected)


if __name__ == "__main__":
    unittest.main()
