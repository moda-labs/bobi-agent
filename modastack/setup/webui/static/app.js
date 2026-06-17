/* modastack setup — the front-end. Vanilla, no build, offline.
   ONE screen: an objective-guided conversation (left) while the team
   materializes as cards (right). The LLM serves the conversation (SSE) and the
   Build pour (SSE). Secrets are captured in dedicated on-demand components,
   never in the chat. Build/Done reuse the generating + done views. */
(() => {
  const NONCE = document.querySelector('meta[name="modastack-nonce"]').content;
  const H = { "x-modastack-nonce": NONCE };
  const $ = (sel, el = document) => el.querySelector(sel);
  // Escapes for both element text AND double/single-quoted attribute contexts
  // (role/service names are user- and LLM-authored and flow into value="…" /
  // data-*="…" sinks — a stray quote would break out of the attribute).
  const esc = (s) => (s || "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Grow a textarea to fit its content (wraps long text instead of scrolling
  // sideways), capped by max-height in CSS which then scrolls vertically.
  const autoGrow = (el) => { if (!el) return; el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; };
  const slugify = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);

  const GENERATING = new Set(["build", "review", "install"]);
  // TBD: real cloud-deploy docs URL.
  const DOCS_CLOUD_URL = "https://docs.modastack.ai/cloud";
  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4"><path d="M5 12l5 5L19 7"/></svg>';
  const TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m2 0v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7"/></svg>';
  const STATUS_LABEL = { connected: "connected", missing: "connect", unknown: "needs check" };
  // Day-to-day channels for the Chat card.
  const CHANNELS = [
    { key: "cli", name: "Command line", soon: false },
    { key: "slack", name: "Slack", soon: false },
    { key: "telegram", name: "Telegram", soon: true },
  ];

  let S = null;            // latest serialized state
  let _connData = null;    // last /api/connect payload (drives connect cards)
  let _prevDone = null;    // per-slot completion last render — drives the celebrate pulse
  let _prevSig = null;     // per-card HTML signatures — diff to re-render only what changed
  let _prevPhase;          // last interview phase — drives the phase-banner ease-in
  let _prevGathered = null; // last N/5 — drives the meter tick-up

  // --- connection state --------------------------------------------------
  // The page is useless without its local setup server. If that server dies
  // (Ctrl-C, closed terminal, crash) the UI must say so and stop pretending to
  // be live — every action would silently fail otherwise. A heartbeat plus
  // fetch-failure detection flips a blocking overlay; it clears itself if the
  // server comes back (e.g. `modastack setup --resume`).
  let _finished = false;        // set when the user intentionally finishes
  let _disconnected = false;
  function markDisconnected() {
    if (_finished || _disconnected) return;
    _disconnected = true;
    if (document.getElementById("disc-ov")) return;
    const ov = document.createElement("div");
    ov.id = "disc-ov";
    ov.className = "disc";
    ov.innerHTML = `<div class="disc-panel">
      <div class="disc-dot"></div>
      <h2>Setup server disconnected</h2>
      <p>The local <code>modastack setup</code> server stopped — closed, interrupted, or crashed. Nothing here works until it's back.</p>
      <div class="disc-cmd"><span class="pr">$</span> modastack setup --resume</div>
      <p class="disc-sub">Run that in your terminal — this page reconnects on its own. Your progress is saved.</p></div>`;
    document.body.appendChild(ov);
  }
  function markConnected() {
    if (!_disconnected) return;
    _disconnected = false;
    const ov = document.getElementById("disc-ov");
    if (ov) ov.remove();
  }

  // --- api helpers -------------------------------------------------------
  async function getJSON(path) {
    let r;
    try { r = await fetch(path, { headers: H }); }
    catch (e) { markDisconnected(); throw e; }   // network failure = server gone
    markConnected();
    return r.json();
  }
  async function postJSON(path, body) {
    let r;
    try {
      r = await fetch(path, {
        method: "POST", headers: { ...H, "content-type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (e) { markDisconnected(); throw e; }
    markConnected();
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  }
  function parseSSE(raw) {
    let ev = "message", data = "";
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) ev = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    try { return { event: ev, data: JSON.parse(data) }; }
    catch { return { event: ev, data: {} }; }
  }
  async function sse(path, body, handlers) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST", headers: { ...H, "content-type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (e) { markDisconnected(); throw e; }
    markConnected();
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      let chunk;
      try { chunk = await reader.read(); }
      catch (e) { markDisconnected(); throw e; }  // stream cut = server gone
      const { value, done } = chunk;
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) !== -1) {
        const ev = parseSSE(buf.slice(0, i));
        buf = buf.slice(i + 2);
        if (handlers[ev.event]) handlers[ev.event](ev.data);
      }
    }
  }

  // --- navigation --------------------------------------------------------
  // The homepage (team hub) isn't a setup stage — it's a client view shown at
  // stage `start` for returning users, and after Finish via the Done button.
  let atHome = false;
  async function refresh() { S = await getJSON("/api/state"); render(); }
  async function boot() {
    S = await getJSON("/api/state");
    // A finished session returns to the hub; so do returning users who already
    // have teams and aren't mid-setup. New users get the welcome on-ramp.
    if (S.finished) {
      atHome = true;
    } else if (S.stage === "start") {
      try { const h = await getJSON("/api/home"); if ((h.teams || []).length) atHome = true; }
      catch { /* no hub — fall through to the welcome on-ramp */ }
    }
    render();
  }
  async function go(stage) {
    const r = await postJSON("/api/advance", { to: stage });
    if (!r.ok) { toast(r.data.error || "can't go there yet"); return; }
    S = r.data; render();
  }
  // Step one stage backward. The wizard is a re-entrant editor, so backward
  // moves are always allowed (the spec/conversation persist). From the build,
  // this cancels the in-flight build (buildGen bump) and returns to editing.
  const BACK_TO = { design: "start", build: "design", review: "design", install: "design", done: "design" };
  async function goBack() {
    const target = BACK_TO[S.stage];
    if (!target) return;
    building = false; buildGen++;   // supersede any in-flight build
    await go(target);
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--surface);font-size:13px;padding:9px 15px;border-radius:8px;z-index:140;box-shadow:0 6px 20px -6px rgba(0,0,0,.4)";
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }
  function setPanes(cols) { $("#panes").style.gridTemplateColumns = cols; }

  // --- welcome: the on-ramp before the intro ----------------------------
  // A calm first screen — what modastack is, how setup goes, what you'll need —
  // shown once per page load on the `start` stage. "Get started" reveals the
  // intro. Purely presentational: no server state, skipped on resume (resume
  // lands on a later stage, never `start`).
  let welcomed = false;
  // Vertical recast of the event-driven flow diagram from buildmoda.ai/bobi:
  // event → team → workflow → gate → outcome. Geometric inline-SVG glyphs
  // (offline; no images), accent reserved for the final checkmark.
  const FLOW_NODES = [
    { label: "Event", eg: "a ticket lands",
      svg: '<path d="M12 3.5l8.5 8.5-8.5 8.5-8.5-8.5z"/>' },
    { label: "Team", eg: "manager + agents",
      svg: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="8.5" y="13.5" width="7" height="7" rx="1.2"/>' },
    { label: "Workflow", eg: "a YAML workflow",
      svg: '<path d="M5 7h14M5 12h14M5 17h9"/>' },
    { label: "Gate", eg: "human approval",
      svg: '<circle cx="12" cy="12" r="8.5"/><path d="M8.4 12.2l2.4 2.4 4.6-5"/>' },
    { label: "Outcome", eg: "shipped", accent: true,
      svg: '<path d="M4.5 12.5l4.5 4.5L19.5 6.5"/>' },
  ];
  function flowDiagramHTML() {
    // --i drives both the staggered fade-in and the looping flow pulse (CSS).
    const nodes = FLOW_NODES.map((n, i) =>
      `<li class="wfnode" style="--i:${i}">
        <span class="wfglyph${n.accent ? " wfg-accent" : ""}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${n.svg}</svg></span>
        <span class="wftext"><span class="wflabel">${esc(n.label)}</span><span class="wfeg">${esc(n.eg)}</span></span>
      </li>`).join("");
    return `<aside class="wflow-side">
      <div class="wflow-head">How it runs</div>
      <ol class="wflow">${nodes}</ol>
      <p class="wflow-cap">Events in. Agents act. Guardrails hold. Work ships.</p>
    </aside>`;
  }
  function renderWelcome() {
    setPanes("1fr");
    $("#main").innerHTML = `<div class="node welcome-screen"><div class="welcome-wrap">
      <main class="welcome">
        <div class="eyebrow">Welcome to modastack</div>
        <h1>Build a team of agents that runs your work</h1>
        <p class="lede">A realtime agent team on your events — reachable from Slack and other chat apps, scheduled to act on their own, or reacting the moment something happens. Not one chatbot waiting for a prompt.</p>

        <div class="wsec-label">How setup works</div>
        <ol class="wsteps">
          <li class="wstep"><span class="wstep-n">1</span><div><b>Describe it.</b> Tell modastack what you want the team to do, in plain words — rough is fine.</div></li>
          <li class="wstep"><span class="wstep-n">2</span><div><b>Watch it take shape.</b> As you talk, modastack designs the roles, automations, and connections, filling them in live.</div></li>
          <li class="wstep"><span class="wstep-n">3</span><div><b>Connect services.</b> Hook up Slack, GitHub, or anything else it needs — deferrable until the end.</div></li>
          <li class="wstep"><span class="wstep-n">4</span><div><b>Build &amp; install.</b> modastack writes the team and installs it, then you start it with one command.</div></li>
        </ol>

        <div class="wmeta">
          <div class="wmeta-row"><span class="wmeta-k">Takes</span><span class="wmeta-v">about 10–20 minutes</span></div>
          <div class="wmeta-row"><span class="wmeta-k">You'll need</span><span class="wmeta-v">the Claude Code CLI (already running this), plus logins for any services you want to connect — added as you go.</span></div>
        </div>

        <div class="actions"><button class="btn primary" id="welcome-go">Get started →</button></div>
      </main>
      ${flowDiagramHTML()}
    </div></div>`;
    $("#welcome-go").addEventListener("click", () => { welcomed = true; renderIntro(); });
  }

  // --- intro: pick a template team or design a new one ------------------
  // Two ways in: start from a registry "template" (download + reverse-fill,
  // non-lossy edit) or design a new team from scratch (auto-named in chat).
  // Modify-an-existing-team will return later in a different shape.
  let introRegistry = null, introBase = "modastack", introLoc = "";
  async function renderIntro() {
    setPanes("1fr");
    const data = await getJSON("/api/intro");
    introBase = data.default_location || introBase;
    introLoc = introBase;
    drawIntro();
    loadTemplates();
  }
  function drawIntro() {
    $("#main").innerHTML = `<main class="node narrow intro">
      <div class="eyebrow">Setup</div>
      <h1>Build an agent team</h1>
      <p class="lede">modastack manages entire teams of agents that collaborate to solve problems and automate your work. Some of our favorites are engineering, support, and marketing teams.</p>
      <p class="intro-lead">Customize your own from scratch, or start from a template.</p>
      <div class="tmpl-list" id="tmpl-list">${templatesHTML()}</div>
      ${locFyiHTML()}
    </main>`;
    wireIntro();
  }
  // Templates come from the configured registries (lazy / network-backed), so
  // the intro paints immediately and fills the list in when they arrive.
  function templatesHTML() {
    // The "customize my own" entry is always the prominent top option — part of
    // the (scrollable) list, not a separate button, and sticky so it keeps
    // popping even when many templates push it past the fold.
    const custom = `<button class="tmpl tmpl-custom" data-newteam>
      <span class="tmpl-glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg></span>
      <span class="tmpl-text"><b>Customize my own agent team</b><span>Start from scratch — describe it and modastack designs it with you.</span></span>
      <span class="tmpl-go">New →</span>
    </button>`;
    let rest = "";
    if (introRegistry === null) rest = `<p class="ihint tmpl-note">Loading templates…</p>`;
    else if (!introRegistry.length) rest = `<p class="ihint tmpl-note">No templates available yet.</p>`;
    else rest = introRegistry.map(t =>
      `<button class="tmpl" data-template="${esc(t.name)}">
        <span class="tmpl-glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="8.5" y="13.5" width="7" height="7" rx="1.2"/></svg></span>
        <span class="tmpl-text"><b>${esc(t.name)}</b><span>${esc(t.description || t.registry || "Agent team template")}</span></span>
        <span class="tmpl-go">Use →</span>
      </button>`).join("");
    return custom + rest;
  }
  function locFyiHTML() {
    return `<p class="loc-fyi">Your team will be managed in <code id="loc-path">${esc(introLoc)}</code> — <button type="button" class="linkbtn" id="loc-change">change location</button> if you'd like.</p>`;
  }
  async function loadTemplates() {
    const data = await getJSON("/api/registry");
    introRegistry = data.teams || [];
    const el = $("#tmpl-list");
    if (el) el.innerHTML = templatesHTML();
  }
  function wireIntro() {
    const lc = $("#loc-change");
    if (lc) lc.addEventListener("click", () => openFolderPicker(p => {
      introLoc = p; const lp = $("#loc-path"); if (lp) lp.textContent = p;
    }));
  }
  // Shared start path: disables the clicked control, posts, advances on success.
  async function startTeam(body, btn, busy) {
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = busy; }
    const r = await postJSON("/api/start", body);
    if (!r.ok) {
      toast(r.data.error || "couldn't start");
      if (btn) { btn.disabled = false; btn.textContent = label; }
      return;
    }
    S = r.data; render();
  }
  // Server-side folder picker (a localhost page can't open a native OS dialog).
  async function openFolderPicker(onPick) {
    let cur = "";
    const ov = document.createElement("div");
    ov.className = "picker";
    ov.innerHTML = `<div class="pick-panel">
      <div class="pick-head"><b>Choose a folder</b><button class="btn ghost xs" id="pick-close">Close</button></div>
      <div class="pick-path" id="pick-path"></div>
      <div class="pick-list" id="pick-list"></div>
      <div class="pick-foot"><span class="pick-cur" id="pick-cur"></span>
        <button class="btn primary xs" id="pick-use">Use this folder</button></div></div>`;
    document.body.appendChild(ov);
    const load = async (path) => {
      const d = await getJSON("/api/browse?path=" + encodeURIComponent(path || ""));
      if (d.error) return;
      cur = d.path;                       // absolute, home-rooted
      $("#pick-path").textContent = cur;
      $("#pick-cur").textContent = cur;
      let rows = "";
      if (d.parent !== null && d.parent !== undefined)
        rows += `<div class="pnode up" data-pick="${esc(d.parent)}">⬆ ..</div>`;
      rows += (d.dirs || []).map(name => {
        const child = cur.replace(/\/$/, "") + "/" + name;
        return `<div class="pnode" data-pick="${esc(child)}">📁 ${esc(name)}</div>`;
      }).join("");
      $("#pick-list").innerHTML = rows || `<div class="pempty">no subfolders here</div>`;
    };
    await load("");
    ov.addEventListener("click", (e) => {
      if (e.target.id === "pick-close" || e.target === ov) { ov.remove(); return; }
      if (e.target.id === "pick-use") { onPick(cur || introBase); ov.remove(); return; }
      const n = e.target.closest("[data-pick]"); if (n) load(n.dataset.pick);
    });
  }

  // --- the one screen: chat + the team materializing as cards ------------
  function renderUnified() {
    setPanes("1fr 380px");
    // Two grid items (the wrapper #main is display:contents): chat | panel.
    $("#main").innerHTML = `
      <section class="chat sketch uni-chat">
        <div class="sketch-top"><span class="st-group"><button class="backbtn" data-back>← Back</button><span class="sketch-eyebrow">modastack · build your team</span></span></div>
        <div class="ch-body" id="chbody"></div>
        <div class="cue" id="cue"></div>
        <div class="ch-input"><textarea id="chinput" rows="1" placeholder="Tell modastack what you want to build…" autocomplete="off"></textarea><button class="btn primary" id="chsend" style="padding:9px 14px">↑</button></div>
      </section>
      <aside class="uni-panel">
        <div class="uni-head"><span class="up-title" id="up-title" title="click to rename"></span><span class="up-meter" id="uni-meter"></span></div>
        <div class="uni-phase" id="uni-phase"></div>
        <div class="uni-cards" id="uni-cards"></div>
        <div class="uni-foot" id="uni-foot"></div>
      </aside>`;
    renderMessages();
    updateCue();
    renderUniCards();
    $("#up-title").addEventListener("click", beginRename);
    if ((S.spec.services || []).length) refreshUniConnections();
    const input = $("#chinput");
    input.focus();
    autoGrow(input);
    input.addEventListener("input", () => autoGrow(input));
    // Enter sends; Shift+Enter inserts a newline (the box grows to fit).
    input.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    $("#chsend").addEventListener("click", () => sendMessage());
  }

  // The five things modastack gathers, each a card that fills in + checks off
  // live: goal, roles, automations, connections, chat.
  // The team's name shows in the panel header as modastack auto-derives it; click
  // to rename. (Empty until the goal firms up enough to name the team.)
  function setTeamTitle() {
    const el = $("#up-title"); if (!el) return;
    el.textContent = S.team_name || "Your team";
    el.classList.toggle("named", !!S.team_name);
  }
  function beginRename() {
    const el = $("#up-title");
    if (!el || $("#rename-in")) return;   // already editing
    el.innerHTML = `<input id="rename-in" autocomplete="off" value="${esc(S.team_name || "")}">`;
    const inp = $("#rename-in"); inp.focus(); inp.select();
    let done = false;
    const commit = async () => {
      if (done) return; done = true;
      const name = inp.value.trim();
      if (name) { const r = await postJSON("/api/rename", { name }); if (r.ok) S = r.data; }
      setTeamTitle();
    };
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") { done = true; setTeamTitle(); }
    });
    inp.addEventListener("blur", commit);
  }
  function renderUniCards() {
    const host = $("#uni-cards");
    if (!host) return;
    setTeamTitle();
    renderPhase();
    const sp = S.spec;
    // The Connections slot counts only when every implied service is truly
    // connected (not merely "named") — the brain's readiness can't see live auth.
    const connOk = servicesSettled(sp);
    const cards = [goalCard(sp), rolesCard(sp), automationsCard(sp),
                   connectionsCard(sp), chatCard()];
    const keys = ["goal", "roles", "autonomous", "services", "chat"];
    const doneNow = {
      goal: sp.readiness.goal === "enough", roles: sp.readiness.roles === "enough",
      autonomous: sp.readiness.autonomous === "enough", services: connOk, chat: !!S.chat,
    };
    // Reconcile per card: only re-create the cards whose markup actually changed,
    // so unchanged cards keep their DOM (and any in-flight animation) instead of
    // the whole panel snapping on every render.
    const fresh = !_prevSig || host.children.length !== cards.length;
    if (fresh) {
      host.innerHTML = cards.join("");
    } else {
      cards.forEach((html, i) => {
        if (html === _prevSig[i]) return;
        const tpl = document.createElement("template");
        tpl.innerHTML = html;
        host.replaceChild(tpl.content.firstElementChild, host.children[i]);
      });
    }
    // Animate only what changed (never on the first paint): a slot completing gets
    // the celebration; any lesser change gets a gentle settle.
    if (_prevSig) {
      keys.forEach((k, i) => {
        const el = host.children[i];
        if (!el) return;
        if (_prevDone && doneNow[k] && _prevDone[k] === false) {
          el.classList.add("celebrate");
          setTimeout(() => el.classList.remove("celebrate"), 1100);
        } else if (_prevSig[i] !== cards[i]) {
          el.classList.add("bump");
          setTimeout(() => el.classList.remove("bump"), 380);
        }
      });
    }
    _prevSig = cards;
    _prevDone = doneNow;

    const slotsEnough = ["goal", "roles", "autonomous"]
      .filter(s => sp.readiness[s] === "enough").length + (connOk ? 1 : 0);
    const gathered = slotsEnough + (S.chat ? 1 : 0);
    const meter = $("#uni-meter");
    if (meter) {
      meter.textContent = `${gathered}/5 gathered`;
      if (_prevGathered != null && gathered > _prevGathered) {
        meter.classList.remove("bump"); void meter.offsetWidth; meter.classList.add("bump");
        setTimeout(() => meter.classList.remove("bump"), 600);
      }
    }
    _prevGathered = gathered;
    const ready = gathered === 5;
    const foot = $("#uni-foot");
    if (foot) foot.innerHTML = ready
      ? `<button class="btn primary" data-go="build">Finish →</button>`
      : `<span class="uni-note">modastack is gathering goal, roles, automations, connections, and chat</span>`;
  }
  // The current interview phase, shown so the user always knows where modastack is
  // and that it's moving methodically. S.phase is "goal" | "role:<name>" |
  // "automations" | "connections" | "wrap" (or empty early on).
  function renderPhase() {
    const el = $("#uni-phase"); if (!el) return;
    const p = S.phase || "";
    if (!p) { el.innerHTML = ""; _prevPhase = ""; return; }
    let label = p, sub = "";
    if (p.startsWith("role:")) {
      const roles = S.spec.roles || [];
      const name = p.slice(5);
      const idx = roles.findIndex(r => (r.name || "") === name);
      label = "interviewing · " + name;
      if (roles.length) sub = `role ${(idx < 0 ? roles.length : idx + 1)} of ${roles.length}`;
    } else {
      label = ({ goal: "setting the goal", automations: "automations",
                 connections: "connections", wrap: "wrapping up" }[p]) || p;
    }
    el.innerHTML = `<span class="ph-dot"></span><span class="ph-lab">${esc(label)}</span>${sub ? `<span class="ph-sub">${esc(sub)}</span>` : ""}`;
    // Ease the banner in when the interview actually moves to a new phase.
    if (_prevPhase !== undefined && _prevPhase !== p) {
      el.classList.remove("phasein"); void el.offsetWidth; el.classList.add("phasein");
    }
    _prevPhase = p;
  }
  // Connections are "settled" only when there's nothing to connect (and the
  // brain confirmed none are needed) OR every live connection card is connected.
  function servicesSettled(sp) {
    const svcs = sp.services || [];
    if (!svcs.length) return sp.readiness.services === "enough";
    const cards = (_connData && _connData.cards) || null;
    if (!cards || !cards.length) return false;
    return cards.every(c => c.status === "connected");
  }
  function slotDot(ok) {
    return `<span class="udot ${ok ? "ok" : "empty"}">${ok ? CHECK : ""}</span>`;
  }
  // A small per-role progress dot: filled+check when complete, hollow otherwise.
  function roleStatusDot(r) {
    const done = (r && r.status) === "complete";
    return `<span class="rdot ${done ? "done" : "wip"}" title="${done ? "complete" : "in progress"}">${done ? CHECK : ""}</span>`;
  }
  function goalCard(sp) {
    const filled = (sp.goal || "").trim();
    return `<div class="ucard ${filled ? "filled" : "empty"}">
      <div class="ut">Goal ${slotDot(sp.readiness.goal === "enough")}</div>
      <div class="ud">${filled ? esc(sp.goal) : `<span class="ph">what should this team do?</span>`}</div></div>`;
  }
  function rolesCard(sp) {
    const roles = sp.roles || [];
    const body = roles.length
      ? roles.map((r, i) => `<div class="urole click" data-roleopen="${i}">
          <div class="urow"><b>${esc(r.name || "role")}</b>${roleStatusDot(r)}</div>
          ${r.responsibility ? `<span>${esc(r.responsibility)}</span>` : `<span class="ph">click to fill in the details</span>`}</div>`).join("")
      : `<span class="ph">modastack will shape the roles as you talk</span>`;
    return `<div class="ucard ${roles.length ? "filled" : "empty"}">
      <div class="ut">Roles ${slotDot(sp.readiness.roles === "enough")}</div>
      <div class="ud">${body}</div>
      <div class="uadd"><button class="lnk add" data-addrole>+ add a role</button></div></div>`;
  }
  function automationsCard(sp) {
    const items = sp.autonomous || [];
    const body = items.length
      ? items.map((a, i) => `<div class="urole click" data-autoopen="${i}">
          <div class="urow"><b>${esc(a.description || "behavior")}</b></div>
          <span>${esc(a.leash || "")}${a.cadence ? " · " + esc(a.cadence) : ""}${a.role ? " · " + esc(a.role) : ""}</span></div>`).join("")
      : (sp.autonomous_confirmed
          ? `<span class="ph">nothing proactive — modastack acts only when asked</span>`
          : `<span class="ph">anything modastack should do on its own?</span>`);
    return `<div class="ucard ${items.length || sp.autonomous_confirmed ? "filled" : "empty"}">
      <div class="ut">Automations ${slotDot(sp.readiness.autonomous === "enough")}</div>
      <div class="ud">${body}</div>
      <div class="uadd"><button class="lnk add" data-addauto>+ add an automation</button></div></div>`;
  }
  // Connections: native services capture a token each; Venn-backed services
  // share ONE key, so they're grouped under a single Venn setup with per-service
  // verification status. Reads live status from the cached /api/connect payload.
  function connectionsCard(sp) {
    const cards = (_connData && _connData.cards) || null;
    const ok = servicesSettled(sp);
    const vennConfigured = !!(_connData && _connData.venn_configured);
    let body;
    if (!cards) {
      const names = (sp.services || []).map(s => (s && s.name) || String(s));
      body = names.length
        ? names.map(n => `<div class="uconn"><span>${esc(n)}</span><span class="cright">${trashBtn(n)}</span></div>`).join("")
        : `<span class="ph">what should the team connect to?</span>`;
    } else if (!cards.length) {
      body = `<span class="ph">no outside services — runs self-contained</span>`;
    } else {
      const native = cards.filter(c => c.kind !== "venn");
      const venn = cards.filter(c => c.kind === "venn");
      body = native.map(connRow).join("") + vennGroup(venn);
    }
    const upsell = vennConfigured ? "" : vennUpsell();
    return `<div class="ucard ${(sp.services || []).length ? "filled" : "empty"}">
      <div class="ut">Connections ${slotDot(ok)}</div>
      <div class="ud">${body}${upsell}</div>
      <div class="uadd"><button class="lnk add" data-addconn>+ add a connection</button></div></div>`;
  }
  // Surfaced when no Venn key is set: one account connects many services at once.
  function vennUpsell() {
    return `<div class="venn-upsell">
      <span class="vu-lab">Connect lots of services at once with <b>Venn</b> — one key, many integrations.</span>
      <span class="vu-row">
        <a class="lnk" href="https://app.venn.ai" target="_blank" rel="noopener">Create a Venn account ↗</a>
        <button class="lnk" data-vennsetup>Sync it here</button>
      </span></div>`;
  }
  function trashBtn(key) {
    return `<button class="lnk trash" data-conntrash="${esc(key)}" title="I don't need this">${TRASH}</button>`;
  }
  function statusBadge(status) {
    if (status === "connected") return `<span class="cbadge connected">${CHECK} connected</span>`;
    if (status === "unknown") return `<span class="cbadge">needs check</span>`;
    return `<span class="cbadge">pending</span>`;
  }
  function connRow(c) {
    const right = c.status === "connected"
      ? `${statusBadge("connected")} <button class="lnk" data-secretopen="${esc(c.key)}">edit</button>`
      : `<button class="btn ghost xs" data-secretopen="${esc(c.key)}">Connect</button>`;
    // Custom services (not native, not on Venn) get an authored API guide.
    const tag = c.kind === "custom"
      ? `<span class="ctag">custom · modastack writes a guide</span>` : "";
    return `<div class="uconn"><span>${esc(c.name)}${tag}</span><span class="cright">${right}${trashBtn(c.key)}</span></div>`;
  }
  function vennGroup(venn) {
    if (!venn.length) return "";
    const keyIn = venn.some(c => (c.methods[0].secrets || []).some(s => s.present));
    const rows = venn.map(c =>
      `<div class="uconn sub"><span>${esc(c.name)}</span><span class="cright">${statusBadge(c.status)}${trashBtn(c.key)}</span></div>`).join("");
    return `<div class="uvenn">
      <div class="uvhead"><span><b>Venn</b> · one key, every service</span>
        <button class="btn ghost xs" data-vennsetup>${keyIn ? "Manage" : "Set up Venn"}</button></div>
      ${rows}</div>`;
  }
  async function trashConnection(key) {
    const r = await postJSON("/api/service/remove", { service_key: key });
    if (!r.ok) { toast(r.data.error || "couldn't remove"); return; }
    S = r.data;
    _connData = await getJSON("/api/connect");
    renderUniCards();
    toast("removed");
  }
  function chatCard() {
    const chosen = S.chat || "cli";
    const opts = CHANNELS.map(ch => {
      const sel = !ch.soon && ch.key === chosen;
      const cls = ch.soon ? "uopt off" : sel ? "uopt sel" : "uopt";
      const attr = ch.soon ? "" : `data-chatset="${ch.key}"`;
      return `<button class="${cls}" ${attr}>${ch.name}${ch.soon ? " · soon" : ""}</button>`;
    }).join("");
    let extra = "";
    if (chosen === "slack") {
      const saved = (S.credentials_saved || []).includes("SLACK_BOT_TOKEN");
      extra = saved
        ? `<div class="secret-saved" style="margin-top:9px">✓ Slack bot token saved
            <button class="lnk" data-secretopen="slack">Edit</button>
            <button class="lnk" data-secretcopy="SLACK_BOT_TOKEN">Copy</button></div>`
        : `<button class="btn ghost xs" data-secretopen="slack" style="margin-top:9px">Set up the Slack app →</button>`;
    }
    return `<div class="ucard ${S.chat ? "filled" : "empty"}">
      <div class="ut">Chat ${slotDot(!!S.chat)}</div>
      <div class="ud"><div class="uopts">${opts}</div>${extra}</div></div>`;
  }
  async function refreshUniConnections() {
    if (!$("#uni-cards")) return;
    _connData = await getJSON("/api/connect");
    renderUniCards();
  }
  async function setChat(key) {
    const r = await postJSON("/api/chat", { channel: key });
    if (!r.ok) { toast(r.data.error || "couldn't set"); return; }
    S = r.data; renderUniCards();
  }

  // --- conversation ------------------------------------------------------
  function cueText() {
    const r = S.spec.readiness.goal;
    if (S.chat && ["goal", "roles", "autonomous", "services"].every(s => S.spec.readiness[s] === "enough"))
      return { cls: "enough", text: "got everything ✓ — finish whenever you're ready" };
    if (r === "enough") return { cls: "", text: "taking shape — keep going" };
    if (r === "thin") return { cls: "", text: "tell me a bit more" };
    return { cls: "", text: "say what you want this team to do" };
  }
  function updateCue() {
    const el = $("#cue"); if (!el) return;
    const c = cueText(); el.textContent = c.text; el.className = "cue " + c.cls;
  }
  function renderMessages(extra) {
    const body = $("#chbody");
    if (!body) return;
    let html = "";
    if (!S.messages.length && !extra) {
      html = `<div class="msg bob">Hi — I'm modastack. Tell me what you want this team to do, in your own words. Rough is fine; we'll sharpen it together.</div>`;
    }
    for (const m of S.messages) {
      html += `<div class="msg ${m.role === "user" ? "you" : "bob"}">${esc(m.content)}</div>`;
    }
    if (extra) html += extra;
    body.innerHTML = html;
    body.scrollTop = body.scrollHeight;
  }
  // A typewriter that reveals buffered text character-by-character regardless
  // of how chunky the network deltas are. Honors reduced-motion.
  function makeTypewriter(el) {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    let buf = "", shown = 0, done = false, timer = null;
    const caret = '<span class="caret"></span>';
    // Trim trailing whitespace from what we render: the model streams the reply,
    // then a newline + the hidden JSON spec block. With white-space:pre-wrap that
    // trailing "\n" would paint as a blank line with the caret on it — flashing
    // while the JSON streams behind the scenes. Trimming keeps the caret pinned
    // to the last real character.
    const vis = (s) => esc(s.replace(/\s+$/, ""));
    const paint = () => { el.innerHTML = vis(buf.slice(0, shown)) + (shown < buf.length || !done ? caret : ""); $("#chbody").scrollTop = 1e9; };
    const tick = () => {
      if (shown < buf.length) { shown = Math.min(buf.length, shown + 2); paint(); }
      if (done && shown >= buf.length) { clearInterval(timer); timer = null; el.innerHTML = vis(buf); }
    };
    if (!reduce) timer = setInterval(tick, 16);
    return {
      push(t) { buf += t; if (reduce) { shown = buf.length; paint(); } },
      finish() {
        done = true;
        if (reduce || !timer) { el.innerHTML = vis(buf); return Promise.resolve(); }
        return new Promise(res => { const iv = setInterval(() => { if (!timer) { clearInterval(iv); res(); } }, 20); });
      },
    };
  }

  let streaming = false, pendingSend = null;
  async function sendMessage(text) {
    const input = $("#chinput");
    const msg = (typeof text === "string" ? text : (input ? input.value : "")).trim();
    if (!msg) return;
    // You can keep typing (and queue another message) while modastack is replying.
    if (streaming) { pendingSend = msg; if (input && typeof text !== "string") { input.value = ""; autoGrow(input); } return; }
    if (input && typeof text !== "string") { input.value = ""; autoGrow(input); }
    streaming = true;
    S.messages.push({ role: "user", content: msg });
    renderMessages('<div class="msg bob" id="streambob"><span class="caret"></span></div>');
    const tw = makeTypewriter($("#streambob"));
    await sse("/api/message", { text: msg }, {
      redacted: () => toast("Scrubbed a secret from that message — add credentials in the connection setup, not the chat."),
      delta: (d) => tw.push(d.text),
      error: (d) => toast(d.message || "something broke"),
      state: (st) => { S = st; },
    });
    await tw.finish();
    streaming = false;
    // Finalize the streaming bubble IN PLACE from the authoritative reply, rather
    // than tearing the whole list down with renderMessages(). That rebuild was
    // what flashed: the typed text briefly kept a trailing blank line, then the
    // list re-rendered to the stripped version — the "line that disappears".
    const sb = $("#streambob");
    const last = S.messages[S.messages.length - 1];
    const finalText = last && last.role !== "user" ? last.content : "";
    if (sb) {
      if (finalText) { sb.textContent = finalText; sb.removeAttribute("id"); }
      else sb.remove();
    } else {
      renderMessages();
    }
    updateCue();
    renderUniCards();
    if ((S.spec.services || []).length) refreshUniConnections();
    if (pendingSend) { const m = pendingSend; pendingSend = null; sendMessage(m); }
  }

  // --- connector card renderer (used by the secret overlays) -------------
  let connSel = {};   // cardKey -> selected method key (persists across redraws)
  let editSecrets = new Set();   // secret vars temporarily re-opened for editing
  // Copy a saved credential to the clipboard without ever showing it on screen.
  async function copySecret(varName) {
    const r = await fetch("/api/credential/value?var=" + encodeURIComponent(varName), { headers: H });
    if (!r.ok) { toast("nothing to copy"); return; }
    const { value } = await r.json();
    try { await navigator.clipboard.writeText(value); toast(`${varName} copied`); }
    catch { toast("copy blocked by the browser"); }
  }
  function rerenderConnCard(cardKey) {
    const card = ((_connData && _connData.cards) || []).concat((_connData && _connData.catalog) || [])
      .find(c => c.key === cardKey);
    const el = document.querySelector(`.pcard[data-cardkey="${cardKey}"]`);
    if (card && el) el.outerHTML = connCard(card);
  }
  function connCard(c) {
    const pill = `<span class="status-pill ${c.status}">· ${esc(STATUS_LABEL[c.status] || c.status)}</span>`;
    const scopes = (c.scopes || []).map(s => `<b>${esc(s)}</b>`).join("");
    const methods = c.methods || [];
    let inner = "";
    if (methods.length) {
      // Default to the satisfied method (so a connected card surfaces its saved
      // secrets with Edit/Copy), else the first.
      const def = (methods.find(x => x.satisfied) || methods[0]).key;
      const sel = connSel[c.key] || def;
      const m = methods.find(x => x.key === sel) || methods[0];
      const note = c.status === "connected" ? `<div class="conn-ok">✓ Connected — ready to use.</div>` : "";
      const tabs = methods.length > 1
        ? `<div class="mtabs">${methods.map(x =>
            `<button class="mtab ${x.key === m.key ? "on" : ""}" data-connmethod="${esc(c.key)}:${esc(x.key)}">${esc(x.label)}</button>`).join("")}</div>`
        : "";
      inner = note + tabs + connMethod(c, m);
    }
    return `<div class="pcard conn" data-cardkey="${esc(c.key)}">
      <div class="pt">${esc(c.name)} ${pill}</div>
      <div class="pd">${esc(c.summary)}</div>
      <div class="scopes">${scopes}</div>${inner}</div>`;
  }
  function connMethod(c, m) {
    const summary = m.summary ? `<div class="msum">${esc(m.summary)}</div>` : "";
    const steps = (m.steps || []).length
      ? `<ol class="steps">${m.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>` : "";
    const secrets = (m.secrets || []).map(s => (s.present && !editSecrets.has(s.var))
      ? `<div class="secret-saved">✓ ${esc(s.label)} saved
          <button class="lnk" data-secretedit="${esc(s.var)}">Edit</button>
          <button class="lnk" data-secretcopy="${esc(s.var)}">Copy</button></div>`
      : `<label class="secret">
          <span class="slabel">${esc(s.label)}${s.optional ? " · optional" : ""}${s.present ? " · re-enter to replace" : ""}</span>
          <input type="password" data-secret="${esc(s.var)}" placeholder="${esc(s.placeholder || s.var)}" autocomplete="off">
          ${s.help ? `<span class="shelp">${esc(s.help)}</span>` : ""}
        </label>`).join("");
    const doc = m.docs_url
      ? `<a class="doclink" href="${esc(m.docs_url)}" target="_blank" rel="noopener">Open guide ↗</a>` : "";
    const hasInputs = (m.secrets || []).some(s => !s.present || editSecrets.has(s.var));
    let btn = "";
    if (m.action === "venn") btn = `<button class="btn primary sm" data-connect="${esc(c.key)}:${esc(m.key)}">${hasInputs ? "Save & verify" : "Re-check"}</button>`;
    else if (hasInputs) btn = `<button class="btn primary sm" data-connect="${esc(c.key)}:${esc(m.key)}">${(m.secrets || []).some(s => s.present) ? "Save" : "Connect"}</button>`;
    const row = (doc || btn) ? `<div class="connect-row">${doc}${btn}</div>` : "";
    return `<div class="method">${summary}${steps}<div class="secrets">${secrets}</div>${row}</div>`;
  }
  function selectMethod(cardKey, methodKey) {
    connSel[cardKey] = methodKey;
    const card = ((_connData && _connData.cards) || []).concat((_connData && _connData.catalog) || [])
      .find(c => c.key === cardKey);
    const el = document.querySelector(`.pcard[data-cardkey="${cardKey}"]`);
    if (card && el) el.outerHTML = connCard(card);
  }
  async function connectMethod(ref) {
    const [cardKey] = ref.split(":");
    const card = document.querySelector(`.pcard[data-cardkey="${cardKey}"]`);
    if (!card) return;
    const inputs = [...card.querySelectorAll("input[data-secret]")].filter(i => i.value.trim());
    for (const input of inputs) {
      const v = input.dataset.secret;
      const r = await postJSON("/api/credential", {
        var_name: v, service: cardKey, value: input.value.trim(),
      });
      if (!r.ok) { toast(r.data.error || `couldn't save ${v}`); return; }
      editSecrets.delete(v);
      if (!(S.credentials_saved || []).includes(v)) (S.credentials_saved ||= []).push(v);
    }
    if (inputs.length) toast(`${cardKey} — saved`); else toast("checking…");
    _connData = await getJSON("/api/connect");
    // Update the open overlay, then the panel.
    const ov = $("#secret-ov");
    if (ov) {
      const c = (_connData.cards || []).concat(_connData.catalog || []).find(x => x.key === cardKey);
      if (c && c.status === "connected") { toast(`${c.name} connected`); ov.remove(); }
      else if (c) { const b = $("#sp-body"); if (b) b.innerHTML = connCard(c); }
    }
    renderUniCards();
  }

  // --- on-demand secret capture: native connectors (token-based) ---------
  async function openSecretCapture(key) {
    _connData = await getJSON("/api/connect");
    const card = (_connData.cards || []).concat(_connData.catalog || [])
      .find(c => c.key === key || (c.name || "").toLowerCase() === key.toLowerCase());
    if (!card) { toast("unknown service"); return; }
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "secret-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>Connect ${esc(card.name)}</b><button class="btn ghost sm" id="sp-close">Close</button></div>
      <div class="sp-body" id="sp-body">${connCard(card)}</div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => {
      if (e.target.id === "sp-close" || e.target === ov) ov.remove();
    });
  }

  // --- on-demand secret capture: unified Venn setup ----------------------
  // Every Venn-backed service shares one key, so setup is ONE place: paste the
  // key once, and each service shows its verification status.
  async function openVennSetup() {
    _connData = await getJSON("/api/connect");
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "venn-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>Connect via Venn</b><button class="btn ghost sm" id="venn-close">Close</button></div>
      <div class="sp-body" id="venn-body"></div></div>`;
    document.body.appendChild(ov);
    drawVenn();
    ov.addEventListener("click", e => {
      if (e.target.id === "venn-close" || e.target === ov) ov.remove();
    });
  }
  function vennCards() {
    return ((_connData && _connData.cards) || []).filter(c => c.kind === "venn");
  }
  function drawVenn() {
    const body = $("#venn-body"); if (!body) return;
    const venn = vennCards();
    const keyIn = venn.some(c => (c.methods[0].secrets || []).some(s => s.present));
    const allConnected = venn.length > 0 && venn.every(c => c.status === "connected");
    const svcs = venn.map(c =>
      `<div class="uconn sub"><span>${esc(c.name)}</span>${statusBadge(c.status)}</div>`).join("");
    // Already fully connected: no "Re-check" busywork. Make it obviously done and
    // let the user simply close.
    if (allConnected) {
      body.innerHTML = `
        <div class="venn-done">
          <div class="vd-seal">${CHECK}</div>
          <div class="vd-copy"><b>All Venn services connected</b>
            <span>You're set — these are live and ready to use.</span></div>
        </div>
        <div class="venn-svcs">${svcs}</div>
        <div class="connect-row" style="justify-content:flex-end">
          <button class="btn primary sm" data-vennclose>Done</button>
        </div>`;
      return;
    }
    const editing = editSecrets.has("VENN_API_KEY");
    const keyField = (keyIn && !editing)
      ? `<div class="secret-saved">✓ Venn key saved
          <button class="lnk" data-secretedit="VENN_API_KEY">Edit</button>
          <button class="lnk" data-secretcopy="VENN_API_KEY">Copy</button></div>`
      : `<label class="secret"><span class="slabel">Venn API key${keyIn ? " · re-enter to replace" : ""}</span>
          <input type="password" id="venn-key" placeholder="venn_…" autocomplete="off">
          <span class="shelp">One key unlocks every Venn service below.</span></label>`;
    body.innerHTML = `
      <p class="pd" style="margin-bottom:10px">One Venn key covers all of these. Connect each service in Venn, then paste the key once — modastack verifies which are live.</p>
      <ol class="steps">
        <li>Sign in at app.venn.ai and create an API key (Settings → API).</li>
        <li>In Venn, connect each service below (one-click OAuth).</li>
        <li>Paste the key here — it covers them all.</li>
      </ol>
      <div class="venn-svcs">${svcs}</div>
      ${keyField}
      <div class="connect-row">
        <a class="doclink" href="https://app.venn.ai" target="_blank" rel="noopener">Open Venn ↗</a>
        <button class="btn primary sm" data-vennsave>${keyIn ? "Re-check" : "Save & verify"}</button>
      </div>`;
  }
  async function vennSave() {
    const input = $("#venn-key");
    if (input && input.value.trim()) {
      const r = await postJSON("/api/credential", {
        var_name: "VENN_API_KEY", service: "venn", value: input.value.trim(),
      });
      if (!r.ok) { toast(r.data.error || "couldn't save"); return; }
      editSecrets.delete("VENN_API_KEY");
      if (!(S.credentials_saved || []).includes("VENN_API_KEY")) (S.credentials_saved ||= []).push("VENN_API_KEY");
      toast("Venn key saved");
    } else {
      toast("checking…");
    }
    _connData = await getJSON("/api/connect");
    drawVenn();
    renderUniCards();
  }

  // --- generating (Build + validate + install, collapsed) ----------------
  // buildGen tags each build run; goBack() bumps it so a cancelled (or hung)
  // build can never resume and jump the user forward to the preview.
  let building = false, buildGen = 0;
  function renderGenerating() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="node narrow">
      <div class="eyebrow">Building</div>
      <h1>Building ${esc(S.team_name || "your team")}</h1>
      <p class="lede" id="genmsg">Writing the pack, checking it, and installing — sit back.</p>
      <div class="genbar"><div class="genbar-fill" id="genfill"></div></div>
      <ul class="genfiles" id="genfiles"></ul>
      <div id="generr"></div>
      <div class="actions"><button class="backbtn" data-back>← Back to editing</button></div>
    </main>`;
    if (!building) runBuildFlow();
  }
  function genAddFile(path, done) {
    let li = document.getElementById("gf-" + path);
    if (!li) {
      li = document.createElement("li");
      li.id = "gf-" + path;
      $("#genfiles").appendChild(li);
    }
    li.innerHTML = `<span class="gf-mark">${done ? "✓" : '<span class="spin"></span>'}</span>${esc(path)}`;
    li.className = done ? "done" : "";
  }
  async function runBuildFlow() {
    building = true;
    const myGen = ++buildGen;
    const live = () => myGen === buildGen;   // false once cancelled via Back
    const set = (id, fn) => { const el = $("#" + id); if (el) fn(el); };
    const fill = () => set("genfill", el => { const p = Math.min(95, +(el.dataset.p || 0) + 12); el.style.width = p + "%"; el.dataset.p = p; });
    try {
      await sse("/api/build", {}, {
        file_start: (e) => { if (!live()) return; genAddFile(e.path, false); set("genmsg", el => el.innerHTML = `writing <b>${esc(e.path)}</b>…`); },
        file_end: (e) => { if (!live()) return; genAddFile(e.path, true); fill(); },
        delta: () => {},
        error: (e) => { throw new Error(e.message || "build failed"); },
        state: (st) => { S = st; },
      });
      if (!live()) return;   // user backed out mid-build
      set("genmsg", el => el.textContent = "Checking it over…");
      const v = await postJSON("/api/validate", {});
      if (!live()) return;
      S = v.data.state || S;
      if (!v.data.passed) return buildFailed("Validation found problems.", v.data.report);
      set("genmsg", el => el.textContent = "Installing…");
      const r = await postJSON("/api/install", {});
      if (!live()) return;
      S = r.data.state || S;
      if (!r.ok) return buildFailed(r.data.error || "Install failed.");
      set("genfill", el => el.style.width = "100%");
      building = false;
      await go("done");
    } catch (err) {
      if (live()) buildFailed(String(err.message || err));
    }
  }
  function buildFailed(msg, report) {
    building = false;
    const el = $("#generr");
    if (!el) return;
    el.innerHTML = `<div class="banner err">${esc(msg)}${report ? `<pre>${esc(report)}</pre>` : ""}</div>
      <div class="actions"><button class="btn ghost" data-go="design">Back to editing</button><button class="btn primary" id="retrybuild">Try again</button></div>`;
    $("#retrybuild").addEventListener("click", () => { $("#generr").innerHTML = ""; runBuildFlow(); });
  }

  // --- done --------------------------------------------------------------
  function talkHint() {
    if (S.chat === "slack") return `<p class="lede">Talk to it in Slack — message the bot in your channel.</p>`;
    if (S.chat === "telegram") return `<p class="lede">Talk to it in Telegram.</p>`;
    return `<p class="lede">Talk to it from the terminal: <code>modastack ask "what's the status?"</code></p>`;
  }
  // Group flat relative paths (agent.yaml, roles/director/ROLE.md, …) into a
  // nested folder tree so the preview reads like the on-disk structure. Each
  // folder carries its full path so it can be collapsed independently.
  function buildFileTree(paths) {
    const root = { dirs: {}, files: [] };
    for (const p of paths) {
      const parts = p.split("/");
      let node = root, prefix = "";
      for (let i = 0; i < parts.length - 1; i++) {
        prefix = prefix ? prefix + "/" + parts[i] : parts[i];
        node.dirs[parts[i]] = node.dirs[parts[i]] || { dirs: {}, files: [], path: prefix };
        node = node.dirs[parts[i]];
      }
      node.files.push({ name: parts[parts.length - 1], path: p });
    }
    return root;
  }
  function renderTree(node, cur, depth, collapsed) {
    let html = "";
    for (const d of Object.keys(node.dirs).sort()) {
      const dir = node.dirs[d];
      const shut = collapsed.has(dir.path);
      html += `<div class="tnode dir" data-dir="${esc(dir.path)}" style="--d:${depth}"><span class="tdi">${shut ? "▸" : "▾"}</span>${esc(d)}</div>`;
      if (!shut) html += renderTree(dir, cur, depth + 1, collapsed);
    }
    for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name)))
      html += `<div class="tnode file ${f.path === cur ? "sel" : ""}" data-ifile="${esc(f.path)}" style="--d:${depth}"><span class="ok">✓</span>${esc(f.name)}</div>`;
    return html;
  }
  // The post-build screen is a PREVIEW (not a finish line): the generated team's
  // files (collapsible folder tree + read-only contents) read live from disk.
  // The real "done" screen comes after Finish (renderFinished).
  async function renderDone() {
    setPanes("1fr");
    const spec = S.spec;
    const where = S.source_dir || "agents/" + S.team_name;
    const counts = `${spec.roles.length || 1} role(s) · ${spec.autonomous.length} automation(s) · ${spec.services.length} service(s)`;
    $("#main").innerHTML = `<main class="filesdone">
      <header class="fd-head">
        <div class="fd-title">
          <div class="eyebrow">Preview · here's what modastack built</div>
          <h1>${esc(S.team_name || "your team")}</h1>
          <p class="fd-meta">${counts} · source at <code>${esc(where)}</code></p>
        </div>
        <div class="fd-actions">
          <button class="btn ghost" data-back>← Keep editing</button>
          <button class="btn ghost" id="fd-reveal">Open folder</button>
          <button class="btn primary" id="fd-finish">Looks good — finish</button>
        </div>
      </header>
      <div class="fd-body" id="fd-body"></div>
    </main>`;

    $("#fd-reveal").addEventListener("click", async () => {
      const r = await postJSON("/api/reveal", {});
      toast(r.ok ? "Opened the team folder." : (r.data.error || "couldn't open the folder"));
    });
    $("#fd-finish").addEventListener("click", async () => {
      const b = $("#fd-finish"); b.disabled = true; b.textContent = "Finishing…";
      try { await postJSON("/api/finish", {}); } catch { /* ignore */ }
      renderFinished();
    });

    const data = await getJSON("/api/files");
    const files = data.files || [];
    const body = $("#fd-body");
    if (!files.length) {
      body.innerHTML = `<div class="fd-empty">
        <p>No files found at <code>${esc(where)}</code>.</p>
        <p class="fd-sub">The build may not have finished writing — go back and try again.</p>
        <button class="btn ghost" data-back>Back to editing</button></div>`;
      return;
    }
    body.innerHTML = `<nav class="tree" id="fd-tree-col">
        <div class="th">Your files</div><div id="fd-tree"></div></nav>
      <section class="slab">
        <div class="slabbar"><span class="th slab-th">Preview</span><span class="s" id="fd-name"></span></div>
        <div class="code" id="fd-code"></div>
      </section>`;

    let cur = files[0];
    const tree = buildFileTree(files);
    const collapsed = new Set();   // folder paths the user has folded shut
    const drawTree = () => { $("#fd-tree").innerHTML = renderTree(tree, cur, 0, collapsed); };
    const openF = async (p) => { cur = p; drawTree(); $("#fd-name").innerHTML = `<b>${esc(p)}</b>`;
      const d = await getJSON("/api/file?path=" + encodeURIComponent(p)); $("#fd-code").textContent = d.content || ""; };
    drawTree(); await openF(cur);

    body.addEventListener("click", (e) => {
      const dir = e.target.closest("[data-dir]");
      if (dir) { const p = dir.dataset.dir; collapsed.has(p) ? collapsed.delete(p) : collapsed.add(p); drawTree(); return; }
      const f = e.target.closest("[data-ifile]"); if (f) openF(f.dataset.ifile);
    });
  }

  // Final screen after Finish. The server now stays alive (the homepage is a
  // re-entrant hub), so its buttons can talk to it: copy/run the start command,
  // link to cloud docs, and head to the homepage.
  function renderFinished() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="done-wrap">
      <div class="seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L19 7"/></svg></div>
      <div class="eyebrow">All set</div>
      <h1>${esc(S.team_name || "your team")} is ready</h1>
      <p class="lede">Installed into <code>.modastack/</code>. We are legion.</p>

      <p class="done-h">Start it whenever you're ready</p>
      <p class="lede">Open a fresh terminal and run this — it turns your agent team on.</p>
      <div class="cmd"><span class="pr">$</span> <span class="cmd-text">modastack start</span>
        <button class="cmd-copy" id="copy-start" title="Copy">Copy</button></div>
      <div class="actions" style="margin-top:8px"><button class="btn ghost" id="run-start">Start it for me →</button></div>

      <p class="done-h">Next steps</p>
      <p class="lede">Want to run modastack in the cloud? <a class="exlink" href="${DOCS_CLOUD_URL}" target="_blank" rel="noopener">Follow these instructions →</a></p>

      <div class="actions" style="margin-top:26px"><button class="btn primary" id="done-home">Done →</button></div>
    </main>`;
    $("#copy-start").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText("modastack start"); toast("Copied."); }
      catch { toast("Copy failed — select the command manually."); }
    });
    $("#run-start").addEventListener("click", async () => {
      const b = $("#run-start"); b.disabled = true; b.textContent = "Starting…";
      const r = await postJSON("/api/run-start", {});
      if (r.ok) { toast("Starting your agent team…"); b.textContent = "Started ✓"; }
      else { toast(r.data.error || "couldn't start it"); b.disabled = false; b.textContent = "Start it for me →"; }
    });
    $("#done-home").addEventListener("click", () => { atHome = true; renderHome(); });
  }

  // --- homepage (the re-entrant team hub) --------------------------------
  // A grid of team cards (boxy, to read distinctly from the template *rows* on
  // the intro). Click a team to open it in the editor (the chat + cards screen,
  // reverse-filled from source); click the "add" card to start a fresh setup.
  async function renderHome() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="node home">
      <div class="eyebrow">modastack</div>
      <h1>Your agent teams</h1>
      <p class="lede">Pick a team to view or update it, or add a new one. modastack keeps each team's source and re-installs your changes when you finish editing.</p>
      <div class="home-grid" id="home-list"><p class="ihint">Loading…</p></div>
    </main>`;
    const data = await getJSON("/api/home");
    const teams = data.teams || [];
    const teamGlyph = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="8.5" y="13.5" width="7" height="7" rx="1.2"/></svg>';
    const cards = teams.map(t =>
      `<button class="hcard" data-openteam="${esc(t.path)}" title="${esc(t.path)}">
        <span class="hcard-glyph">${teamGlyph}</span>
        <b>${esc(t.name)}</b>
        <span class="hcard-desc">${esc(t.description || "Agent team")}</span>
        <span class="hcard-foot">Open →</span>
      </button>`).join("");
    const add = `<button class="hcard hcard-add" data-addteam>
      <span class="hcard-glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg></span>
      <b>Add an agent team</b>
      <span class="hcard-foot">New setup →</span>
    </button>`;
    $("#home-list").innerHTML = cards + add;
  }

  // --- panel modals: inspect/edit a role or automation, add new items ----
  // All reuse the .secret-ov shell so Escape-to-close already works.
  function openRoleModal(i) {
    const r = (S.spec.roles || [])[i]; if (!r) return;
    const sysVal = Array.isArray(r.systems) ? r.systems.join(", ") : (r.systems || "");
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "role-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>${esc(r.name || "Role")}</b><button class="btn ghost sm" id="role-close">Close</button></div>
      <div class="sp-body">
        <label class="fld"><span class="flab">Role name</span>
          <input id="r-name" value="${esc(r.name || "")}" autocomplete="off"></label>
        <label class="fld"><span class="flab">What it does</span>
          <textarea id="r-resp" rows="2">${esc(r.responsibility || "")}</textarea></label>
        <label class="fld"><span class="flab">What a good job looks like</span>
          <textarea id="r-good" rows="2">${esc(r.good_looks_like || "")}</textarea></label>
        <label class="fld"><span class="flab">Systems it accesses</span>
          <input id="r-sys" value="${esc(sysVal)}" placeholder="comma-separated — e.g. github, slack" autocomplete="off">
          <span class="fhelp">Comma-separated.</span></label>
        <label class="fld"><span class="flab">What triggers it to run</span>
          <textarea id="r-trig" rows="2">${esc(r.triggers || "")}</textarea></label>
        <div class="sp-actions"><button class="btn primary sm" id="r-save">Save role</button></div>
      </div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => { if (e.target.id === "role-close" || e.target === ov) ov.remove(); });
    $("#r-save").addEventListener("click", async () => {
      const fields = {
        name: $("#r-name").value, responsibility: $("#r-resp").value,
        good_looks_like: $("#r-good").value, systems: $("#r-sys").value,
        triggers: $("#r-trig").value,
      };
      const res = await postJSON("/api/role/update", { index: i, fields });
      if (!res.ok) { toast(res.data.error || "couldn't save"); return; }
      S = res.data; ov.remove(); renderUniCards(); toast("role saved");
    });
  }
  function openAutoModal(i) {
    const a = (S.spec.autonomous || [])[i]; if (!a) return;
    const roles = S.spec.roles || [];
    const roleOpts = ['<option value="">(any role)</option>'].concat(
      roles.map(r => `<option value="${esc(r.name)}" ${a.role === r.name ? "selected" : ""}>${esc(r.name)}</option>`)).join("");
    const leashOpts = [["notify", "notify · just tells you"], ["ask", "ask first · waits for approval"], ["act", "act · does it, reports"]]
      .map(([v, l]) => `<option value="${v}" ${a.leash === v ? "selected" : ""}>${esc(l)}</option>`).join("");
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "auto-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>Automation</b><button class="btn ghost sm" id="auto-close">Close</button></div>
      <div class="sp-body yamlish">
        <label class="fld"><span class="flab">description</span>
          <textarea id="a-desc" rows="2">${esc(a.description || "")}</textarea></label>
        <label class="fld"><span class="flab">role <span class="fhelp inline">which agent runs it</span></span>
          <select id="a-role">${roleOpts}</select></label>
        <label class="fld"><span class="flab">when <span class="fhelp inline">a schedule (1d, 15m) or an event</span></span>
          <input id="a-when" value="${esc(a.cadence || "")}" placeholder="e.g. 1d, 9am daily, when a PR opens" autocomplete="off"></label>
        <label class="fld"><span class="flab">leash</span>
          <select id="a-leash">${leashOpts}</select></label>
        <label class="fld"><span class="flab">command <span class="fhelp inline">what the agent is told to do</span></span>
          <textarea id="a-cmd" rows="2">${esc(a.command || "")}</textarea></label>
        <div class="sp-actions"><button class="btn primary sm" id="a-save">Save automation</button></div>
      </div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => { if (e.target.id === "auto-close" || e.target === ov) ov.remove(); });
    $("#a-save").addEventListener("click", async () => {
      const fields = {
        description: $("#a-desc").value, role: $("#a-role").value,
        cadence: $("#a-when").value, leash: $("#a-leash").value,
        command: $("#a-cmd").value,
      };
      const res = await postJSON("/api/automation/update", { index: i, fields });
      if (!res.ok) { toast(res.data.error || "couldn't save"); return; }
      S = res.data; ov.remove(); renderUniCards(); toast("automation saved");
    });
  }
  // Add a role / automation / connection by describing it — the description is
  // routed into the conversation so the brain ingests it. Connections also offer
  // the custom "build an integration on the fly" placeholder.
  function openDescribeModal(kind) {
    const meta = {
      role: { title: "Add a role", ph: "Describe the role — what it does, what a good job looks like, what it needs to access.", lead: "Tell modastack about the role and it'll add it to the team." },
      auto: { title: "Add an automation", ph: "Describe something the team should do on its own — e.g. 'post a daily digest at 9am'.", lead: "Describe the proactive behavior; modastack wires it up." },
      conn: { title: "Add a connection", ph: "What should the team connect to? e.g. 'our Notion workspace'.", lead: "Name a service and modastack will work out how to connect it." },
    }[kind];
    const custom = kind === "conn" ? `
      <div class="custom-build">
        <div class="cb-head">Not on Venn? Build a custom integration</div>
        <p class="fhelp">Paste an official MCP server link or an API key and modastack will try to build an MCP/CLI for it. This runs as a background job you can come back to — it's a rabbit hole of its own, so it's coming soon.</p>
        <label class="fld"><span class="flab">Service name</span><input id="cb-name" placeholder="e.g. PostHog" autocomplete="off"></label>
        <label class="fld"><span class="flab">MCP server link <span class="fhelp inline">optional</span></span><input id="cb-mcp" placeholder="https://…" autocomplete="off"></label>
        <label class="fld"><span class="flab">API key <span class="fhelp inline">optional</span></span><input id="cb-key" type="password" placeholder="stored in .env, never sent to the model" autocomplete="off"></label>
        <div class="sp-actions"><button class="btn ghost sm" id="cb-build">Build integration</button></div>
        <div class="cb-status" id="cb-status"></div>
      </div>` : "";
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "describe-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>${meta.title}</b><button class="btn ghost sm" id="d-close">Close</button></div>
      <div class="sp-body">
        <p class="fhelp">${meta.lead}</p>
        <label class="fld"><textarea id="d-text" rows="3" placeholder="${esc(meta.ph)}"></textarea></label>
        <div class="sp-actions"><button class="btn primary sm" id="d-send">Add</button></div>
        ${custom}
      </div></div>`;
    document.body.appendChild(ov);
    $("#d-text").focus();
    ov.addEventListener("click", e => { if (e.target.id === "d-close" || e.target === ov) ov.remove(); });
    $("#d-send").addEventListener("click", () => {
      const t = $("#d-text").value.trim();
      if (!t) { toast("say a little about it"); return; }
      ov.remove(); sendMessage(t);
    });
    if (kind === "conn") {
      $("#cb-build").addEventListener("click", async () => {
        const name = $("#cb-name").value.trim();
        if (!name) { toast("name the service first"); return; }
        const res = await postJSON("/api/build-integration", {
          service_name: name, mcp_url: $("#cb-mcp").value.trim(),
          api_key: $("#cb-key").value.trim(),
        });
        if (!res.ok) { toast(res.data.error || "couldn't queue"); return; }
        if (res.data.state) S = res.data.state;
        const st = $("#cb-status");
        if (st) st.innerHTML = `<div class="cb-queued">⏳ ${esc(res.data.message || "queued — coming soon")}</div>`;
        renderUniCards();
        if ((S.spec.services || []).length) refreshUniConnections();
      });
    }
  }

  // --- top-level render + events ----------------------------------------
  function render() {
    if (atHome) { renderHome(); return; }   // the team hub overlays any stage
    const st = S.stage;
    if (st === "start") { welcomed ? renderIntro() : renderWelcome(); return; }
    if (GENERATING.has(st)) { renderGenerating(); return; }
    if (st === "done") { renderDone(); return; }
    renderUnified();            // design + editing (the one screen)
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-back]")) { goBack(); return; }
    const go_ = e.target.closest("[data-go]");
    if (go_) { go(go_.dataset.go); return; }
    const im = e.target.closest("[data-intromode]");
    if (im) { if (!im.disabled) { introMode = im.dataset.intromode; drawIntro(); } return; }
    const ro = e.target.closest("[data-roleopen]");
    if (ro) { openRoleModal(+ro.dataset.roleopen); return; }
    const ao = e.target.closest("[data-autoopen]");
    if (ao) { openAutoModal(+ao.dataset.autoopen); return; }
    if (e.target.closest("[data-addrole]")) { openDescribeModal("role"); return; }
    if (e.target.closest("[data-addauto]")) { openDescribeModal("auto"); return; }
    if (e.target.closest("[data-addconn]")) { openDescribeModal("conn"); return; }
    const ct = e.target.closest("[data-conntrash]");
    if (ct) { trashConnection(ct.dataset.conntrash); return; }
    const cs = e.target.closest("[data-chatset]");
    if (cs) { setChat(cs.dataset.chatset); return; }
    const cm = e.target.closest("[data-connmethod]");
    if (cm) { const [k, mk] = cm.dataset.connmethod.split(":"); selectMethod(k, mk); return; }
    const cn = e.target.closest("[data-connect]");
    if (cn) { connectMethod(cn.dataset.connect); return; }
    const so = e.target.closest("[data-secretopen]");
    if (so) { openSecretCapture(so.dataset.secretopen); return; }
    const se = e.target.closest("[data-secretedit]");
    if (se) {
      editSecrets.add(se.dataset.secretedit);
      if ($("#venn-ov")) drawVenn();
      else { const pc = se.closest(".pcard[data-cardkey]"); if (pc) rerenderConnCard(pc.dataset.cardkey); }
      return;
    }
    const sco = e.target.closest("[data-secretcopy]");
    if (sco) { copySecret(sco.dataset.secretcopy); return; }
    const vs = e.target.closest("[data-vennsetup]");
    if (vs) { openVennSetup(); return; }
    const vsv = e.target.closest("[data-vennsave]");
    if (vsv) { vennSave(); return; }
    if (e.target.closest("[data-vennclose]")) { const o = $("#venn-ov"); if (o) o.remove(); return; }
  });

  // Escape closes the topmost dismissible popup — the folder picker or a
  // connection-setup overlay (Venn / native token). The disconnect overlay is
  // a blocking state, not a popup, so it's deliberately left alone.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const popups = document.querySelectorAll(".picker, .secret-ov");
    if (popups.length) { popups[popups.length - 1].remove(); e.preventDefault(); }
  });

  // Heartbeat: detect a dead server even while the page sits idle (no fetch
  // would otherwise fire). ~4s cadence — quick enough to notice, light enough
  // to ignore. Recovers automatically if the server returns.
  async function heartbeat() {
    if (_finished) return;
    try {
      const r = await fetch("/api/ping", { headers: H });
      if (r.ok) markConnected(); else markDisconnected();
    } catch { markDisconnected(); }
  }
  setInterval(heartbeat, 4000);

  boot();
})();
