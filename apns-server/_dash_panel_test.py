"""小南面板 (xiaonan-dash) 聚合层 + panel-data 端点回归测试 (2026-10-09, offline).

Covers:
1. parse        — diary.health 正文解析: 饮食段落/睡眠/心率/体重, 含空格变体与缺项
2. ledger       — Notion 账本记录摊平 + 本月汇总 (None 金额跳过/分类降序) + 分页
3. shenkong     — 日程本 14 天圆点状态机 (done/missed/pending/none, 精确标题匹配)
                  + 日记原句截取 + diary 日期推断
4. cache        — 每板块 TTL 命中/过期重取/失败回 stale/无缓存降级
5. degrade      — token 文件缺失时对应板块 degraded, 聚合整体仍 ok
6. http         — GET /plugins/xiaonan-dash/panel-data: 未授权 401 / scoped token
                  不放行 / 其他插件 id 404 / 未知插件 404 / 正常 200 契约
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dash_panel  # noqa: E402
from dash_panel import DashPanelService, SourceUnavailable  # noqa: E402
from plugins_store import PluginStore  # noqa: E402
from push import PushHandler  # noqa: E402

SECRET = "dash-panel-test-secret"

HEALTH_FULL = """## 🍽️🏃 Day209 饮食+运动健康
日期: 2026-10-08
标签: 饮食, 运动, 健康
心情: 🥶 冷
😴 睡眠（10/7 22:30 → 10/8 07:10，Tasker 08:17）
总时长 8h40m / 实睡 8h16m / 清醒 24m
深睡 1h40m / 浅睡 5h6m / REM 1h30m
🫀 心脏
心率 90（最低 56 / 平均 64 / 最高 93）；血氧 97%
⚖️ 身体
体重 57.4kg（07:30）
🍽️ 饮食
早餐：全麦面包 ×2 + 牛奶咖啡 + 鸡蛋 ×2
午餐（11:50）：大虾土豆泥沙拉佐酱油
晚餐：排骨蔬菜锅 + 酱牛肉
🏃 运动
截至 08:17 步数 851
🧍 状态
胀气
"""

HEALTH_COMPACT = """## 🍽️🏃 Day208 饮食+运动健康
日期: 2026-10-07
😴 睡眠（10/6 23:53 → 10/7 08:01）
总时长 8h8m / 实睡 7h40m / 清醒 28m
深睡 2h31m（33%）/ 浅睡 3h46m
🫀 心脏
心率 85（最低56 / 平均67 / 最高91）
⚖️ 身体
周期 Day15
🍽️ 饮食
早餐（全季酒店自助）：包子皮 + 馄饨 + 牛奶咖啡
🏃 运动
"""

HEALTH_MISSING = """## 🍽️🏃 Day203 饮食+运动健康
日期: 2026-10-02
😴 睡眠
🫀 心脏
⚖️ 身体
体重 55.5 kg
🧍 状态
直接没有饮食段落
"""


def _notion_page(title, amount, day, category):
    return {
        "properties": {
            "项目": {"title": [{"plain_text": title}]},
            "金额": {"number": amount},
            "日期": {"date": {"start": day}},
            "分类": {"select": {"name": category} if category else None},
        }
    }


def _service(tmp: Path, **kw) -> DashPanelService:
    return DashPanelService(
        memory_config_path=tmp / "config.toml",
        notion_token_path=tmp / ".notion_token",
        schedule_path=tmp / "events.json",
        **kw,
    )


# ---------- 1. diary.health 解析 ----------

class ParseHealthTest(unittest.TestCase):
    def test_full_entry(self):
        e = dash_panel.parse_health_entry(HEALTH_FULL)
        self.assertEqual(e["day"], 209)
        self.assertEqual(e["date"], "2026-10-08")
        self.assertEqual(e["sleep"], {
            "sleep_real": "8h16m", "sleep_deep": "1h40m",
            "hr_morning": 90, "hr_min": 56, "weight_kg": 57.4,
        })
        self.assertEqual(e["diet_lines"], [
            "早餐：全麦面包 ×2 + 牛奶咖啡 + 鸡蛋 ×2",
            "午餐（11:50）：大虾土豆泥沙拉佐酱油",
            "晚餐：排骨蔬菜锅 + 酱牛肉",
        ])

    def test_compact_spacing_and_percent_suffix(self):
        e = dash_panel.parse_health_entry(HEALTH_COMPACT)
        self.assertEqual(e["sleep"]["hr_morning"], 85)
        self.assertEqual(e["sleep"]["hr_min"], 56)
        self.assertEqual(e["sleep"]["sleep_deep"], "2h31m")
        self.assertIsNone(e["sleep"]["weight_kg"])
        self.assertEqual(len(e["diet_lines"]), 1)

    def test_missing_fields_stay_none(self):
        e = dash_panel.parse_health_entry(HEALTH_MISSING)
        self.assertEqual(e["day"], 203)
        self.assertIsNone(e["sleep"]["sleep_real"])
        self.assertIsNone(e["sleep"]["hr_morning"])
        self.assertEqual(e["sleep"]["weight_kg"], 55.5)
        self.assertEqual(e["diet_lines"], [])  # 没有「🍽️ 饮食」段落标题 → 空

    def test_garbage_input(self):
        self.assertEqual(dash_panel.parse_health_entry(None), {})
        e = dash_panel.parse_health_entry("随便一段没有结构的话")
        self.assertIsNone(e["day"])
        self.assertEqual(e["diet_lines"], [])


# ---------- 2. Notion 账本 ----------

class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="_dash_ledger_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_parse_page(self):
        e = dash_panel.parse_notion_ledger_page(
            _notion_page("瑞幸 柠檬气泡咖啡", 13.9, "2026-10-08", "餐饮"))
        self.assertEqual(e, {"date": "2026-10-08", "category": "餐饮",
                             "amount": 13.9, "title": "瑞幸 柠檬气泡咖啡"})
        # 缺日期丢弃; 缺分类落「未分类」; 空标题兜底
        self.assertIsNone(dash_panel.parse_notion_ledger_page(
            _notion_page("x", 1, None, "餐饮")))
        e = dash_panel.parse_notion_ledger_page(_notion_page("", None, "2026-10-01", None))
        self.assertEqual(e["category"], "未分类")
        self.assertIsNone(e["amount"])
        self.assertEqual(e["title"], "（无标题）")
        self.assertIsNone(dash_panel.parse_notion_ledger_page({"properties": "bad"}))

    def test_summarize(self):
        entries = [
            {"date": "2026-10-08", "category": "餐饮", "amount": 13.9, "title": "a"},
            {"date": "2026-10-07", "category": "餐饮", "amount": 35.2, "title": "b"},
            {"date": "2026-10-06", "category": "订阅", "amount": 100.0, "title": "c"},
            {"date": "2026-10-05", "category": "其他", "amount": None, "title": "d"},
        ]
        s = dash_panel.summarize_ledger(entries, "2026-10")
        self.assertEqual(s["month"], "2026-10")
        self.assertAlmostEqual(s["total"], 149.1)
        self.assertEqual(s["count"], 4)  # None 金额仍计数
        self.assertEqual(s["categories"], [
            {"name": "订阅", "total": 100.0},
            {"name": "餐饮", "total": 49.1},
        ])  # None 金额不进分类汇总; 按金额降序
        self.assertEqual(len(s["recent"]), 4)

    def test_query_pages_pagination(self):
        calls = []

        def fake_post(url, headers, payload, limit):
            calls.append(payload)
            if "start_cursor" not in payload:
                return {"results": [_notion_page("a", 1, "2026-10-02", "餐饮")],
                        "has_more": True, "next_cursor": "cur2"}
            return {"results": [_notion_page("b", 2, "2026-10-01", "交通")],
                    "has_more": False, "next_cursor": None}

        svc = _service(self.tmp)
        svc._http_post_json = fake_post
        pages = svc._notion_query_pages("fake-token", "2026-10-01")
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["start_cursor"], "cur2")
        # 月初过滤 + 日期倒序 + token 走 header 不进 URL
        self.assertEqual(calls[0]["filter"], {"property": "日期", "date": {"on_or_after": "2026-10-01"}})
        self.assertEqual(calls[0]["sorts"], [{"property": "日期", "direction": "descending"}])

    def test_fetch_ledger_end_to_end(self):
        (self.tmp / ".notion_token").write_text("fake-notion-token", encoding="utf-8")
        svc = _service(self.tmp)
        svc._http_post_json = lambda url, headers, payload, limit: {
            "results": [
                _notion_page("瑞幸", 13.9, "2026-10-08", "餐饮"),
                _notion_page("话费", 40, "2026-10-01", "通讯"),
            ],
            "has_more": False,
        }
        data = svc._fetch_ledger()
        self.assertEqual(data["total"], 53.9)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["recent"][0]["title"], "瑞幸")  # 按日期倒序


# ---------- 3. 深空日活 ----------

class ShenkongTest(unittest.TestCase):
    def test_dots_status_machine(self):
        today = date(2026, 10, 9)
        events = [
            {"title": "深空日活", "date": "2026-10-08", "done": True},
            {"title": "深空日活", "date": "2026-10-07", "done": False},
            {"title": "深空日活", "date": "2026-10-09", "done": False},
            {"title": "深空日活批次续期（再排10/9起）", "date": "2026-10-08", "done": True},
            {"title": "  深空日活  ", "date": "2026-10-06", "done": True},
            {"title": "深空日活", "date": "bad-date", "done": True},
            "not-a-dict",
        ]
        dots = dash_panel.shenkong_dots(events, today)
        self.assertEqual(len(dots), 14)
        by_date = {d["date"]: d["status"] for d in dots}
        self.assertEqual(by_date["2026-10-08"], "done")
        self.assertEqual(by_date["2026-10-07"], "missed")
        self.assertEqual(by_date["2026-10-06"], "done")   # 标题首尾空格容忍
        self.assertEqual(by_date["2026-10-09"], "pending")
        self.assertEqual(by_date["2026-09-26"], "none")   # 14 天窗口第一天
        # 「批次续期」不是打卡事件, 不能单独把一天点亮 —— 10/08 已由真事件 done 覆盖,
        # 另起一天验证: 只有续期事件的日期应为 none
        events2 = [{"title": "深空日活批次续期", "date": "2026-10-05", "done": True}]
        by_date2 = {d["date"]: d["status"] for d in dash_panel.shenkong_dots(events2, today)}
        self.assertEqual(by_date2["2026-10-05"], "none")

    def test_quote_extract(self):
        q = dash_panel.extract_shenkong_quote("前面一堆字" * 10 + "深空日活续排到 10/18，明天不带肉。" + "后面一堆字" * 10)
        self.assertIn("深空日活", q)
        self.assertTrue(q.startswith("…") and q.endswith("…"))
        # 没有「深空日活」时回落「深空」
        q2 = dash_panel.extract_shenkong_quote("晾完正好 21:30 去深空做日活见哥哥。")
        self.assertIn("深空", q2)
        self.assertIsNone(dash_panel.extract_shenkong_quote("完全没有提到"))
        self.assertIsNone(dash_panel.extract_shenkong_quote(None))

    def test_diary_date(self):
        # 正文「日期:」行优先
        item = {"content": "日期: 2026-10-08\n……", "createdAt": "2026-10-08T17:00:10.156Z"}
        self.assertEqual(dash_panel.diary_date_of(item), date(2026, 10, 8))
        # 无日期行: 凌晨同步 (UTC 17:00 = 北京 01:00) 记的是前一天
        item2 = {"content": "没有日期行", "createdAt": "2026-10-08T17:00:10.156Z"}
        self.assertEqual(dash_panel.diary_date_of(item2), date(2026, 10, 8))
        # 白天手动记的日记算当天 (UTC 10:00 → 北京 18:00)
        item3 = {"content": "没有日期行", "createdAt": "2026-10-08T10:00:00Z"}
        self.assertEqual(dash_panel.diary_date_of(item3), date(2026, 10, 8))
        self.assertIsNone(dash_panel.diary_date_of({"content": 1}))

    def test_fetch_shenkong_quotes_window(self):
        tmp = Path(tempfile.mkdtemp(prefix="_dash_sk_test_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "config.toml").write_text('token=fake-memory-token', encoding="utf-8")
        svc = _service(tmp)

        def fake_get(url, headers, limit):
            return [
                {"content": "深空日活做完了", "createdAt": "2026-10-08T17:00:00Z"},
                {"content": "深空日活做完了", "createdAt": "2026-10-08T17:00:01Z"},  # 同日去重
                {"content": "没提", "createdAt": "2026-10-07T17:00:00Z"},
                {"content": "提到深空了", "createdAt": "2026-09-01T17:00:00Z"},      # 窗口外
            ]

        svc._http_get_json = fake_get
        quotes = svc._fetch_shenkong_quotes(date(2026, 10, 9))
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["date"], "2026-10-08")
        self.assertIn("深空日活", quotes[0]["quote"])

        # 记忆库挂掉: quotes 降级为空列表, 不抛
        def boom(url, headers, limit):
            raise SourceUnavailable("upstream unreachable")
        svc._http_get_json = boom
        self.assertEqual(svc._fetch_shenkong_quotes(date(2026, 10, 9)), [])


# ---------- 4. 缓存 / 5. 降级 ----------

class CacheAndDegradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="_dash_cache_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_ttl_hit_and_expiry(self):
        now = [1000.0]
        svc = _service(self.tmp, panel_ttl={"diet": 60}, now_fn=lambda: now[0])
        calls = []

        def fetcher():
            calls.append(1)
            return {"source": "t", "days": [{"n": len(calls)}]}

        p1 = svc._panel("diet", fetcher)
        p2 = svc._panel("diet", fetcher)
        self.assertEqual(len(calls), 1)          # TTL 内不重复取
        self.assertIs(p1, p2)
        self.assertFalse(p1["stale"])
        now[0] += 61
        p3 = svc._panel("diet", fetcher)
        self.assertEqual(len(calls), 2)          # 过期重取
        self.assertEqual(p3["days"][0]["n"], 2)

    def test_error_falls_back_to_stale(self):
        now = [1000.0]
        svc = _service(self.tmp, panel_ttl={"diet": 60}, now_fn=lambda: now[0])
        state = {"fail": False}

        def fetcher():
            if state["fail"]:
                raise SourceUnavailable("upstream unreachable")
            return {"source": "t", "days": []}

        ok = svc._panel("diet", fetcher)
        now[0] += 61
        state["fail"] = True
        stale = svc._panel("diet", fetcher)
        self.assertEqual(stale["status"], "ok")  # 旧数据继续可用
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["fetched_at"], ok["fetched_at"])

    def test_error_without_cache_degrades(self):
        svc = _service(self.tmp)
        payload = svc._panel("diet", lambda: (_ for _ in ()).throw(SourceUnavailable("记忆库 token 未配置")))
        self.assertEqual(payload["status"], "degraded")
        self.assertIn("数据源暂未接入", payload["error"])
        self.assertFalse(payload["stale"])

    def test_unexpected_exception_also_degrades(self):
        svc = _service(self.tmp)
        payload = svc._panel("ledger", lambda: 1 / 0)
        self.assertEqual(payload["status"], "degraded")

    def test_missing_tokens_degrade_panels_not_whole_dash(self):
        # 两个 token 文件都不存在 + 日程本不存在 → 四板块全降级, 聚合仍 200 契约
        svc = _service(self.tmp)
        out = svc.aggregate()
        self.assertTrue(out["ok"])
        self.assertEqual(out["plugin"], "xiaonan-dash")
        for name in ("diet", "sleep", "ledger", "shenkong"):
            self.assertEqual(out["panels"][name]["status"], "degraded", name)
            self.assertIn("数据源暂未接入", out["panels"][name]["error"])
        # 降级内容绝不带 token/路径细节泄露之外的敏感物 (error 只含通用原因)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(str(self.tmp), blob)

    def test_memory_token_regex_matches_push_proxy(self):
        (self.tmp / "config.toml").write_text(
            'foo = 1\ntoken=abc123def\nother = "x"\n', encoding="utf-8")
        svc = _service(self.tmp)
        self.assertEqual(svc._memory_token(), "abc123def")
        (self.tmp / ".notion_token").write_text("  ntn_xyz \n", encoding="utf-8")
        self.assertEqual(svc._notion_token(), "ntn_xyz")


# ---------- 6. HTTP 端点 ----------

class DashPanelHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="_dash_http_test_"))
        self.builtin_dir = self.tmp / "plugins-builtin"
        for pid in ("xiaonan-dash", "other-plugin"):
            root = self.builtin_dir / pid
            root.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({
                "id": pid, "name": pid, "version": "0.1.0",
                "description": "test", "entry": "index.html",
            }), encoding="utf-8")
            (root / "index.html").write_text(f"<html>{pid}</html>", encoding="utf-8")
        self.store = PluginStore(data_dir=self.tmp / "plugins", builtin_dir=self.builtin_dir)
        # 假聚合服务, 不碰真实数据源
        self.fake_payload = {
            "ok": True, "plugin": "xiaonan-dash", "generated_at": "2026-10-09T14:00:00+08:00",
            "panels": {n: {"status": "ok", "stale": False,
                           "fetched_at": "2026-10-09T14:00:00+08:00"}
                       for n in ("diet", "sleep", "ledger", "shenkong")},
        }
        original = PushHandler._dash_panel_service
        PushHandler._dash_panel_service = types.SimpleNamespace(
            aggregate=lambda: self.fake_payload)
        self.addCleanup(setattr, PushHandler, "_dash_panel_service", original)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def handler(self, path: str, headers: dict | None = None) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
        handler.headers = dict(headers or {})
        handler.client_address = ("127.0.0.1", 1)
        handler.responses = []
        handler._send_json = lambda status, payload, **kw: handler.responses.append(
            (status, payload, kw)
        )
        handler.state = types.SimpleNamespace(
            allowed_ips=[],
            shared_secret=SECRET,
            strict_auth=True,
            web_session_enabled=False,
        )
        handler._plugins_store = self.store
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        return handler

    @staticmethod
    def last(handler):
        return handler.responses[-1]

    def auth(self):
        return {"X-Auth-Token": SECRET}

    def test_ok_contract(self):
        h = self.handler("/plugins/xiaonan-dash/panel-data", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(set(payload["panels"]), {"diet", "sleep", "ledger", "shenkong"})
        # token/secret 绝不出现在响应里
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_auth_fail_closed(self):
        h = self.handler("/plugins/xiaonan-dash/panel-data")
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

        h = self.handler("/plugins/xiaonan-dash/panel-data",
                         headers={"X-Auth-Token": "wrong"})
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

        # scoped token 只配碰 data/*, 对 panel-data 无效
        token, _ = self.store.mint_scoped_token("xiaonan-dash", SECRET)
        h = self.handler("/plugins/xiaonan-dash/panel-data",
                         headers={"X-Plugin-Token": token})
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

    def test_other_plugin_id_falls_to_static_404(self):
        h = self.handler("/plugins/other-plugin/panel-data", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 404)

    def test_unknown_plugin_404(self):
        h = self.handler("/plugins/nonexistent/panel-data", headers=self.auth())
        h.do_GET()
        self.assertEqual(self.last(h)[0], 404)

    def test_aggregate_crash_returns_500(self):
        PushHandler._dash_panel_service = types.SimpleNamespace(
            aggregate=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        h = self.handler("/plugins/xiaonan-dash/panel-data", headers=self.auth())
        h.do_GET()
        status, payload, _ = self.last(h)
        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
