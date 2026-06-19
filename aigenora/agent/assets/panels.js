/**
 * panels.js v1 — shared utility for protocol ui/index.html panels.
 *
 * Exports pure functions only. No DOM components, no localStorage.
 * Each protocol HTML composes these to render its own three independent panels.
 */
// ---- SSE ----

let _sse = null;
let _sseHandlers = {};

export function connectSSE(onEvent) {
  if (_sse) { try { _sse.close(); } catch(_){} }
  _sseHandlers = {};
  _sse = new EventSource("/sse/stream");
  const names = ["snapshot", "strategy", "event", "detail", "whispers", "whisper", "whisper_ack"];
  names.forEach(n => {
    _sse.addEventListener(n, e => {
      try { onEvent(n, JSON.parse(e.data)); } catch(_){}
    });
  });
  _sse.onerror = () => {};
  return _sse;
}

export function closeSSE() {
  if (_sse) { _sse.close(); _sse = null; }
}

// ---- Read endpoints ----

export async function fetchSnapshot() {
  return await fetch("/api/snapshot").then(r => r.json());
}

export async function fetchStrategy() {
  return await fetch("/api/strategy").then(r => r.json());
}

export async function fetchWhispers() {
  return await fetch("/api/whispers").then(r => r.json()).catch(() => []);
}

export async function fetchDetails() {
  return await fetch("/api/details").then(r => r.json()).catch(() => []);
}

// ---- Write endpoints ----

export async function postDecide(body) {
  const r = await fetch("/api/decide", { method: "POST", body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  data._http = r.status;
  return data;
}

export async function postStrategySet(strategy, meta) {
  const payload = { ...strategy, ...(meta || {}) };
  const r = await fetch("/api/strategy/set", { method: "POST", body: JSON.stringify(payload) });
  return { ok: r.ok, status: r.status };
}

export async function postStrategyMerge(patch, meta) {
  const payload = { ...patch, ...(meta || {}) };
  const r = await fetch("/api/strategy/merge", { method: "POST", body: JSON.stringify(payload) });
  return { ok: r.ok, status: r.status };
}

export async function postWhisper(text, opts) {
  const body = { text, ...(opts || {}) };
  const r = await fetch("/api/whisper", { method: "POST", body: JSON.stringify(body) });
  return await r.json().catch(() => ({}));
}

export async function postWhisperAck(id, ack) {
  const body = { id, ...ack };
  const r = await fetch("/api/whisper/ack", { method: "POST", body: JSON.stringify(body) });
  return await r.json().catch(() => ({}));
}

// ---- Utilities ----

export function formatTs(ts) {
  if (!ts) return "";
  try { return new Date(ts).toLocaleTimeString([], { hour12: false }); }
  catch(_) { return ts.slice(11, 19); }
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function safeJson(text, fallback) {
  try { return JSON.parse(text); } catch(_) { return fallback; }
}

export function ackChip(latestAck) {
  if (!latestAck) return { symbol: "⏳", color: "pending", tooltip: "pending" };
  const s = latestAck.status || "pending";
  const map = {
    executed:  { symbol: "✅", color: "executed",  tooltip: `executed: ${latestAck.action || ""}` },
    dismissed: { symbol: "⚪", color: "dismissed", tooltip: "dismissed" },
    unparsed:  { symbol: "❓", color: "unparsed",  tooltip: "unparsed" },
    error:     { symbol: "❌", color: "error",     tooltip: `error: ${latestAck.reason_code || ""}` },
  };
  return map[s] || { symbol: "⏳", color: "pending", tooltip: s };
}

export function genIdempotencyKey() {
  return 'xxxxxxxx-xxxx-4xxx'.replace(/[x]/g, () => (Math.random()*16|0).toString(16));
}

export function genAgentLabel(prefix) {
  prefix = prefix || "browser";
  try {
    let id = sessionStorage.getItem("aigenora.agent_id");
    if (!id) {
      id = prefix + "-" + Math.random().toString(36).slice(2, 10);
      sessionStorage.setItem("aigenora.agent_id", id);
    }
    return id;
  } catch(_) { return prefix + "-anon"; }
}
