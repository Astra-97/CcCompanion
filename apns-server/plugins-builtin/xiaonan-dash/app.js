/* xiaonan-dash —— 小南面板插件 (2026-10-09, 方案 B: 服务端直读数据源)。
 *
 * 数据：GET /plugins/xiaonan-dash/panel-data 同源 fetch —— 服务端聚合
 * 记忆库 diary.health / Notion 账本 / 本机日程本，token 不出服务端。
 * App 插件 WebView 对 /plugins/ 同源请求自动代加 X-Auth-Token；
 * PWA 浏览器带 web session cookie 也可命中。页面本身零凭据、零写入。
 */
"use strict";

const PLUGIN_ID = "xiaonan-dash";
const AUTO_REFRESH_MS = 180 * 1000;

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function parseDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function mdLabel(iso) {
  const d = parseDate(iso);
  if (!d) return String(iso || "—");
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function weekdayLabel(iso) {
  const d = parseDate(iso);
  return d ? WEEKDAYS[d.getDay()] : "";
}

function fmtMoney(n) {
  if (typeof n !== "number" || !isFinite(n)) return "—";
  return "¥" + n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/* ---- 外观: 复用 App 壁纸 + 玻璃风 (与 packing 插件同款) ---- */
/* 真机观察 (2026-10-10): App 插件 WebView 的代取通道偶发把单个请求吞掉
   (连服务器访问日志都没有), 壁纸请求被吞就永远黑底。加有限重试自愈。 */
async function fetchWithRetry(url, attempts) {
  let lastErr = null;
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (res.ok) return res;
      lastErr = new Error("http " + res.status);
    } catch (e) { lastErr = e; }
    await new Promise((r) => setTimeout(r, 1500 * (i + 1)));
  }
  throw lastErr || new Error("fetch failed");
}

/* 壁纸两阶段 (2026-10-10 Astra 要求「一点开就是」):
   1) 立即挂插件目录内置 wallpaper.jpg —— 与 app.js/style.css 同批静态请求,
      不等任何异步 fetch, 打开即有。cron 每分钟把它与当前聊天壁纸同步压缩。
   2) 内置加载失败才回落 appearance-asset 端点 (壁纸刚换、同步还没跑的场景)。 */
function mountWallpaper() {
  const img = document.getElementById("bg-image");
  let attempts = 0;
  let fellBack = false;
  img.addEventListener("load", () => {
    img.classList.remove("hidden");
    document.body.classList.add("has-wallpaper");
  });
  img.addEventListener("error", () => {
    img.classList.add("hidden");
    document.body.classList.remove("has-wallpaper");
    if (attempts < 2) {
      attempts++;
      setTimeout(() => { img.src = "wallpaper.jpg?r=" + attempts; }, 1500 * attempts);
    } else if (!fellBack) {
      fellBack = true;
      applyAppearanceFallback(img);
    }
  });
  img.src = "wallpaper.jpg";
}

async function applyAppearanceFallback(img) {
  try {
    const res = await fetchWithRetry(`/plugins/${PLUGIN_ID}/appearance`, 3);
    const data = await res.json().catch(() => null);
    const bgUrl = data && data.ok && typeof data.bg_url === "string" ? data.bg_url : "";
    if (bgUrl) img.src = bgUrl;
  } catch (e) { /* 静默回落: 无壁纸不代表页面坏了 */ }
}

/* ---- 数据 ---- */
async function loadPanelData() {
  const res = await fetchWithRetry(`/plugins/${PLUGIN_ID}/panel-data`, 3);
  const data = await res.json();
  if (!data || !data.ok || !data.panels) throw new Error("panel-data 格式异常");
  return data;
}

/* 板块底部: 来源 + 数据时间 + stale/降级标注 */
function renderFoot(footEl, panel, sourceFallback) {
  footEl.textContent = "";
  if (!panel) return;
  if (panel.status !== "ok") {
    footEl.appendChild(el("span", "degraded-tag", panel.error || "数据源暂未接入"));
    return;
  }
  const parts = [];
  if (panel.source || sourceFallback) parts.push("来源：" + (panel.source || sourceFallback));
  if (panel.fetched_at) parts.push("数据时间 " + String(panel.fetched_at).replace("T", " ").slice(5, 16));
  footEl.appendChild(document.createTextNode(parts.join(" · ")));
  if (panel.stale) footEl.appendChild(el("span", "stale-tag", "缓存"));
}

function renderDegraded(bodyEl, panel) {
  bodyEl.textContent = "";
  const box = el("p", "degraded-box", (panel && panel.error) || "数据源暂未接入");
  bodyEl.appendChild(box);
}

/* ---- 🍽️ 饮食 ---- */
function renderDiet(panel) {
  const body = document.getElementById("diet-body");
  body.textContent = "";
  if (!panel || panel.status !== "ok") { renderDegraded(body, panel); return; }
  const days = Array.isArray(panel.days) ? panel.days : [];
  if (!days.length) {
    body.appendChild(el("p", "muted", "最近几天还没有饮食记录，当天日记同步后就会出现。"));
    return;
  }
  for (const day of days) {
    const card = el("div", "day-card");
    const title = day.day ? `Day${day.day} · ${mdLabel(day.date)} ${weekdayLabel(day.date)}`
                          : `${mdLabel(day.date)} ${weekdayLabel(day.date)}`;
    card.appendChild(el("p", "day-title", title));
    const ul = el("ul", "diet-list");
    for (const line of (day.lines || [])) {
      ul.appendChild(el("li", null, "🍽️ " + line));
    }
    card.appendChild(ul);
    body.appendChild(card);
  }
}

/* ---- 😴 睡眠 · 心率 · 体重 ---- */
function renderSleep(panel) {
  const body = document.getElementById("sleep-body");
  body.textContent = "";
  if (!panel || panel.status !== "ok") { renderDegraded(body, panel); return; }
  const rows = Array.isArray(panel.rows) ? panel.rows : [];
  if (!rows.length) { body.appendChild(el("p", "muted", "暂无睡眠记录")); return; }
  const table = el("table", "sleep-table");
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["日期", "实睡", "深睡", "最低心率", "晨起心率", "体重"]) {
    hr.appendChild(el("th", null, h));
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  const dash = () => el("td", "dim", "—");
  for (const r of rows) {
    const tr = el("tr");
    tr.appendChild(el("td", null, mdLabel(r.date)));
    tr.appendChild(r.sleep_real ? el("td", null, r.sleep_real) : dash());
    tr.appendChild(r.sleep_deep ? el("td", null, r.sleep_deep) : dash());
    tr.appendChild(r.hr_min != null ? el("td", null, r.hr_min) : dash());
    tr.appendChild(r.hr_morning != null ? el("td", null, r.hr_morning) : dash());
    tr.appendChild(r.weight_kg != null ? el("td", null, r.weight_kg + "kg") : dash());
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  body.appendChild(table);
}

/* ---- 💸 花钱账本 ---- */
function renderLedger(panel) {
  const body = document.getElementById("ledger-body");
  body.textContent = "";
  if (!panel || panel.status !== "ok") { renderDegraded(body, panel); return; }

  const summary = el("div", "ledger-summary");
  const totalBox = el("div", "summary-box");
  totalBox.appendChild(el("p", "summary-num", fmtMoney(panel.total)));
  totalBox.appendChild(el("p", "summary-label", `${panel.month || ""} 合计`));
  const countBox = el("div", "summary-box");
  countBox.appendChild(el("p", "summary-num", panel.count != null ? panel.count : "—"));
  countBox.appendChild(el("p", "summary-label", "本月笔数"));
  summary.appendChild(totalBox);
  summary.appendChild(countBox);
  body.appendChild(summary);

  const cats = Array.isArray(panel.categories) ? panel.categories : [];
  if (cats.length) {
    const chips = el("div", "cat-chips");
    for (const c of cats) {
      const chip = el("span", "cat-chip");
      chip.appendChild(el("span", "cat-name", c.name));
      chip.appendChild(el("span", "cat-amt", fmtMoney(c.total)));
      chips.appendChild(chip);
    }
    body.appendChild(chips);
  }

  const recent = Array.isArray(panel.recent) ? panel.recent : [];
  if (recent.length) {
    body.appendChild(el("p", "section-label", "最近记账"));
    const ul = el("ul", "ledger-list");
    for (const e of recent) {
      const li = el("li", "ledger-item");
      const left = el("div", "ledger-left");
      left.appendChild(el("p", "ledger-title", e.title));
      left.appendChild(el("p", "ledger-meta", `${e.date} · ${e.category}`));
      li.appendChild(left);
      li.appendChild(el("span", "ledger-amt", fmtMoney(e.amount)));
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }
}

/* ---- 🌌 深空日活 ---- */
function renderShenkong(panel) {
  const body = document.getElementById("shenkong-body");
  body.textContent = "";
  if (!panel || panel.status !== "ok") { renderDegraded(body, panel); return; }

  const dots = Array.isArray(panel.dots) ? panel.dots : [];
  if (dots.length) {
    const grid = el("div", "dot-grid");
    for (const d of dots) {
      const cell = el("div", "dot-cell");
      const cls = { done: "dot done", missed: "dot missed", pending: "dot pending", none: "dot none" }[d.status] || "dot none";
      cell.appendChild(el("span", cls, ""));
      cell.appendChild(el("span", "dot-label", mdLabel(d.date)));
      grid.appendChild(cell);
    }
    body.appendChild(grid);
    body.appendChild(el("p", "muted small",
      "实心 = 日程本已核销；转圈 = 今天还没到时候；空心 = 那天没打成/没排。"));
  }

  const quotes = Array.isArray(panel.quotes) ? panel.quotes : [];
  if (quotes.length) {
    body.appendChild(el("p", "section-label", "日记原句"));
    const ul = el("ul", "quote-list");
    for (const q of quotes) {
      const li = el("li", "quote-item");
      li.appendChild(el("span", "quote-date", mdLabel(q.date) + "："));
      li.appendChild(el("span", "quote-text", q.quote));
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }
}

/* ---- 头部: 今天日期 + Day 序号 + 生成时间 ---- */
function renderHeader(data) {
  const todayEl = document.getElementById("today-line");
  const genEl = document.getElementById("generated-line");
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let dayPart = "";
  const anchor = data.panels && data.panels.diet && data.panels.diet.day_anchor;
  if (anchor && anchor.day && parseDate(anchor.date)) {
    const diffDays = Math.round((today - parseDate(anchor.date)) / 86400000);
    dayPart = ` · Day${anchor.day + diffDays}`;
  }
  todayEl.textContent = `今天 ${now.getMonth() + 1}月${now.getDate()}日 ${WEEKDAYS[now.getDay()]}${dayPart}`;
  genEl.textContent = data.generated_at
    ? "生成于 " + String(data.generated_at).replace("T", " ").slice(0, 16) + "（北京时间）· 数据实时读取，各板块独立缓存"
    : "";
}

async function refresh() {
  const btn = document.getElementById("refresh-btn");
  btn.disabled = true;
  try {
    const data = await loadPanelData();
    renderHeader(data);
    const p = data.panels || {};
    renderDiet(p.diet);
    renderSleep(p.sleep);
    renderLedger(p.ledger);
    renderShenkong(p.shenkong);
    renderFoot(document.getElementById("diet-foot"), p.diet);
    renderFoot(document.getElementById("sleep-foot"), p.sleep);
    renderFoot(document.getElementById("ledger-foot"), p.ledger);
    renderFoot(document.getElementById("shenkong-foot"), p.shenkong);
  } catch (e) {
    document.getElementById("today-line").textContent = "面板数据加载失败（" + (e && e.message ? e.message : "网络错误") + "），稍后点 ↻ 重试";
  } finally {
    btn.disabled = false;
  }
}

/* 在 App 插件 WebView 内 (原生注入了 CCBridge shim): 页面变透明,
   壁纸由 App 原生层按功能页同款绘制 (2026-10-10 Astra 反馈壁纸断层/打架);
   浏览器/PWA 里保持内置壁纸。 */
const IN_APP = typeof window.CCBridge !== "undefined";
if (IN_APP) document.documentElement.classList.add("in-app");

if (!IN_APP) mountWallpaper();
document.getElementById("refresh-btn").addEventListener("click", refresh);
refresh();
setInterval(refresh, AUTO_REFRESH_MS);
