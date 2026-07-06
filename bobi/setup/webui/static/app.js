/* bobi setup — the front-end. Vanilla, no build, offline.
   ONE screen: an objective-guided conversation (left) while the team
   materializes as cards (right). The LLM serves the conversation (SSE) and the
   Build pour (SSE). Secrets are captured in dedicated on-demand components,
   never in the chat. Build/Done reuse the generating + done views. */
(() => {
  const NONCE = document.querySelector('meta[name="bobi-nonce"]').content;
  // Mount prefix when hosted inside the unified web app ("" standalone).
  // Every /api and /static request goes through the helpers below, which
  // prefix it - keep it that way.
  const BASE = (document.querySelector('meta[name="bobi-base"]') || {}).content || "";
  // Hosted inside the unified app: the dashboard is home. The SPA never
  // shows its own hub, and every home-exit leaves to the shell instead.
  const HOSTED = !!BASE;
  const H = { "x-bobi-webui-token": NONCE };
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
  // The cloud-deploy runbook shipped in-repo (the image, Fly provisioner, and
  // GitOps). Points at the GitHub blob so the finalization screen's link
  // works without a docs site.
  const DOCS_CLOUD_URL = "https://github.com/moda-labs/bobi-agent/blob/main/docs/CONTAINERIZED_DEPLOYMENT.md";
  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4"><path d="M5 12l5 5L19 7"/></svg>';
  const TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m2 0v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7"/></svg>';
  const HELP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M9.3 9.2a2.8 2.8 0 0 1 5.4 1c0 1.9-2.7 2.5-2.7 2.5"/><circle cx="12" cy="16.7" r="0.6" fill="currentColor" stroke="none"/></svg>';
  const STATUS_LABEL = { connected: "connected", missing: "connect", unknown: "needs check" };
  // Day-to-day channels for the Chat card.
  const CHANNELS = [
    { key: "cli", name: "Command line", soon: false },
    { key: "slack", name: "Slack", soon: false },
    { key: "telegram", name: "Telegram", soon: true },
  ];
  const INGRESS_LABELS = {
    local: "Local only",
    quick_tunnel: "Quick tunnel",
    bobi_cloud: "Bobi cloud",
    custom_worker: "Custom Worker",
  };

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
  // server comes back (e.g. `bobi setup <name> --resume`).
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
      <p>The local <code>bobi setup</code> server stopped — closed, interrupted, or crashed. Nothing here works until it's back.</p>
      <div class="disc-cmd"><span class="pr">$</span> bobi setup &lt;name&gt; --resume</div>
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
    try { r = await fetch(BASE + path, { headers: H }); }
    catch (e) { markDisconnected(); throw e; }   // network failure = server gone
    markConnected();
    return r.json();
  }
  async function postJSON(path, body) {
    let r;
    try {
      r = await fetch(BASE + path, {
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
      res = await fetch(BASE + path, {
        method: "POST", headers: { ...H, "content-type": "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (e) { markDisconnected(); throw e; }
    markConnected();
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    const dispatch = (raw) => {
      const ev = parseSSE(raw);
      if (handlers[ev.event]) handlers[ev.event](ev.data);
    };
    const pump = () => {
      let i;
      while ((i = buf.indexOf("\n\n")) !== -1) {
        dispatch(buf.slice(0, i));
        buf = buf.slice(i + 2);
      }
    };
    for (;;) {
      let chunk;
      try { chunk = await reader.read(); }
      catch (e) { markDisconnected(); throw e; }  // stream cut = server gone
      const { value, done } = chunk;
      if (done) {
        buf += dec.decode();
        pump();
        break;
      }
      buf += dec.decode(value, { stream: true });
      pump();
    }
    if (buf.trim()) dispatch(buf);
  }

  // --- navigation --------------------------------------------------------
  // The homepage (team hub) isn't a setup stage — it's a client view shown at
  // stage `start` for returning users, and after Finish via the Done button.
  let atHome = false;
  let homeLibrary = "";   // the BOBI_HOME/agents library; cached from /api/home for import
  async function refresh() { S = await getJSON("/api/state"); render(); }
  async function boot() {
    S = await getJSON("/api/state");
    if (HOSTED) {
      // The unified app already welcomed the user and owns the home screen.
      welcomed = true;
      hostedChrome();
      if (S.finished) { location.href = "/#/"; return; }
      // Edit-an-existing-team deep link: /setup/?open=<source path> jumps
      // straight into the open-mode conversation (cards pre-filled).
      const openPath = new URLSearchParams(location.search).get("open");
      if (openPath && S.stage === "start") {
        const r = await postJSON("/api/start",
          { mode: "open", location: openPath, team_path: openPath });
        if (r.ok) { S = r.data; } else { toast(r.data.error || "couldn't open the team"); }
      }
      render();
      return;
    }
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
  // Hosted titlebar: the address chip becomes the way back to the dashboard.
  function hostedChrome() {
    const addr = document.querySelector(".titlebar .addr");
    if (addr) addr.innerHTML = '<a class="addr-back" href="/#/">&larr; dashboard</a>';
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
  // The intro lives at the client-only `start` stage (no server `BACK_TO`
  // entry), so its Back routes by where it was entered from: back to the team
  // hub, or to the welcome on-ramp on first run.
  function introBack() {
    if (HOSTED) { location.href = "/#/"; return; }
    if (introFrom === "hub") { atHome = true; render(); }
    else { welcomed = false; render(); }
  }
  // Jump to the homepage (team hub) from anywhere — the titlebar brand and the
  // welcome screen both call this. The hub overlays any stage and is re-entrant,
  // so leaving mid-flow is safe; the server keeps each team's state.
  function goHome() {
    if (HOSTED) { location.href = "/#/"; return; }
    if (atHome) { renderHome(); return; }
    atHome = true; welcomed = true;   // don't fall back to the welcome on-ramp
    building = false; buildGen++;      // supersede any in-flight build
    renderHome();
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
  // A calm first screen — what bobi is, how setup goes, what you'll need —
  // shown once per page load on the `start` stage. "Get started" reveals the
  // intro. Purely presentational: no server state, skipped on resume (resume
  // lands on a later stage, never `start`).
  let welcomed = false;
  // Where the intro was entered from, so its Back button knows where to return:
  // "hub" (came from the team grid via "Add an agent team") or "welcome" (the
  // first-run on-ramp).
  let introFrom = "welcome";
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
        <div class="eyebrow">Welcome to bobi</div>
        <h1>Build a team of agents that runs your work</h1>
        <p class="lede">A realtime agent team on your events — reachable from Slack and other chat apps, scheduled to act on their own, or reacting the moment something happens. Not one chatbot waiting for a prompt.</p>

        <div class="wsec-label">How setup works</div>
        <ol class="wsteps">
          <li class="wstep"><span class="wstep-n">1</span><div><b>Describe it.</b> Tell bobi what you want the team to do, in plain words — rough is fine.</div></li>
          <li class="wstep"><span class="wstep-n">2</span><div><b>Watch it take shape.</b> As you talk, bobi designs the roles, automations, and connections, filling them in live.</div></li>
          <li class="wstep"><span class="wstep-n">3</span><div><b>Connect services.</b> Hook up Slack, GitHub, or anything else it needs — deferrable until the end.</div></li>
          <li class="wstep"><span class="wstep-n">4</span><div><b>Build &amp; install.</b> bobi writes the team and installs it, then you start it with one command.</div></li>
        </ol>

        <div class="wsec-label">The agent that runs it</div>
        <div id="harness-card" class="harness"><p class="ihint">Checking your harness…</p></div>

        <div class="wmeta">
          <div class="wmeta-row"><span class="wmeta-k">Takes</span><span class="wmeta-v">about 10–20 minutes</span></div>
          <div class="wmeta-row"><span class="wmeta-k">You'll also need</span><span class="wmeta-v">logins for any services you want to connect — added as you go.</span></div>
        </div>

        <div class="actions"><button class="btn primary" id="welcome-go">Get started →</button><button class="btn ghost" id="welcome-home">Go to homepage</button></div>
      </main>
      ${flowDiagramHTML()}
    </div></div>`;
    $("#welcome-go").addEventListener("click", () => { welcomed = true; introFrom = "welcome"; renderIntro(); });
    $("#welcome-home").addEventListener("click", goHome);
    loadHarness();
  }

  // The harness card on the welcome screen: which agent runs the team, and
  // whether it's authenticated. bobi's own setup brain runs on this same
  // harness, so an un-authed harness means setup can't function — say so plainly
  // with the one command to fix it, and a Re-check that re-polls after login.
  function harnessCardHTML(hs) {
    const modeLabel = hs.auth_mode === "api_key" ? "API key"
      : hs.auth_mode === "subscription" ? "subscription" : "";
    const ok = hs.authenticated;
    const authRow = ok
      ? `<span class="harness-ok">✓ authenticated${modeLabel ? ` · ${esc(modeLabel)}` : ""}</span>`
      : `<span class="harness-bad">✗ ${hs.cli_present ? "not logged in" : "Claude Code CLI not found"}</span>`;
    const fix = ok ? "" : `
      <div class="harness-fix">
        <p>${hs.cli_present
          ? `Log in so your agents can run. In your terminal:`
          : `Install the Claude Code CLI, then log in. In your terminal:`}</p>
        <div class="cmd"><span class="pr">$</span> <span class="cmd-text">${esc(hs.login_command)}</span>
          <button class="cmd-copy" data-copycmd title="Copy">Copy</button></div>
      </div>`;
    return `
      <div class="harness-rows">
        <div class="harness-row"><span class="harness-k">Agent</span><span class="harness-v">${esc(hs.agent)} · <span class="mono">${esc(hs.model)}</span></span></div>
        <div class="harness-row"><span class="harness-k">Login</span><span class="harness-v">${authRow}</span></div>
      </div>
      ${fix}
      <div class="harness-actions"><button class="btn ghost xs" id="harness-recheck">Re-check</button></div>`;
  }
  async function loadHarness() {
    const card = $("#harness-card");
    if (!card) return;
    let hs;
    try { hs = await getJSON("/api/harness"); }
    catch { hs = null; }  // server gone — the disconnect overlay also shows
    if (!$("#harness-card")) return;  // navigated away while fetching
    // A failed/malformed response shouldn't strand the card on "Checking…" —
    // render a recoverable error state with a working Re-check.
    if (!hs || typeof hs.authenticated !== "boolean") {
      card.classList.remove("harness-warn");
      card.innerHTML = `<p class="ihint">Couldn't check the harness.</p>
        <div class="harness-actions"><button class="btn ghost xs" id="harness-recheck">Re-check</button></div>`;
      $("#harness-recheck").addEventListener("click", loadHarness);
      return;
    }
    card.classList.toggle("harness-warn", !hs.authenticated);
    card.innerHTML = harnessCardHTML(hs);
    // Copy goes through the delegated [data-copycmd] handler — one code path.
    $("#harness-recheck").addEventListener("click", loadHarness);
  }

  // --- intro: pick a template team or design a new one ------------------
  // Two ways in: start from a registry "template" (download + reverse-fill,
  // non-lossy edit) or design a new team from scratch (auto-named in chat).
  // Modify-an-existing-team will return later in a different shape.
  let introRegistry = null, introBase = "bobi", introLoc = "", introLocChanged = false;
  async function renderIntro() {
    setPanes("1fr");
    const data = await getJSON("/api/intro");
    introBase = data.default_location || introBase;
    introLoc = introBase;
    introLocChanged = false;
    drawIntro();
    loadTemplates();
  }
  // A consistent header for the full-screen pages: an optional Back button sits
  // to the LEFT of the eyebrow, title below — so back navigation is always in
  // the same place. `back` is {attr, label} or null, where `attr` is the bare
  // data-* the click delegation listens for ("data-back" / "data-introback").
  // `title` is trusted HTML (the caller escapes any interpolated value).
  function pageHead(eyebrow, title, back) {
    const top = back
      ? `<div class="phead-top"><button class="backbtn" ${back.attr}>← ${esc(back.label || "Back")}</button><span class="eyebrow">${esc(eyebrow)}</span></div>`
      : `<div class="eyebrow">${esc(eyebrow)}</div>`;
    return `<div class="phead">${top}<h1>${title}</h1></div>`;
  }
  function drawIntro() {
    $("#main").innerHTML = `<main class="node narrow intro">
      ${pageHead("Setup", "Build an agent team", { attr: "data-introback", label: "Back" })}
      <p class="lede">bobi manages entire teams of agents that collaborate to solve problems and automate your work. Some of our favorites are engineering, support, and marketing teams.</p>

      <section class="isec">
        <div class="isec-head"><span class="isec-num">1</span><h2 class="isec-title">Where to set it up</h2></div>
        <p class="isec-lede">bobi keeps your team's editable source here, then installs it into the selected agent's <code>run/package/</code> when you finish.</p>
        <div class="locbox">
          <span class="locbox-path" id="loc-path" title="${esc(introLoc)}">${esc(introLoc)}</span>
          <button type="button" class="btn ghost xs" id="loc-change">Change…</button>
        </div>
      </section>

      <section class="isec">
        <div class="isec-head"><span class="isec-num">2</span><h2 class="isec-title">Choose a starting point</h2></div>
        <p class="isec-lede">Customize your own from scratch, or start from one of our templates.</p>
        <div class="tmpl-list" id="tmpl-list">${templatesHTML()}</div>
      </section>
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
      <span class="tmpl-text"><b>Customize my own agent team</b><span>Start from scratch — describe it and bobi designs it with you.</span></span>
      <span class="tmpl-go">New →</span>
    </button>`;
    // A template row; official teams (shipped from the canonical bobi
    // registry) carry a badge so they read as trusted, not third-party.
    const row = (t) =>
      `<button class="tmpl" data-template="${esc(t.name)}">
        <span class="tmpl-glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="8.5" y="13.5" width="7" height="7" rx="1.2"/></svg></span>
        <span class="tmpl-text"><b>${esc(t.name)}${t.official ? `<span class="tmpl-badge">official</span>` : ""}</b><span>${esc(t.description || t.registry || "Agent team template")}</span></span>
        <span class="tmpl-go">Use →</span>
      </button>`;
    let rest = "";
    if (introRegistry === null) rest = `<p class="ihint tmpl-note">Loading templates…</p>`;
    else if (!introRegistry.length) rest = `<p class="ihint tmpl-note">No templates available yet.</p>`;
    else {
      // Group official templates under their own header; anything from a
      // user-added registry follows under a neutral "Community" header.
      const official = introRegistry.filter(t => t.official);
      const other = introRegistry.filter(t => !t.official);
      if (official.length)
        rest += `<div class="tmpl-head">Official templates</div>` + official.map(row).join("");
      if (other.length)
        rest += `<div class="tmpl-head">Community templates</div>` + other.map(row).join("");
    }
    return custom + rest;
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
      introLoc = p; introLocChanged = true;
      const lp = $("#loc-path"); if (lp) { lp.textContent = p; lp.title = p; }
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
  // Import an existing team from anywhere on disk: pick its folder, then copy it
  // into the library and open it (mode "open" — the server validates it's a real
  // team and surfaces a clear error if not). No new endpoint; reuses /api/start.
  function importTeam() {
    openFolderPicker(picked => {
      const name = (picked || "").replace(/\/+$/, "").split("/").pop() || "team";
      const lib = (homeLibrary || introBase).replace(/\/+$/, "");
      atHome = false;
      startTeam({ mode: "open", location: lib + "/" + name, team_path: picked }, null, "Importing…");
    });
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
        <div class="sketch-top"><span class="st-group"><button class="backbtn" data-back>← Back</button><span class="sketch-eyebrow">bobi · build your team</span></span></div>
        <div class="ch-body" id="chbody"></div>
        <div class="cue" id="cue"></div>
        <div class="ch-input"><textarea id="chinput" rows="1" placeholder="Tell bobi what you want to build…" autocomplete="off"></textarea><button class="btn primary" id="chsend" style="padding:9px 14px">↑</button></div>
      </section>
      <aside class="uni-panel">
        <div class="uni-head">
          <div class="up-headrow"><span class="up-title" id="up-title" title="click to rename"></span><span class="up-meter" id="uni-meter"></span></div>
          <span class="up-loc" id="up-loc"></span>
        </div>
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

  // The five things bobi gathers, each a card that fills in + checks off
  // live: goal, roles, automations, connections, chat.
  // The team's name shows in the panel header as bobi auto-derives it; click
  // to rename. (Empty until the goal firms up enough to name the team.)
  function setTeamTitle() {
    const el = $("#up-title"); if (!el) return;
    el.textContent = S.team_name || "Your team";
    el.classList.toggle("named", !!S.team_name);
    // Key detail at the top of the panel: where the team's source lives.
    const loc = $("#up-loc");
    if (loc) { loc.textContent = S.source_dir || ""; loc.title = S.source_dir || ""; }
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
    // Workflows is an OPTIONAL card — it renders and celebrates like the
    // others but never counts toward the N/5 gathered meter.
    const cards = [goalCard(sp), rolesCard(sp), workflowsCard(sp),
                   automationsCard(sp), connectionsCard(sp), chatCard()];
    const keys = ["goal", "roles", "workflows", "autonomous", "services", "chat"];
    const doneNow = {
      goal: sp.readiness.goal === "enough", roles: sp.readiness.roles === "enough",
      workflows: !!(sp.workflows || []).length || !!sp.workflows_confirmed,
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
    // Finish is a soft gate: always clickable, but an incomplete spec gets a
    // "you sure?" confirmation instead of a grayed-out button. The only hard
    // floor left is server-side (the goal must be non-empty to build).
    const foot = $("#uni-foot");
    if (foot) {
      const html = (ready ? ""
        : `<span class="uni-note">bobi is gathering goal, roles, automations, connections, and chat</span>`)
        + `<button class="btn primary" id="uni-finish">Finish →</button>`;
      // Rebuild only on change — SSE-driven re-renders must not recreate the
      // button mid-press (killing focus and eating the click).
      if (foot.innerHTML !== html) {
        foot.innerHTML = html;
        $("#uni-finish").addEventListener("click", () =>
          _lastReady ? go("build") : confirmFinish(_lastGathered));
      }
      _lastReady = ready; _lastGathered = gathered;
    }
  }
  let _lastReady = false, _lastGathered = 0;
  // The not-everything-gathered confirmation before an early Finish.
  function confirmFinish(gathered) {
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "finish-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>Finish early?</b><button class="btn ghost sm" id="fin-close">Close</button></div>
      <div class="sp-body">
        <p class="pd">You haven't completed all the agent setup — ${gathered} of 5 gathered so far. bobi fills reasonable gaps at build, and you can reopen the team to keep editing anytime. Are you sure you want to move on?</p>
        <div class="sp-actions">
          <button class="btn ghost sm" id="fin-stay">Keep setting up</button>
          <button class="btn primary sm" id="fin-go">Move on →</button>
        </div>
      </div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => {
      if (e.target.id === "fin-close" || e.target.id === "fin-stay" || e.target === ov) { ov.remove(); return; }
      if (e.target.id === "fin-go") { ov.remove(); go("build"); }
    });
  }
  // The current interview phase, shown so the user always knows where bobi is
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
      label = ({ goal: "setting the goal", workflows: "designing workflows",
                 automations: "automations",
                 connections: "connections", wrap: "wrapping up" }[p]) || p;
    }
    el.innerHTML = `<span class="ph-dot"></span><span class="ph-lab">${esc(label)}</span>${sub ? `<span class="ph-sub">${esc(sub)}</span>` : ""}`;
    // Ease the banner in when the interview actually moves to a new phase.
    if (_prevPhase !== undefined && _prevPhase !== p) {
      el.classList.remove("phasein"); void el.offsetWidth; el.classList.add("phasein");
    }
    _prevPhase = p;
  }
  // Connections are "settled" when there's nothing to connect (and the brain
  // confirmed none are needed) OR every live connection card is handled. A card
  // counts as handled when it's "connected" (native token present / Venn linked)
  // OR "added" — a user MCP that's been configured but can't be verified here
  // (it connects at runtime). A card that still needs a token/config ("missing"
  // / "needs_auth" / "error") keeps the slot open until the user connects or
  // removes it. The brain identifying which services are needed is NOT the same
  // as them being connected, so a service slot scored "enough" does not settle
  // connections on its own when unconnected cards exist.
  function servicesSettled(sp) {
    const svcs = sp.services || [];
    if (!svcs.length) return sp.readiness.services === "enough";
    const cards = (_connData && _connData.cards) || null;
    if (!cards || !cards.length) return false;
    return cards.every(c => c.status === "connected" || c.status === "added");
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
      : `<span class="ph">bobi will shape the roles as you talk</span>`;
    return `<div class="ucard ${roles.length ? "filled" : "empty"}">
      <div class="ut">Roles ${slotDot(sp.readiness.roles === "enough")}</div>
      <div class="ud">${body}</div>
      <div class="uadd"><button class="lnk add" data-addrole>+ add a role</button></div></div>`;
  }
  // Workflows — optional, proposed by bobi once the roles settle. Click a
  // flow to read its generated YAML (the dark-slab popup); refinements go
  // through the conversation, so there's no structured editor here.
  function workflowsCard(sp) {
    const items = sp.workflows || [];
    const settled = !!items.length || !!sp.workflows_confirmed;
    const body = items.length
      ? items.map((w, i) => {
          const hitl = (w.steps || []).some(s => s && s.hitl);
          const n = (w.steps || []).length;
          return `<div class="urole click" data-wfopen="${i}">
            <div class="urow"><b>${esc(w.name || "workflow")}</b>${hitl ? `<span class="wf-hitl" title="pauses for human approval">human gate</span>` : ""}</div>
            <span>${esc(w.trigger || w.description || "")}${n ? ` · ${n} step${n === 1 ? "" : "s"}` : ""}</span></div>`;
        }).join("")
      : (sp.workflows_confirmed
          ? `<span class="ph">no set flows — bobi handles work ad hoc</span>`
          : `<span class="ph">bobi proposes repeatable flows once the roles are set</span>`);
    return `<div class="ucard ${settled ? "filled" : "empty"}">
      <div class="ut">Workflows <span class="ut-opt">optional</span></div>
      <div class="ud">${body}</div>
      <div class="uadd"><button class="lnk add" data-addwf>+ add a workflow</button></div></div>`;
  }
  function automationsCard(sp) {
    const items = sp.autonomous || [];
    const body = items.length
      ? items.map((a, i) => `<div class="urole click" data-autoopen="${i}">
          <div class="urow"><b>${esc(a.description || "behavior")}</b></div>
          <span>${esc(a.leash || "")}${a.trigger === "event" ? " · on event" : ""}${a.cadence ? " · " + esc(a.cadence) : ""}${a.role ? " · " + esc(a.role) : ""}</span></div>`).join("")
      : (sp.autonomous_confirmed
          ? `<span class="ph">nothing proactive — bobi acts only when asked</span>`
          : `<span class="ph">anything bobi should do on its own?</span>`);
    return `<div class="ucard ${items.length || sp.autonomous_confirmed ? "filled" : "empty"}">
      <div class="ut">Automations ${slotDot(sp.readiness.autonomous === "enough")}</div>
      <div class="ud">${body}</div>
      <div class="uadd"><button class="lnk add" data-addauto>+ add an automation</button></div></div>`;
  }
  // Connections: native + hosted-MCP + custom services each get their own row
  // (a token, an MCP wire-up, or a custom key); Venn-backed services share ONE
  // key, grouped under a single Venn setup with per-service verification status.
  // Reads live status from the cached /api/connect payload.
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
      // Every service is its own row; Venn-backed ones are tagged "(Venn)" and
      // reached through the single account-level Venn connection (the row below).
      body = cards.map(connRow).join("");
    }
    // Venn is ONE account-level connection many services share — pinned as the
    // top row (shown whenever we have connection data) so it can be set up /
    // managed first; the per-service rows follow.
    const ingress = ingressRow();
    const venn = cards ? vennAccountRow(vennConfigured) : "";
    return `<div class="ucard ${(sp.services || []).length ? "filled" : "empty"}">
      <div class="ut">Connections ${slotDot(ok)}</div>
      <div class="ud">${ingress}${venn}${body}</div>
      <div class="uadd"><button class="lnk add" data-addconn>+ add a connection</button></div></div>`;
  }
  function ingressRow() {
    const ing = S.ingress || { mode: "local", url: "", verified: false };
    const label = INGRESS_LABELS[ing.mode] || "Ingress";
    const detail = ing.mode === "local"
      ? "loopback only"
      : (ing.url || "needs URL");
    const badge = ing.verified
      ? `<span class="cbadge connected">${CHECK} verified</span>`
      : ing.error
      ? `<span class="cbadge warn">check failed</span>`
      : `<span class="cbadge">not checked</span>`;
    return `<div class="uconn ingress-row">
      <span><b>Webhook ingress</b><span class="ctag">${esc(label)} · ${esc(detail)}</span></span>
      <span class="cright">${badge}<button class="lnk" data-ingressopen>Configure</button></span></div>`;
  }
  // Account-level Venn: one connection the user manages globally; any number of
  // Venn-backed services (Gmail, Slack, Salesforce…) are reached through it. One
  // link to set up or manage; hover the ⓘ for what Venn is.
  function vennAccountRow(configured) {
    const help = "Connect lots of services at once with Venn — one key, many integrations";
    const badge = configured
      ? `<span class="cbadge connected">${CHECK} connected</span>`
      : `<span class="cbadge">not connected</span>`;
    return `<div class="uconn venn-acct">
      <span class="venn-lab">Venn<span class="help" tabindex="0" role="img"
        aria-label="${esc(help)}" data-tip="${esc(help)}">${HELP}</span></span>
      <span class="cright">${badge}
        <button class="lnk" data-vennsetup>${configured ? "Manage Venn" : "Set up Venn"}</button></span></div>`;
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
    // A hosted-MCP server with no key to capture is wired in deterministically —
    // nothing for the user to do here, so show "wired" rather than a Connect CTA.
    const mcpNoSecret = c.kind === "mcp"
      && !((c.methods && c.methods[0] && c.methods[0].secrets || []).length);
    let right;
    if (c.kind === "venn") {
      // Venn-backed services are reached through the account-level Venn
      // connection (the Venn row) — no per-service key to capture here.
      right = c.status === "connected"
        ? `<span class="cbadge connected">${CHECK} connected via Venn</span>`
        : `<span class="cbadge">${c.status === "unknown" ? "via Venn" : "needs Venn"}</span>`;
    } else if (c.kind === "custom") {
      // Not native/Venn/known MCP — connect it by pointing at a remote MCP.
      right = `<button class="btn ghost xs" data-addmcp="${esc(c.name)}">Connect</button>`;
    } else if (c.user_mcp) {
      // A user-added MCP. A subtle status dot (left of the name) carries the
      // state — connected / added / needs-config / error; the action is just
      // finish-or-edit. Verify it from chat ("test the substack connection").
      right = `<button class="lnk" data-editmcp="${esc(c.key)}">${c.status === "needs_auth" ? "finish" : "edit"}</button>`;
    } else if (mcpNoSecret) {
      right = `<span class="cbadge connected">${CHECK} wired</span>`;
    } else if (c.status === "connected") {
      right = `${statusBadge("connected")} <button class="lnk" data-secretopen="${esc(c.key)}">edit</button>`;
    } else {
      right = `<button class="btn ghost xs" data-secretopen="${esc(c.key)}">Connect</button>`;
    }
    // Tag how each service is reached: through Venn, a hosted MCP (the registry
    // one-click, or a user-added connection's own note), or a custom service
    // that needs a connection.
    const tag = c.kind === "venn"
      ? `<span class="ctag">via Venn</span>`
      : c.kind === "mcp"
      ? `<span class="ctag">${esc(c.note || "hosted MCP · 1-click")}</span>`
      : c.kind === "custom"
      ? `<span class="ctag">connect an MCP</span>` : "";
    const dot = c.user_mcp ? connDot(c) : "";
    return `<div class="uconn"><span>${dot}${esc(c.name)}${tag}</span><span class="cright">${right}${trashBtn(c.key)}</span></div>`;
  }
  // A subtle status dot for a user MCP row: green = connected (tested OK),
  // red = test failed, amber = needs config, grey = added but not yet tested.
  function connDot(c) {
    const map = {
      connected: ["ok", "Connected — verified"],
      error: ["err", c.note || "Test failed"],
      needs_auth: ["warn", "Needs configuration"],
      added: ["idle", "Added — not tested yet (try “test the connection” in chat)"],
    };
    const [cls, tip] = map[c.status] || ["idle", ""];
    return `<span class="cdot ${cls}" title="${esc(tip)}"></span>`;
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

  function openIngressModal() {
    const cur = S.ingress || { mode: "local", url: "" };
    const ov = document.createElement("div");
    ov.className = "secret-ov";
    ov.id = "ingress-ov";
    const team = S.team_name || "<name>";
    ov.innerHTML = `<div class="secret-panel ingress-panel">
      <div class="sp-head"><b>Webhook ingress</b><button class="x" id="ingress-close">×</button></div>
      <div class="sp-body">
        <p class="fhelp">Pick how GitHub, Slack, and Linear can reach the event server. Local-only is fine for command-line teams; webhook services need an internet-facing HTTPS URL.</p>
        <div class="ingress-options">
          ${ingressOption("local", "Local only", "No public webhooks. Bobi auto-starts loopback when the team runs.")}
          ${ingressOption("quick_tunnel", "Quick tunnel", "Fast local test with cloudflared or ngrok in front of localhost:8080.")}
          ${ingressOption("bobi_cloud", "Bobi cloud", "Durable shared Worker for hosted webhook delivery.")}
          ${ingressOption("custom_worker", "Custom Worker", "Durable event server you operate, usually a Cloudflare Worker.")}
        </div>
        <div id="ingress-fields"></div>
        <div class="sp-actions"><button class="btn primary sm" id="ingress-save">Verify & save</button></div>
        <div class="mcp-status" id="ingress-status"></div>
      </div></div>`;
    document.body.appendChild(ov);
    let mode = cur.mode || "local";
    ov.addEventListener("click", e => {
      if (e.target.id === "ingress-close" || e.target === ov) { ov.remove(); return; }
      const opt = e.target.closest("[data-ingressmode]");
      if (opt) { mode = opt.dataset.ingressmode; draw(); return; }
    });
    $("#ingress-save").addEventListener("click", () => verifyIngress(mode, ov));
    function draw() {
      ov.querySelectorAll("[data-ingressmode]").forEach(b => b.classList.toggle("on", b.dataset.ingressmode === mode));
      const url = cur.mode === mode ? (cur.url || "") : "";
      let html = "";
      if (mode === "local") {
        html = `<div class="ingress-note">
          <b>Local-only event server</b>
          <span>Run <code>bobi agent ${esc(team)} start</code>. Bobi will start the loopback server automatically; external webhook providers cannot reach it.</span>
        </div>`;
      } else if (mode === "quick_tunnel") {
        html = `<div class="ingress-note">
          <b>Quick tunnel</b>
          <span>Start the local event server, run a tunnel, then paste its HTTPS URL. The empty env override forces loopback startup even if a remote URL was previously saved.</span>
          <div class="cmd mini"><span class="pr">$</span><span class="cmd-text">BOBI_EVENT_SERVER= bobi agent ${esc(team)} event-server start
cloudflared tunnel --url http://127.0.0.1:8080</span></div>
        </div>
        <label class="fld"><span class="flab">Tunnel URL</span>
          <input id="ingress-url" placeholder="https://example.trycloudflare.com" autocomplete="off" value="${esc(url)}"></label>`;
      } else if (mode === "bobi_cloud") {
        html = `<div class="ingress-note">
          <b>Bobi cloud Worker</b>
          <span>Setup will verify the shared Worker and save it as <code>BOBI_EVENT_SERVER</code> for this agent.</span>
        </div>`;
      } else {
        html = `<div class="ingress-note">
          <b>Custom Worker</b>
          <span>Use your own deployed event server. Verification checks <code>/health</code> before saving.</span>
        </div>
        <label class="fld"><span class="flab">Event server URL</span>
          <input id="ingress-url" placeholder="https://events.example.workers.dev" autocomplete="off" value="${esc(url)}"></label>`;
      }
      $("#ingress-fields").innerHTML = html;
      $("#ingress-save").textContent = mode === "local" ? "Use local only" : "Verify & save";
      const inp = $("#ingress-url");
      if (inp) inp.addEventListener("keydown", e => {
        if (e.key === "Enter") { e.preventDefault(); verifyIngress(mode, ov); }
      });
    }
    draw();
  }
  function ingressOption(mode, label, sub) {
    return `<button class="ingress-opt" type="button" data-ingressmode="${mode}">
      <b>${esc(label)}</b><span>${esc(sub)}</span></button>`;
  }
  async function verifyIngress(mode, ov) {
    const st = $("#ingress-status");
    const url = ($("#ingress-url") && $("#ingress-url").value || "").trim();
    st.innerHTML = `<div class="fhelp"><span class="spin"></span> Checking ingress…</div>`;
    const r = await postJSON("/api/ingress/verify", { mode, url });
    if (!r.ok || !r.data.ok) {
      st.innerHTML = `<div class="mcp-err">${esc((r.data && (r.data.error || r.data.message)) || "couldn't verify ingress")}</div>`;
      if (r.data && r.data.state) { S = r.data.state; renderUniCards(); }
      return;
    }
    S = r.data.state;
    renderUniCards();
    ov.remove();
    toast("ingress verified");
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
      html = `<div class="msg bob">Hi — I'm bobi. Tell me what you want this team to do, in your own words. Rough is fine; we'll sharpen it together.</div>`;
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
    // You can keep typing (and queue another message) while bobi is replying.
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
      // Reconcile user bubbles with authoritative server state (handles redaction).
      const chbody = $("#chbody");
      const youEls = chbody ? chbody.querySelectorAll(".msg.you") : [];
      let yi = 0;
      for (const m of S.messages) {
        if (m.role === "user") {
          if (youEls[yi] && youEls[yi].textContent !== m.content) youEls[yi].textContent = m.content;
          yi++;
        }
      }
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
    const r = await fetch(BASE + "/api/credential/value?var=" + encodeURIComponent(varName), { headers: H });
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

  // --- Venn setup: a small flow (key → loading → pick / error → done) ----
  // Venn is ONE account-level connection. Paste the key, bobi pulls the
  // MCPs in your Venn account, you pick which to add to THIS team, confirm, and
  // they appear as their own rows. "Open Venn" stays available the whole time.
  let vennStep = "key";          // key | loading | error | pick | done
  let vennServers = [];          // [{name, connected}] from the account
  let vennSel = new Set();        // server names the user has ticked
  let vennErr = "";              // error-state message
  let vennAddedMsg = "";         // done-state summary
  const OPEN_VENN = '<a class="doclink" href="https://app.venn.ai" target="_blank" rel="noopener">Open Venn ↗</a>';

  async function openVennSetup() {
    _connData = await getJSON("/api/connect");
    vennErr = ""; vennAddedMsg = "";
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "venn-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>Connect via Venn</b><button class="btn ghost sm" id="venn-close">Close</button></div>
      <div class="sp-body" id="venn-body"></div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => {
      if (e.target.id === "venn-close" || e.target === ov) closeVenn();
    });
    // Already connected? Skip the key step and go straight to discovery.
    if (_connData && _connData.venn_configured) { vennLoadServers(); }
    else { vennStep = "key"; drawVenn(); }
  }
  function closeVenn() { const o = $("#venn-ov"); if (o) o.remove(); renderUniCards(); }

  // Servers come back as plain names (everything the key can reach). A service
  // is ON iff it's already on the team — that team membership IS the toggle
  // state; there's no separate "live" concept in the picker.
  function setVennServers(names) {
    vennServers = (names || []).slice();
    const have = new Set((S.spec.services || [])
      .map(s => ((s && s.name) || String(s) || "").toLowerCase()));
    vennSel = new Set(vennServers.filter(n => have.has((n || "").toLowerCase())));
  }
  async function vennLoadServers() {
    vennStep = "loading"; drawVenn();
    const r = await getJSON("/api/venn/servers");
    if (!r || !r.ok) {
      vennErr = (r && r.error) || "Couldn't reach Venn with that key.";
      vennStep = "error"; drawVenn(); return;
    }
    setVennServers(r.servers);
    vennStep = "pick"; drawVenn();
  }

  function drawVenn() {
    const body = $("#venn-body"); if (!body) return;
    let html;
    if (vennStep === "key") {
      html = `
        <p class="pd">Paste your Venn API key. bobi pulls in the services
        you've connected in Venn — pick which ones this team should use.</p>
        <ol class="steps">
          <li>Sign in at <a class="doclink" href="https://app.venn.ai" target="_blank" rel="noopener">app.venn.ai</a> and create an API key (Settings → API).</li>
          <li>Connect the services you want in Venn (one-click OAuth).</li>
          <li>Paste the key below.</li>
        </ol>
        <label class="secret"><span class="slabel">Venn API key</span>
          <input type="password" id="venn-key" placeholder="venn_…" autocomplete="off">
          <span class="shelp">Stored in .env on this machine; never sent to the model.</span></label>
        <div class="connect-row">${OPEN_VENN}
          <button class="btn primary sm" data-vennconnect>Connect Venn</button></div>`;
    } else if (vennStep === "loading") {
      html = `<div class="venn-loading"><span class="spinner"></span>
          Checking your Venn account…</div>
        <div class="connect-row">${OPEN_VENN}</div>`;
    } else if (vennStep === "error") {
      html = `
        <div class="venn-err">
          <div class="ve-seal">!</div>
          <div class="vd-copy"><b>Couldn't connect to Venn</b>
            <span>${esc(vennErr)}</span></div>
        </div>
        <div class="connect-row">${OPEN_VENN}
          <button class="btn primary sm" data-vennretry>Try another key</button></div>`;
    } else if (vennStep === "pick") {
      const rows = vennServers.length
        ? vennServers.map(n => {
            const on = vennSel.has(n);
            return `<label class="venn-pick ${on ? "on" : ""}">
              <input type="checkbox" data-vennpick="${esc(n)}" ${on ? "checked" : ""}>
              <span class="vp-name">${esc(n)}</span>
              <span class="vp-state">${on ? "on" : "off"}</span></label>`;
          }).join("")
        : `<p class="pd">No services are available on this Venn account yet —
            connect some in Venn, then come back and re-check.</p>`;
      html = `
        <div class="venn-ok"><span class="cbadge connected">${CHECK} Venn connected</span>
          <span class="pd">Toggle the services this team should use${
            vennServers.length ? ` — ${vennServers.length} available` : ""}.</span></div>
        <div class="venn-list">${rows}</div>
        <div class="connect-row">${OPEN_VENN}
          <button class="btn primary sm" data-vennapply>Save</button></div>`;
    } else if (vennStep === "done") {
      html = `
        <div class="venn-done"><div class="vd-seal">${CHECK}</div>
          <div class="vd-copy"><b>${esc(vennAddedMsg)}</b>
            <span>They're on your team — manage them anytime from the Connections panel.</span></div></div>
        <div class="connect-row">${OPEN_VENN}
          <button class="btn primary sm" data-vennclose>Close</button></div>`;
    }
    body.innerHTML = html;
  }

  async function vennConnect() {
    const input = $("#venn-key");
    const val = input ? input.value.trim() : "";
    if (!val) { toast("paste your Venn key first"); return; }
    vennStep = "loading"; drawVenn();
    // Verify BEFORE saving: a bad key returns ok:false and is never persisted,
    // so it can't flip the Venn row to "connected".
    const r = await postJSON("/api/venn/connect", { key: val });
    if (!r.ok || !r.data.ok) {
      vennErr = (r.data && r.data.error) || "Couldn't connect to Venn.";
      vennStep = "error"; drawVenn(); return;
    }
    if (r.data.state) S = r.data.state;
    if (!(S.credentials_saved || []).includes("VENN_API_KEY"))
      (S.credentials_saved ||= []).push("VENN_API_KEY");
    setVennServers(r.data.servers);
    vennStep = "pick"; drawVenn();
  }
  // Sync a server's toggle to the team-membership set without redrawing (keeps
  // scroll position); reflect the new state in the row.
  function vennToggle(name, checked, label) {
    if (checked) vennSel.add(name); else vennSel.delete(name);
    if (label) {
      label.classList.toggle("on", checked);
      const st = label.querySelector(".vp-state");
      if (st) st.textContent = checked ? "on" : "off";
    }
  }
  async function vennApply() {
    // Reconcile: the team's Venn services become exactly the toggled-on set
    // (within the available universe). Turning everything off is valid.
    const r = await postJSON("/api/venn/apply",
                             { servers: [...vennSel], available: vennServers });
    if (!r.ok) { toast(r.data.error || "couldn't save"); return; }
    if (r.data.state) S = r.data.state;
    _connData = await getJSON("/api/connect");
    const a = (r.data.added || []).length, rm = (r.data.removed || []).length;
    vennAddedMsg = (a || rm)
      ? `Updated your team's Venn services${a ? ` · +${a}` : ""}${rm ? ` · −${rm}` : ""}`
      : "No changes — your Venn services are up to date";
    vennStep = "done"; drawVenn();
    renderUniCards();
  }

  // --- generating (Build + validate + install, collapsed) ----------------
  // buildGen tags each build run; goBack() bumps it so a cancelled (or hung)
  // build can never resume and jump the user forward to the preview.
  let building = false, buildGen = 0;
  function renderGenerating() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="node narrow">
      ${pageHead("Building", `Building ${esc(S.team_name || "your team")}`, { attr: "data-back", label: "Back to editing" })}
      <p class="lede" id="genmsg">Writing the pack, checking it, and installing — sit back.</p>
      <div class="genbar"><div class="genbar-fill" id="genfill"></div></div>
      <ul class="genfiles" id="genfiles"></ul>
      <div id="generr"></div>
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
  function agentCommandName() {
    return slugify(S.team_name) || "new-agent";
  }
  function talkHint() {
    if (S.chat === "slack") return `<p class="lede">Talk to it in Slack — message the bot in your channel.</p>`;
    if (S.chat === "telegram") return `<p class="lede">Talk to it in Telegram.</p>`;
    return `<p class="lede">Talk to it from the terminal: <code>bobi agent ${esc(agentCommandName())} ask "what's the status?"</code></p>`;
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
    const where = S.source_dir || "$BOBI_HOME/agents/" + S.team_name + "/src";
    const counts = `${spec.roles.length || 1} role(s) · ${spec.autonomous.length} automation(s) · ${spec.services.length} service(s)`;
    $("#main").innerHTML = `<main class="filesdone">
      <header class="fd-head">
        <button class="backbtn" data-back>← Keep editing</button>
        <div class="fd-title">
          <div class="eyebrow">Preview · here's what bobi built</div>
          <h1>${esc(S.team_name || "your team")}</h1>
          <p class="fd-meta">${counts} · source at <code>${esc(where)}</code></p>
        </div>
        <div class="fd-actions">
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
      const b = $("#fd-finish"); b.disabled = true;
      // Finish marks the state complete; launching stays a deliberate action
      // in both modes (the hosted on_finish redirects, it does not launch).
      b.textContent = "Finishing…";
      let r = null;
      try { r = await postJSON("/api/finish", {}); } catch { /* ignore */ }
      const d = (r && r.data) || {};
      if (d.redirect) {
        // Launched — back to the unified app (dashboard / agent view).
        _finished = true;
        location.href = d.redirect;
        return;
      }
      if (d.launch_error) toast("Launch failed: " + d.launch_error);
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

  // Final screen after Finish: "All set" on one line, then a next-steps
  // carousel — one full card per step (test locally / deploy / finalize
  // Slack), cycled forward and back. The server stays alive through this
  // screen (the Slack step saves a channel and sends a real test message);
  // "Close & end setup" is what actually stops it, via /api/shutdown.
  let nsIndex = 0;
  let nsSlackDraft = "";   // unsaved channel input, kept across carousel nav
  function cmdRow(label, cmd) {
    return `<div class="cmd-item">${label ? `<span class="cmd-k">${esc(label)}</span>` : ""}
      <div class="cmd"><span class="pr">$</span> <span class="cmd-text">${esc(cmd)}</span>
        <button class="cmd-copy" data-copycmd title="Copy">Copy</button></div></div>`;
  }
  // A paste-into-your-agent prompt (Claude Code / Codex), styled like a command.
  function promptRow(text) {
    return `<div class="cmd prompt"><span class="cmd-text">${esc(text)}</span>
      <button class="cmd-copy" data-copycmd title="Copy">Copy</button></div>`;
  }
  // Keep in lockstep with bobi/templates/slack-app.manifest.yaml (scopes.bot)
  // — the manifest is the source of truth that create-slack-bot prefills.
  const SLACK_SCOPES = ["app_mentions:read", "channels:history",
    "channels:read", "chat:write", "files:read", "files:write",
    "groups:history", "groups:read", "im:history", "im:read", "im:write",
    "mpim:history", "users:read"];
  function nsSteps() {
    const name = agentCommandName();
    const teamSlug = slugify(S.team_name) || "your-team";
    const steps = [{
      key: "test", label: "Test locally",
      title: "Take it for a spin in your terminal",
      html: `
        <p class="ns-lede">Your team runs on this machine with one command. Open a fresh terminal and work through these:</p>
        <div class="cmdlist">
          ${cmdRow("turn it on", `bobi agent ${name} start`)}
          ${cmdRow("check its health", `bobi agent ${name} status`)}
          ${cmdRow("ask it a question", `bobi agent ${name} ask "what can you do?"`)}
          ${cmdRow("give it a task", `bobi agent ${name} message "introduce yourself and list your roles"`)}
          ${cmdRow("watch what it's doing", `bobi agent ${name} events`)}
          ${cmdRow("stop / restart anytime", `bobi agent ${name} restart`)}
        </div>`,
    }, {
      key: "deploy", label: "Deploy",
      title: "Deploy it somewhere that stays awake",
      html: `
        <p class="ns-lede">Two good homes. Either way, ask Claude Code or Codex to do the wiring — copy a prompt below and paste it into your coding agent.</p>
        <div class="deploy">
          <div class="deploy-opt">
            <div class="deploy-head"><span class="deploy-tag">Local</span><span class="deploy-sub">this machine, a Mac mini, or a server you own</span></div>
            <p class="deploy-lede">Reuses the Claude Code login you already have.</p>
            ${promptRow(`Help me run my bobi agent team "${name}" as an always-on service on this machine: configure its event server so it can receive webhooks, and make "bobi agent ${name} start" survive reboots. The framework docs are at https://github.com/moda-labs/bobi-agent (see docs/EVENT_SERVER.md).`)}
          </div>
          <div class="deploy-opt">
            <div class="deploy-head"><span class="deploy-tag">Cloud</span><span class="deploy-sub">always-on, on Fly.io</span></div>
            <p class="deploy-lede">A dedicated container + volume + secrets; the instance needs its own <span class="mono">ANTHROPIC_API_KEY</span>.</p>
            ${promptRow(`Read ${DOCS_CLOUD_URL} and deploy my bobi agent team "${teamSlug}" to Fly.io using scripts/provision-instance.sh. Walk me through the env file and secrets it needs.`)}
            <p class="deploy-note"><a class="exlink" href="${DOCS_CLOUD_URL}" target="_blank" rel="noopener">Full runbook →</a></p>
          </div>
        </div>`,
    }];
    if (S.chat === "slack") {
      const savedCh = (S.credentials_saved || []).includes("SLACK_CHANNELS");
      // A verified non-local ingress (the ingress wizard) already IS the
      // event server Slack calls — show the concrete Request URL instead of
      // the contradictory "deploy first" placeholder copy.
      const ing = S.ingress || {};
      const ingUrl = (ing.verified && ing.mode !== "local" && ing.url)
        ? ing.url.replace(/\/+$/, "") + "/webhooks/slack" : "";
      const urlSteps = ingUrl
        ? `<li>Your event server is already verified. At <a class="exlink" href="https://api.slack.com/apps" target="_blank" rel="noopener">api.slack.com/apps</a> → your app → <b>Event Subscriptions</b>, set the Request URL to <span class="mono">${esc(ingUrl)}</span>.</li>`
        : `<li>Deploy first (previous step) — your event server's public URL is what Slack calls.</li>
            <li>At <a class="exlink" href="https://api.slack.com/apps" target="_blank" rel="noopener">api.slack.com/apps</a> → your app → <b>Event Subscriptions</b>, set the Request URL to <span class="mono">&lt;your event server&gt;/webhooks/slack</span>.</li>`;
      steps.push({
        key: "slack", label: "Finalize Slack",
        title: "Finish wiring Slack",
        html: `
          <p class="ns-lede">${ingUrl ? "" : "After you deploy, "}Slack needs to know where to send events — then your team is reachable in your workspace.</p>
          <ol class="ns-list">
            ${urlSteps}
            <li>Install the app to your workspace with the scopes below (the <span class="mono">bobi create-slack-bot</span> manifest prefills them).</li>
            <li>Invite the bot to a dedicated channel (<span class="mono">/invite @your-bot</span>), then save that channel here.</li>
          </ol>
          <div class="scopes">${SLACK_SCOPES.map(s => `<b>${esc(s)}</b>`).join("")}</div>
          <div class="slackrow">
            <input id="ns-slackch" aria-label="Slack channel" placeholder="#channel or channel ID (C…)" autocomplete="off" value="${esc(nsSlackDraft)}">
            <button class="btn ghost xs" id="ns-slacksave">Save</button>
            <button class="btn primary xs" id="ns-slacktest">Send a test message</button>
          </div>
          <div class="ns-slackstatus${savedCh ? " ok" : ""}" id="ns-slackstatus">${savedCh ? "✓ channel saved" : ""}</div>`,
      });
    }
    return steps;
  }
  function renderFinished() {
    setPanes("1fr");
    nsIndex = 0;
    const where = S.source_dir || "$BOBI_HOME/agents/" + agentCommandName() + "/src";
    $("#main").innerHTML = `<main class="done-wrap">
      <div class="done-head">
        <div class="seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L19 7"/></svg></div>
        <div class="done-head-copy"><div class="eyebrow">All set</div>
          <h1>${esc(S.team_name || "your team")} is ready</h1></div>
      </div>
      <p class="lede">Your agent team is created on this machine — source at <code>${esc(where)}</code>, installed into its <code>run/package/</code>. Two things left: try it, then deploy it.</p>
      <div class="ns-bar">
        <div class="ns-dots" id="ns-dots"></div>
        <div class="ns-nav">
          <button class="btn ghost xs" id="ns-prev">← Previous</button>
          <button class="btn primary xs" id="ns-next">Next →</button>
        </div>
      </div>
      <section class="ns-step" id="ns-step"></section>
      <div class="ns-exit">
        <button class="btn ghost" id="ns-home">${HOSTED ? "Go to dashboard" : "Go to homepage"}</button>
        ${HOSTED ? "" : `<button class="btn ghost" id="ns-close">Close &amp; end setup</button>`}
      </div>
    </main>`;
    drawNsStep();
    $("#ns-prev").addEventListener("click", () => { if (nsIndex > 0) { nsIndex--; drawNsStep(); } });
    $("#ns-next").addEventListener("click", () => {
      if (nsIndex < nsSteps().length - 1) { nsIndex++; drawNsStep(); }
      else { goHome(); }
    });
    $("#ns-home").addEventListener("click", goHome);
    const nsClose = $("#ns-close");
    if (nsClose) nsClose.addEventListener("click", endSession);
  }
  function drawNsStep() {
    const steps = nsSteps();
    const s = steps[nsIndex];
    $("#ns-dots").innerHTML = steps.map((st, i) =>
      `<button class="ns-dot ${i === nsIndex ? "on" : ""}" data-nsgo="${i}">
        <span class="ns-dot-n">${i + 1}</span>${esc(st.label)}</button>`).join("");
    $("#ns-step").innerHTML = `
      <div class="ns-eyebrow">Step ${nsIndex + 1} of ${steps.length}</div>
      <h2>${esc(s.title)}</h2>${s.html}`;
    $("#ns-prev").disabled = nsIndex === 0;
    $("#ns-next").textContent = nsIndex === steps.length - 1 ? "Done →" : "Next →";
    if (s.key === "slack") wireSlackStep();
  }
  function wireSlackStep() {
    // kind: "ok" (confirmed success, green) | "err" (red) | "busy" (muted) —
    // in-flight copy must not pre-announce success in green.
    const status = (msg, kind) => { const el = $("#ns-slackstatus");
      if (el) { el.textContent = msg;
        el.classList.toggle("err", kind === "err");
        el.classList.toggle("ok", kind === "ok"); } };
    $("#ns-slackch").addEventListener("input",
      e => { nsSlackDraft = e.target.value; });
    // Disable the pressed button while its request is in flight — a double
    // click must not post two test messages or race two saves.
    const oneShot = (btn, fn) => btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await fn(); } finally { btn.disabled = false; }
    });
    oneShot($("#ns-slacksave"), async () => {
      const ch = ($("#ns-slackch").value || "").trim();
      if (!ch) { status("enter a #channel or channel ID first", "err"); return; }
      status("saving…", "busy");
      const r = await postJSON("/api/slack/channel", { channel: ch });
      if (!r.ok) { status(r.data.error || "couldn't save", "err"); return; }
      if (r.data.state) S = r.data.state;
      status(`✓ channel saved (${r.data.channel})`, "ok");
    });
    oneShot($("#ns-slacktest"), async () => {
      status("sending…", "busy");
      const r = await postJSON("/api/slack/test", {});
      status(r.ok ? "✓ test message sent — check the channel"
                  : (r.data.error || "test failed"), r.ok ? "ok" : "err");
    });
  }
  // Close & end setup: stop the local server and leave a static goodbye —
  // nothing left on screen depends on it. Standalone only: hosted in the
  // unified app the dashboard owns the server's lifecycle (the Close button
  // isn't rendered there).
  async function endSession() {
    _finished = true;   // suppress the disconnect overlay; this exit is chosen
    try { await postJSON("/api/shutdown", {}); } catch { /* it's exiting */ }
    setPanes("1fr");
    $("#main").innerHTML = `<main class="done-wrap ns-bye">
      <div class="done-head">
        <div class="seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L19 7"/></svg></div>
        <div class="done-head-copy"><div class="eyebrow">Setup ended</div>
          <h1>${esc(S.team_name || "Your team")} is installed</h1></div>
      </div>
      <p class="lede">The setup server has stopped — you can close this tab. Start the team anytime with <code>bobi agent ${esc(agentCommandName())} start</code>, or run <code>bobi setup</code> again to keep editing.</p>
    </main>`;
  }

  // --- homepage (the re-entrant team hub) --------------------------------
  // A grid of team cards (boxy, to read distinctly from the template *rows* on
  // the intro). Click a team to open it in the editor (the chat + cards screen,
  // reverse-filled from source); click the "add" card to start a fresh setup.
  async function renderHome() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="node home">
      <div class="eyebrow">bobi</div>
      <h1>Your agent teams</h1>
      <p class="lede">Pick a team to view or update it, or add a new one. bobi keeps each team's source and re-installs your changes when you finish editing.</p>
      <div class="home-grid" id="home-list"><p class="ihint">Loading…</p></div>
      <p class="home-import">Already have a team elsewhere? <button type="button" class="linkbtn" data-importteam>Import a team from your computer</button>.</p>
    </main>`;
    const data = await getJSON("/api/home");
    const teams = data.teams || [];
    homeLibrary = data.library || homeLibrary;   // cached for import (below)
    const teamGlyph = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.2"/><rect x="8.5" y="13.5" width="7" height="7" rx="1.2"/></svg>';
    const cards = teams.map(t =>
      `<button class="hcard" data-openteam="${esc(t.path)}" title="${esc(t.path)}">
        <span class="hcard-glyph">${teamGlyph}</span>
        <b>${esc(t.name)}</b>
        <span class="hcard-desc">${esc(t.description || "Agent team")}</span>
        <span class="hcard-path">${esc(t.path)}</span>
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
    const trig = a.trigger === "event" ? "event" : "schedule";
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "auto-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>Automation</b><button class="btn ghost sm" id="auto-close">Close</button></div>
      <div class="sp-body yamlish">
        <label class="fld"><span class="flab">description</span>
          <textarea id="a-desc" rows="2">${esc(a.description || "")}</textarea></label>
        <label class="fld"><span class="flab">role <span class="fhelp inline">which agent runs it</span></span>
          <select id="a-role">${roleOpts}</select></label>
        <div class="fld"><span class="flab">fires <span class="fhelp inline">on a schedule, or reacting to an event</span></span>
          <div class="seg" id="a-trigger">
            <button type="button" class="seg-btn ${trig === "schedule" ? "on" : ""}" data-trig="schedule">on a schedule</button>
            <button type="button" class="seg-btn ${trig === "event" ? "on" : ""}" data-trig="event">on an event</button>
          </div></div>
        <label class="fld"><span class="flab">when <span class="fhelp inline" id="a-when-help"></span></span>
          <input id="a-when" value="${esc(a.cadence || "")}" autocomplete="off"></label>
        <label class="fld"><span class="flab">leash</span>
          <select id="a-leash">${leashOpts}</select></label>
        <label class="fld"><span class="flab">command <span class="fhelp inline">what the agent is told to do</span></span>
          <textarea id="a-cmd" rows="2">${esc(a.command || "")}</textarea></label>
        <div class="sp-actions"><button class="btn primary sm" id="a-save">Save automation</button></div>
      </div></div>`;
    document.body.appendChild(ov);
    // The trigger segment reshapes the "when" field's hint + placeholder:
    // schedule = an interval; event = what it reacts to (a webhook-fed event).
    const syncTrig = () => {
      const on = ov.querySelector("#a-trigger .seg-btn.on");
      const isEvent = on && on.dataset.trig === "event";
      $("#a-when-help").textContent = isEvent
        ? "the event it reacts to" : "an interval or time";
      $("#a-when").placeholder = isEvent
        ? "e.g. when an email arrives, when a PR opens"
        : "e.g. 1d, 15m, 9am daily";
    };
    ov.querySelectorAll("#a-trigger .seg-btn").forEach(b =>
      b.addEventListener("click", () => {
        ov.querySelectorAll("#a-trigger .seg-btn").forEach(x =>
          x.classList.toggle("on", x === b));
        syncTrig();
      }));
    syncTrig();
    ov.addEventListener("click", e => { if (e.target.id === "auto-close" || e.target === ov) ov.remove(); });
    $("#a-save").addEventListener("click", async () => {
      const on = ov.querySelector("#a-trigger .seg-btn.on");
      const fields = {
        description: $("#a-desc").value, role: $("#a-role").value,
        cadence: $("#a-when").value, leash: $("#a-leash").value,
        command: $("#a-cmd").value,
        trigger: on ? on.dataset.trig : "schedule",
      };
      const res = await postJSON("/api/automation/update", { index: i, fields });
      if (!res.ok) { toast(res.data.error || "couldn't save"); return; }
      S = res.data; ov.remove(); renderUniCards(); toast("automation saved");
    });
  }
  // A workflow's generated YAML, read-only in the dark slab — the machine
  // writes in the dark. Refinements go through the conversation.
  async function openWorkflowModal(i) {
    const w = (S.spec.workflows || [])[i]; if (!w) return;
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "wf-ov";
    ov.innerHTML = `<div class="secret-panel wf-panel">
      <div class="sp-head"><b id="wf-path">${esc("workflows/" + (slugify(w.name) || "workflow") + ".yaml")}</b><button class="btn ghost sm" id="wf-close">Close</button></div>
      <div class="wf-slab"><div class="code" id="wf-code">loading…</div></div>
      <div class="wf-foot">Written at build, exactly like this. To change it, just tell bobi in the chat.</div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => { if (e.target.id === "wf-close" || e.target === ov) ov.remove(); });
    const d = await getJSON("/api/workflow/yaml?index=" + i);
    const code = $("#wf-code");
    if (!code) return;   // closed while fetching
    if (d.error) { code.textContent = d.error; return; }
    code.textContent = d.yaml || "";
    if (d.path) $("#wf-path").textContent = d.path;
  }
  // Add a role / automation / workflow by describing it — the description is
  // routed into the conversation so the brain ingests it. Connections also offer
  // the custom "build an integration on the fly" placeholder.
  function openDescribeModal(kind) {
    const meta = {
      role: { title: "Add a role", ph: "Describe the role — what it does, what a good job looks like, what it needs to access.", lead: "Tell bobi about the role and it'll add it to the team." },
      auto: { title: "Add an automation", ph: "Describe something the team should do on its own — e.g. 'post a daily digest at 9am' or 'when an email arrives, triage it'.", lead: "Describe the proactive behavior; bobi wires it up." },
      wf: { title: "Add a workflow", ph: "Describe the flow step by step — e.g. 'when an issue lands: triage it, write a fix, wait for my approval, open a PR'.", lead: "Describe the repeatable flow and where you want to approve; bobi codifies it as a workflow." },
    }[kind];
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "describe-ov";
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>${meta.title}</b><button class="btn ghost sm" id="d-close">Close</button></div>
      <div class="sp-body">
        <p class="fhelp">${meta.lead}</p>
        <label class="fld"><textarea id="d-text" rows="3" placeholder="${esc(meta.ph)}"></textarea></label>
        <div class="sp-actions"><button class="btn primary sm" id="d-send">Add</button></div>
      </div></div>`;
    document.body.appendChild(ov);
    $("#d-text").focus();
    ov.addEventListener("click", e => { if (e.target.id === "d-close" || e.target === ov) ov.remove(); });
    $("#d-send").addEventListener("click", () => {
      const t = $("#d-text").value.trim();
      if (!t) { toast("say a little about it"); return; }
      ov.remove(); sendMessage(t);
    });
  }
  // Add a connection = point bobi at an MCP server (the Claude-style
  // connector form). Two transports:
  //  - Remote: name + URL + an optional API key.
  //  - Local:  name + command (+ optional args + env var names) for a
  //            stdio/command-based server (e.g. a locally-installed MCP).
  // When the assistant guesses a connection is needed (a custom service like
  // PostHog), the row's Connect opens this prefilled with the name. (OAuth-authed
  // MCPs aren't supported yet — a follow-up; API key is the only remote auth.)
  function openMcpModal(prefill, existing) {
    const editing = !!(existing && existing.cfg);
    const ov = document.createElement("div");
    ov.className = "secret-ov"; ov.id = "mcp-ov";
    ov._editKey = editing ? existing.key : null;
    ov._editCfg = editing ? existing.cfg : null;
    ov.innerHTML = `<div class="secret-panel">
      <div class="sp-head"><b>${editing ? "Edit connection" : "Add a connection"}</b><button class="btn ghost sm" id="mcp-close">Close</button></div>
      <div class="sp-body">
        <p class="fhelp">Connect an MCP server — a remote URL, or a local command-based server installed on this machine.</p>
        <div class="seg" id="mcp-transport" style="margin-bottom:10px">
          <button class="seg-btn on" data-transport="http">Remote URL</button>
          <button class="seg-btn" data-transport="stdio">Local command</button>
        </div>
        <label class="fld"><span class="flab">Name</span>
          <input id="mcp-name" placeholder="e.g. PostHog" autocomplete="off" value="${esc(prefill || "")}"></label>
        <div id="mcp-http-fields">
          <label class="fld"><span class="flab">Remote server URL</span>
            <input id="mcp-url" placeholder="https://mcp.example.com/mcp" autocomplete="off"></label>
          <label class="fld"><span class="flab">API key <span class="fhelp inline">optional — only if the server needs one</span></span>
            <input id="mcp-key" type="password" placeholder="stored in .env, never sent to the model" autocomplete="off"></label>
        </div>
        <div id="mcp-stdio-fields" hidden>
          <label class="fld"><span class="flab">Project folder <span class="fhelp inline">point at the server's folder — we'll figure out the rest</span></span>
            <div class="mcp-detect-row">
              <input id="mcp-folder" placeholder="/path/to/the/mcp-server" autocomplete="off">
              <button class="btn ghost sm" id="mcp-detect" type="button">Detect</button>
            </div></label>
          <div id="mcp-detect-status"></div>
          <label class="fld"><span class="flab">Command</span>
            <input id="mcp-command" placeholder="e.g. substack-mcp" autocomplete="off"></label>
          <label class="fld"><span class="flab">Arguments <span class="fhelp inline">optional — space-separated</span></span>
            <input id="mcp-args" placeholder="--stdio --port 0" autocomplete="off"></label>
          <div id="mcp-detected-env"></div>
          <label class="fld"><span class="flab">More environment variables <span class="fhelp inline">optional — one per line, NAME or NAME=value</span></span>
            <textarea id="mcp-env" rows="2" placeholder="EXTRA_VAR=…" autocomplete="off"></textarea></label>
          <p class="fhelp">Any value you enter is stored in .env as a <code>\${NAME}</code> reference, never inline. Leave a value blank to set it later.</p>
        </div>
        <div class="sp-actions"><button class="btn primary sm" id="mcp-add">${editing ? "Save" : "Add"}</button></div>
        <div class="mcp-status" id="mcp-status"></div>
      </div></div>`;
    document.body.appendChild(ov);
    ov.addEventListener("click", e => { if (e.target.id === "mcp-close" || e.target === ov) ov.remove(); });
    const selectTab = name => ov.querySelectorAll("#mcp-transport .seg-btn").forEach(x => {
      const on = x.dataset.transport === name;
      x.classList.toggle("on", on);
      if (x.dataset.transport === "stdio") $("#mcp-stdio-fields").hidden = !on;
      if (x.dataset.transport === "http") $("#mcp-http-fields").hidden = (name === "stdio");
    });
    ov.querySelectorAll("#mcp-transport .seg-btn").forEach(b => b.addEventListener("click", () => {
      selectTab(b.dataset.transport);
      (b.dataset.transport === "stdio" ? $("#mcp-folder") : $("#mcp-url")).focus();
    }));
    $("#mcp-detect").addEventListener("click", () => mcpDetect(ov));
    $("#mcp-folder").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); mcpDetect(ov); } });
    $("#mcp-add").addEventListener("click", () => mcpAdd(ov));
    // Editing an existing connection: repopulate every field from the stored
    // config (secret VALUES are never sent to the client, so creds show as
    // "saved — leave blank to keep"; re-enter only to change them).
    if (editing) {
      const cfg = existing.cfg;
      $("#mcp-name").value = cfg.label || existing.key || "";
      const stdio = cfg.type === "stdio" || !!cfg.command;
      selectTab(stdio ? "stdio" : "http");
      if (stdio) {
        $("#mcp-command").value = cfg.command || "";
        $("#mcp-args").value = (cfg.args || []).map(shellQuote).join(" ");
        renderEnvRows((cfg.env_vars || []).map(v => ({
          name: v, secret: isSecretName(v), required: false,
          saved: (S.credentials_saved || []).includes(v),
        })), true);
      } else {
        $("#mcp-url").value = cfg.url || "";
        if (cfg.secret_var && (S.credentials_saved || []).includes(cfg.secret_var))
          $("#mcp-key").placeholder = "saved — leave blank to keep";
      }
      $("#mcp-name").focus();
    } else {
      (prefill ? $("#mcp-url") : $("#mcp-name")).focus();
    }
  }
  // A loose mirror of the backend secret heuristic, for rendering edit rows
  // (the server doesn't round-trip per-var secret flags).
  function isSecretName(name) {
    const up = (name || "").toUpperCase();
    if (/(_PATH|_DIR|_FILE|_URL|_URI|_HOST|_PORT|_ENDPOINT)$/.test(up)) return false;
    return /(COOKIE|TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE|CREDENTIAL|AUTH|ACCESS_KEY)/.test(up)
      || /_KEY$|_PAT$/.test(up) || up === "PAT";
  }
  // Shell-quote one arg so the space-joined string round-trips through the
  // server's shlex split (the detected --directory path often has spaces).
  function shellQuote(s) {
    return /[^A-Za-z0-9_\/.:=@%+-]/.test(s) ? "'" + s.replace(/'/g, "'\\''") + "'" : s;
  }
  // Scan a local folder and prefill the command / args / env-var fields.
  async function mcpDetect(ov) {
    // Accept a pasted path wrapped in quotes (copied from Finder/terminal) —
    // strip one layer and reflect the cleaned value back into the field.
    let path = ($("#mcp-folder").value || "").trim();
    if (path.length >= 2 && path[0] === path[path.length - 1] && (path[0] === "'" || path[0] === '"')) {
      path = path.slice(1, -1).trim();
      $("#mcp-folder").value = path;
    }
    const st = $("#mcp-detect-status");
    if (!path) { st.innerHTML = `<div class="mcp-err">enter a folder path</div>`; return; }
    st.innerHTML = `<div class="fhelp">Scanning ${esc(path)}…</div>`;
    const r = await postJSON("/api/mcp/detect", { path });
    if (!r.ok) { st.innerHTML = `<div class="mcp-err">${esc((r.data && r.data.error) || "couldn't detect")}</div>`; return; }
    const d = r.data;
    if (!($("#mcp-name").value || "").trim() && d.name) $("#mcp-name").value = d.name;
    $("#mcp-command").value = d.command || "";
    $("#mcp-args").value = (d.args || []).map(shellQuote).join(" ");
    renderEnvRows(d.env || []);
    const bits = [`<b>${esc(d.runtime || "detected")}</b> · ${(d.env || []).length} env var(s) found`];
    if (d.alt_scripts && d.alt_scripts.length)
      bits.push(`other entrypoints: ${d.alt_scripts.map(esc).join(", ")}`);
    (d.notes || []).forEach(n => bits.push("⚠ " + esc(n)));
    st.innerHTML = `<div class="mcp-detected-note">${bits.join(" · ")}</div>`;
  }
  // Render env vars as labelled, value-fillable rows. Secrets get a masked
  // input; required/optional, a "saved" flag, and any README hint show inline.
  // `alwaysKeep` marks rows that must persist even when left blank (editing an
  // existing connection: a blank value keeps the saved secret, but the var
  // declaration must survive).
  function renderEnvRows(env, alwaysKeep) {
    const box = $("#mcp-detected-env");
    if (!env || !env.length) { box.innerHTML = ""; return; }
    const heading = alwaysKeep ? "environment variables" : "detected environment variables";
    box.innerHTML = `<div class="flab" style="margin:6px 0">${heading}</div>` +
      env.map(e => {
        const badge = e.required ? `<span class="ebadge req">required</span>`
                                 : `<span class="ebadge opt">optional</span>`;
        const sec = e.secret ? `<span class="ebadge sec">secret</span>` : ``;
        const saved = e.saved ? `<span class="ebadge ok">saved</span>` : ``;
        const hint = e.hint ? `<span class="ehint">${esc(e.hint)}</span>` : ``;
        const ph = e.saved ? "leave blank to keep current"
          : (e.required ? "value (stored in .env)"
                        : "optional — blank uses the server's default");
        return `<div class="erow">
          <div class="erow-head"><code>${esc(e.name)}</code> ${badge}${sec}${saved}${hint}</div>
          <input class="erow-val" data-env="${esc(e.name)}" data-req="${e.required ? 1 : 0}"
                 data-keep="${alwaysKeep ? 1 : 0}"
                 type="${e.secret ? "password" : "text"}" autocomplete="off"
                 placeholder="${ph}"></div>`;
      }).join("");
  }
  // Parse the env textarea into [{name, value}] — one var per line, NAME=value
  // or a bare NAME (value supplied later). Splits on the FIRST '=' so values
  // that themselves contain '=' (e.g. a cookie string) survive intact.
  function parseEnvLines(text) {
    return (text || "").split("\n").map(l => l.trim()).filter(Boolean).map(l => {
      const i = l.indexOf("=");
      return i < 0 ? { name: l, value: "" }
                   : { name: l.slice(0, i).trim(), value: l.slice(i + 1).trim() };
    }).filter(e => e.name);
  }
  async function mcpAdd(ov) {
    const stdio = !!ov.querySelector("#mcp-transport .seg-btn.on[data-transport='stdio']");
    const replaces = ov._editKey || "";
    let payload;
    if (stdio) {
      // Keep a row if it has a value, is required, or is an edit row (data-keep:
      // its ${VAR} declaration must persist; a blank value keeps the saved
      // secret). Blank optional detected rows are dropped so the server's own
      // default isn't clobbered with an empty value.
      const detected = [...ov.querySelectorAll("#mcp-detected-env .erow-val")]
        .map(i => ({ name: i.dataset.env, value: i.value.trim(),
                     required: i.dataset.req === "1", keep: i.dataset.keep === "1" }))
        .filter(e => e.value || e.required || e.keep)
        .map(e => ({ name: e.name, value: e.value }));
      payload = {
        name: ($("#mcp-name").value || "").trim(),
        transport: "stdio",
        command: ($("#mcp-command").value || "").trim(),
        args: ($("#mcp-args").value || "").trim(),
        env: [...detected, ...parseEnvLines($("#mcp-env").value)],
        replaces,
      };
    } else {
      const key = (($("#mcp-key") || {}).value || "").trim();
      // Preserve api_key auth when editing without re-entering the key (a blank
      // field keeps the saved secret); don't silently downgrade to "none".
      const keepApiKey = !key && ov._editCfg && ov._editCfg.auth === "api_key";
      payload = {
        name: ($("#mcp-name").value || "").trim(),
        url: ($("#mcp-url").value || "").trim(),
        auth: (key || keepApiKey) ? "api_key" : "none",
        api_key: key,
        replaces,
      };
    }
    const r = await postJSON("/api/mcp/add", payload);
    if (!r.ok) {
      const st = $("#mcp-status");
      if (st) st.innerHTML = `<div class="mcp-err">${esc(r.data.error || "couldn't add")}</div>`;
      return;
    }
    if (r.data.state) S = r.data.state;
    _connData = await getJSON("/api/connect");
    ov.remove();
    renderUniCards();
    // Honest: nothing is verified here yet — the row says what's still needed.
    toast("Connection added");
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
    // Copy the command/prompt text sitting next to any [data-copycmd] button.
    const cc = e.target.closest("[data-copycmd]");
    if (cc) {
      const t = cc.closest(".cmd") && $(".cmd-text", cc.closest(".cmd"));
      if (t) navigator.clipboard.writeText(t.textContent)
        .then(() => toast("Copied."))
        .catch(() => toast("Copy failed — select the text manually."));
      return;
    }
    const nsgo = e.target.closest("[data-nsgo]");
    if (nsgo) { nsIndex = +nsgo.dataset.nsgo; drawNsStep(); return; }
    if (e.target.closest("[data-home]")) { goHome(); return; }
    if (e.target.closest("[data-back]")) { goBack(); return; }
    const go_ = e.target.closest("[data-go]");
    if (go_) { go(go_.dataset.go); return; }
    if (e.target.closest("[data-addteam]")) {
      atHome = false; welcomed = true; introFrom = "hub"; renderIntro();
      return;
    }
    if (e.target.closest("[data-introback]")) { introBack(); return; }
    if (e.target.closest("[data-importteam]")) { importTeam(); return; }
    const openteam = e.target.closest("[data-openteam]");
    if (openteam) {
      if (!openteam.disabled) {
        const path = openteam.dataset.openteam;
        atHome = false;
        startTeam({ mode: "open", location: path, team_path: path }, openteam, "Opening…");
      }
      return;
    }
    const newteam = e.target.closest("[data-newteam]");
    if (newteam) {
      if (!newteam.disabled)
        startTeam({ mode: "create", location: introLoc }, newteam, "Starting…");
      return;
    }
    const tmpl = e.target.closest("[data-template]");
    if (tmpl) {
      if (!tmpl.disabled) {
        const name = tmpl.dataset.template;
        // At the default location the server picks the template's own library
        // slot (agents/<name>/src). Appending the name to .../new-agent/src
        // here would bury the team one level deeper than the home scan reads.
        // A user-chosen folder is a container: the template lands in a child
        // named after it.
        const body = { mode: "registry", team: name };
        // slugify the child folder: the name comes from the registry (possibly
        // third-party) and must not steer the path (e.g. "../..").
        if (introLocChanged)
          body.location = introLoc.replace(/\/+$/, "") + "/" + (slugify(name) || "team");
        startTeam(body, tmpl, "Downloading…");
      }
      return;
    }
    const im = e.target.closest("[data-intromode]");
    if (im) { if (!im.disabled) { introMode = im.dataset.intromode; drawIntro(); } return; }
    const ro = e.target.closest("[data-roleopen]");
    if (ro) { openRoleModal(+ro.dataset.roleopen); return; }
    const ao = e.target.closest("[data-autoopen]");
    if (ao) { openAutoModal(+ao.dataset.autoopen); return; }
    const wo = e.target.closest("[data-wfopen]");
    if (wo) { openWorkflowModal(+wo.dataset.wfopen); return; }
    if (e.target.closest("[data-addrole]")) { openDescribeModal("role"); return; }
    if (e.target.closest("[data-addauto]")) { openDescribeModal("auto"); return; }
    if (e.target.closest("[data-addwf]")) { openDescribeModal("wf"); return; }
    if (e.target.closest("[data-addconn]")) { openMcpModal(); return; }
    const addmcp = e.target.closest("[data-addmcp]");
    if (addmcp) { openMcpModal(addmcp.dataset.addmcp); return; }
    const editmcp = e.target.closest("[data-editmcp]");
    if (editmcp) {
      const k = editmcp.dataset.editmcp;
      const cfg = ((S.spec && S.spec.mcp_servers) || {})[k];
      openMcpModal(null, cfg ? { key: k, cfg } : null);
      return;
    }
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
    if (e.target.closest("[data-ingressopen]")) { openIngressModal(); return; }
    const vs = e.target.closest("[data-vennsetup]");
    if (vs) { openVennSetup(); return; }
    if (e.target.closest("[data-vennconnect]")) { vennConnect(); return; }
    if (e.target.closest("[data-vennretry]")) { vennStep = "key"; drawVenn(); return; }
    if (e.target.closest("[data-vennapply]")) { vennApply(); return; }
    if (e.target.closest("[data-vennclose]")) { closeVenn(); return; }
  });
  // Picker checkboxes (Venn MCP selection) sync to the selected set on toggle.
  document.addEventListener("change", (e) => {
    const vp = e.target.closest("[data-vennpick]");
    if (vp) vennToggle(vp.dataset.vennpick, vp.checked, vp.closest(".venn-pick"));
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
      const r = await fetch(BASE + "/api/ping", { headers: H });
      if (r.ok) markConnected(); else markDisconnected();
    } catch { markDisconnected(); }
  }
  setInterval(heartbeat, 4000);

  boot();
})();
