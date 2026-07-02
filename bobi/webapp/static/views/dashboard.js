/* Dashboard — the home screen: a card grid of every agent on this machine
   plus a create card. Cards navigate (installed → the agent's dashboard,
   design-only → the editor); lifecycle actions live on the agent's own
   dashboard, not here. */

import { openSetup } from "../shell.js";

export function mountDashboard(el, { api }) {
  el.innerHTML = "";

  const page = document.createElement("div");
  page.className = "page";

  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.textContent = "Your agents";
  head.appendChild(title);
  page.appendChild(head);

  const grid = document.createElement("div");
  grid.className = "agent-grid";
  page.appendChild(grid);

  el.appendChild(page);

  let lastAgents = [];

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
    const st = !a.installed ? "design" : a.running ? "running" : "stopped";
    status.className = "status " + st;
    status.textContent = st === "design" ? "draft" : st;
    top.appendChild(name);
    top.appendChild(status);
    c.appendChild(top);

    const d = document.createElement("div");
    d.className = "agent-desc";
    d.textContent = a.description || (a.installed
      ? "An installed agent team."
      : "A design that hasn't been installed yet.");
    c.appendChild(d);

    const go = document.createElement("span");
    go.className = "agent-go";
    go.textContent = a.installed ? "Open →" : "Edit design →";
    c.appendChild(go);

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

  async function poll() {
    const { ok, data } = await api("/api/dashboard");
    if (ok && data && Array.isArray(data.agents)) {
      lastAgents = data.agents;
      render();
    }
  }

  render();   // paint the create card immediately; agents fill in
  poll();
  const timer = setInterval(poll, 4000);
  return () => clearInterval(timer);
}
