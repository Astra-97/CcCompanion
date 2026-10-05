/* hello-test —— 最小链路验证：KV doc `state` 存 {count}，读写走与模板同一套封装。 */
"use strict";

const PLUGIN_ID = "hello-test";

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

function log(msg) {
  const el = document.getElementById("log");
  el.textContent += (el.textContent ? "\n" : "") + msg;
}

async function refresh() {
  const cur = await kvRead("state");
  const n = (cur.body && typeof cur.body.count === "number") ? cur.body.count : 0;
  document.getElementById("counter-view").textContent = `count=${n} (v${cur.version})`;
}

document.getElementById("inc-btn").addEventListener("click", async () => {
  try {
    const r = await kvMutate("state", (body) => ({
      count: ((body && body.count) || 0) + 1,
    }));
    log(`写入成功 -> v${r.version}`);
    await refresh();
  } catch (e) {
    log("写入失败: " + JSON.stringify(e));
  }
});

document.getElementById("token-save").addEventListener("click", () => {
  try {
    localStorage.setItem("cc_plugin_token_" + PLUGIN_ID, document.getElementById("token-input").value.trim());
    log("token 已保存到 localStorage");
  } catch (e) { log("保存失败: " + e); }
});

if (bridge) {
  document.getElementById("token-box").style.display = "none";
  log("检测到 App bridge，走原生代发通道");
} else {
  log("无 bridge，走 fetch + scoped token 通道");
}
refresh().catch((e) => log("读取失败: " + JSON.stringify(e)));
