"""小南面板 (xiaonan-dash 插件) 服务端聚合层 (2026-10-09)。

方案 B: 不代理原 dash.xiaonancaleb.xyz 静态快照站, 直接读三个数据源
自己渲染, 并接上原面板 v1 没接的真打卡:

1. 记忆库 HTTP API (diary.health 每日运动健康日记)
   → 🍽️ 饮食板块 + 😴 睡眠/心率/体重表格
2. Notion 账本「方小南又花钱了」database query
   → 💸 花钱账本 (本月合计/笔数/分类汇总/最近记账)
3. 本机日程本 /root/schedule/events.json (title == 深空日活 的 done 标记)
   → 🌌 深空日活近 14 天真打卡圆点, 外加记忆库日记语义检索的原句线索

全部只读。每个板块独立 TTL 缓存 (120-300s), 单个数据源不可用时只降级
该板块 (status=degraded + error), 有旧缓存时回 stale 副本, 绝不让一个源
拖死整个面板。token 只进内存: 记忆库 bearer 从 /root/.codex/config.toml
提取 (与 push.py memory proxy 同款正则), Notion token 读 /root/.notion_token;
两者都绝不进日志/响应/落盘。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger("dash_panel")

TZ = ZoneInfo("Asia/Shanghai")

MEMORY_BASE = "https://memory.xiaonancaleb.xyz"
MEMORY_CONFIG_PATH = Path("/root/.codex/config.toml")
NOTION_TOKEN_PATH = Path("/root/.notion_token")
# 账本库 ID 出自记忆库 core 档案「方小南的钱」 (2026-10-09 实测可查)。
LEDGER_DATABASE_ID = "52cc0899a1f84c60afdf51d579ee264c"
NOTION_API_VERSION = "2022-06-28"
SCHEDULE_PATH = Path("/root/schedule/events.json")

REQUEST_TIMEOUT_SEC = 10
MEMORY_RESPONSE_LIMIT = 4 * 1024 * 1024
NOTION_RESPONSE_LIMIT = 4 * 1024 * 1024

# 板块缓存 TTL (秒): 饮食/睡眠同源自记忆库, 账本走 Notion, 打卡读本机文件。
PANEL_TTL = {"diet": 180, "sleep": 300, "ledger": 300, "shenkong": 120}

SHENKONG_WINDOW_DAYS = 14
SHENKONG_EVENT_TITLE = "深空日活"
LEDGER_RECENT_LIMIT = 10
HEALTH_ENTRY_LIMIT = 6
MAX_DIET_DAYS = 4
MAX_DIET_LINES_PER_DAY = 8
MAX_LEDGER_PAGES = 5
QUOTE_LIMIT = 6

DAY_RE = re.compile(r"Day\s*(\d+)")
DATE_RE = re.compile(r"日期[:：]\s*(\d{4}-\d{2}-\d{2})")
DURATION = r"(\d+h\d+m|\d+m|\d+h)"
SLEEP_REAL_RE = re.compile(r"实睡\s*" + DURATION)
SLEEP_DEEP_RE = re.compile(r"深睡\s*" + DURATION)
HR_MORNING_RE = re.compile(r"心率\s*(\d{2,3})\s*[（(]")
HR_MIN_RE = re.compile(r"最低\s*(\d{2,3})")
WEIGHT_RE = re.compile(r"体重\s*(\d+(?:\.\d+)?)\s*kg")
# 饮食段落终止于下一个段落标题 (emoji 或小标题)。
DIET_SECTION_END = tuple("🏃🧍🫀⚖😴🌡📊🌙💊🩹📱##")


class SourceUnavailable(RuntimeError):
    """单个数据源取数失败; 上层据此降级对应板块。"""


def _now_bj() -> datetime:
    return datetime.now(TZ)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_health_entry(content: str) -> dict[str, Any]:
    """从一篇 diary.health 日记正文提取面板需要的字段 (best-effort, 缺项为 None)。"""
    if not isinstance(content, str):
        return {}
    day_m = DAY_RE.search(content)
    date_m = DATE_RE.search(content)
    entry: dict[str, Any] = {
        "day": int(day_m.group(1)) if day_m else None,
        "date": date_m.group(1) if date_m else None,
    }

    sleep: dict[str, Any] = {}
    m = SLEEP_REAL_RE.search(content)
    sleep["sleep_real"] = m.group(1) if m else None
    m = SLEEP_DEEP_RE.search(content)
    sleep["sleep_deep"] = m.group(1) if m else None
    m = HR_MORNING_RE.search(content)
    sleep["hr_morning"] = int(m.group(1)) if m else None
    m = HR_MIN_RE.search(content)
    sleep["hr_min"] = int(m.group(1)) if m else None
    m = WEIGHT_RE.search(content)
    sleep["weight_kg"] = float(m.group(1)) if m else None
    entry["sleep"] = sleep

    lines = content.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("🍽️ 饮食") or stripped == "🍽️饮食":
            start = i + 1
            break
    diet: list[str] = []
    if start is not None:
        for line in lines[start:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(DIET_SECTION_END):
                break
            diet.append(stripped)
            if len(diet) >= MAX_DIET_LINES_PER_DAY:
                break
    entry["diet_lines"] = diet
    return entry


def parse_notion_ledger_page(page: dict[str, Any]) -> dict[str, Any] | None:
    """把一页 Notion 账本记录摊平成 {date, category, amount, title}; 缺日期则丢弃。"""
    props = page.get("properties") if isinstance(page, dict) else None
    if not isinstance(props, dict):
        return None
    date_prop = props.get("日期") or {}
    date_value = (date_prop.get("date") or {}).get("start")
    if not isinstance(date_value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value[:10]):
        return None
    title_parts = (props.get("项目") or {}).get("title") or []
    title = "".join(
        part.get("plain_text", "") for part in title_parts if isinstance(part, dict)
    ).strip()
    amount = (props.get("金额") or {}).get("number")
    category = ((props.get("分类") or {}).get("select") or {}).get("name") or "未分类"
    return {
        "date": date_value[:10],
        "category": str(category),
        "amount": float(amount) if isinstance(amount, (int, float)) else None,
        "title": title or "（无标题）",
    }


def summarize_ledger(entries: list[dict[str, Any]], month: str) -> dict[str, Any]:
    """本月合计/笔数/分类汇总/最近记账 (entries 已按日期倒序)。"""
    total = sum(e["amount"] for e in entries if e["amount"] is not None)
    by_category: dict[str, float] = {}
    for e in entries:
        if e["amount"] is None:
            continue
        by_category[e["category"]] = by_category.get(e["category"], 0.0) + e["amount"]
    categories = [
        {"name": name, "total": round(amount, 2)}
        for name, amount in sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {
        "month": month,
        "total": round(total, 2),
        "count": len(entries),
        "categories": categories,
        "recent": entries[:LEDGER_RECENT_LIMIT],
    }


def shenkong_dots(events: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """近 14 天打卡圆点: done=已完成 / missed=过去没打 / pending=今天还没打 / none=当天无日程。"""
    by_date: dict[str, bool] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("title") or "").strip() != SHENKONG_EVENT_TITLE:
            continue
        day = str(event.get("date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        by_date[day] = by_date.get(day, False) or bool(event.get("done"))
    dots: list[dict[str, Any]] = []
    for offset in range(SHENKONG_WINDOW_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        if by_date.get(key):
            status = "done"
        elif key in by_date:
            status = "pending" if day >= today else "missed"
        else:
            status = "none"
        dots.append({"date": key, "status": status})
    return dots


def extract_shenkong_quote(content: str) -> str | None:
    """从日记正文截取含「深空日活」(或「深空」) 的一句线索, 带省略号。"""
    if not isinstance(content, str):
        return None
    compact = re.sub(r"\s+", " ", content).strip()
    for needle in ("深空日活", "深空"):
        idx = compact.find(needle)
        if idx < 0:
            continue
        start = max(0, idx - 30)
        end = min(len(compact), idx + 40)
        snippet = compact[start:end]
        if start > 0:
            snippet = "…" + snippet
        if end < len(compact):
            snippet += "…"
        return snippet
    return None


def diary_date_of(item: dict[str, Any]) -> date | None:
    """日记条目对应的 diary 日期: 正文「日期:」行优先, 否则按同步时间
    (Notion 同步在凌晨跑, 记的是前一天) 用 UTC+8 再减 12h 的日期兜底。"""
    content = item.get("content")
    if isinstance(content, str):
        m = DATE_RE.search(content)
        if m:
            try:
                return date.fromisoformat(m.group(1))
            except ValueError:
                pass
    created = _parse_dt(item.get("createdAt"))
    if created is None:
        return None
    return (created + timedelta(hours=8) - timedelta(hours=12)).date()


class DashPanelService:
    """四板块聚合 + 每板块 TTL 缓存 + 失败降级。线程安全, 全程只读。"""

    def __init__(
        self,
        *,
        memory_base: str = MEMORY_BASE,
        memory_config_path: Path = MEMORY_CONFIG_PATH,
        notion_token_path: Path = NOTION_TOKEN_PATH,
        schedule_path: Path = SCHEDULE_PATH,
        ledger_database_id: str = LEDGER_DATABASE_ID,
        panel_ttl: dict[str, int] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._memory_base = memory_base
        self._memory_config_path = Path(memory_config_path)
        self._notion_token_path = Path(notion_token_path)
        self._schedule_path = Path(schedule_path)
        self._ledger_database_id = ledger_database_id
        self._ttl = dict(PANEL_TTL if panel_ttl is None else panel_ttl)
        self._now_fn = now_fn
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._memory_token_cache: str | None = None
        self._notion_token_cache: str | None = None

    # ---- token (只进内存) ----

    def _memory_token(self) -> str | None:
        if self._memory_token_cache:
            return self._memory_token_cache
        try:
            text = self._memory_config_path.read_text(encoding="utf-8")
        except OSError:
            return None
        m = re.search(r'token=([^"&\s]+)', text)
        if not m:
            return None
        self._memory_token_cache = m.group(1)
        return self._memory_token_cache

    def _notion_token(self) -> str | None:
        if self._notion_token_cache:
            return self._notion_token_cache
        try:
            token = self._notion_token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not token:
            return None
        self._notion_token_cache = token
        return token

    # ---- HTTP (可被测试替换) ----

    @staticmethod
    def _http_get_json(url: str, headers: dict[str, str], limit: int) -> Any:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                raw = resp.read(limit + 1)
        except urllib.error.HTTPError as e:
            raise SourceUnavailable(f"upstream http {e.code}") from e
        except Exception as e:
            raise SourceUnavailable(f"upstream unreachable: {type(e).__name__}") from e
        if len(raw) > limit:
            raise SourceUnavailable("upstream response too large")
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SourceUnavailable("upstream returned invalid json") from e

    @staticmethod
    def _http_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], limit: int) -> Any:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                raw = resp.read(limit + 1)
        except urllib.error.HTTPError as e:
            raise SourceUnavailable(f"notion http {e.code}") from e
        except Exception as e:
            raise SourceUnavailable(f"notion unreachable: {type(e).__name__}") from e
        if len(raw) > limit:
            raise SourceUnavailable("notion response too large")
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SourceUnavailable("notion returned invalid json") from e

    # ---- 记忆库 ----

    def _memory_get(self, path: str, params: dict[str, str]) -> Any:
        token = self._memory_token()
        if not token:
            raise SourceUnavailable("记忆库 token 未配置")
        url = self._memory_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._http_get_json(
            url,
            {
                "Authorization": f"Bearer {token}",
                # Cloudflare 按 UA 拦截, 必须伪装 curl (与 memory proxy 同款)。
                "User-Agent": "curl/7.81.0",
                "Accept": "application/json",
            },
            MEMORY_RESPONSE_LIMIT,
        )

    def _health_entries(self) -> list[dict[str, Any]]:
        data = self._memory_get("/api/memories", {
            "category": "diary",
            "subcategory": "diary.health",
            "limit": str(HEALTH_ENTRY_LIMIT),
            "sort_by": "createdAt",
            "sort_order": "desc",
        })
        items = data if isinstance(data, list) else data.get("memories") or []
        entries: list[dict[str, Any]] = []
        for item in items[:HEALTH_ENTRY_LIMIT]:
            if not isinstance(item, dict):
                continue
            parsed = parse_health_entry(item.get("content") or "")
            if parsed.get("date") is None:
                diary_day = diary_date_of(item)
                parsed["date"] = diary_day.isoformat() if diary_day else None
            if parsed.get("date") is None:
                continue
            entries.append(parsed)
        entries.sort(key=lambda e: e["date"], reverse=True)
        if not entries:
            raise SourceUnavailable("记忆库 diary.health 暂无可用日记")
        return entries

    # ---- 板块: 饮食 / 睡眠 ----

    def _fetch_diet(self) -> dict[str, Any]:
        entries = self._health_entries()
        days = [
            {"day": e["day"], "date": e["date"], "lines": e["diet_lines"]}
            for e in entries
            if e["diet_lines"]
        ][:MAX_DIET_DAYS]
        if not days:
            raise SourceUnavailable("最近日记里没有饮食段落")
        return {
            "source": "记忆库 diary.health（每晚从 Notion 同步）",
            "days": days,
            "day_anchor": {"day": entries[0]["day"], "date": entries[0]["date"]},
        }

    def _fetch_sleep(self) -> dict[str, Any]:
        entries = self._health_entries()
        rows = []
        for e in entries:
            row = {"date": e["date"], **e["sleep"]}
            rows.append(row)
        return {
            "source": "记忆库/Notion 运动健康日记（手表数据, 「—」= 当天没记）",
            "rows": rows,
        }

    # ---- 板块: 花钱账本 ----

    def _notion_query_pages(self, token: str, month_start: str) -> list[dict[str, Any]]:
        url = f"https://api.notion.com/v1/databases/{self._ledger_database_id}/query"
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
        }
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LEDGER_PAGES):
            payload: dict[str, Any] = {
                "page_size": 100,
                "sorts": [{"property": "日期", "direction": "descending"}],
                "filter": {"property": "日期", "date": {"on_or_after": month_start}},
            }
            if cursor:
                payload["start_cursor"] = cursor
            data = self._http_post_json(url, headers, payload, NOTION_RESPONSE_LIMIT)
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise SourceUnavailable("notion 返回格式异常")
            pages.extend(p for p in results if isinstance(p, dict))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return pages

    def _fetch_ledger(self) -> dict[str, Any]:
        token = self._notion_token()
        if not token:
            raise SourceUnavailable("Notion token 未配置")
        month_start = _now_bj().date().replace(day=1).isoformat()
        pages = self._notion_query_pages(token, month_start)
        entries = [e for e in (parse_notion_ledger_page(p) for p in pages) if e]
        entries.sort(key=lambda e: e["date"], reverse=True)
        return {
            "source": "Notion 账本「方小南又花钱了」（按「分类」汇总本月, 金额原样）",
            **summarize_ledger(entries, month_start[:7]),
        }

    # ---- 板块: 深空日活 ----

    def _read_schedule_events(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._schedule_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SourceUnavailable(f"日程本不可读: {type(e).__name__}") from e
        if not isinstance(data, list):
            raise SourceUnavailable("日程本格式异常")
        return data

    def _fetch_shenkong_quotes(self, today: date) -> list[dict[str, Any]]:
        """日记原句线索: 拉最近日记全文 grep「深空」(原面板同款「尽力而为」),
        只保留 14 天窗口内的; 取数失败只降级这一部分, 不拖垮打卡圆点。"""
        try:
            data = self._memory_get("/api/memories", {
                "category": "diary",
                "limit": "25",
                "sort_by": "createdAt",
                "sort_order": "desc",
            })
        except SourceUnavailable:
            return []
        items = data if isinstance(data, list) else data.get("memories") or []
        window_start = today - timedelta(days=SHENKONG_WINDOW_DAYS - 1)
        quotes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or "深空" not in content:
                continue
            day = diary_date_of(item)
            if day is None or day < window_start or day > today:
                continue
            key = day.isoformat()
            if key in seen:
                continue
            quote = extract_shenkong_quote(content)
            if not quote:
                continue
            seen.add(key)
            quotes.append({"date": key, "quote": quote})
            if len(quotes) >= QUOTE_LIMIT:
                break
        quotes.sort(key=lambda q: q["date"], reverse=True)
        return quotes

    def _fetch_shenkong(self) -> dict[str, Any]:
        events = self._read_schedule_events()
        today = _now_bj().date()
        return {
            "source": "小克 VPS 日程本（真打卡, done=已完成）+ 记忆库日记原句线索",
            "today": today.isoformat(),
            "dots": shenkong_dots(events, today),
            "quotes": self._fetch_shenkong_quotes(today),
        }

    # ---- 聚合 ----

    def aggregate(self) -> dict[str, Any]:
        fetchers: dict[str, Callable[[], dict[str, Any]]] = {
            "diet": self._fetch_diet,
            "sleep": self._fetch_sleep,
            "ledger": self._fetch_ledger,
            "shenkong": self._fetch_shenkong,
        }
        panels: dict[str, Any] = {}
        for name, fetcher in fetchers.items():
            panels[name] = self._panel(name, fetcher)
        return {
            "ok": True,
            "plugin": "xiaonan-dash",
            "generated_at": _iso(_now_bj()),
            "panels": panels,
        }

    def _panel(self, name: str, fetcher: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        now = self._now_fn()
        with self._lock:
            cached = self._cache.get(name)
        if cached and now - cached["t"] < self._ttl.get(name, 60):
            return cached["data"]
        try:
            data = fetcher()
        except Exception as e:
            logger.warning("dash panel %s fetch failed: %s", name, e)
            if cached:
                stale = dict(cached["data"])
                stale["stale"] = True
                return stale
            return {
                "status": "degraded",
                "stale": False,
                "fetched_at": _iso(_now_bj()),
                "error": f"数据源暂未接入（{e}）",
            }
        payload = {
            "status": "ok",
            "stale": False,
            "fetched_at": _iso(_now_bj()),
            **data,
        }
        with self._lock:
            self._cache[name] = {"t": now, "data": payload}
        return payload
