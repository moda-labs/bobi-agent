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
    mountSetupEntry(el);
    return;
  }
  setSubtitle("agents");
  teardown = mountDashboard(el, { api });
}

/* Setup entry — name the team, then hand off to the hosted onboarding
   flow at /setup/ (the full setup experience, served by this same app). */
async function mountSetupEntry(el) {
  el.innerHTML = "";
  const page = document.createElement("div");
  page.className = "page setup-entry";
  page.innerHTML = `
    <h1>Create a team</h1>
    <p class="setup-hint">Name your agent team, then design it in a guided
      conversation. You can rename it during setup.</p>
    <form class="setup-form" data-el="form">
      <input data-el="name" type="text" placeholder="e.g. content-review"
             autocomplete="off" spellcheck="false">
      <button class="btn primary" type="submit">Start onboarding</button>
    </form>
    <div class="setup-resume" data-el="resume" hidden></div>
    <div class="action-error" data-el="error" hidden></div>`;
  el.appendChild(page);

  const els = {};
  for (const n of page.querySelectorAll("[data-el]")) els[n.dataset.el] = n;

  const { ok, data } = await api("/api/setup/current");
  if (ok && data && data.active && data.name) {
    els.resume.hidden = false;
    els.resume.innerHTML = "";
    const p = document.createElement("p");
    p.textContent = `An onboarding session for “${data.name}” is in progress.`;
    const b = document.createElement("a");
    b.className = "btn";
    b.href = "/setup/";
    b.textContent = "Resume it";
    els.resume.appendChild(p);
    els.resume.appendChild(b);
  }

  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    els.error.hidden = true;
    const name = els.name.value.trim();
    const { ok, data } = await api("/api/setup/open", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    if (ok && data && data.url) {
      location.href = data.url;
      return;
    }
    els.error.textContent = (data && data.error) || "could not start setup";
    els.error.hidden = false;
  });
  els.name.focus();
}

window.addEventListener("hashchange", route);
route();
