/* demo-checkin —— 最小打卡插件。
 * 数据: KV doc "checkins", body = { records: [{ ts: "<iso>", note: "" }, ...] }
 * 通道: 优先 App bridge (window.CCBridge)，无 bridge 时降级 fetch + scoped token。
 * 协议细节见 plugins-builtin/README.md。
 */
"use strict";

const PLUGIN_ID = "demo-checkin";
const DOC = "checkins";

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

function setStatus(text) {
  document.getElementById("status").textContent = text;
}

function render(records) {
  const ul = document.getElementById("records");
  ul.textContent = "";
  records.slice().reverse().forEach((r) => {
    const li = document.createElement("li");
    li.textContent = String(r.ts || "").replace("T", " ").slice(0, 19);
    ul.appendChild(li);
  });
  setStatus(`共 ${records.length} 次打卡`);
}

async function load() {
  const cur = await kvRead(DOC);
  const records = (cur.body && Array.isArray(cur.body.records)) ? cur.body.records : [];
  render(records);
}

document.getElementById("checkin-btn").addEventListener("click", async () => {
  const btn = document.getElementById("checkin-btn");
  btn.disabled = true;
  try {
    await kvMutate(DOC, (body) => {
      const records = (body && Array.isArray(body.records)) ? body.records.slice() : [];
      records.push({ ts: new Date().toISOString(), note: "" });
      return { records };
    });
    setStatus("打卡成功");
    await load();
  } catch (e) {
    setStatus("打卡失败: " + JSON.stringify(e));
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("token-save").addEventListener("click", () => {
  try {
    localStorage.setItem("cc_plugin_token_" + PLUGIN_ID, document.getElementById("token-input").value.trim());
    setStatus("token 已保存，重新加载…");
    load().catch((e) => setStatus("读取失败: " + JSON.stringify(e)));
  } catch (e) { setStatus("保存失败: " + e); }
});

if (bridge) {
  document.getElementById("dev-box").style.display = "none";
}
load().catch((e) => setStatus("读取失败: " + JSON.stringify(e)));
