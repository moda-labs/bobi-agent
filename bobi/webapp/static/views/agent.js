/* Agent view — one agent's dashboard: run controls in the header, the
   subagent roster (left), and chat (right). Roster/chat ported from the
   standalone agentui SPA, routed under #/agents/<name> with team-scoped
   endpoints. */

import { openSetup, fmtUsd, fmtSpend, fmtTok, EST_NOTE, healthChip,
         fmtAgo } from "../shell.js";

export function mountAgent(el, { api, name }) {
  const base = "/api/agents/" + encodeURIComponent(name);

  el.innerHTML = "";

  const head = document.createElement("div");
  head.className = "agent-ctl";
  head.innerHTML = `
    <span class="agent-ctl-name">${name.replace(/[&<>]/g, "")}</span>
    <span class="status" data-el="runStatus">…</span>
    <span class="ctl-spacer"></span>
    <div class="agent-ctl-actions" data-el="actions"></div>
    <button class="btn" data-el="edit" type="button">Edit source</button>`;
  el.appendChild(head);

  const shell = document.createElement("div");
  shell.className = "shell";
  shell.innerHTML = `
    <aside class="sidebar">
      <div class="health-panel" data-el="healthPanel" hidden></div>
      <div class="spend-panel" data-el="spendPanel" hidden></div>
      <div class="side-head mono"><a class="side-back" href="#/">&larr; agents</a> · subagents</div>
      <div class="cards" data-el="cards"></div>
      <p class="empty" data-el="empty" hidden>No active subagents. Press Start
        above to bring the team up.</p>
      <div class="sessions-panel" data-el="sessionsPanel" hidden>
        <div class="sessions-head mono"><span>session log</span>
          <span class="sessions-counts" data-el="sessionCounts"></span></div>
        <div data-el="sessionRows"></div>
      </div>
    </aside>
    <section class="pane">
      <div class="placeholder" data-el="placeholder">
        <span class="mark big" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="34" height="34" fill="none"
               stroke="currentColor" stroke-width="1.4" stroke-linecap="round"
               stroke-linejoin="round">
            <circle cx="12" cy="12" r="3.2"></circle>
            <path d="M12 2.4v3M12 18.6v3M2.4 12h3M18.6 12h3"></path>
            <path d="M5.2 5.2l2.1 2.1M16.7 16.7l2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1"></path>
          </svg>
        </span>
        <p>Select a subagent to start chatting.</p>
      </div>
      <div class="chat" data-el="chat" hidden>
        <div class="chat-head">
          <span class="chat-name" data-el="chatName"></span>
          <span class="chip" data-el="chatRole"></span>
          <span class="chip" data-el="chatStatus"></span>
        </div>
        <div class="slab" data-el="transcript" aria-live="polite"></div>
        <p class="chat-ended mono" data-el="chatEnded" hidden></p>
        <form class="composer" data-el="composer">
          <textarea data-el="input" rows="1" placeholder="Message this agent…"
                    autocomplete="off"></textarea>
          <button data-el="send" type="submit">Send</button>
        </form>
      </div>
    </section>`;
  el.appendChild(shell);

  const els = {};
  for (const n of el.querySelectorAll("[data-el]")) {
    els[n.dataset.el] = n;
  }

  // --- run controls (header) -----------------------------------------
  let runState = null;   // last /status payload
  let busyVerb = null;   // "starting" | "stopping" | "restarting" while acting

  function renderControls() {
    // Same worst-signal-wins derivation as the dashboard cards (#733):
    // hosted /status cards carry reachability + manager_status.
    const chip = busyVerb ? { label: busyVerb, cls: "starting" }
                          : healthChip(runState);
    els.runStatus.className = "status " + chip.cls;
    els.runStatus.textContent = chip.label;

    els.actions.innerHTML = "";
    const btn = (label, kind, onClick, disabled) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn " + kind;
      b.textContent = label;
      b.disabled = !!disabled;
      if (onClick) b.addEventListener("click", onClick);
      els.actions.appendChild(b);
    };
    if (busyVerb) {
      btn(busyVerb + "…", "", null, true);
    } else if (runState && runState.running) {
      btn("Stop", "", () => act("stop"));
      btn("Restart", "", () => act("restart"));
    } else if (runState) {
      btn("Start", "primary", () => act("start"));
    }
  }

  // No standalone /status poller: the health poll below carries a superset
  // of what the header chip needs and keeps runState fresh. act() still
  // probes /status directly for its settle loop.

  async function act(verb) {
    busyVerb = verb === "start" ? "starting"
      : verb === "stop" ? "stopping" : "restarting";
    renderControls();
    const { ok, data } = await api(base + "/" + verb,
                                   { method: "POST", body: "{}" });
    if (!ok) {
      busyVerb = null;
      renderControls();
      ctlError((data && (data.report || data.error)) || verb + " failed");
      return;
    }
    const wantRunning = verb !== "stop";
    for (let i = 0; i < 40; i++) {
      const { ok: sok, data: sd } = await api(base + "/status");
      if (sok && sd && sd.running === wantRunning) { runState = sd; break; }
      await new Promise((r) => setTimeout(r, 750));
    }
    busyVerb = null;
    renderControls();
    poll();          // refresh the roster right away
    pollHealth();    // and the health panel/chip
    pollSessions();  // a stop lands sessions in the log
  }

  els.edit.addEventListener("click", async () => {
    const err = await openSetup({ name, mode: "open" });
    if (err) ctlError(err);
  });

  function ctlError(msg) {
    const note = document.createElement("div");
    note.className = "action-error";
    note.textContent = msg;
    head.insertAdjacentElement("afterend", note);
    setTimeout(() => note.remove(), 12000);
  }

  // name -> [{who, text, error, pending}] so a chat survives roster refreshes.
  const history = new Map();
  let selected = null;
  let lastAgents = [];
  let sessionLog = [];            // last /sessions payload rows
  let sessionLogTruncated = false; // the runtime capped the row list
  let sending = false;
  let messagesLoading = false;

  // Whether a session is over comes from the wire (`ended`, derived
  // server-side from the active vocabulary) so this view never has to
  // enumerate terminal words. The alias map is presentation only: legacy
  // words reuse an existing chip color ("done" = success, "error" = a
  // turn-level failure, cancelled = torn down); an unmapped word degrades
  // to the neutral dot.
  const CHIP_ALIAS = { done: "completed", error: "failed", cancelled: "stopped" };
  const chipClass = (s) =>
    Object.hasOwn(CHIP_ALIAS, s) ? CHIP_ALIAS[s] : s;

  // --- tiny, safe markdown (agent replies) ---------------------------
  // Everything is HTML-escaped FIRST, then a fixed set of inline/block
  // transforms run on the escaped text — so agent output can never inject
  // markup. No CDN, no deps.
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                      .replace(/>/g, "&gt;");

  function mdInline(t) {
    // Inline code spans hide behind \x00N\x00 sentinels while the other
    // transforms run, then restore. A NUL can never occur in the escaped
    // text (the standalone agentui used a bare " N " sentinel, which ate
    // plain numbers in prose - fixed here).
    const codes = [];
    t = t.replace(/`([^`]+)`/g, (_, c) => `\x00${codes.push(c) - 1}\x00`);
    t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
      const safe = /^(https?:|mailto:)/i.test(url) ? url : "#";
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    });
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
         .replace(/__([^_]+)__/g, "<strong>$1</strong>");
    t = t.replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
         .replace(/(^|[^A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])/g, "$1<em>$2</em>");
    return t.replace(/\x00(\d+)\x00/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function renderMarkdown(src) {
    const lines = esc(src).split("\n");
    let html = "", para = [], list = null, i = 0;
    const flushP = () => {
      if (para.length) { html += "<p>" + mdInline(para.join(" ")) + "</p>"; para = []; }
    };
    const closeL = () => { if (list) { html += `</${list}>`; list = null; } };
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        flushP(); closeL(); i++;
        const code = [];
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i++;
        html += "<pre><code>" + code.join("\n") + "</code></pre>";
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flushP(); closeL(); const l = Math.min(h[1].length + 2, 6);
        html += `<h${l}>` + mdInline(h[2]) + `</h${l}>`; i++; continue; }
      if (/^>\s?/.test(line)) { flushP(); closeL();
        html += "<blockquote>" + mdInline(line.replace(/^>\s?/, "")) + "</blockquote>"; i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushP(); closeL(); html += "<hr>"; i++; continue; }
      const ul = line.match(/^\s*[-*+]\s+(.*)$/);
      if (ul) { flushP(); if (list !== "ul") { closeL(); html += "<ul>"; list = "ul"; }
        html += "<li>" + mdInline(ul[1]) + "</li>"; i++; continue; }
      const ol = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ol) { flushP(); if (list !== "ol") { closeL(); html += "<ol>"; list = "ol"; }
        html += "<li>" + mdInline(ol[1]) + "</li>"; i++; continue; }
      if (/^\s*$/.test(line)) { flushP(); closeL(); i++; continue; }
      closeL(); para.push(line.trim()); i++;
    }
    flushP(); closeL();
    return html;
  }

  function renderCards(agents) {
    els.empty.hidden = agents.length > 0;
    els.cards.innerHTML = "";
    for (const a of agents) {
      const card = document.createElement("button");
      card.className = "card" + (a.name === selected ? " active" : "");
      card.type = "button";

      const top = document.createElement("div");
      top.className = "card-top";
      const nameEl = document.createElement("span");
      nameEl.className = "card-name";
      nameEl.textContent = a.name;
      top.appendChild(nameEl);
      if (a.is_manager) {
        const b = document.createElement("span");
        b.className = "badge";
        b.textContent = "mgr";
        top.appendChild(b);
      }
      card.appendChild(top);

      if (a.title) {
        const t = document.createElement("div");
        t.className = "card-title";
        t.textContent = a.title;
        card.appendChild(t);
      }

      const meta = document.createElement("div");
      meta.className = "card-meta";
      const status = document.createElement("span");
      status.className = "status " + (a.status || "");
      status.textContent = a.status || "unknown";
      meta.appendChild(status);
      if (a.role && !a.is_manager) {
        const r = document.createElement("span");
        r.textContent = "· " + a.role;
        meta.appendChild(r);
      }
      const cost = fmtUsd(a.total_cost_usd);
      if (cost) {
        const cEl = document.createElement("span");
        cEl.className = "card-cost";
        cEl.textContent = cost;
        cEl.title = "Cumulative recorded spend for this session";
        meta.appendChild(cEl);
      }
      card.appendChild(meta);

      card.addEventListener("click", () => selectAgent(a.name));
      els.cards.appendChild(card);
    }
  }

  async function selectAgent(sub) {
    selected = sub;
    els.placeholder.hidden = true;
    els.chat.hidden = false;
    updateChatHead();
    renderCards(lastAgents);
    renderSessionRows();
    renderTranscript();
    if (!els.composer.hidden) els.input.focus();
    loadMessages(sub);
  }

  function sameMessages(a, b) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (a[i].who !== b[i].who || a[i].text !== b[i].text ||
          !!a[i].error !== !!b[i].error || !!a[i].pending !== !!b[i].pending) {
        return false;
      }
    }
    return true;
  }

  async function loadMessages(sub) {
    if (!sub || messagesLoading) return;
    // Don't clobber the optimistic pending row while a UI-originated blocking
    // chat request is in flight. The final reply path updates history directly.
    if (sending && sub === selected) return;
    messagesLoading = true;
    let result;
    try {
      result = await api(base + "/subagents/" + encodeURIComponent(sub) + "/messages");
    } catch {
      return;
    } finally {
      messagesLoading = false;
    }
    const { ok, data } = result;
    if (selected !== sub) return;
    if (ok && data && Array.isArray(data.messages)) {
      const next = data.messages.map((m) => ({ who: m.role, text: m.text }));
      const current = history.get(sub) || [];
      if (!sameMessages(current, next)) {
        history.set(sub, next);
        renderTranscript();
      }
    } else if (!history.has(sub)) {
      history.set(sub, []);
    }
  }

  function updateChatHead() {
    // The roster (3s poll) is the fresher source for active sessions; a
    // terminal session only appears in the session log.
    const a = lastAgents.find((x) => x.name === selected)
      || sessionLog.find((x) => x.name === selected)
      || { name: selected };
    els.chatName.textContent = a.name;
    els.chatRole.textContent = a.is_manager ? "manager" : (a.role || "agent");
    els.chatStatus.textContent = a.status || "";
    els.chatStatus.hidden = !a.status;
    // An ended session's transcript stays readable; the composer goes away
    // (chat targets a live process). Re-derived on every poll, so a session
    // that ends while selected flips read-only by itself.
    const ended = a.ended === true;
    els.composer.hidden = ended;
    els.chatEnded.hidden = !ended;
    if (ended) {
      els.chatEnded.textContent = "session ended: " + a.status
        + (a.error ? " · " + a.error : "");
    }
  }

  function renderTranscript() {
    const msgs = history.get(selected) || [];
    els.transcript.innerHTML = "";
    for (const m of msgs) {
      const wrap = document.createElement("div");
      wrap.className = "msg " + (m.error ? "error" : m.who)
        + (m.pending ? " pending" : "");
      const who = document.createElement("div");
      who.className = "who";
      who.textContent = m.who === "user" ? "you" : m.error ? "error" : selected;
      const body = document.createElement("div");
      body.className = "body";
      // Markdown only for the agent's own replies; user/error/pending stay literal.
      if (m.who === "agent" && !m.error && !m.pending) {
        body.innerHTML = renderMarkdown(m.text);
      } else {
        body.textContent = m.text;
      }
      wrap.appendChild(who);
      wrap.appendChild(body);
      els.transcript.appendChild(wrap);
    }
    els.transcript.scrollTop = els.transcript.scrollHeight;
  }

  function push(sub, msg) {
    if (!history.has(sub)) history.set(sub, []);
    history.get(sub).push(msg);
    if (sub === selected) renderTranscript();
  }

  // Submit-then-poll: POST returns a message id immediately; the reply
  // arrives in the transcript (messages poll) and this watcher tracks the
  // job's status endpoint for completion/errors. No held-open request.
  async function sendMessage(text) {
    if (sending || !selected) return;
    const sub = selected;
    sending = true;
    els.send.disabled = true;
    push(sub, { who: "user", text });
    const pending = { who: "agent", text: "", pending: true };
    push(sub, pending);

    const finish = (replacement) => {
      const msgs = history.get(sub);
      const idx = msgs.indexOf(pending);
      if (idx >= 0) {
        if (replacement) msgs[idx] = replacement;
        else msgs.splice(idx, 1);
      }
      if (sub === selected) renderTranscript();
      sending = false;
      els.send.disabled = false;
      els.input.focus();
    };

    const { ok, data } = await api(base + "/chat", {
      method: "POST",
      body: JSON.stringify({ subagent: sub, text }),
    });
    if (!ok || !data || !data.message_id) {
      finish({ who: "agent", error: true,
               text: (data && data.error) || "delivery failed" });
      return;
    }

    const watch = async () => {
      const r = await api(base + "/chat/" + data.message_id);
      const job = r.data || {};
      if (r.ok && job.status === "pending") {
        setTimeout(watch, 1500);
        return;
      }
      if (r.ok && job.status === "done") {
        // Reply is persisted — drop the placeholder and pull the truth.
        finish(null);
        loadMessages(sub);
        return;
      }
      finish({ who: "agent", error: true,
               text: job.error || "delivery failed" });
    };
    setTimeout(watch, 1200);
  }

  async function poll() {
    const { ok, data } = await api(base + "/subagents");
    if (ok && data) {
      lastAgents = data.subagents || [];
      renderCards(lastAgents);
      if (selected) updateChatHead();
      // The selected session just left the active roster and the log
      // doesn't know it ended yet: refresh the log now so the pane flips
      // read-only without waiting out the slower session poll (during
      // which a stale log row would keep the composer live against a
      // dead process). The `ended` guard keeps this a one-shot.
      if (selected && !lastAgents.some((x) => x.name === selected)) {
        const row = sessionLog.find((x) => x.name === selected);
        if (!row || !row.ended) pollSessions();
      }
    }
  }

  function pollMessages() {
    if (selected) loadMessages(selected);
  }

  // --- session log (observability, #733 vertical 3) --------------------
  // Terminal outcomes under the roster: what ended, how, and why. Active
  // sessions already live above as roster cards, so the panel lists only
  // ended runs; the counts line covers the whole history. Clicking a row
  // opens the session's transcript read-only in the chat pane.
  const MAX_SESSION_ROWS = 50;

  function renderSessionRows() {
    const rows = sessionLog.filter((s) => s.ended);
    if (!rows.length) { els.sessionsPanel.hidden = true; return; }
    els.sessionsPanel.hidden = false;
    els.sessionRows.innerHTML = "";
    for (const s of rows.slice(0, MAX_SESSION_ROWS)) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "session-row" + (s.name === selected ? " active" : "");
      const top = document.createElement("div");
      top.className = "session-top";
      const nm = document.createElement("span");
      nm.className = "session-name";
      nm.textContent = s.name;
      top.appendChild(nm);
      const chip = document.createElement("span");
      chip.className = "status " + chipClass(s.status);
      chip.textContent = s.status;
      top.appendChild(chip);
      row.appendChild(top);
      const bits = [];
      const when = fmtAgo((s.terminal_at || s.last_activity || 0) * 1000);
      if (when) bits.push(when);
      if (s.role && !s.is_manager) bits.push(s.role);
      if (s.is_manager) bits.push("manager");
      const cost = fmtUsd(s.total_cost_usd);
      if (cost) bits.push(cost);
      if (bits.length) {
        const meta = document.createElement("div");
        meta.className = "session-meta mono";
        meta.textContent = bits.join(" · ");
        row.appendChild(meta);
      }
      if (s.error) {
        const err = document.createElement("div");
        err.className = "session-error";
        err.textContent = s.error;
        err.title = s.error;
        row.appendChild(err);
      }
      row.addEventListener("click", () => selectAgent(s.name));
      els.sessionRows.appendChild(row);
    }
    // Honest footer: what this render hides, plus the runtime's own cap
    // (`truncated`: a hosted box sends only the newest rows, so the counts
    // line above may cover more history than arrived here).
    const hidden = rows.length - MAX_SESSION_ROWS;
    if (hidden > 0 || sessionLogTruncated) {
      const more = document.createElement("div");
      more.className = "session-more mono";
      const bits = [];
      if (hidden > 0) bits.push("+ " + hidden + " older sessions");
      if (sessionLogTruncated) bits.push("older history capped by the runtime");
      more.textContent = bits.join(" · ");
      els.sessionRows.appendChild(more);
    }
  }

  function renderSessionCounts(counts) {
    const bits = [];
    if (counts.completed) bits.push(counts.completed + " completed");
    if (counts.failed) bits.push(counts.failed + " failed");
    if (counts.crashed) bits.push(counts.crashed + " crashed");
    els.sessionCounts.textContent = bits.join(" · ");
  }

  // A hosted supervisor that predates the session_log command never replies,
  // so a failed poll there costs a full command timeout server-side. After
  // two straight misses, back off to every 6th tick (~1 minute) until a poll
  // succeeds again.
  let sessionPollMisses = 0;
  let sessionPollSkips = 0;

  async function pollSessions() {
    if (sessionPollMisses >= 2) {
      sessionPollSkips = (sessionPollSkips + 1) % 6;
      if (sessionPollSkips !== 0) return;
    }
    const { ok, data } = await api(base + "/sessions");
    if (!ok || !data) { sessionPollMisses++; return; }
    sessionPollMisses = 0;
    sessionLog = data.sessions || [];
    sessionLogTruncated = !!data.truncated;
    renderSessionCounts(data.counts || {});
    renderSessionRows();
    if (selected) updateChatHead();
  }

  // --- spend panel (observability, #733) ------------------------------
  // Team total plus the top-spend models, folded from existing per-session
  // cost. A read-only surface: no controls, hidden until there is spend.
  // Models that report no dollars (the codex brain, #760) show a fold-time
  // estimate marked "~ … est", or raw token volume when no defensible
  // estimate exists (model not in the price table, or pre-split history).
  function renderSpend(data) {
    const recorded = (data && data.total_cost_usd) || 0;
    const estimated = (data && data.estimated_cost_usd) || 0;
    const byModel = (data && data.by_model) || {};
    const byEst = (data && data.estimated_by_model) || {};
    const tokens = (data && data.tokens_by_model) || {};
    const total = fmtSpend(recorded, estimated);
    const hasTokens = Object.keys(tokens).length > 0;
    if (!total && !hasTokens) { els.spendPanel.hidden = true; return; }
    els.spendPanel.hidden = false;
    // The figure is lifetime-cumulative: it sums each session's recorded cost
    // across all sessions still on disk, persists across restarts, and is not
    // scoped to a time period. Label it so it is not read as "today".
    els.spendPanel.title =
      "Cumulative recorded spend across all sessions on disk (not a time period)."
      + (estimated > 0 || hasTokens ? EST_NOTE : "");
    const n = data.sessions_counted || 0;
    // Recorded dollars rank first, then estimates, then token-only volume.
    const rows = Object.entries(byModel)
      .filter(([, v]) => v > 0)
      .map(([k, v]) => [k, fmtUsd(v)]);
    for (const [k, v] of Object.entries(byEst)) {
      if (v > 0) rows.push([k, `~${fmtUsd(v)} est`]);
    }
    for (const [k, t] of Object.entries(tokens)) {
      if (byModel[k] > 0 || byEst[k] > 0) continue;
      rows.push(
        [k, `${fmtTok(t.input_tokens)} in / ${fmtTok(t.output_tokens)} out`]);
    }
    els.spendPanel.innerHTML = "";
    const head = document.createElement("div");
    head.className = "spend-head mono";
    const headLabel = document.createElement("span");
    headLabel.textContent = "spend";
    const headTotal = document.createElement("span");
    headTotal.className = "spend-total";
    headTotal.textContent = total || "—";
    head.appendChild(headLabel);
    head.appendChild(headTotal);
    els.spendPanel.appendChild(head);
    const sub = document.createElement("div");
    sub.className = "spend-sub";
    sub.textContent = `cumulative · ${n} session${n === 1 ? "" : "s"}`;
    els.spendPanel.appendChild(sub);
    for (const [key, val] of rows.slice(0, 4)) {
      const row = document.createElement("div");
      row.className = "spend-row";
      const label = document.createElement("span");
      label.className = "spend-label";
      // key is "provider:model" - show the model, keep provider in the title.
      label.textContent = key.includes(":") ? key.split(":").slice(1).join(":") : key;
      label.title = key;
      const amt = document.createElement("span");
      amt.className = "spend-amt";
      amt.textContent = val;
      row.appendChild(label);
      row.appendChild(amt);
      els.spendPanel.appendChild(row);
    }
  }

  async function pollSpend() {
    const { ok, data } = await api(base + "/spend");
    if (ok && data) renderSpend(data);
  }

  // --- health panel (observability, #733) ------------------------------
  // Manager liveness + the supervisor's lifecycle trail, above the roster.
  // A local team has no supervisor: the restart fields are null and the
  // trail is empty, so the panel shows just the manager line. A hosted
  // team adds reachability, restart count, and the 48h lifecycle trail.
  // Severity per supervisor lifecycle event (the sidecar's vocabulary);
  // an event not listed here renders with the neutral dot.
  const LIFECYCLE_CLASS = {
    probe_failing: "bad", budget_exhausted: "bad",
    probe_recovered: "ok", manager_started: "ok",
    manager_restarted: "warn",
  };

  function renderHealth(data) {
    const mgr = data && data.manager;
    if (!mgr) { els.healthPanel.hidden = true; return; }
    els.healthPanel.hidden = false;
    els.healthPanel.innerHTML = "";

    // Degraded reachability outranks the manager's own status: a stale or
    // unreachable heartbeat means the status line can no longer be trusted.
    const reach = data.reachability;
    const degraded = reach === "stale" || reach === "unreachable";
    const st = degraded ? reach
      : mgr.status || (mgr.running ? "running" : "stopped");

    const head = document.createElement("div");
    head.className = "health-head mono";
    const label = document.createElement("span");
    label.textContent = "health";
    const chip = document.createElement("span");
    chip.className = "status " + st;
    chip.textContent = st;
    head.appendChild(label);
    head.appendChild(chip);
    els.healthPanel.appendChild(head);

    const bits = [];
    if (mgr.pid) bits.push("pid " + mgr.pid);
    if (typeof mgr.restart_count === "number") {
      bits.push(mgr.restart_count + " restart" +
                (mgr.restart_count === 1 ? "" : "s"));
    }
    // Heartbeat age answers "how long since we last heard" - meaningful
    // only when degraded. On a live box it is implied, and the timestamp is
    // the box's own clock, so skew would contradict the (server-derived)
    // live chip.
    if (degraded && data.last_heartbeat_at) {
      const ago = fmtAgo(Date.parse(data.last_heartbeat_at));
      if (ago) bits.push("hb " + ago);
    }
    if (bits.length) {
      const sub = document.createElement("div");
      sub.className = "health-sub";
      sub.textContent = bits.join(" · ");
      if (mgr.last_restart_reason) {
        sub.title = "last restart: " + mgr.last_restart_reason;
      }
      els.healthPanel.appendChild(sub);
    }

    for (const ev of (data.lifecycle || []).slice(0, 6)) {
      const row = document.createElement("div");
      row.className = "health-row";
      const evEl = document.createElement("span");
      // hasOwn guard: an event name from the wire must never resolve an
      // inherited Object property into the class list.
      evEl.className = "health-event " +
        (Object.hasOwn(LIFECYCLE_CLASS, ev.event)
          ? LIFECYCLE_CLASS[ev.event] : "");
      evEl.textContent = ev.event;
      if (ev.reason) evEl.title = ev.reason;
      const when = document.createElement("span");
      when.className = "health-when";
      when.textContent =
        fmtAgo(ev.received_at || (ev.at ? Date.parse(ev.at) : NaN));
      row.appendChild(evEl);
      row.appendChild(when);
      els.healthPanel.appendChild(row);
    }
  }

  async function pollHealth() {
    const { ok, data } = await api(base + "/health");
    if (!ok || !data) return;
    renderHealth(data);
    // One liveness source: the header chip derives from the same payload
    // (installed is implied - only installed teams have an agent view).
    const mgr = data.manager || {};
    runState = { installed: true, running: !!mgr.running,
                 reachability: data.reachability,
                 manager_status: mgr.status };
    if (!busyVerb) renderControls();
  }

  els.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = els.input.value.trim();
    if (!text) return;
    els.input.value = "";
    els.input.style.height = "auto";
    sendMessage(text);
  });

  // Enter sends, Shift+Enter newlines; textarea auto-grows.
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      els.composer.requestSubmit();
    }
  });
  els.input.addEventListener("input", () => {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 160) + "px";
  });

  poll();
  pollMessages();
  pollSpend();
  pollHealth();
  pollSessions();
  const t1 = setInterval(poll, 3000);
  const t2 = setInterval(pollMessages, 3000);
  const t3 = setInterval(pollHealth, 4000);
  const t4 = setInterval(pollSpend, 8000);
  // The log only changes when a session ends, and the hosted read is a
  // full command RPC - poll slower than the live surfaces. Lifecycle
  // actions and a roster drop of the selected session refresh it directly.
  const t5 = setInterval(pollSessions, 10000);
  return () => { [t1, t2, t3, t4, t5].forEach(clearInterval); };
}
