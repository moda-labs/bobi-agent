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

// --- router ---------------------------------------------------------

let teardown = null; // current view's cleanup (clears pollers)

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] === "agents" && parts[1]) {
    return { view: "agent", name: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "setup") return { view: "setup" };
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
    stub(el, "Create a team",
         "Onboarding moves in here soon. Until then run `bobi setup` in a terminal.");
    return;
  }
  setSubtitle("agents");
  teardown = mountDashboard(el, { api });
}

window.addEventListener("hashchange", route);
route();
