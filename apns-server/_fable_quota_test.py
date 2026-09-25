"""肥波 (Fable) 周额度估计回归测试 (2026-09-25, offline; tmp 目录持久化).

Covers:
1. model     — Fable 家族判定: 大小写不敏感/含版本号, 非 Fable 不计
2. parse     — statusline 原始 JSON → 样本; 缺 7d% 不记账
3. accounting— 定稿口径: Fable delta ×2 计入, 非 Fable 忽略但锚点推进,
               首样本只锚定不记账, 估计值 clamp [0,100]
4. reset     — 7d% 下降 / resets_at 变化 → 周重置清零重记, 新周存量归当前模型
5. pseudo    — F1 回归: fallback→实测迁移/resets_at 间歇缺失 flap 不伪重置暴冲,
               两端均实测的 resets_at 变化仍判跨周, 旧状态文件缺 source 字段保守处理
6. persist   — JSON 落盘 + 重载恢复, 损坏文件从头开始
7. handler   — GET /fable-quota: tracker 缺失 503, 正常 200 带口径字段
8. sampler   — tmux option 读取容错 (非零退出/坏 JSON/超大), loop 单 tick 记账
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fable_quota  # noqa: E402
from push import PushHandler  # noqa: E402

WEEK = 7 * 86400
T0 = 1_790_000_000.0  # 任意锚点


def _sample(model, pct, *, ts=T0, resets_at=None, five_pct=None):
    return {
        "ts": ts,
        "model": model,
        "is_fable": fable_quota.is_fable_model(model),
        "seven_day_pct": float(pct),
        "five_hour_pct": five_pct,
        "seven_day_resets_at": resets_at,
    }


class ModelFamilyTests(unittest.TestCase):
    def test_fable_variants(self):
        for name in ("Fable 5.1", "fable", "FABLE 5", "claude-fable-5-1", "Claude Fable"):
            self.assertTrue(fable_quota.is_fable_model(name), name)

    def test_non_fable(self):
        for name in ("Opus 4.8", "claude-opus-5", "Sonnet 5", "Haiku 4.5", "", None):
            self.assertFalse(fable_quota.is_fable_model(name), name)


class ParseStatusPayloadTests(unittest.TestCase):
    PAYLOAD = {
        "model": {"id": "claude-fable-5-1", "display_name": "Fable 5.1"},
        "rate_limits": {
            "five_hour": {"used_percentage": 6, "resets_at": 1790344200},
            "seven_day": {"used_percentage": 49, "resets_at": 1790334000},
        },
    }

    def test_real_shape(self):
        sample = fable_quota.parse_status_payload(self.PAYLOAD, now=T0)
        self.assertIsNotNone(sample)
        self.assertEqual(sample["model"], "Fable 5.1")
        self.assertTrue(sample["is_fable"])
        self.assertEqual(sample["seven_day_pct"], 49.0)
        self.assertEqual(sample["five_hour_pct"], 6.0)
        self.assertEqual(sample["seven_day_resets_at"], 1790334000)
        self.assertEqual(sample["ts"], T0)

    def test_missing_rate_limits(self):
        for payload in ({}, {"rate_limits": {}}, {"rate_limits": {"seven_day": {}}},
                        {"rate_limits": {"seven_day": {"used_percentage": "x"}}},
                        "not-a-dict", None):
            self.assertIsNone(fable_quota.parse_status_payload(payload), payload)

    def test_model_id_fallback_and_missing_optionals(self):
        sample = fable_quota.parse_status_payload(
            {"model": {"id": "claude-opus-5"},
             "rate_limits": {"seven_day": {"used_percentage": 3}}},
            now=T0,
        )
        self.assertEqual(sample["model"], "claude-opus-5")
        self.assertFalse(sample["is_fable"])
        self.assertIsNone(sample["five_hour_pct"])
        self.assertIsNone(sample["seven_day_resets_at"])


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tracker = fable_quota.FableQuotaTracker(
            Path(self._tmp.name) / "fable_quota.json"
        )

    def test_first_sample_anchors_without_credit(self):
        event = self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        self.assertEqual(event["kind"], "init")
        self.assertEqual(self.tracker.snapshot()["fable_estimate_pct"], 0.0)
        self.assertEqual(self.tracker.snapshot()["official_seven_day_pct"], 40.0)

    def test_fable_delta_credited_double(self):
        self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        event = self.tracker.ingest(_sample("Fable 5.1", 41, resets_at=T0 + WEEK))
        self.assertEqual(event["kind"], "credit")
        self.assertAlmostEqual(event["credited"], 2.0)
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 2.0)

    def test_non_fable_delta_ignored_but_anchor_moves(self):
        self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        event = self.tracker.ingest(_sample("Opus 4.8", 43, resets_at=T0 + WEEK))
        self.assertEqual(event["kind"], "ignored_non_fable")
        self.assertEqual(self.tracker.snapshot()["fable_estimate_pct"], 0.0)
        # 锚点已推进: 下一个 Fable 样本只计 43→44 这一段, 不回头补 Opus 的 3%
        event = self.tracker.ingest(_sample("Fable 5.1", 44, resets_at=T0 + WEEK))
        self.assertAlmostEqual(event["credited"], 2.0)
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 2.0)

    def test_coarse_delta_between_samples(self):
        # 采样间隔内涨 5%: 按相邻样本差值记账, 归属后一个样本的模型
        self.tracker.ingest(_sample("Opus 4.8", 10, resets_at=T0 + WEEK))
        event = self.tracker.ingest(_sample("Fable 5.1", 15, resets_at=T0 + WEEK))
        self.assertAlmostEqual(event["credited"], 10.0)

    def test_estimate_clamped_at_100(self):
        self.tracker.ingest(_sample("Fable 5.1", 0, resets_at=T0 + WEEK))
        self.tracker.ingest(_sample("Fable 5.1", 80, resets_at=T0 + WEEK))
        self.assertEqual(self.tracker.snapshot()["fable_estimate_pct"], 100.0)

    def test_unchanged_pct_is_noop(self):
        self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        event = self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        self.assertEqual(event["kind"], "noop")
        self.assertEqual(self.tracker.snapshot()["fable_estimate_pct"], 0.0)


class WeekResetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tracker = fable_quota.FableQuotaTracker(
            Path(self._tmp.name) / "fable_quota.json"
        )

    def _build_estimate(self):
        self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        self.tracker.ingest(_sample("Fable 5.1", 45, resets_at=T0 + WEEK))
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 10.0)

    def test_pct_drop_resets_and_clears(self):
        self._build_estimate()
        event = self.tracker.ingest(_sample("Opus 4.8", 2, resets_at=T0 + 2 * WEEK))
        self.assertEqual(event["kind"], "week_reset")
        self.assertEqual(self.tracker.snapshot()["fable_estimate_pct"], 0.0)

    def test_reset_credits_current_sample_when_fable(self):
        self._build_estimate()
        event = self.tracker.ingest(_sample("Fable 5.1", 3, resets_at=T0 + 2 * WEEK))
        self.assertEqual(event["kind"], "week_reset")
        self.assertAlmostEqual(event["credited"], 6.0)
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 6.0)

    def test_resets_at_change_without_drop_also_resets(self):
        self._build_estimate()
        # 7d% 没降但重置点变了 → 同样视为跨周
        event = self.tracker.ingest(_sample("Opus 4.8", 50, resets_at=T0 + 2 * WEEK))
        self.assertEqual(event["kind"], "week_reset")
        self.assertEqual(self.tracker.snapshot()["fable_estimate_pct"], 0.0)

    def test_week_window_from_resets_at(self):
        self.tracker.ingest(_sample("Fable 5.1", 1, resets_at=T0 + WEEK))
        week = self.tracker.snapshot()["week"]
        self.assertEqual(week["reset_at"], T0 + WEEK)
        self.assertEqual(week["start_at"], T0)
        self.assertTrue(week["reset_bj"])

    def test_week_window_fallback_friday_19_bj(self):
        # 无 resets_at 时回落配置刷新点 (周五 19:00 UTC+8)
        friday_noon_utc = 1790337600.0  # 2026-09-25 周五 20:00 北京 (12:00 UTC)
        start, end = fable_quota.week_window_fallback(friday_noon_utc)
        self.assertEqual(end - start, WEEK)
        # 当周五 20:00 北京已过 19:00 → 本周起点就是当天 19:00 北京 = 11:00 UTC
        self.assertEqual(start, friday_noon_utc - 3600)


class PseudoResetTests(unittest.TestCase):
    """F1 回归: resets_at 间歇缺失 / fallback→实测迁移不得触发伪周重置暴冲。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tracker = fable_quota.FableQuotaTracker(
            Path(self._tmp.name) / "fable_quota.json"
        )

    def test_fallback_to_first_real_reset_is_silent_anchor_correction(self):
        # 审核用例①: 兜底阶段积累估计后, 首个带 resets_at 的样本到达
        # 不得清零暴冲 (旧实现 43%×2 → 86.0)。
        self.tracker.ingest(_sample("Fable 5.1", 40))  # 无 resets_at → fallback 锚
        event = self.tracker.ingest(_sample("Fable 5.1", 42))
        self.assertEqual(event["kind"], "credit")
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 4.0)
        # 首个实测 resets_at 样本: 只静默校正锚点, 按 delta 正常记账
        event = self.tracker.ingest(_sample("Fable 5.1", 43, resets_at=T0 + WEEK))
        self.assertEqual(event["kind"], "credit")
        self.assertTrue(event["anchor_corrected"])
        self.assertAlmostEqual(event["credited"], 2.0)
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 6.0)
        # 锚点已切到实测来源
        week = self.tracker.snapshot()["week"]
        self.assertEqual(week["reset_at"], T0 + WEEK)
        self.assertEqual(week["reset_source"], "statusline")

    def test_intermittent_resets_at_flap_does_not_pseudo_reset(self):
        # 审核用例②: resets_at 间歇缺失 → known 在真实值与兜底值间 flap,
        # 恢复实测时不得重复伪重置 (旧实现 est 暴冲 84.0)。
        self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        self.tracker.ingest(_sample("Fable 5.1", 41, resets_at=T0 + WEEK))
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 2.0)
        # 缺 resets_at 的样本: 锚点翻成 fallback 值, 但不算跨周
        event = self.tracker.ingest(_sample("Fable 5.1", 41))
        self.assertEqual(event["kind"], "noop")
        # 恢复 resets_at: 与 known (fallback) 不等但来源是 fallback → 不伪重置
        event = self.tracker.ingest(_sample("Fable 5.1", 42, resets_at=T0 + WEEK))
        self.assertEqual(event["kind"], "credit")
        self.assertTrue(event["anchor_corrected"])
        self.assertAlmostEqual(event["credited"], 2.0)
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 4.0)

    def test_fallback_anchor_never_rolls_week(self):
        # fallback → fallback: 两端都不是实测, 永不判跨周
        self.tracker.ingest(_sample("Fable 5.1", 40, ts=T0))
        event = self.tracker.ingest(_sample("Fable 5.1", 45, ts=T0 + 8 * 86400))
        self.assertNotEqual(event["kind"], "week_reset")
        self.assertAlmostEqual(self.tracker.snapshot()["fable_estimate_pct"], 10.0)

    def test_real_reset_change_still_rolls_after_correction(self):
        # 校正到实测锚点后, 真正的 resets_at 变化 (两端均实测) 仍判跨周
        self.tracker.ingest(_sample("Fable 5.1", 40))  # fallback 锚
        self.tracker.ingest(_sample("Fable 5.1", 41, resets_at=T0 + WEEK))
        event = self.tracker.ingest(_sample("Fable 5.1", 3, resets_at=T0 + 2 * WEEK))
        self.assertEqual(event["kind"], "week_reset")
        self.assertAlmostEqual(event["credited"], 6.0)

    def test_reload_legacy_state_without_source_is_conservative(self):
        # 升级前的旧状态文件没有 week_reset_source: 视为非实测锚点,
        # 首个 resets_at 变化只校正不暴冲 (真实跨周由 pct 下降路径兜底)。
        path = Path(self._tmp.name) / "fable_quota.json"
        self.tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK))
        self.tracker.ingest(_sample("Fable 5.1", 45, resets_at=T0 + WEEK))
        stored = json.loads(path.read_text())
        del stored["week_reset_source"]
        path.write_text(json.dumps(stored))
        reloaded = fable_quota.FableQuotaTracker(path)
        event = reloaded.ingest(_sample("Fable 5.1", 46, resets_at=T0 + 2 * WEEK))
        self.assertNotEqual(event["kind"], "week_reset")
        self.assertTrue(event["anchor_corrected"])
        self.assertAlmostEqual(event["credited"], 2.0)


class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fable_quota.json"
            tracker = fable_quota.FableQuotaTracker(path)
            tracker.ingest(_sample("Fable 5.1", 40, resets_at=T0 + WEEK, five_pct=6))
            tracker.ingest(_sample("Fable 5.1", 42, resets_at=T0 + WEEK, five_pct=7))
            stored = json.loads(path.read_text())
            self.assertAlmostEqual(stored["fable_estimate_pct"], 4.0)
            self.assertEqual(stored["last_sample"]["model"], "Fable 5.1")
            self.assertEqual(len(stored["samples"]), 2)

            reloaded = fable_quota.FableQuotaTracker(path)
            snap = reloaded.snapshot()
            self.assertAlmostEqual(snap["fable_estimate_pct"], 4.0)
            self.assertEqual(snap["official_seven_day_pct"], 42.0)
            self.assertEqual(snap["sample_count"], 2)
            # 重载后锚点仍在: 继续记账不重复计入存量
            event = reloaded.ingest(_sample("Fable 5.1", 43, resets_at=T0 + WEEK))
            self.assertAlmostEqual(event["credited"], 2.0)

    def test_corrupt_state_file_starts_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fable_quota.json"
            path.write_text("{not json")
            tracker = fable_quota.FableQuotaTracker(path)
            self.assertEqual(tracker.snapshot()["fable_estimate_pct"], 0.0)

    def test_samples_ring_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = fable_quota.FableQuotaTracker(
                Path(directory) / "fable_quota.json", max_samples=3
            )
            for i in range(6):
                tracker.ingest(_sample("Opus 4.8", i, resets_at=T0 + WEEK))
            self.assertEqual(tracker.snapshot()["sample_count"], 3)


class HandlerTests(unittest.TestCase):
    def _make_handler(self, tracker):
        handler = object.__new__(PushHandler)
        handler.responses = []
        handler._send_json = lambda status, payload: handler.responses.append((status, payload))
        handler.state = SimpleNamespace(fable_quota_tracker=tracker)
        return handler

    def test_disabled_returns_503(self):
        handler = self._make_handler(None)
        handler._handle_fable_quota_get()
        status, body = handler.responses[-1]
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])

    def test_snapshot_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = fable_quota.FableQuotaTracker(Path(directory) / "q.json")
            tracker.ingest(_sample("Fable 5.1", 49, resets_at=T0 + WEEK, five_pct=6))
            handler = self._make_handler(tracker)
            handler._handle_fable_quota_get()
            status, body = handler.responses[-1]
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["fable_estimate_pct"], 0.0)
            self.assertEqual(body["official_seven_day_pct"], 49.0)
            self.assertEqual(body["current_model"], "Fable 5.1")
            self.assertEqual(body["last_sample"]["five_hour_pct"], 6)
            self.assertEqual(body["week"]["reset_at"], T0 + WEEK)
            self.assertEqual(body["multiplier"], 2.0)
            self.assertIn("Fable", body["methodology"])


class SamplerTests(unittest.TestCase):
    def test_read_option_tolerates_failures(self):
        for proc in (SimpleNamespace(returncode=1, stdout=""),
                     SimpleNamespace(returncode=0, stdout="not json"),
                     SimpleNamespace(returncode=0, stdout="x" * (65 * 1024)),
                     SimpleNamespace(returncode=0, stdout='["list"]')):
            result = fable_quota.read_claude_status_option(runner=lambda *a, **k: proc)
            self.assertEqual(result, {}, proc)

    def test_read_option_parses_payload(self):
        payload = {"rate_limits": {"seven_day": {"used_percentage": 1}}}
        proc = SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        self.assertEqual(
            fable_quota.read_claude_status_option(runner=lambda *a, **k: proc), payload
        )

    def test_sampler_loop_single_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = fable_quota.FableQuotaTracker(Path(directory) / "q.json")
            stop = threading.Event()
            payload = {
                "model": {"display_name": "Fable 5.1"},
                "rate_limits": {"seven_day": {"used_percentage": 7, "resets_at": T0 + WEEK}},
            }

            def read_once():
                stop.set()  # 一个 tick 后退出循环
                return payload

            fable_quota.sampler_loop(
                tracker, interval_seconds=5, stop_event=stop, read_status=read_once,
                now=lambda: T0,
            )
            snap = tracker.snapshot()
            self.assertEqual(snap["official_seven_day_pct"], 7.0)
            self.assertEqual(snap["current_model"], "Fable 5.1")


if __name__ == "__main__":
    unittest.main()
