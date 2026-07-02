/* Dashboard — every agent slot on this machine as a row card:
   running / stopped / design-only, with start/stop/open actions. */

export function mountDashboard(el, { api }) {
  el.innerHTML = "";

  const page = document.createElement("div");
  page.className = "page";

  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.textContent = "Your agents";
  const create = document.createElement("a");
  create.className = "btn primary";
  create.href = "#/setup";
  create.textContent = "Create team";
  head.appendChild(title);
  head.appendChild(create);
  page.appendChild(head);

  const list = document.createElement("div");
  list.className = "agent-list";
  page.appendChild(list);

  const empty = document.createElement("p");
  empty.className = "empty";
  empty.hidden = true;
  empty.textContent = "No agents yet. Create your first team to get started.";
  page.appendChild(empty);

  el.appendChild(page);

  // Track in-flight lifecycle actions so polling doesn't clobber the
  // "starting…"/"stopping…" state of a card mid-action.
  const busy = new Map(); // name -> "starting" | "stopping"
  let lastAgents = [];

  function statusOf(a) {
    if (busy.has(a.name)) return busy.get(a.name);
    if (!a.installed) return "design";
    return a.running ? "running" : "stopped";
  }

  function render() {
    list.innerHTML = "";
    empty.hidden = lastAgents.length > 0;
    for (const a of lastAgents) {
      list.appendChild(renderCard(a));
    }
  }

  function renderCard(a) {
    const st = statusOf(a);
    const card = document.createElement("div");
    card.className = "agent-card";

    const info = document.createElement("div");
    info.className = "agent-info";
    const top = document.createElement("div");
    top.className = "agent-top";
    const name = document.createElement("a");
    name.className = "agent-name";
    name.textContent = a.name;
    if (a.installed) name.href = "#/agents/" + encodeURIComponent(a.name);
    const status = document.createElement("span");
    status.className = "status " + st;
    status.textContent =
      st === "design" ? "not installed" :
      st === "starting" ? "starting…" :
      st === "stopping" ? "stopping…" : st;
    top.appendChild(name);
    top.appendChild(status);
    info.appendChild(top);
    if (a.description) {
      const d = document.createElement("div");
      d.className = "agent-desc";
      d.textContent = a.description;
      info.appendChild(d);
    }
    card.appendChild(info);

    const actions = document.createElement("div");
    actions.className = "agent-actions";
    if (a.installed) {
      if (st === "running") {
        actions.appendChild(btn("Open", "primary", () => {
          location.hash = "#/agents/" + encodeURIComponent(a.name);
        }));
        actions.appendChild(btn("Stop", "", () => act(a.name, "stop")));
      } else if (st === "stopped") {
        actions.appendChild(btn("Start", "primary", () => act(a.name, "start")));
      } else {
        const b = btn(st === "stopping" ? "Stopping…" : "Starting…", "", null);
        b.disabled = true;
        actions.appendChild(b);
      }
    } else {
      const hint = document.createElement("span");
      hint.className = "agent-hint";
      hint.textContent = "design only";
      actions.appendChild(hint);
    }
    card.appendChild(actions);
    return card;
  }

  function btn(label, kind, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn " + kind;
    b.textContent = label;
    if (onClick) b.addEventListener("click", onClick);
    return b;
  }

  async function act(name, verb) {
    busy.set(name, verb === "start" ? "starting" : "stopping");
    render();
    const { ok, data } = await api(
      "/api/agents/" + encodeURIComponent(name) + "/" + verb,
      { method: "POST", body: "{}" });
    if (!ok) {
      busy.delete(name);
      alertError(name, verb, data);
      await poll();
      return;
    }
    // Keep the busy state until polling observes the new run state.
    await waitForState(name, verb === "start");
    busy.delete(name);
    await poll();
  }

  async function waitForState(name, wantRunning, tries = 40) {
    for (let i = 0; i < tries; i++) {
      const { ok, data } = await api(
        "/api/agents/" + encodeURIComponent(name) + "/status");
      if (ok && data && data.running === wantRunning) return;
      await new Promise((r) => setTimeout(r, 750));
    }
  }

  function alertError(name, verb, data) {
    const detail = data && (data.report || data.error) || "unknown error";
    // A visible but unobtrusive failure row on the card list.
    const note = document.createElement("div");
    note.className = "action-error";
    note.textContent = `${verb} ${name} failed: ${detail}`;
    page.insertBefore(note, list);
    setTimeout(() => note.remove(), 12000);
  }

  async function poll() {
    const { ok, data } = await api("/api/dashboard");
    if (ok && data && Array.isArray(data.agents)) {
      lastAgents = data.agents;
      render();
    }
  }

  poll();
  const timer = setInterval(poll, 4000);
  return () => clearInterval(timer);
}
