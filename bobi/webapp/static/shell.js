/* bobi app shell — hash router + shared API client. Vanilla ES modules,
   no build step. Routes:
     #/                 dashboard (all agents on this machine)
     #/agents/<name>    one agent: subagent roster + chat
     #/setup            onboarding (create/modify a team)              */

import { mountDashboard } from "./views/dashboard.js";

const TOKEN = document
  .querySelector('meta[name="bobi-webui-token"]')
  .getAttribute("content");

let missedPings = 0;

/** Shared fetch wrapper: token header, JSON body/response, health track. */
export async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...opts,
      headers: {
        "x-bobi-webui-token": TOKEN,
        ...(opts.body ? { "Content-Type": "application/json" } : {}),
        ...(opts.headers || {}),
      },
    });
  } catch {
    noteFailure();
    return { ok: false, status: 0, data: null };
  }
  noteSuccess();
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  return { ok: res.ok, status: res.status, data };
}

function noteSuccess() {
  missedPings = 0;
  document.getElementById("gone").hidden = true;
  document.getElementById("health").className = "dot";
}

function noteFailure() {
  missedPings += 1;
  const health = document.getElementById("health");
  health.className = "dot stale";
  if (missedPings >= 3) {
    health.className = "dot down";
    document.getElementById("gone").hidden = false;
  }
}

export function setSubtitle(text) {
  document.getElementById("subtitle").textContent = text;
}

// Spend formatting, shared by the dashboard and agent views (#733):
// "$1.23" for dollars, "$0.0042" for cents-and-under; "" when there is no
// recorded spend, so a fresh install stays uncluttered. One home for the
// zero/rounding contract the spend panels depend on.
export function fmtUsd(n) {
  if (!n || n <= 0) return "";
  return "$" + (n >= 1 ? n.toFixed(2) : n.toFixed(4));
}

// The "~$X est" honesty marker (#760): estimated figures are fold-time
// list-price math over token counts for models that report no dollars (the
// codex brain), and must never render indistinguishably from a bill. One
// home for the marker so the total and the per-model rows cannot drift.
export function fmtEst(n) {
  const e = fmtUsd(n);
  return e ? `~${e} est` : "";
}

// Combined recorded + estimated spend display (#760). Recorded dollars are
// provider-reported. "" when there is neither, same as fmtUsd.
export function fmtSpend(recorded, estimated) {
  const r = fmtUsd(recorded);
  const e = fmtEst(estimated);
  if (r && e) return `${r} + ${e}`;
  return e || r;
}

// Compact token count for spend fallbacks: 1234 -> "1.2K", 3400000 -> "3.4M".
export function fmtTok(n) {
  if (!n || n <= 0) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
  return String(n);
}

// Tooltip suffix explaining the ~ figures wherever they appear.
export const EST_NOTE =
  " Figures marked ~ are estimates: token usage priced at current list " +
  "rates, not provider billing (plan-based usage has no marginal dollar cost).";

// Health chip derivation, shared by the dashboard cards and the agent
// header (#733 system health): hosted cards carry `reachability` (heartbeat
// age: live/stale/unreachable) and the sidecar's `manager_status`; local
// cards only running/stopped. The worst signal wins, so a silent or wedged
// team never renders as a calm "running".
export function healthChip(a) {
  if (!a) return { label: "…", cls: "" };
  // Truthy, matching every other `a.installed` branch in the card renderer.
  if (!a.installed) return { label: "draft", cls: "design" };
  if (a.reachability === "unreachable") {
    return { label: "unreachable", cls: "unreachable" };
  }
  if (a.reachability === "stale") return { label: "stale", cls: "stale" };
  if (a.manager_status === "wedged") return { label: "wedged", cls: "wedged" };
  return a.running ? { label: "running", cls: "running" }
                   : { label: "stopped", cls: "stopped" };
}

// Relative time for health/lifecycle rows: epoch ms → "12s ago" / "3m ago".
// "" for a missing timestamp so callers can hide the element.
export function fmtAgo(ms) {
  if (!ms || !Number.isFinite(ms)) return "";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 172800) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

// --- router ---------------------------------------------------------

let teardown = null; // current view's cleanup (clears pollers)

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = hash.split("?", 2);
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "agents" && parts[1]) {
    return { view: "agent", name: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "setup") {
    return {
      view: "setup",
      name: parts[1] ? decodeURIComponent(parts[1]) : "",
      model: params.get("model") || "",
    };
  }
  return { view: "dashboard" };
}

function stub(el, title, hint) {
  el.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "stub";
  const h = document.createElement("h2");
  h.textContent = title;
  const p = document.createElement("p");
  p.textContent = hint;
  wrap.appendChild(h);
  wrap.appendChild(p);
  el.appendChild(wrap);
}

async function route() {
  if (teardown) { teardown(); teardown = null; }
  const el = document.getElementById("view");
  const r = parseRoute();
  if (r.view === "agent") {
    const mod = await import("./views/agent.js").catch(() => null);
    if (mod) {
      setSubtitle(r.name);
      teardown = mod.mountAgent(el, { api, name: r.name });
      return;
    }
    stub(el, r.name, "The agent view is coming in this build.");
    return;
  }
  if (r.view === "setup") {
    setSubtitle("setup");
    mountSetupEntry(el, r);
    return;
  }
  setSubtitle("agents");
  teardown = mountDashboard(el, { api });
}

/** Open (or resume) an onboarding/editor session and navigate to it.
    body: {} to create, {name, mode: "open"} to edit an existing design.
    Returns null on success (navigation happens), else an error string. */
export async function openSetup(body) {
  const { ok, data } = await api("/api/setup/open", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
  if (ok && data && data.url) {
    location.href = data.url;
    return null;
  }
  return (data && data.error) || "could not start setup";
}

/* #/setup — a transient route: kick off onboarding and hand the browser
   to the hosted setup flow. Kept for deep links; the dashboard's create
   card is the primary entry. */
async function mountSetupEntry(el, routeInfo = {}) {
  el.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "stub";
  const h = document.createElement("h2");
  h.textContent = "Opening setup…";
  wrap.appendChild(h);
  el.appendChild(wrap);

  const body = {};
  if (routeInfo.name) body.name = routeInfo.name;
  if (routeInfo.model) body.model = routeInfo.model;
  const err = await openSetup(body);
  if (err) {
    h.textContent = "Couldn't open setup";
    const p = document.createElement("p");
    p.textContent = err;
    wrap.appendChild(p);
  }
}

window.addEventListener("hashchange", route);
route();
