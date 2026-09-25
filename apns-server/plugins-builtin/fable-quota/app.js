/* 肥波额度插件 — 读服务端 GET /fable-quota 渲染估计值与官方 7d% 对照。
 * 鉴权: 与插件静态托管同一道 _require_auth (App WebView / PWA 会话 cookie
 * 同源自动携带); 页面不接触任何凭据。每分钟自动刷新一次。
 */
"use strict";

const REFRESH_MS = 60 * 1000;

function $(id) { return document.getElementById(id); }

function fmtPct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return (Number.isInteger(n) ? n.toFixed(0) : n.toFixed(1)) + "%";
}

function fmtTs(epoch) {
  if (!epoch) return "—";
  const d = new Date(Number(epoch) * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function loadQuota() {
  const err = $("error-line");
  err.textContent = "";
  let res;
  try {
    res = await fetch("/fable-quota", { cache: "no-store", credentials: "same-origin" });
  } catch (e) {
    err.textContent = "网络错误，稍后再试。";
    return;
  }
  if (res.status === 503) {
    err.textContent = "服务端肥波采样未启用。";
    return;
  }
  if (!res.ok) {
    err.textContent = `读取失败 (HTTP ${res.status})。`;
    return;
  }
  const data = await res.json();
  $("fable-pct").textContent = fmtPct(data.fable_estimate_pct);
  $("official-pct").textContent = fmtPct(data.official_seven_day_pct);
  const bar = $("fable-bar");
  const pct = Math.max(0, Math.min(100, Number(data.fable_estimate_pct) || 0));
  bar.style.width = pct + "%";
  bar.classList.toggle("hot", pct >= 80);

  const model = data.current_model || "—";
  $("current-model").textContent = model;
  const isFable = /fable/i.test(model);
  $("model-badge").classList.toggle("hidden", !isFable);

  const week = data.week || {};
  $("week-start").textContent = week.start_bj || fmtTs(week.start_at);
  $("week-reset").textContent = week.reset_bj || fmtTs(week.reset_at);

  const s = data.last_sample || {};
  $("sample-ts").textContent = fmtTs(s.ts);
  $("sample-model").textContent = s.model || "—";
  $("sample-7d").textContent = fmtPct(s.seven_day_pct);
  $("sample-5h").textContent = fmtPct(s.five_hour_pct);

  $("methodology").textContent = data.methodology || "—";
}

$("refresh-btn").addEventListener("click", loadQuota);
loadQuota();
setInterval(loadQuota, REFRESH_MS);
