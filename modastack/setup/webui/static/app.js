/* bobbi setup — the wizard front-end. Vanilla, no build, offline.
   Drives the mode-aware stage machine off /api/state; the LLM serves the
   Design conversation (SSE) and the Build pour (SSE). Files surface only at
   Build (watch) and Review (edit). */
(() => {
  const NONCE = document.querySelector('meta[name="bobbi-nonce"]').content;
  const H = { "x-bobbi-nonce": NONCE };
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => (s || "").replace(/[&<>]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

  // Perceived steps shown in the rail. Build/Review/Install collapse into the
  // generating transition (they map to "done"); Start lands straight in Design.
  const PERCEIVED = ["design", "automate", "connect", "chat", "done"];
  const LABELS = {
    design: "Design", automate: "Automate", connect: "Connect",
    chat: "Chat", done: "Done",
  };
  const TO_PERCEIVED = {
    start: "design", design: "design", automate: "automate",
    connect: "connect", chat: "chat",
    build: "done", review: "done", install: "done", done: "done",
  };
  const GENERATING = new Set(["build", "review", "install"]);
  const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4"><path d="M5 12l5 5L19 7"/></svg>';

  let S = null;            // latest serialized state

  // --- api helpers -------------------------------------------------------
  async function getJSON(path) {
    const r = await fetch(path, { headers: H });
    return r.json();
  }
  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { ...H, "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
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
    const res = await fetch(path, {
      method: "POST", headers: { ...H, "content-type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
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
    t.style.cssText = "position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--surface);font-size:13px;padding:9px 15px;border-radius:8px;z-index:99;box-shadow:0 6px 20px -6px rgba(0,0,0,.4)";
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  // --- rail --------------------------------------------------------------
  function renderRail() {
    const cur = TO_PERCEIVED[S.stage] || "design";
    const i = PERCEIVED.indexOf(cur);
    const gen = GENERATING.has(S.stage);
    $("#counter").textContent =
      String(i + 1).padStart(2, "0") + " / " + String(PERCEIVED.length).padStart(2, "0");
    $("#steps").innerHTML = PERCEIVED.map((id, idx) => {
      let cls = idx < i ? "done" : idx === i ? "current" : "todo";
      // "done" can't be reached until there's a goal to build from.
      if (id === "done" && idx > i && !S.spec.goal.trim()) cls += " blocked";
      const dot = idx < i ? `<span class="dot">${CHECK}</span>`
        : idx === i ? (gen ? '<span class="dot"><span class="spin"></span></span>'
                          : '<span class="dot"><i></i></span>')
          : '<span class="dot"></span>';
      // Done isn't a click target; you reach it by building.
      const go = id === "done" ? "" : ` data-go="${id}"`;
      return `<div class="step ${cls}"${go}>${dot}${LABELS[id]}</div>`;
    }).join("");
    const team = S.team_name || "your team";
    $("#hint").innerHTML = `Building <b>${esc(team)}</b> locally. Nothing leaves this machine.`;
  }

  // --- per-stage views ---------------------------------------------------
  function setPanes(cols) { $("#panes").style.gridTemplateColumns = cols; }

  const views = {
    design() {
      setPanes("216px 1fr");
      const cue = readinessCue();
      $("#main").innerHTML = `<main class="node" style="padding:0;overflow:hidden;display:flex;">
        <section class="chat sketch">
          <div class="sketch-top"><span class="sketch-eyebrow">Design · describe your team</span></div>
          <div class="ch-body" id="chbody"></div>
          <div class="cue ${cue.cls}" id="cue">${cue.text}</div>
          <div id="readygo" class="readygo"></div>
          <div class="chips">
            <span class="chip" data-chip="It should also handle PR reviews.">+ also handle PR reviews</span>
            <span class="chip" data-chip="Post a daily summary of what changed.">+ post a daily summary</span>
            <span class="chip" data-chip="Tell me when something needs my attention.">+ flag things for me</span>
          </div>
          <div class="ch-input"><input id="chinput" placeholder="Tell bobbi what you want to build…" autocomplete="off"><button class="btn primary" id="chsend" style="padding:9px 14px">↑</button></div>
        </section></main>`;
      renderMessages();
      updateReadyGo();
      const input = $("#chinput");
      input.focus();
      input.addEventListener("keydown", e => { if (e.key === "Enter") sendMessage(); });
      $("#chsend").addEventListener("click", () => sendMessage());
    },

    automate() { renderAutomate(); },
    connect() { renderConnect(); },
    chat() { renderChat(); },
    generating() { renderGenerating(); },

    done() {
      setPanes("216px 1fr");
      const spec = S.spec;
      const counts = `${spec.roles.length || 1} role(s) · ${spec.autonomous.length} automation(s) · ${spec.services.length} service(s)`;
      $("#main").innerHTML = `<main class="done-wrap">
        <div class="seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 12l5 5L19 7"/></svg></div>
        <div class="eyebrow">Done</div>
        <h1>team bobbi is ready</h1>
        <p class="lede">${esc(S.team_name)} is installed. We are legion.</p>
        ${talkHint()}
        <p class="lede" style="margin-top:18px">Start it whenever you're ready:</p>
        <div class="cmd"><span class="pr">$</span> bobbi start</div>
        <p class="lede" style="font-size:13px">${counts} · written to <code>.bobbi/</code></p>
        <div class="actions"><button class="btn ghost" id="viewfiles">View files</button><button class="btn primary" id="finishbtn">Finish</button></div>
      </main>`;
      $("#viewfiles").addEventListener("click", openInspector);
      $("#finishbtn").addEventListener("click", async () => {
        await postJSON("/api/finish", {});
        toast("Setup complete — you can close this tab.");
      });
    },
  };

  // --- design helpers ----------------------------------------------------
  function readinessCue() {
    const r = S.spec.readiness.goal;
    if (r === "enough") return { cls: "enough", text: "got it ✓ — clear enough to build whenever you are" };
    if (r === "thin") return { cls: "", text: "taking shape — tell me a bit more, or move on anytime" };
    return { cls: "", text: "say what you want this team to do" };
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
  // The inline "ready to move on" affordance — appears between the chat and
  // the input once bobbi judges the goal clear enough. The rail stays the
  // always-available path; this is the gentle nudge, never a wall.
  function updateReadyGo() {
    const el = $("#readygo");
    if (!el) return;
    el.innerHTML = S.spec.readiness.goal === "enough"
      ? `<button class="btn primary" data-go="automate">This is the team — keep going →</button>`
      : "";
  }
  // A typewriter that reveals buffered text character-by-character regardless
  // of how chunky the network deltas are (the SDK streams in blocks). Honors
  // reduced-motion by rendering instantly.
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
    if (streaming) { pendingSend = msg; if (input && typeof text !== "string") input.value = ""; return; }
    if (input && typeof text !== "string") input.value = "";
    streaming = true;
    S.messages.push({ role: "user", content: msg });
    renderMessages('<div class="msg bob" id="streambob"><span class="caret"></span></div>');
    const tw = makeTypewriter($("#streambob"));
    await sse("/api/message", { text: msg }, {
      redacted: () => toast("Scrubbed a secret from that message — add credentials in Connect, not the chat."),
      delta: (d) => tw.push(d.text),
      error: (d) => toast(d.message || "something broke"),
      state: (st) => { S = st; },
    });
    await tw.finish();
    streaming = false;
    // Refresh WITHOUT rebuilding the input (preserve anything typed mid-stream).
    renderMessages();
    const cue = readinessCue();
    if ($("#cue")) { $("#cue").textContent = cue.text; $("#cue").className = "cue " + cue.cls; }
    updateReadyGo();
    renderRail();
    if (pendingSend) { const m = pendingSend; pendingSend = null; sendMessage(m); }
  }

  // --- automate ----------------------------------------------------------
  let autoItems = [];   // [{description, leash, cadence, on}]
  async function renderAutomate() {
    setPanes("216px 1fr");
    $("#main").innerHTML = `<main class="node narrow">
      <div class="eyebrow">Automate · bobbi's ideas</div>
      <h1>Anything you want bobbi to do on its own?</h1>
      <p class="lede">Without you asking. From what you sketched, bobbi came up with a few — switch on what's useful, set how much leash each gets, or skip the lot. You're always in control.</p>
      <div id="autobody"><p class="lede"><span class="spin"></span> bobbi is thinking…</p></div>
      <div class="actions"><button class="btn ghost" data-go="design">Back</button><button class="btn primary" id="autonext">Next: Connect →</button><span class="note">nothing? just skip →</span></div>
    </main>`;
    $("#autonext").addEventListener("click", commitAutomate);
    // seed from already-committed behaviors, else ask the suggester
    if (S.spec.autonomous.length) {
      autoItems = S.spec.autonomous.map(b => ({ ...b, on: true }));
      drawAuto();
    } else {
      const r = await postJSON("/api/automate/suggest", {});
      autoItems = (r.data.suggestions || []).map(s => ({ ...s, on: true }));
      drawAuto();
    }
  }
  function drawAuto() {
    const leashWord = { notify: "bobbi tells you — you act.", ask: "bobbi asks before acting.", act: "bobbi just does it." };
    const body = $("#autobody");
    if (!autoItems.length) {
      body.innerHTML = `<p class="lede">No proactive behaviors — that's a fine answer. You can add them later.</p>`;
      return;
    }
    body.innerHTML = `<div class="cardgrid one">` + autoItems.map((it, idx) => `
      <div class="pcard ${it.on ? "sel" : "off"}" data-toggle="${idx}">
        <div class="pt">${esc(it.description)}<span class="meta ${it.on ? "" : "muted"}">${esc(it.cadence || "")}</span></div>
        <div class="pd">${esc(it.rationale || "")} <b style="color:var(--muted);font-weight:500">${leashWord[it.leash] || ""}</b></div>
        <div class="leash" data-leash="${idx}" style="margin-top:10px">
          ${["notify", "ask", "act"].map(l => `<button class="${it.leash === l ? "on" : ""}" data-set-leash="${idx}:${l}">${l}</button>`).join("")}
        </div>
      </div>`).join("") + `</div>`;
  }
  async function commitAutomate() {
    const behaviors = autoItems.filter(it => it.on)
      .map(({ description, leash, cadence }) => ({ description, leash, cadence }));
    await postJSON("/api/automate", { behaviors });
    await go("connect");
  }

  // --- connect -----------------------------------------------------------
  async function renderConnect() {
    setPanes("216px 1fr");
    $("#main").innerHTML = `<main class="node narrow">
      <div class="eyebrow">Connect</div>
      <h1>Give the team its tools</h1>
      <p class="lede">From what you sketched, bobbi needs these. Connect what you want — skip any and add them later; auth never blocks the build.</p>
      <div id="connbody"><p class="lede"><span class="spin"></span> checking connections…</p></div>
      <div class="actions"><button class="btn ghost" data-go="automate">Back</button><button class="btn primary" data-go="chat">Next: Chat →</button><span class="note">connect later is fine →</span></div>
    </main>`;
    drawConnect(await getJSON("/api/connect"));
  }
  function drawConnect(data) {
    const cards = data.cards || [];
    const body = $("#connbody");
    if (!cards.length) {
      body.innerHTML = `<p class="lede">No outside services needed — this team runs self-contained.</p>`;
      return;
    }
    body.innerHTML = `<div class="cardgrid one">` + cards.map(c => {
      const pill = `<span class="status-pill ${c.status}">· ${c.status}</span>`;
      const scopes = (c.scopes || []).map(s => `<b>${esc(s)}</b>`).join("");
      let connect = "";
      if (c.status !== "connected" && c.credential_var) {
        connect = `<div class="credrow">
          <input placeholder="${esc(c.credential_var)}" data-cred="${esc(c.credential_var)}" data-svc="${esc(c.key)}" type="password">
          <button class="btn primary" style="padding:8px 13px" data-connect="${esc(c.key)}">Connect</button></div>`;
      } else if (c.status !== "connected") {
        connect = `<div class="pd" style="margin-top:9px;color:var(--faint)">Authorize via Venn — <code>bobbi</code> opens it for you, or connect later.</div>`;
      }
      return `<div class="pcard">
        <div class="pt">${esc(c.name)} ${pill}</div>
        <div class="pd">${esc(c.summary)}</div>
        <div class="scopes">${scopes}</div>${connect}</div>`;
    }).join("") + `</div>`;
  }
  async function connectCredential(key) {
    const row = document.querySelector(`[data-connect="${key}"]`).closest(".credrow");
    const input = $("input", row);
    const r = await postJSON("/api/credential", {
      var_name: input.dataset.cred, service: input.dataset.svc, value: input.value,
    });
    if (!r.ok) { toast(r.data.error || "couldn't save"); return; }
    toast(r.data.saved ? `${input.dataset.cred} saved` : "skipped");
    drawConnect(await getJSON("/api/connect"));
  }

  // --- chat (how you talk to the team) -----------------------------------
  const CHANNELS = [
    { key: "cli", name: "Command line", desc: "Talk to your team from the terminal with <code>bobbi ask</code> — nothing to set up.", cred: "" },
    { key: "slack", name: "Slack", desc: "Message the team in a Slack channel or DM; it replies in thread.", cred: "SLACK_BOT_TOKEN" },
    { key: "telegram", name: "Telegram", desc: "Coming soon.", cred: "", soon: true },
  ];
  async function renderChat() {
    setPanes("216px 1fr");
    const chosen = S.chat || "cli";
    $("#main").innerHTML = `<main class="node narrow">
      <div class="eyebrow">Chat</div>
      <h1>How will you talk to your team?</h1>
      <p class="lede">Once it's running, this is how you reach it day to day. You can change it later.</p>
      <div class="cardgrid one">${CHANNELS.map(ch => `
        <div class="pcard ${ch.soon ? "off" : ch.key === chosen ? "sel" : ""}" ${ch.soon ? "" : `data-channel="${ch.key}"`}>
          <div class="pt">${ch.name}${ch.key === chosen && !ch.soon ? ' <span class="meta">· selected</span>' : ""}</div>
          <div class="pd">${ch.desc}</div>
          <div class="chsetup" id="chsetup-${ch.key}"></div>
        </div>`).join("")}</div>
      <div class="actions"><button class="btn ghost" data-go="connect">Back</button><button class="btn primary" data-go="build">Build my team →</button></div>
    </main>`;
    drawChatSetup(chosen);
  }
  function drawChatSetup(chosen) {
    const ch = CHANNELS.find(c => c.key === chosen);
    const el = $(`#chsetup-${chosen}`);
    if (!el || !ch || !ch.cred) return;
    el.innerHTML = `<div class="credrow"><input type="password" placeholder="${ch.cred}" data-cred="${ch.cred}" data-svc="${ch.key}">
      <button class="btn primary" style="padding:8px 13px" data-savecred="${ch.cred}">Save token</button></div>`;
  }
  async function chooseChannel(key) {
    const r = await postJSON("/api/chat", { channel: key });
    if (r.ok) { S = r.data; renderChat(); }
  }
  function talkHint() {
    if (S.chat === "slack") return `<p class="lede">Talk to it in Slack — message the bot in your channel.</p>`;
    if (S.chat === "telegram") return `<p class="lede">Talk to it in Telegram.</p>`;
    return `<p class="lede">Talk to it from the terminal: <code>bobbi ask "what's the status?"</code></p>`;
  }

  // --- generating (Build + validate + install, collapsed) ----------------
  let building = false;
  async function renderGenerating() {
    setPanes("216px 1fr");
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
      installMissing = r.data.missing_credentials || [];
      if ($("#genfill")) $("#genfill").style.width = "100%";
      building = false;
      await go("done");
    } catch (err) {
      buildFailed(String(err.message || err));
    }
  }
  let installMissing = [];
  function buildFailed(msg, report) {
    building = false;
    const el = $("#generr");
    if (!el) return;
    el.innerHTML = `<div class="banner err">${esc(msg)}${report ? `<pre>${esc(report)}</pre>` : ""}</div>
      <div class="actions"><button class="btn ghost" data-go="chat">Back</button><button class="btn primary" id="retrybuild">Try again</button></div>`;
    $("#retrybuild").addEventListener("click", () => { $("#generr").innerHTML = ""; runBuildFlow(); });
  }

  // --- file inspector (read-only overlay, reachable from Done) ------------
  async function openInspector() {
    const data = await getJSON("/api/files");
    const files = data.files || [];
    let cur = files[0];
    const ov = document.createElement("div");
    ov.className = "inspector";
    ov.innerHTML = `<div class="insp-panel">
      <div class="insp-head">agents/${esc(S.team_name)}/ <button class="btn ghost" id="insp-close" style="padding:5px 11px">Close</button></div>
      <div class="insp-body"><nav class="tree"><div id="insp-tree"></div></nav>
        <section class="slab"><div class="corners"><i></i><i></i><i></i><i></i></div>
          <div class="slabbar"><div class="s" id="insp-name"></div></div>
          <div class="code" id="insp-code"></div></section></div></div>`;
    document.body.appendChild(ov);
    const drawTree = () => { $("#insp-tree").innerHTML = files.map(p =>
      `<div class="tnode file ${p === cur ? "sel" : ""}" data-ifile="${esc(p)}"><span class="ok">✓</span>${esc(p)}</div>`).join(""); };
    const openF = async (p) => { cur = p; drawTree(); $("#insp-name").innerHTML = `<b>${esc(p)}</b>`;
      const d = await getJSON("/api/file?path=" + encodeURIComponent(p)); $("#insp-code").textContent = d.content || ""; };
    drawTree(); if (cur) await openF(cur);
    ov.addEventListener("click", (e) => {
      if (e.target.id === "insp-close" || e.target === ov) { ov.remove(); return; }
      const f = e.target.closest("[data-ifile]"); if (f) openF(f.dataset.ifile);
    });
  }

  async function saveCredInline(varName) {
    const input = document.querySelector(`input[data-cred="${varName}"]`);
    const r = await postJSON("/api/credential", { var_name: varName, value: input.value, service: input.dataset.svc || "" });
    if (r.ok && r.data.saved) { input.disabled = true; toast(`${varName} saved`); }
    else toast(r.data.error || "skipped");
  }

  // --- top-level render + events ----------------------------------------
  function render() {
    renderRail();
    const st = S.stage;
    if (st === "automate") views.automate();
    else if (st === "connect") views.connect();
    else if (st === "chat") views.chat();
    else if (GENERATING.has(st)) views.generating();
    else if (st === "done") views.done();
    else views.design();            // start + design
  }

  document.addEventListener("click", (e) => {
    const go_ = e.target.closest("[data-go]");
    if (go_) { go(go_.dataset.go); return; }
    const chip = e.target.closest("[data-chip]");
    if (chip) { sendMessage(chip.dataset.chip); return; }
    const tg = e.target.closest("[data-toggle]");
    if (tg) { const i = +tg.dataset.toggle; autoItems[i].on = !autoItems[i].on; drawAuto(); return; }
    const sl = e.target.closest("[data-set-leash]");
    if (sl) { const [i, l] = sl.dataset.setLeash.split(":"); autoItems[+i].leash = l; drawAuto(); return; }
    const ch = e.target.closest("[data-channel]");
    if (ch) { chooseChannel(ch.dataset.channel); return; }
    const cn = e.target.closest("[data-connect]");
    if (cn) { connectCredential(cn.dataset.connect); return; }
    const sc = e.target.closest("[data-savecred]");
    if (sc) { saveCredInline(sc.dataset.savecred); return; }
    const sw = e.target.closest("[data-accent-set]");
    if (sw) {
      document.documentElement.dataset.accent = sw.dataset.accentSet;
      document.querySelectorAll(".sw").forEach(x => x.classList.remove("sel"));
      sw.classList.add("sel"); return;
    }
    if (e.target.closest("#retro")) {
      const h = document.documentElement;
      h.dataset.retro = h.dataset.retro === "on" ? "off" : "on";
    }
  });

  refresh();
})();
