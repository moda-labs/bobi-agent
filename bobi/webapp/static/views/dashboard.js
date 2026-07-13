/* Dashboard — the home screen: a card grid of every agent on this machine
   plus a create card. Cards navigate (installed → the agent's dashboard,
   design-only → the editor); lifecycle actions live on the agent's own
   dashboard, not here. */

import { openSetup, fmtUsd, healthChip } from "../shell.js";

export function mountDashboard(el, { api }) {
  el.innerHTML = "";

  const page = document.createElement("div");
  page.className = "page";

  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.textContent = "Your agents";
  head.appendChild(title);
  const fleetSpend = document.createElement("span");
  fleetSpend.className = "fleet-spend";
  fleetSpend.hidden = true;
  head.appendChild(fleetSpend);
  page.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "agent-grid";
  page.appendChild(grid);

  el.appendChild(page);

  let lastAgents = [];
  let spendByTeam = new Map();   // name -> total_cost_usd (installed teams)

  function card(a) {
    const c = document.createElement("button");
    c.type = "button";
    c.className = "agent-tile";

    const top = document.createElement("div");
    top.className = "agent-top";
    const name = document.createElement("span");
    name.className = "agent-name";
    name.textContent = a.name;
    const status = document.createElement("span");
    // Distinct health badge (#733): reachability/wedged outrank
    // running/stopped on cards that carry them (hosted fleet).
    const chip = healthChip(a);
    status.className = "status " + chip.cls;
    status.textContent = chip.label;
    top.appendChild(name);
    top.appendChild(status);
    c.appendChild(top);

    const d = document.createElement("div");
    d.className = "agent-desc";
    d.textContent = a.description || (a.installed
      ? "An installed agent team."
      : "A design that hasn't been installed yet.");
    c.appendChild(d);

    const foot = document.createElement("div");
    foot.className = "agent-foot";
    const spend = fmtUsd(spendByTeam.get(a.name));
    if (a.installed && spend) {
      const s = document.createElement("span");
      s.className = "tile-spend";
      s.textContent = spend;
      s.title = "Cumulative recorded spend for this team";
      foot.appendChild(s);
    }
    const go = document.createElement("span");
    go.className = "agent-go";
    go.textContent = a.installed ? "Open →" : "Edit design →";
    foot.appendChild(go);
    c.appendChild(foot);

    c.addEventListener("click", async () => {
      if (a.installed) {
        location.hash = "#/agents/" + encodeURIComponent(a.name);
        return;
      }
      const err = await openSetup({ name: a.name, mode: "open" });
      if (err) showError(err);
    });
    return c;
  }

  function createCard() {
    const c = document.createElement("button");
    c.type = "button";
    c.className = "agent-tile create";
    c.innerHTML = `
      <span class="create-glyph" aria-hidden="true">+</span>
      <span class="agent-name">Create a new agent</span>
      <span class="agent-desc">From scratch or from a template, in a guided
        conversation.</span>`;
    c.addEventListener("click", async () => {
      const err = await openSetup({});
      if (err) showError(err);
    });
    return c;
  }

  function showError(msg) {
    const note = document.createElement("div");
    note.className = "action-error";
    note.textContent = msg;
    page.insertBefore(note, grid);
    setTimeout(() => note.remove(), 12000);
  }

  function render() {
    grid.innerHTML = "";
    for (const a of lastAgents) grid.appendChild(card(a));
    grid.appendChild(createCard());
  }

  function renderFleetSpend(data) {
    const total = fmtUsd(data && data.total_cost_usd);
    if (!total) { fleetSpend.hidden = true; return; }
    const n = data.sessions_counted || 0;
    fleetSpend.textContent = `${total} spent · ${n} session${n === 1 ? "" : "s"}`;
    // Lifetime-cumulative across every session on disk, not a time period.
    fleetSpend.title =
      "Cumulative recorded spend across all teams and sessions on disk (not a time period)";
    fleetSpend.hidden = false;
  }

  async function poll() {
    const [dash, spend] = await Promise.all([
      api("/api/dashboard"),
      api("/api/fleet/spend"),
    ]);
    if (spend.ok && spend.data) {
      spendByTeam = new Map(
        (spend.data.teams || []).map((t) => [t.name, t.total_cost_usd]));
      renderFleetSpend(spend.data);
    }
    if (dash.ok && dash.data && Array.isArray(dash.data.agents)) {
      lastAgents = dash.data.agents;
      render();
    }
  }

  render();   // paint the create card immediately; agents fill in
  poll();
  const timer = setInterval(poll, 4000);
  return () => clearInterval(timer);
}
