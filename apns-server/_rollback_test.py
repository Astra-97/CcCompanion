"""rollback_driver 单测 — 纯函数部分（定位 / 副作用上锁 / 截断）。

跑法：cd apns-server && python3 _rollback_test.py
不碰 tmux、不碰 live 会话；「真实 fork jsonl」用例只读 session 文件做
不变量校验，不打印内容。
"""

from __future__ import annotations

import glob
import json
import os
import tempfile
import unittest
from pathlib import Path

import rollback_driver as rd


def _u(i: int) -> str:
    return f"uuid-{i:04d}"


def _user(i: int, text: str, *, channel: bool = False, ts: str = "") -> dict:
    content = text
    if channel:
        content = (
            f'<channel source="companion" ts="{ts}">'
            f'{text}<metadata_json>{{"user_record_ts": "{ts}"}}</metadata_json></channel>'
        )
    e = {
        "type": "user",
        "uuid": _u(i),
        "parentUuid": _u(i - 1) if i > 0 else None,
        "sessionId": "old-sid",
        "message": {"role": "user", "content": content},
        "timestamp": f"2026-07-08T10:{i:02d}:00.000Z",
    }
    if channel:
        e["isMeta"] = True
        e["promptSource"] = "system"
    else:
        e["promptSource"] = "typed"
    return e


def _assistant(i: int, text: str = "好", tools: list[dict] | None = None) -> dict:
    content: list = [{"type": "text", "text": text}]
    for t in tools or []:
        content.append({"type": "tool_use", "id": f"tool-{i}", "name": t["name"], "input": t.get("input", {})})
    return {
        "type": "assistant",
        "uuid": _u(i),
        "parentUuid": _u(i - 1),
        "sessionId": "old-sid",
        "message": {"role": "assistant", "model": "claude-x", "content": content},
        "timestamp": f"2026-07-08T10:{i:02d}:01.000Z",
    }


def _system(i: int) -> dict:
    return {
        "type": "system",
        "uuid": _u(i),
        "parentUuid": _u(i - 1),
        "sessionId": "old-sid",
        "content": "hook output",
    }


def _simple_branch() -> list[dict]:
    """u0 user / u1 asst / u2 user(channel) / u3 asst / u4 user / u5 asst"""
    return [
        _user(0, "第一条"),
        _assistant(1, "回1"),
        _user(2, "第二条 channel", channel=True, ts="2026-07-08T10:02:00.000+08:00"),
        _assistant(3, "回2"),
        _user(4, "第三条"),
        _assistant(5, "回3"),
    ]


class ActiveBranchTest(unittest.TestCase):
    def test_fork_takes_latest_branch(self):
        entries = _simple_branch()
        # fork：从 u3 分出新分支（rewind 后 append 在尾部）
        forked_user = {
            "type": "user", "uuid": "fork-u", "parentUuid": _u(3),
            "sessionId": "old-sid", "promptSource": "typed",
            "message": {"role": "user", "content": "fork后的新消息"},
        }
        forked_asst = {
            "type": "assistant", "uuid": "fork-a", "parentUuid": "fork-u",
            "sessionId": "old-sid",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "fork回"}]},
        }
        branch = rd.active_branch(entries + [forked_user, forked_asst])
        uuids = [e["uuid"] for e in branch]
        self.assertEqual(uuids, [_u(0), _u(1), _u(2), _u(3), "fork-u", "fork-a"])
        # 废弃分支的 u4/u5 不在活跃分支里
        self.assertNotIn(_u(4), uuids)


class FindTargetTest(unittest.TestCase):
    def test_ts_match_beats_text(self):
        branch = _simple_branch()
        idx = rd.find_target_index(branch, user_record_ts="2026-07-08T10:02:00.000+08:00", raw_text="第三条")
        self.assertEqual(idx, 2)

    def test_text_fallback_takes_last_hit(self):
        branch = _simple_branch()
        idx = rd.find_target_index(branch, raw_text="第三条")
        self.assertEqual(idx, 4)

    def test_not_found(self):
        with self.assertRaises(rd.RollbackRefused):
            rd.find_target_index(_simple_branch(), raw_text="不存在的消息")


class SideEffectTest(unittest.TestCase):
    def test_telegram_always_refused(self):
        branch = _simple_branch()
        branch[3] = _assistant(3, "回2", tools=[{"name": "mcp__plugin_telegram_telegram__reply"}])
        self.assertTrue(rd.scan_side_effects(branch, 2))

    def test_same_turn_reply_allowed(self):
        branch = _simple_branch()
        branch[5] = _assistant(5, "回3", tools=[{"name": "mcp__companion__reply"}])
        self.assertEqual(rd.scan_side_effects(branch, 4), [])

    def test_later_turn_reply_refused(self):
        branch = _simple_branch()
        branch[5] = _assistant(5, "回3", tools=[{"name": "mcp__companion__reply"}])
        self.assertTrue(rd.scan_side_effects(branch, 2))

    def test_git_push_refused(self):
        branch = _simple_branch()
        branch[3] = _assistant(3, "回2", tools=[{"name": "Bash", "input": {"command": "cd /x && git push origin main"}}])
        self.assertTrue(rd.scan_side_effects(branch, 2))


class TruncateTest(unittest.TestCase):
    def test_basic_truncate(self):
        branch = _simple_branch()
        kept = rd.build_truncated_events(branch, 4, "new-sid")
        self.assertEqual([e["uuid"] for e in kept], [_u(0), _u(1), _u(2), _u(3)])
        # 目标(u4)和之后(u5)都不在
        self.assertNotIn(_u(4), [e["uuid"] for e in kept])
        # 链重建：首条 None，逐条衔接，sessionId 全部换新
        prev = None
        for e in kept:
            self.assertEqual(e["parentUuid"], prev)
            self.assertEqual(e["sessionId"], "new-sid")
            prev = e["uuid"]
        # 截断点校验：最后一条 == 目标的 parent
        self.assertEqual(kept[-1]["uuid"], branch[4]["parentUuid"])
        # 原 branch 未被改写（深拷贝）
        self.assertEqual(branch[0]["sessionId"], "old-sid")
        self.assertIsNone(branch[0]["parentUuid"])

    def test_channel_target_truncatable(self):
        """v1 的原生 Rewind 认不了 channel 消息；v2 截断必须支持。"""
        branch = _simple_branch()
        kept = rd.build_truncated_events(branch, 2, "new-sid")
        self.assertEqual([e["uuid"] for e in kept], [_u(0), _u(1)])

    def test_first_message_refused(self):
        with self.assertRaises(rd.RollbackRefused) as cm:
            rd.build_truncated_events(_simple_branch(), 0, "new-sid")
        self.assertEqual(cm.exception.code, "target_is_first")

    def test_broken_chain_aborts(self):
        branch = _simple_branch()
        branch[4]["parentUuid"] = "uuid-9999"  # 篡改：目标 parent 对不上
        with self.assertRaises(rd.RollbackError) as cm:
            rd.build_truncated_events(branch, 4, "new-sid")
        self.assertEqual(cm.exception.code, "truncate_verify_failed")

    def test_non_keep_parent_spliced(self):
        """目标的直接 parent 是 system 条目：截断丢弃它，链在最后一个
        user/assistant 处拼接。"""
        branch = [
            _user(0, "第一条"),
            _assistant(1, "回1"),
            _system(2),
            _user(3, "第二条"),
            _assistant(4, "回2"),
        ]
        kept = rd.build_truncated_events(branch, 3, "new-sid")
        self.assertEqual([e["uuid"] for e in kept], [_u(0), _u(1)])

    def test_target_not_user_prompt_refused(self):
        branch = _simple_branch()
        with self.assertRaises(rd.RollbackRefused):
            rd.build_truncated_events(branch, 3, "new-sid")  # assistant 条

    def test_verify_truncated_file_roundtrip(self):
        branch = _simple_branch()
        kept = rd.build_truncated_events(branch, 4, "new-sid")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                for e in kept:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            rd.verify_truncated_file(p, "new-sid", len(kept), kept[-1]["uuid"])
            with self.assertRaises(rd.RollbackError):
                rd.verify_truncated_file(p, "new-sid", len(kept) + 1, kept[-1]["uuid"])
            with self.assertRaises(rd.RollbackError):
                rd.verify_truncated_file(p, "wrong-sid", len(kept), kept[-1]["uuid"])


class FormatInjectionTest(unittest.TestCase):
    def test_prefix(self):
        out = rd.format_injection("你好", "2026-07-08T20:15:33.123+08:00")
        self.assertEqual(out, "[2026-07-08 20:15:33] 你好")

    def test_bad_ts_no_prefix(self):
        self.assertEqual(rd.format_injection("你好", "not-a-ts"), "你好")
        self.assertEqual(rd.format_injection("你好", ""), "你好")


class RealForkedJsonlTest(unittest.TestCase):
    """对着真实 fork 过的 session jsonl 验证截断不变量（只读，不打印内容）。"""

    def _find_forked(self) -> str | None:
        import collections
        for path in sorted(glob.glob("/root/.claude/projects/-root/*.jsonl"), key=os.path.getmtime, reverse=True):
            if os.path.getsize(path) > 8_000_000:
                continue
            children: collections.Counter = collections.Counter()
            try:
                for e in rd.read_session_entries(path):
                    pu = e.get("parentUuid")
                    if isinstance(pu, str):
                        children[pu] += 1
            except Exception:
                continue
            if any(v > 1 for v in children.values()):
                return path
        return None

    def test_truncate_invariants_on_real_fork(self):
        path = self._find_forked()
        if not path:
            self.skipTest("no forked session jsonl on this machine")
        entries = rd.read_session_entries(path)
        branch = rd.active_branch(entries)
        self.assertTrue(branch)
        # 活跃分支本身链条连续
        for prev, cur in zip(branch, branch[1:]):
            self.assertEqual(cur.get("parentUuid"), prev.get("uuid"))
        # 目标取活跃分支里最后一条用户发言（且不是第一条）
        idx = -1
        for i, e in enumerate(branch):
            if i > 0 and rd.is_user_prompt(e):
                idx = i
        if idx < 1:
            self.skipTest("no non-first user prompt in active branch")
        kept = rd.build_truncated_events(branch, idx, "test-sid")
        self.assertTrue(kept)
        dropped_uuids = {e.get("uuid") for e in branch[idx:]}
        prev_u = None
        for e in kept:
            self.assertIn(e.get("type"), rd.KEEP_TYPES)
            self.assertEqual(e.get("sessionId"), "test-sid")
            self.assertEqual(e.get("parentUuid"), prev_u)
            self.assertNotIn(e.get("uuid"), dropped_uuids)
            prev_u = e.get("uuid")
        # 末条衔接目标 parent（parent 为 keep 类型时严格相等）
        if branch[idx - 1].get("type") in rd.KEEP_TYPES:
            self.assertEqual(kept[-1]["uuid"], branch[idx]["parentUuid"])
        # json 可序列化
        for e in kept:
            json.dumps(e, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
