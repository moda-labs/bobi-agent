/* bobbi setup — the front-end. Vanilla, no build, offline.
   ONE screen: an objective-guided conversation (left) while the team
   materializes as cards (right). The LLM serves the conversation (SSE) and the
   Build pour (SSE). Secrets are captured in dedicated on-demand components,
   never in the chat. Build/Done reuse the generating + done views. */
(() => {
  const NONCE = document.querySelector('meta[name="bobbi-nonce"]').content;
  const H = { "x-bobbi-nonce": NONCE };
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => (s || "").replace(/[&<>]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  // Grow a textarea to fit its content (wraps long text instead of scrolling
  // sideways), capped by max-height in CSS which then scrolls vertically.
  const autoGrow = (el) => { if (!el) return; el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; };
  const slugify = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);

  const GENERATING = new Set(["build", "review", "install"]);
  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4"><path d="M5 12l5 5L19 7"/></svg>';
  const STATUS_LABEL = { connected: "connected", missing: "connect", unknown: "needs check" };
  // Day-to-day channels for the Chat card.
  const CHANNELS = [
    { key: "cli", name: "Command line", soon: false },
    { key: "slack", name: "Slack", soon: false },
    { key: "telegram", name: "Telegram", soon: true },
  ];

  let S = null;            // latest serialized state
  let _connData = null;    // last /api/connect payload (drives connect cards)

  // --- connection state --------------------------------------------------
  // The page is useless without its local setup server. If that server dies
  // (Ctrl-C, closed terminal, crash) the UI must say so and stop pretending to
  // be live — every action would silently fail otherwise. A heartbeat plus
  // fetch-failure detection flips a blocking overlay; it clears itself if the
  // server comes back (e.g. `bobbi setup --resume`).
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
      <p>The local <code>bobbi setup</code> server stopped — closed, interrupted, or crashed. Nothing here works until it's back.</p>
      <div class="disc-cmd"><span class="pr">$</span> bobbi setup --resume</div>
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
  async function refresh() { S = await getJSON("/api/state"); render(); }
  async function go(stage) {
    const r = await postJSON("/api/advance", { to: stage });
    if (!r.ok) { toast(r.data.error || "can't go there yet"); return; }
    S = r.data; render();
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--surface);font-size:13px;padding:9px 15px;border-radius:8px;z-index:140;box-shadow:0 6px 20px -6px rgba(0,0,0,.4)";
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }
  function setPanes(cols) { $("#panes").style.gridTemplateColumns = cols; }

  // --- intro: create / modify existing / from a registry ----------------
  // Three ways in, all landing in the same chat+cards editor. Create authors
  // from scratch (auto-named in the chat); modify and registry reverse-fill an
  // existing team and edit it non-lossily.
  let introTeams = [], introRegistry = null, introBase = "bobbi",
      introScanDir = "", introMode = "create";
  async function renderIntro() {
    setPanes("1fr");
    const data = await getJSON("/api/intro");
    introTeams = data.teams || [];
    introBase = data.default_location || introBase;
    introScanDir = data.scan_dir || introBase;
    drawIntro();
  }
  function drawIntro() {
    $("#main").innerHTML = `<main class="node narrow intro">
      <div class="eyebrow">Setup</div>
      <h1>Build an agent team</h1>
      <p class="lede">Start fresh, modify a team you already have, or pull one from a registry. bobbi keeps the source in a folder you choose, then installs it into <code>.bobbi/</code> when you're done.</p>
      <div class="introtabs">
        <button class="itab ${introMode === "create" ? "on" : ""}" data-intromode="create">Create new</button>
        <button class="itab ${introMode === "open" ? "on" : ""}" data-intromode="open">Modify existing</button>
        <button class="itab ${introMode === "registry" ? "on" : ""}" data-intromode="registry">From a registry</button>
      </div>
      <div id="introbody"></div>
      <div class="actions"><button class="btn primary" id="introstart">Start →</button></div>
    </main>`;
    drawIntroBody();
    $("#introstart").addEventListener("click", introStart);
  }
  // A location field with a Browse button that opens the folder picker.
  function locFieldHTML(label, value) {
    return `<label class="ifield"><span>${esc(label)}</span>
      <div class="locrow"><input id="introloc" autocomplete="off" value="${esc(value)}">
        <button type="button" class="btn ghost xs" id="introbrowse">Browse…</button></div></label>`;
  }
  function wireBrowse() {
    const b = $("#introbrowse");
    if (b) b.addEventListener("click", () => openFolderPicker(p => {
      const loc = $("#introloc"); loc.value = p; loc.dataset.touched = "1";
    }));
  }
  // Modify's "which folder holds your teams?" — a scan dir, defaulting to the
  // library, that the user can point anywhere under home.
  function scanFieldHTML(value) {
    return `<label class="ifield"><span>Folder to scan for teams</span>
      <div class="locrow"><input id="introscan" autocomplete="off" value="${esc(value)}">
        <button type="button" class="btn ghost xs" id="introscanbrowse">Browse…</button></div></label>`;
  }
  function teamListHTML(teams) {
    if (!teams.length)
      return `<p class="ihint" id="iteams-empty">No teams in that folder. Point the scan at one that has them, or create a new team.</p>`;
    return teams.map((t, i) =>
      `<label class="iteam"><input type="radio" name="iteam" value="${esc(t.name)}" data-path="${esc(t.path)}" ${i === 0 ? "checked" : ""}>
        <b>${esc(t.name)}</b><span>${esc(t.path)}</span></label>`).join("");
  }
  async function rescan(dir) {
    const d = await getJSON("/api/teams?dir=" + encodeURIComponent(dir || ""));
    if (d.error) { toast(d.error); return; }
    introScanDir = d.dir || dir;
    introTeams = d.teams || [];
    drawOpenBody();
  }
  function drawOpenBody() {
    const el = $("#introbody");
    el.innerHTML = `
      ${scanFieldHTML(introScanDir)}
      <div class="iteams" id="iteams">${teamListHTML(introTeams)}</div>
      ${locFieldHTML("Edit this team in place (or point somewhere else to fork it)", (introTeams[0] || {}).path || "")}`;
    const loc = $("#introloc");
    const sync = () => { if (!loc.dataset.touched) { const r = document.querySelector("input[name=iteam]:checked"); loc.value = (r && r.dataset.path) || ""; } };
    el.querySelectorAll("input[name=iteam]").forEach(r => r.addEventListener("change", sync));
    loc.addEventListener("input", () => loc.dataset.touched = "1");
    // Rescan when the scan dir changes (Enter or blur), or via its own Browse.
    const scan = $("#introscan");
    const doScan = () => { const v = scan.value.trim(); if (v && v !== introScanDir) rescan(v); };
    scan.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); doScan(); } });
    scan.addEventListener("blur", doScan);
    $("#introscanbrowse").addEventListener("click", () =>
      openFolderPicker(p => { scan.value = p; rescan(p); }));
    wireBrowse();
  }
  function drawIntroBody() {
    const el = $("#introbody");
    if (introMode === "create") {
      el.innerHTML = `<p class="ihint">bobbi names the team for you as you describe what it should do — you can rename it any time. The team gets its own folder under here.</p>
        ${locFieldHTML("Where teams go (a folder you own — not .bobbi/)", introBase + "/")}`;
      $("#introloc").focus();
      wireBrowse();
    } else if (introMode === "open") {
      drawOpenBody();
    } else {  // registry
      if (introRegistry === null) {
        el.innerHTML = `<p class="ihint">Looking for teams in your registries…</p>`;
        loadRegistry();
        return;
      }
      if (!introRegistry.length) {
        el.innerHTML = `<p class="ihint">No registry teams found. Add one with <code>bobbi agents add-registry &lt;repo&gt;</code>, or create a team from scratch.</p>`;
        return;
      }
      el.innerHTML = `
        <div class="iteams">${introRegistry.map((t, i) =>
          `<label class="iteam"><input type="radio" name="rteam" value="${esc(t.name)}" ${i === 0 ? "checked" : ""}>
            <b>${esc(t.name)}</b><span>${esc(t.description || t.registry || "")}</span></label>`).join("")}</div>
        ${locFieldHTML("Download a copy to", `${introBase}/${introRegistry[0].name}`)}`;
      const loc = $("#introloc");
      const sync = () => { if (!loc.dataset.touched) { const r = document.querySelector("input[name=rteam]:checked"); loc.value = `${introBase}/${(r && r.value) || "team"}`; } };
      el.querySelectorAll("input[name=rteam]").forEach(r => r.addEventListener("change", sync));
      loc.addEventListener("input", () => loc.dataset.touched = "1");
      wireBrowse();
    }
  }
  async function loadRegistry() {
    const data = await getJSON("/api/registry");
    introRegistry = data.teams || [];
    if (introMode === "registry") drawIntroBody();
  }
  async function introStart() {
    const loc = (($("#introloc") || {}).value || "").trim();
    if (!loc) { toast("choose a location"); return; }
    const body = { mode: introMode, location: loc };
    if (introMode === "open") {
      const sel = document.querySelector("input[name=iteam]:checked");
      if (!sel) { toast("pick a team to modify (or scan a folder that has one)"); return; }
      body.team_path = sel.dataset.path || "";
    } else if (introMode === "registry") {
      body.team = (document.querySelector("input[name=rteam]:checked") || {}).value || "";
    }
    const start = $("#introstart");
    if (start) { start.disabled = true; start.textContent = introMode === "registry" ? "Downloading…" : "Starting…"; }
    const r = await postJSON("/api/start", body);
    if (!r.ok) {
      toast(r.data.error || "couldn't start");
      if (start) { start.disabled = false; start.textContent = "Start →"; }
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
        <div class="sketch-top"><span class="sketch-eyebrow">bobbi · build your team</span></div>
        <div class="ch-body" id="chbody"></div>
        <div class="cue" id="cue"></div>
        <div class="chips" id="chips"></div>
        <div class="ch-input"><textarea id="chinput" rows="1" placeholder="Tell bobbi what you want to build…" autocomplete="off"></textarea><button class="btn primary" id="chsend" style="padding:9px 14px">↑</button></div>
      </section>
      <aside class="uni-panel">
        <div class="uni-head"><span class="up-title" id="up-title" title="click to rename"></span><span class="up-meter" id="uni-meter"></span></div>
        <div class="uni-cards" id="uni-cards"></div>
        <div class="uni-foot" id="uni-foot"></div>
      </aside>`;
    renderMessages();
    updateCue();
    renderChips();
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

  // The five things bobbi gathers, each a card that fills in + checks off
  // live: goal, roles, automations, connections, chat.
  // The team's name shows in the panel header as bobbi auto-derives it; click
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
    const sp = S.spec;
    host.innerHTML = [goalCard(sp), rolesCard(sp), automationsCard(sp),
                      connectionsCard(sp), chatCard()].join("");
    // meter + Finish gate: Finish appears only once all five are gathered.
    const slotsEnough = ["goal", "roles", "autonomous", "services"]
      .filter(s => sp.readiness[s] === "enough").length;
    const gathered = slotsEnough + (S.chat ? 1 : 0);
    $("#uni-meter").textContent = `${gathered}/5 gathered`;
    const ready = gathered === 5;
    const foot = $("#uni-foot");
    if (foot) foot.innerHTML = ready
      ? `<button class="btn primary" data-go="build">Finish →</button>`
      : `<span class="uni-note">bobbi is gathering goal, roles, automations, connections, and chat</span>`;
  }
  function slotDot(ok) {
    return `<span class="udot ${ok ? "ok" : "empty"}">${ok ? CHECK : ""}</span>`;
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
      ? roles.map(r => `<div class="urole"><b>${esc(r.name || "role")}</b>${r.responsibility ? `<span>${esc(r.responsibility)}</span>` : ""}</div>`).join("")
      : `<span class="ph">bobbi will shape the roles as you talk</span>`;
    return `<div class="ucard ${roles.length ? "filled" : "empty"}">
      <div class="ut">Roles ${slotDot(sp.readiness.roles === "enough")}</div>
      <div class="ud">${body}</div></div>`;
  }
  function automationsCard(sp) {
    const items = sp.autonomous || [];
    const body = items.length
      ? items.map(a => `<div class="urole"><b>${esc(a.description || "behavior")}</b><span>${esc(a.leash || "")}${a.cadence ? " · " + esc(a.cadence) : ""}</span></div>`).join("")
      : (sp.autonomous_confirmed
          ? `<span class="ph">nothing proactive — bobbi acts only when asked</span>`
          : `<span class="ph">anything bobbi should do on its own?</span>`);
    return `<div class="ucard ${items.length || sp.autonomous_confirmed ? "filled" : "empty"}">
      <div class="ut">Automations ${slotDot(sp.readiness.autonomous === "enough")}</div>
      <div class="ud">${body}</div></div>`;
  }
  // Connections: native services capture a token each; Venn-backed services
  // share ONE key, so they're grouped under a single Venn setup with per-service
  // verification status. Reads live status from the cached /api/connect payload.
  function connectionsCard(sp) {
    const cards = (_connData && _connData.cards) || null;
    const ok = sp.readiness.services === "enough";
    let body;
    if (!cards) {
      const names = (sp.services || []).map(s => (s && s.name) || String(s));
      body = names.length
        ? names.map(n => `<div class="uconn"><span>${esc(n)}</span></div>`).join("")
        : `<span class="ph">what should the team connect to?</span>`;
    } else if (!cards.length) {
      body = `<span class="ph">no outside services — runs self-contained</span>`;
    } else {
      const native = cards.filter(c => c.kind !== "venn");
      const venn = cards.filter(c => c.kind === "venn");
      body = native.map(connRow).join("") + vennGroup(venn);
    }
    return `<div class="ucard ${(sp.services || []).length ? "filled" : "empty"}">
      <div class="ut">Connections ${slotDot(ok)}</div>
      <div class="ud">${body}</div></div>`;
  }
  function statusBadge(status) {
    if (status === "connected") return `<span class="cbadge ok">✓ connected</span>`;
    if (status === "unknown") return `<span class="cbadge">needs check</span>`;
    return `<span class="cbadge">pending</span>`;
  }
  function connRow(c) {
    const right = c.status === "connected"
      ? `${statusBadge("connected")} <button class="lnk" data-secretopen="${esc(c.key)}">edit</button>`
      : `<button class="btn ghost xs" data-secretopen="${esc(c.key)}">Connect</button>`;
    // Custom services (not native, not on Venn) get an authored API guide.
    const tag = c.kind === "custom"
      ? `<span class="ctag">custom · bobbi writes a guide</span>` : "";
    return `<div class="uconn"><span>${esc(c.name)}${tag}</span><span class="cright">${right}</span></div>`;
  }
  function vennGroup(venn) {
    if (!venn.length) return "";
    const keyIn = venn.some(c => (c.methods[0].secrets || []).some(s => s.present));
    const rows = venn.map(c =>
      `<div class="uconn sub"><span>${esc(c.name)}</span>${statusBadge(c.status)}</div>`).join("");
    return `<div class="uvenn">
      <div class="uvhead"><span><b>Venn</b> · one key, every service</span>
        <button class="btn ghost xs" data-vennsetup>${keyIn ? "Manage" : "Set up Venn"}</button></div>
      ${rows}</div>`;
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
  function renderChips() {
    const el = $("#chips"); if (!el) return;
    const chips = (S.suggestions || []).slice(0, 3);
    el.innerHTML = chips.map(c =>
      `<span class="chip" data-chip="${esc(c)}">+ ${esc(c)}</span>`).join("");
  }
  function renderMessages(extra) {
    const body = $("#chbody");
    if (!body) return;
    let html = "";
    if (!S.messages.length && !extra) {
      html = `<div class="msg bob">Hi — I'm bobbi. Tell me what you want this team to do, in your own words. Rough is fine; we'll sharpen it together.</div>`;
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
    const paint = () => { el.innerHTML = esc(buf.slice(0, shown)) + (shown < buf.length || !done ? caret : ""); $("#chbody").scrollTop = 1e9; };
    const tick = () => {
      if (shown < buf.length) { shown = Math.min(buf.length, shown + 2); paint(); }
      if (done && shown >= buf.length) { clearInterval(timer); timer = null; el.innerHTML = esc(buf); }
    };
    if (!reduce) timer = setInterval(tick, 16);
    return {
      push(t) { buf += t; if (reduce) { shown = buf.length; paint(); } },
      finish() {
        done = true;
        if (reduce || !timer) { el.innerHTML = esc(buf); return Promise.resolve(); }
        return new Promise(res => { const iv = setInterval(() => { if (!timer) { clearInterval(iv); res(); } }, 20); });
      },
    };
  }

  let streaming = false, pendingSend = null;
  async function sendMessage(text) {
    const input = $("#chinput");
    const msg = (typeof text === "string" ? text : (input ? input.value : "")).trim();
    if (!msg) return;
    // You can keep typing (and queue another message) while bobbi is replying.
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
    // Refresh WITHOUT rebuilding the input (preserve anything typed mid-stream).
    renderMessages();
    updateCue();
    renderChips();
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
    const svcs = venn.map(c =>
      `<div class="uconn sub"><span>${esc(c.name)}</span>${statusBadge(c.status)}</div>`).join("");
    const editing = editSecrets.has("VENN_API_KEY");
    const keyField = (keyIn && !editing)
      ? `<div class="secret-saved">✓ Venn key saved
          <button class="lnk" data-secretedit="VENN_API_KEY">Edit</button>
          <button class="lnk" data-secretcopy="VENN_API_KEY">Copy</button></div>`
      : `<label class="secret"><span class="slabel">Venn API key${keyIn ? " · re-enter to replace" : ""}</span>
          <input type="password" id="venn-key" placeholder="venn_…" autocomplete="off">
          <span class="shelp">One key unlocks every Venn service below.</span></label>`;
    body.innerHTML = `
      <p class="pd" style="margin-bottom:10px">One Venn key covers all of these. Connect each service in Venn, then paste the key once — bobbi verifies which are live.</p>
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
  let building = false;
  function renderGenerating() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="node narrow">
      <div class="eyebrow">Building</div>
      <h1>Building ${esc(S.team_name || "your team")}</h1>
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
    const fill = () => { const el = $("#genfill"); if (el) el.style.width = Math.min(95, +(el.dataset.p || 0) + 12) + "%", el.dataset.p = Math.min(95, +(el.dataset.p || 0) + 12); };
    try {
      await sse("/api/build", {}, {
        file_start: (e) => { genAddFile(e.path, false); $("#genmsg").innerHTML = `writing <b>${esc(e.path)}</b>…`; },
        file_end: (e) => { genAddFile(e.path, true); fill(); },
        delta: () => {},
        error: (e) => { throw new Error(e.message || "build failed"); },
        state: (st) => { S = st; },
      });
      $("#genmsg").textContent = "Checking it over…";
      const v = await postJSON("/api/validate", {});
      S = v.data.state || S;
      if (!v.data.passed) return buildFailed("Validation found problems.", v.data.report);
      $("#genmsg").textContent = "Installing…";
      const r = await postJSON("/api/install", {});
      S = r.data.state || S;
      if (!r.ok) return buildFailed(r.data.error || "Install failed.");
      if ($("#genfill")) $("#genfill").style.width = "100%";
      building = false;
      await go("done");
    } catch (err) {
      buildFailed(String(err.message || err));
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
    return `<p class="lede">Talk to it from the terminal: <code>bobbi ask "what's the status?"</code></p>`;
  }
  // The post-build screen IS a built-in file browser: a success banner, the
  // generated team's files (tree + contents) read live from disk, a button to
  // open the real folder on your machine, and Finish.
  async function renderDone() {
    setPanes("1fr");
    const spec = S.spec;
    const where = S.source_dir || "agents/" + S.team_name;
    const counts = `${spec.roles.length || 1} role(s) · ${spec.autonomous.length} automation(s) · ${spec.services.length} service(s)`;
    $("#main").innerHTML = `<main class="filesdone">
      <header class="fd-head">
        <div class="fd-seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L19 7"/></svg></div>
        <div class="fd-title">
          <div class="eyebrow">Done · here's what bobbi built</div>
          <h1>${esc(S.team_name || "your team")} is ready</h1>
          <p class="fd-meta">${counts} · source at <code>${esc(where)}</code> · installed into <code>.bobbi/</code></p>
        </div>
        <div class="fd-actions">
          <button class="btn ghost" id="fd-reveal">Open folder</button>
          <button class="btn primary" id="fd-finish">Finish</button>
        </div>
      </header>
      <div class="fd-body" id="fd-body"></div>
      <div class="fd-foot"><span class="fd-run"><span class="pr">$</span> bobbi start</span> ${talkHint()}</div>
    </main>`;

    $("#fd-reveal").addEventListener("click", async () => {
      const r = await postJSON("/api/reveal", {});
      toast(r.ok ? "Opened the team folder." : (r.data.error || "couldn't open the folder"));
    });
    $("#fd-finish").addEventListener("click", async () => {
      $("#fd-finish").disabled = true;
      _finished = true;   // the server is about to stop on purpose — not a disconnect
      try { await postJSON("/api/finish", {}); } catch { /* server stopped mid-response */ }
      renderFinished();
    });

    const data = await getJSON("/api/files");
    const files = data.files || [];
    const body = $("#fd-body");
    if (!files.length) {
      body.innerHTML = `<div class="fd-empty">
        <p>No files found at <code>${esc(where)}</code>.</p>
        <p class="fd-sub">The build may not have finished writing — go back and try again.</p>
        <button class="btn ghost" data-go="design">Back to editing</button></div>`;
      return;
    }
    body.innerHTML = `<nav class="tree" id="fd-tree"></nav>
      <section class="slab"><div class="slabbar"><span class="s" id="fd-name"></span></div>
        <div class="code" id="fd-code"></div></section>`;
    let cur = files[0];
    const drawTree = () => { $("#fd-tree").innerHTML = files.map(p =>
      `<div class="tnode file ${p === cur ? "sel" : ""}" data-ifile="${esc(p)}"><span class="ok">✓</span>${esc(p)}</div>`).join(""); };
    const openF = async (p) => { cur = p; drawTree(); $("#fd-name").innerHTML = `<b>${esc(p)}</b>`;
      const d = await getJSON("/api/file?path=" + encodeURIComponent(p)); $("#fd-code").textContent = d.content || ""; };
    drawTree(); await openF(cur);
    $("#fd-tree").addEventListener("click", (e) => {
      const f = e.target.closest("[data-ifile]"); if (f) openF(f.dataset.ifile);
    });
  }

  // Final screen after Finish. The local setup server has stopped, so this is
  // intentionally static — no buttons that would need the (now-gone) server.
  function renderFinished() {
    setPanes("1fr");
    $("#main").innerHTML = `<main class="done-wrap">
      <div class="seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L19 7"/></svg></div>
      <div class="eyebrow">All set</div>
      <h1>${esc(S.team_name || "your team")} is ready</h1>
      <p class="lede">Installed into <code>.bobbi/</code>. We are legion.</p>
      ${talkHint()}
      <p class="lede" style="margin-top:18px">Start it whenever you're ready:</p>
      <div class="cmd"><span class="pr">$</span> bobbi start</div>
      <p class="lede" style="font-size:13px;color:var(--faint)">Setup is complete and this local server has stopped — you can close this tab.</p>
    </main>`;
  }

  // --- top-level render + events ----------------------------------------
  function render() {
    const st = S.stage;
    if (st === "start") { renderIntro(); return; }
    if (GENERATING.has(st)) { renderGenerating(); return; }
    if (st === "done") { renderDone(); return; }
    renderUnified();            // design + editing (the one screen)
  }

  document.addEventListener("click", (e) => {
    const go_ = e.target.closest("[data-go]");
    if (go_) { go(go_.dataset.go); return; }
    const im = e.target.closest("[data-intromode]");
    if (im) { if (!im.disabled) { introMode = im.dataset.intromode; drawIntro(); } return; }
    const chip = e.target.closest("[data-chip]");
    if (chip) { sendMessage(chip.dataset.chip); return; }
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

  refresh();
})();
