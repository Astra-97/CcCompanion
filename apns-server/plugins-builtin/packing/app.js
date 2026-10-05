/* packing —— 打包清单插件。
 *
 * 数据：KV doc `items`，结构 { items: [{id, name, status, order}], updated_at }。
 * status ∈ PENDING / PACKED / NOT_BRINGING，与原生 PackingList.kt 的枚举名一致；
 * order 为添加顺序（存储序），展示排序按状态分组（对应 sortPackingForDisplay）。
 * 所有写走 kvMutate 重读-合并-重试（If-Match 乐观并发）。
 */
"use strict";

const PLUGIN_ID = "packing";
const DOC = "items";

const STATUS = { PENDING: "PENDING", PACKED: "PACKED", NOT_BRINGING: "NOT_BRINGING" };
const STATUS_LABEL = { PENDING: "待打包", PACKED: "已打包", NOT_BRINGING: "不带" };
const STATUS_NEXT = { PENDING: "PACKED", PACKED: "NOT_BRINGING", NOT_BRINGING: "PENDING" };
const STATUS_RANK = { PENDING: 0, PACKED: 1, NOT_BRINGING: 2 };

/* 与原生 PackingList.kt 的 seedDefaultPackingItems() 对齐（29 项）；
 * 仅当 items 文档不存在（迁移也没发生过）时才播种。 */
const SEED_NAMES = [
  "颈枕", "拖鞋", "手机充电器", "墨镜", "帽子", "薄外套/长袖（溶洞十几度）",
  "折叠伞", "袜子补到x4-5双（7天量）", "短袖补到x4-5（7天量）", "内裤补到x4-5（7天量）",
  "洗脸巾", "睡衣", "防晒霜", "卸妆湿巾", "插排", "散利痛", "咖啡",
  "iPad+充电器", "充电宝+充电线", "耳机", "护手霜", "空瓶子", "手机支架",
  "茶苯海明", "电动牙刷+充电器", "手表+充电器", "经期用品（包括暖贴）", "眼罩", "身份证",
];

const bridge = typeof window !== "undefined" ? window.CCBridge : null;

function getToken() {
  try { return localStorage.getItem("cc_plugin_token_" + PLUGIN_ID) || ""; }
  catch (e) { return ""; }
}

async function kvRead(doc) {
  if (bridge && typeof bridge.readData === "function") {
    return bridge.readData(doc);
  }
  const res = await fetch(`/plugins/${PLUGIN_ID}/data/${encodeURIComponent(doc)}`, {
    cache: "no-store",
    headers: { "X-Plugin-Token": getToken() },
  });
  if (res.status === 404) return { version: 0, body: null };
  if (!res.ok) throw { status: res.status };
  const data = await res.json();
  return { version: data.version, body: data.body };
}

async function kvWrite(doc, body, version) {
  if (bridge && typeof bridge.writeData === "function") {
    return bridge.writeData(doc, body, version);
  }
  const res = await fetch(`/plugins/${PLUGIN_ID}/data/${encodeURIComponent(doc)}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-Plugin-Token": getToken(),
      "If-Match": String(version),
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 409) throw { status: 409, version: data.version, body: data.body };
  if (!res.ok) throw { status: res.status, error: data.error };
  return { version: data.version };
}

async function kvMutate(doc, mutate, maxRetries = 4) {
  let cur = await kvRead(doc);
  for (let attempt = 0; ; attempt++) {
    try {
      return await kvWrite(doc, mutate(cur.body), cur.version);
    } catch (e) {
      if (e && e.status === 409 && attempt + 1 < maxRetries) {
        cur = { version: e.version, body: e.body };
        continue;
      }
      throw e;
    }
  }
}

/* ---- 数据层 ---- */

function normalizeItems(body) {
  const arr = body && Array.isArray(body.items) ? body.items : [];
  return arr
    .filter((it) => it && typeof it.id === "string" && it.id && typeof it.name === "string" && it.name)
    .map((it, idx) => ({
      id: it.id,
      name: it.name,
      status: STATUS[it.status] || (it.packed ? STATUS.PACKED : STATUS.PENDING),
      order: typeof it.order === "number" ? it.order : idx,
    }));
}

function sortForDisplay(items) {
  return items.slice().sort((a, b) =>
    (STATUS_RANK[a.status] - STATUS_RANK[b.status]) || (a.order - b.order)
  );
}

/* 对应原生 summarizePacking：分母只算打算带的。 */
function summarize(items) {
  const packed = items.filter((i) => i.status === STATUS.PACKED).length;
  const notBringing = items.filter((i) => i.status === STATUS.NOT_BRINGING).length;
  return { packed, bringTotal: items.length - notBringing, notBringing, total: items.length };
}

function seedBody() {
  return {
    items: SEED_NAMES.map((name, i) => ({
      id: "seed_" + (i + 1),
      name,
      status: STATUS.PENDING,
      order: i,
    })),
    updated_at: Date.now(),
  };
}

function withTimestamp(items) {
  return { items, updated_at: Date.now() };
}

/* ---- 渲染 ---- */

const listEl = document.getElementById("item-list");
const summaryEl = document.getElementById("summary");
const progressEl = document.getElementById("progress-bar");
const emptyHintEl = document.getElementById("empty-hint");

function render(items) {
  const sorted = sortForDisplay(items);
  listEl.textContent = "";
  emptyHintEl.classList.toggle("hidden", sorted.length > 0);

  for (const item of sorted) {
    const li = document.createElement("li");
    li.className = "item-card" +
      (item.status === STATUS.PACKED ? " is-packed" : "") +
      (item.status === STATUS.NOT_BRINGING ? " is-not-bringing" : "");

    const badge = document.createElement("span");
    badge.className = "status-badge status-" + item.status.toLowerCase();
    badge.textContent = STATUS_LABEL[item.status];
    badge.addEventListener("click", () => cycle(item.id));

    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.name;
    name.addEventListener("click", () => cycle(item.id));

    const del = document.createElement("button");
    del.className = "del-btn";
    del.type = "button";
    del.textContent = "删除";
    del.addEventListener("click", () => removeItem(item.id));

    li.appendChild(badge);
    li.appendChild(name);
    li.appendChild(del);
    listEl.appendChild(li);
  }

  const s = summarize(items);
  const pct = s.bringTotal <= 0 ? 100 : Math.round((s.packed / s.bringTotal) * 100);
  summaryEl.textContent =
    `共 ${s.total} 项 · 已打包 ${s.packed}/${s.bringTotal}` +
    (s.notBringing > 0 ? ` · 不带 ${s.notBringing}` : "") +
    ` · ${pct}%`;
  progressEl.style.width = pct + "%";
}

function logError(e) {
  const el = document.getElementById("log");
  el.textContent = "操作失败: " + JSON.stringify(e && e.status ? e : String(e));
}

/* ---- 操作 ---- */

async function reload() {
  const cur = await kvRead(DOC);
  if (cur.version === 0) {
    /* 文档不存在且迁移未发生：播种默认清单（If-Match:0 防并发重复播种）。 */
    try {
      await kvWrite(DOC, seedBody(), 0);
      const seeded = await kvRead(DOC);
      render(normalizeItems(seeded.body));
      return;
    } catch (e) {
      if (!(e && e.status === 409)) throw e;
      const again = await kvRead(DOC);
      render(normalizeItems(again.body));
      return;
    }
  }
  render(normalizeItems(cur.body));
}

async function cycle(id) {
  try {
    await kvMutate(DOC, (body) => {
      const items = normalizeItems(body).map((it) =>
        it.id === id ? Object.assign({}, it, { status: STATUS_NEXT[it.status] }) : it
      );
      return withTimestamp(items);
    });
    await reload();
  } catch (e) { logError(e); }
}

async function addItem() {
  const input = document.getElementById("add-input");
  const name = input.value.trim();
  if (!name) return;
  input.value = "";
  try {
    await kvMutate(DOC, (body) => {
      const items = normalizeItems(body);
      const maxOrder = items.reduce((m, it) => Math.max(m, it.order), -1);
      items.push({
        id: "item_" + Date.now() + "_" + Math.floor(Math.random() * 1e6),
        name,
        status: STATUS.PENDING,
        order: maxOrder + 1,
      });
      return withTimestamp(items);
    });
    await reload();
  } catch (e) { logError(e); }
}

async function removeItem(id) {
  try {
    await kvMutate(DOC, (body) =>
      withTimestamp(normalizeItems(body).filter((it) => it.id !== id))
    );
    await reload();
  } catch (e) { logError(e); }
}

/* 对应原生 clearedPackingItems：全部（含不带的）清回待打包。 */
async function resetAll() {
  try {
    await kvMutate(DOC, (body) =>
      withTimestamp(normalizeItems(body).map((it) =>
        Object.assign({}, it, { status: STATUS.PENDING })
      ))
    );
    await reload();
  } catch (e) { logError(e); }
}

document.getElementById("add-btn").addEventListener("click", addItem);
document.getElementById("add-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addItem();
});
document.getElementById("reset-btn").addEventListener("click", resetAll);
document.getElementById("token-save").addEventListener("click", () => {
  try {
    localStorage.setItem("cc_plugin_token_" + PLUGIN_ID, document.getElementById("token-input").value.trim());
  } catch (e) { logError(e); }
});

if (bridge) {
  document.getElementById("token-box").classList.add("hidden");
}

reload().catch(logError);
