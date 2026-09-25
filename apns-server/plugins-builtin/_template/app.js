/* 插件模板 app.js —— KV 读写 + 409 版本冲突重试的最小示例。
 *
 * 数据通道优先级：
 * 1. App 原生 bridge（window.CCBridge，主链路）：App 代发请求，页面不接触任何凭据。
 *    JS 侧约定（App 端实现时对齐）：
 *      await window.CCBridge.readData(doc)                 -> { version, body }（不存在时 { version: 0, body: null }）
 *      await window.CCBridge.writeData(doc, body, version) -> { version }；版本冲突时 throw { status: 409, version, body }
 *      window.CCBridge.sendToXiaoke(text)                  -> 把一段文本发给小克（可选能力）
 * 2. fetch + scoped token（纯浏览器降级通道，方便测试）：
 *    请求头 X-Plugin-Token: <token>，token 由持有 X-Auth-Token 的一端
 *    POST /plugins/<id>/token 签发（fail-closed，仅 shared_secret 可签）。
 */
"use strict";

const PLUGIN_ID = "_template"; // 复制模板时改成你的插件 id（与目录名一致）

const bridge = typeof window !== "undefined" ? window.CCBridge : null;

function getToken() {
  try { return localStorage.getItem("cc_plugin_token_" + PLUGIN_ID) || ""; }
  catch (e) { return ""; }
}

async function kvRead(doc) {
  if (bridge && typeof bridge.readData === "function") {
    return bridge.readData(doc); // -> { version, body }
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
    return bridge.writeData(doc, body, version); // -> { version }，409 时 throw
  }
  const res = await fetch(`/plugins/${PLUGIN_ID}/data/${encodeURIComponent(doc)}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-Plugin-Token": getToken(),
      "If-Match": String(version), // 0 = 仅新建；N = 当前 version 为 N 才写
    },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 409) throw { status: 409, version: data.version, body: data.body };
  if (!res.ok) throw { status: res.status, error: data.error };
  return { version: data.version };
}

/* 乐观并发：重读-合并-重试。写冲突 (409) 时用响应里的最新 version/body 重新合并。 */
async function kvMutate(doc, mutate, maxRetries = 4) {
  let cur = await kvRead(doc);
  for (let attempt = 0; ; attempt++) {
    try {
      return await kvWrite(doc, mutate(cur.body), cur.version);
    } catch (e) {
      if (e && e.status === 409 && attempt + 1 < maxRetries) {
        cur = { version: e.version, body: e.body }; // 409 响应自带当前版本和内容
        continue;
      }
      throw e;
    }
  }
}

/* ---- 以下为模板页面本身的演示逻辑 ---- */

function log(msg) {
  const el = document.getElementById("log");
  el.textContent += (el.textContent ? "\n" : "") + msg;
}

async function refresh() {
  const cur = await kvRead("counter");
  const n = (cur.body && typeof cur.body.count === "number") ? cur.body.count : 0;
  document.getElementById("counter-view").textContent = `count=${n} (v${cur.version})`;
}

document.getElementById("inc-btn").addEventListener("click", async () => {
  try {
    const r = await kvMutate("counter", (body) => ({
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
