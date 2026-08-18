/* Agent view — the single-agent page (#887 / plan
   plans/2026-07-31-single-agent-view.md).

   Three elements, in the order the questions get asked:

     1. a status strip — is this thing running, and recover it if not
     2. an identity header — what is it (saved / about popovers)
     3. one runs table — what did it do, and what failed

   This replaced the five-panel page (needs-attention, health, spend,
   roster, session log) plus the chat column. Chat lives in Slack and the
   CLI; this page observes and recovers.

   Every value is rendered from the read models behind /health, /overview,
   /runs and /spend. Formatting happens HERE on purpose: the server sends
   raw epochs and seconds because it does not know the viewer's timezone. */

import { fmtUsd, fmtEst, fmtTok, EST_NOTE } from "../shell.js";
import { continuationRelay } from "./composer.js";

/* --- formatting ------------------------------------------------------ */

/** "2d 4h" / "18m" / "42s" — an elapsed span, coarse on purpose. */
function fmtDur(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "";
  const s = Math.round(seconds);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h " + (m % 60) + "m";
  return Math.floor(h / 24) + "d " + (h % 24) + "h";
}

/** Clock time for today, date + clock for anything older. */
function fmtWhen(epochSeconds) {
  if (!epochSeconds) return "";
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const clock = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return clock;
  const day = d.toLocaleDateString([], { month: "short", day: "numeric" });
  return day + " " + clock;
}

function fmtIso(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  return Number.isFinite(t) ? fmtWhen(t / 1000) : "";
}

/** A transcript line's clock, seconds included — debugging wants them. */
function fmtStamp(iso) {
  if (!iso) return "--:--:--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

/** One strip segment's value, per the server's `kind`. */
function fmtSegment(seg) {
  const v = seg.value;
  if (seg.kind === "duration") return fmtDur(v);
  if (seg.kind === "time") return fmtWhen(v);
  if (seg.kind === "count") return String(v ?? 0);
  return v == null ? "" : String(v);
}

function mk(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* --- the composer ---------------------------------------------------- */

/** How long the reply poll waits before it stops claiming to be waiting.

    The server owns the turn budget (`DEFAULT_CHAT_TIMEOUT`, 300s), so the
    client has to outlive it - quit at 30s and a reply that arrives well
    inside the budget reads as a failure that never happened. */
const CHAT_WAIT_MS = 330000;
const CHAT_POLL_MS = 1500;

/* --- the view -------------------------------------------------------- */

export function mountAgent(el, { api, name }) {
  const base = "/api/agents/" + encodeURIComponent(name);

  el.innerHTML = "";
  const page = mk("div", "agent-page");
  page.innerHTML = `
    <header class="agent-page-header">
      <div class="ah-body">
        <div class="ah-name">
          <h1 data-el="title"></h1>
          <p class="desc" data-el="desc"></p>
        </div>
        <div class="ah-right">
          <div class="agent-header-state" data-el="band"></div>
          <span class="stat-popover" data-el="savedWrap">
            <span class="chip" data-el="savedChip" tabindex="0"
                  role="button" aria-expanded="false">saved …</span>
            <span class="popover"><span class="pop-card" data-el="savedCard">
            </span></span>
          </span>
          <span class="stat-popover" data-el="aboutWrap">
            <span class="chip" data-el="aboutChip" tabindex="0"
                  role="button" aria-expanded="false">about</span>
            <span class="popover"><span class="pop-card" data-el="aboutCard">
            </span></span>
          </span>
        </div>
      </div>
      <div class="band-report" data-el="report" hidden></div>
    </header>

    <div class="agent-content">
      <section class="telemetry-grid" data-el="telemetry" hidden></section>

      <section class="runs-section">
        <div class="section-label">
          <span>Runs</span>
          <span class="count" data-el="runsCount"></span>
        </div>
        <div class="runs-controls">
          <span class="runs-search-field bobi-field">
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="11" cy="11" r="6.5"></circle>
              <path d="m16 16 4 4"></path>
            </svg>
            <input class="runs-search" data-el="runsSearch" type="search"
                   aria-label="Search runs" placeholder="Search runs">
          </span>
          <div class="tabs" data-el="tabs" role="tablist"
               aria-label="Filter runs"></div>
        </div>
        <div class="panel runs-panel">
          <div class="runs-scroll">
            <table class="runs">
              <thead><tr>
                <th style="width:118px">Status</th>
                <th>Run</th>
                <th style="width:150px">When</th>
                <th style="width:130px" class="r-tok">Tokens · cost</th>
                <th style="width:280px"></th>
              </tr></thead>
              <tbody data-el="runRows"></tbody>
            </table>
          </div>
          <p class="runs-empty" data-el="runsEmpty" hidden></p>
          <div class="runs-pager" data-el="runsPager"></div>
        </div>
      </section>
    </div>

    <div class="modal-backdrop" data-el="backdrop">
      <div class="modal" role="dialog" aria-modal="true"
           aria-label="Run detail">
        <div class="modal-head">
          <span class="eyebrow" data-el="slabKind"></span>
          <span class="path" data-el="slabTitle"></span>
          <span class="meta" data-el="slabMeta"></span>
          <button class="btn bobi-btn small" data-el="slabClose" type="button">Close</button>
        </div>
        <div class="transcript" data-el="slabBody"></div>
        <div class="composer" data-el="slabComposer" hidden></div>
      </div>
    </div>`;
  el.appendChild(page);

  const els = {};
  page.querySelectorAll("[data-el]").forEach((n) => {
    els[n.getAttribute("data-el")] = n;
  });

  els.title.textContent = name;

  let timers = [];
  let health = null;
  let overview = null;
  let runs = null;
  let spend = null;
  let tab = "all";          // all | running | awaiting_action | failed
  let query = "";
  let pageIndex = 0;
  let searchTimer = null;
  let runsRequest = 0;
  let busyVerb = null;
  let runsError = "";       // why the table is empty, when it is not "no runs"

  /* --- the agent that isn't there ----------------------------------- */

  /** Every read 404s when the route names an agent this machine does not
      have (a stale bookmark, a deleted team, a typo). Rendering the page
      shell anyway offers a Start button for nothing and leaves the table
      saying "Loading…" forever, so say the true thing instead. */
  let missing = false;
  function showMissing() {
    if (missing) return;
    missing = true;
    timers.forEach(clearInterval);
    timers = [];
    page.innerHTML = "";
    const wrap = mk("div", "stub");
    wrap.appendChild(mk("h2", null, name));
    wrap.appendChild(mk("p", null,
      "No agent by that name is installed on this machine."));
    const back = mk("a", "btn bobi-btn quiet", "All agents");
    back.href = "#/";
    wrap.appendChild(back);
    page.appendChild(wrap);
  }

  /* --- 1. page header + status ------------------------------------- */

  // Chrome is lowercase, and a state is a label rather than data — the
  // design system's rule, and the reason none of this shouts any more.
  const STATE_WORD = {
    running: "running", stopped: "stopped", not_responding: "not responding",
  };
  const STATE_CLASS = {
    running: "running", stopped: "stopped",
    not_responding: "failed",
  };

  function renderBand() {
    const state = (health && health.state) || "stopped";
    els.band.className = "agent-header-state";
    els.band.innerHTML = "";
    els.band.title = (health && health.detail) || "";

    const status = mk("span", "status-badge " +
      (STATE_CLASS[state] || "stopped"));
    status.appendChild(mk("span", "status-dot"));
    status.appendChild(mk("span", null, STATE_WORD[state] || "—"));
    els.band.appendChild(status);

    // Telemetry uses the design library's MetricTile composition. Whatever
    // the runtime could not read is absent, never synthesized.
    els.telemetry.innerHTML = "";
    for (const seg of (health && health.segments) || []) {
      const box = mk("div", "metric-tile");
      box.appendChild(mk("span", "metric-label", seg.label));
      const value = fmtSegment(seg);
      box.appendChild(mk("span", "metric-value",
        seg.note ? `${value} · ${seg.note}` : value));
      els.telemetry.appendChild(box);
    }
    els.telemetry.hidden = !els.telemetry.children.length;

    const actions = mk("span", "agent-header-actions");
    const btn = (label, cls, verb) => {
      const b = mk("button", "btn bobi-btn " + cls,
                   busyVerb ? busyVerb + "…" : label);
      b.type = "button";
      if (busyVerb) b.disabled = true;
      else b.addEventListener("click", () => act(verb));
      actions.appendChild(b);
    };
    if (state === "running") {
      btn("Restart", "small", "restart");
      btn("Stop", "small", "stop");
    } else if (state === "not_responding") {
      btn("Restart agent", "primary big", "restart");
    } else {
      btn("Start agent", "primary big", "start");
    }
    els.band.appendChild(actions);
  }

  async function act(verb) {
    busyVerb = verb === "start" ? "starting"
      : verb === "stop" ? "stopping" : "restarting";
    renderBand();
    els.report.hidden = true;
    const { ok, data } = await api(base + "/" + verb,
                                   { method: "POST", body: "{}" });
    if (!ok) {
      busyVerb = null;
      renderBand();
      // A failed start carries a preflight report. Keep it attached to the
      // page header instead of dropping it into a transient toast.
      showReport(verb + " failed",
                 (data && (data.report || data.error)) || "");
      return;
    }
    const wantRunning = verb !== "stop";
    for (let i = 0; i < 40; i++) {
      const { ok: sok, data: sd } = await api(base + "/status");
      if (sok && sd && sd.running === wantRunning) break;
      await new Promise((r) => setTimeout(r, 750));
    }
    busyVerb = null;
    pollHealth();
    pollRuns();
  }

  /** The strip's inline failure band. `head` names which action failed. */
  function showReport(head, text) {
    els.report.innerHTML = "";
    els.report.appendChild(mk("span", "rep-head", head));
    els.report.appendChild(document.createTextNode(text || ""));
    els.report.hidden = false;
  }

  /* --- 2. identity ------------------------------------------------- */

  function renderIdentity() {
    els.desc.textContent = (overview && overview.description) || "";
    els.desc.hidden = !(overview && overview.description);
    renderSaved();
    renderAbout();
  }

  function kv(card, k, v, cls) {
    const row = mk("div", "kv");
    row.appendChild(mk("span", "k", k));
    row.appendChild(mk("span", "v" + (cls ? " " + cls : ""), v));
    card.appendChild(row);
  }

  /** saved — the value story. Estimates never present as a bill. */
  function renderSaved() {
    const card = els.savedCard;
    card.innerHTML = "";
    if (!spend) { card.appendChild(mk("div", "note", "…")); return; }

    const cache = spend.script_cache || {};
    const estimated = spend.estimated_cost_usd || 0;
    const recorded = spend.total_cost_usd || 0;
    const cacheSaved = cache.estimated_savings_usd || 0;
    const total = estimated + cacheSaved;

    // Pluralised like the dashboard's session count - a fresh team's first
    // run rendered "1 runs".
    const runs = spend.sessions_counted || 0;
    const ran = `${runs} run${runs === 1 ? "" : "s"}`;
    els.savedChip.textContent = total > 0
      ? `saved ~${fmtUsd(total)} · ${ran}`
      : `saved · ${ran}`;

    // Every figure below is lifetime-cumulative (the fold applies no time
    // filter), so the window is stated on the heading rather than beside
    // one row - "saved ~$50" is unreadable without it.
    const head = mk("div", "eyebrow", "saved");
    head.appendChild(document.createTextNode(" · "));
    head.appendChild(mk("span", "scope", "lifetime"));
    card.appendChild(head);
    kv(card, "list-price value of tokens", fmtEst(estimated) || "—");
    kv(card, "recorded spend", recorded > 0 ? fmtUsd(recorded)
                                            : "$0 (subscription)");
    if (cache.cached_runs) {
      kv(card, "script-cache ticks", `${cache.cached_runs} at ~$0`);
      // priced_monitors is the honesty dial: 0 means nothing COULD be
      // priced, not that nothing was saved.
      kv(card, "script-cache saved",
         cache.priced_monitors ? fmtEst(cacheSaved) : "no priced basis");
    }
    if (total > 0) {
      const hr = mk("hr"); card.appendChild(hr);
      kv(card, "total saved", fmtEst(total), "strong");
    }

    const tokens = spend.tokens_by_model || {};
    const names = Object.keys(tokens);
    if (names.length) {
      card.appendChild(mk("hr"));
      for (const model of names.slice(0, 4)) {
        const t = tokens[model] || {};
        kv(card, model,
           fmtTok((t.input_tokens || 0) + (t.output_tokens || 0)) + " tok");
      }
    }
    // The eyebrow owns the window; this owns the limit ON it - sessions are
    // read off disk, so "lifetime" means every run still there.
    card.appendChild(mk("div", "note",
      "Estimated at API list price for the tokens this team actually used." +
      " Counted over runs still on disk." + EST_NOTE));
  }

  /** about — composition, read-only. Editing lives in setup. */
  function renderAbout() {
    const card = els.aboutCard;
    card.innerHTML = "";
    if (!overview) { card.appendChild(mk("div", "note", "…")); return; }

    if (overview.roles && overview.roles.length) {
      card.appendChild(mk("div", "eyebrow", "roles"));
      for (const role of overview.roles) {
        kv(card, role.name, role.description || "—", "prose");
      }
      card.appendChild(mk("hr"));
    }

    card.appendChild(mk("div", "eyebrow", "reaches"));
    const chat = overview.chat || {};
    kv(card, "chat", chat.service
      ? chat.service + (chat.channels && chat.channels.length
          ? " · " + chat.channels.join(", ") : "")
      : "—");
    kv(card, "services",
       (overview.services || []).map((s) => s.name).join(" · ") || "—");

    const auto = overview.automations || {};
    card.appendChild(mk("hr"));
    card.appendChild(mk("div", "eyebrow", "automations"));
    kv(card, "scheduled", `${auto.monitors || 0} monitors` +
      (auto.paused_monitors ? ` (${auto.paused_monitors} off)` : ""));
    kv(card, "event-triggered", `${auto.workflows || 0} workflows`);

    const brain = overview.brain || {};
    const cap = overview.spend_cap || {};
    card.appendChild(mk("hr"));
    card.appendChild(mk("div", "eyebrow", "brain"));
    kv(card, [brain.kind, brain.model].filter(Boolean).join(" · ") || "—",
       brain.max_turns ? `max ${brain.max_turns} turns` : "");
    kv(card, "spend cap",
       `${cap.value || 0} inv/hr` + (cap.is_default ? " (default)" : ""));
    card.appendChild(mk("div", "note", "Composition is read-only here."));
  }

  // Hover opens these (CSS); tap toggles them, because hover-only is dead
  // on touch. Clicking anywhere else closes.
  for (const key of ["saved", "about"]) {
    const wrap = els[key + "Wrap"];
    const chip = els[key + "Chip"];
    const toggle = (e) => {
      e.stopPropagation();
      const open = wrap.classList.toggle("open");
      chip.setAttribute("aria-expanded", String(open));
      for (const other of ["saved", "about"]) {
        if (other !== key) els[other + "Wrap"].classList.remove("open");
      }
    };
    chip.addEventListener("click", toggle);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e); }
    });
  }
  const closePopovers = () => {
    for (const key of ["saved", "about"]) {
      els[key + "Wrap"].classList.remove("open");
      els[key + "Chip"].setAttribute("aria-expanded", "false");
    }
  };
  document.addEventListener("click", closePopovers);

  /* --- 3. runs table ------------------------------------------------ */

  const TABS = [
    { key: "all", label: "all" },
    { key: "running", label: "running" },
    { key: "awaiting_action", label: "awaiting action" },
    { key: "failed", label: "failed" },
  ];

  function renderTabs() {
    const counts = (runs && runs.counts) || {};
    els.tabs.innerHTML = "";
    for (const t of TABS) {
      // ALL stays bare: the panel head's "⌁ N runs" IS the all-count, and
      // printing it again one gap to the right reads as two facts.
      const n = t.key === "all" ? null : counts[t.key];
      const on = tab === t.key;
      const b = mk("button", "tab" + (on ? " active" : ""),
                   n == null ? t.label : `${t.label} · ${n}`);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.addEventListener("click", () => {
        if (tab === t.key) return;
        tab = t.key;
        pageIndex = 0;
        renderTabs();
        pollRuns();
      });
      els.tabs.appendChild(b);
    }
  }

  /** A run's status as a LABEL — sentence case, not a shout. The status
      vocabulary is one word each, so capitalising the first is the whole
      rule; `not_responding` never reaches a row. */
  const STATUS_LABELS = {
    awaiting_action: "Awaiting action",
    closed: "Closed",
  };
  const STATUS_LABEL = (s) => STATUS_LABELS[s] ||
    (s ? s[0].toUpperCase() + s.slice(1) : "");

  function renderRuns() {
    const rows = (runs && runs.runs) || [];
    els.runRows.innerHTML = "";
    const counts = (runs && runs.counts) || {};
    // The eyebrow beside this already says "runs", so the count is a
    // number and nothing else.
    els.runsCount.textContent = counts.all ? String(counts.all) : "";

    if (!rows.length) {
      els.runsEmpty.hidden = false;
      els.runsEmpty.textContent = !runs
        ? (runsError || "Loading…")
        : query
          ? `No runs match “${query}”.`
        : tab === "awaiting_action"
          ? "No workflows are waiting for approval or clarification."
        : tab === "failed"
          ? "No failed or crashed runs."
          : tab === "running"
            ? "No live runs."
            : "No runs yet. Start the agent and its first work will appear here.";
      return;
    }
    els.runsEmpty.hidden = true;

    for (const row of rows) {
      const tr = mk("tr", "bobi-row");
      tr.tabIndex = 0;
      tr.setAttribute("role", "button");

      const stat = mk("td");
      const chip = mk("span", "rstat " + row.status);
      chip.appendChild(mk("span", "rdot"));
      chip.appendChild(mk("span", null, STATUS_LABEL(row.status)));
      stat.appendChild(chip);
      tr.appendChild(stat);

      const run = mk("td");
      run.appendChild(mk("div", "r-title", row.title || "—"));
      if (row.status === "awaiting_action") {
        const pending = ((row.detail && row.detail.await_event) || "action")
          .replaceAll("_", " ");
        run.appendChild(mk("div", "r-pending", `Awaiting ${pending}`));
      }
      if (row.origin) run.appendChild(mk("div", "r-origin", row.origin));
      const note = row.error || (row.detail && row.detail.note) || "";
      if (note) {
        run.appendChild(mk("div", "r-note" + (row.error ? " bad" : ""), note));
      }
      tr.appendChild(run);

      const when = mk("td", "r-when");
      when.appendChild(mk("span", null, fmtIso(row.started_at) || "—"));
      if (row.duration_seconds != null) {
        when.appendChild(document.createTextNode(" "));
        when.appendChild(mk("span", "dur", fmtDur(row.duration_seconds)));
      }
      tr.appendChild(when);

      // Tokens and cost are independent: a session can record dollars with
      // no per-model token split (a legacy entry), and one can record
      // tokens with no dollars (subscription auth). Show whichever exists
      // rather than hiding a real cost behind a missing token count.
      const cost = row.cost_usd > 0 ? fmtUsd(row.cost_usd)
                                    : fmtEst(row.est_cost_usd);
      const parts = [];
      if (row.tokens) parts.push(fmtTok(row.tokens) + " tok");
      if (cost) parts.push(cost);
      tr.appendChild(mk("td", "r-tok", parts.join(" · ") || "—"));

      const act = mk("td", "r-act");
      act.appendChild(rowActions(row));
      tr.appendChild(act);

      tr.addEventListener("click", () => openSlab(row));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openSlab(row);
        }
      });
      els.runRows.appendChild(tr);
    }
  }

  function renderPager() {
    els.runsPager.innerHTML = "";
    if (!runs) return;
    const total = runs.total == null ? ((runs.runs || []).length) : runs.total;
    const limit = runs.limit || 100;
    const offset = runs.offset || 0;
    const start = total ? offset + 1 : 0;
    const end = Math.min(offset + (runs.runs || []).length, total);
    const summary = query
      ? `${start}–${end} of ${total} matches`
      : `${start}–${end} of ${total}`;
    els.runsPager.appendChild(mk("span", "pager-summary", summary));

    const prev = mk("button", "btn bobi-btn small", "Previous");
    prev.type = "button";
    prev.disabled = pageIndex === 0;
    prev.addEventListener("click", () => { pageIndex -= 1; pollRuns(); });
    els.runsPager.appendChild(prev);

    const pages = Math.max(1, Math.ceil(total / limit));
    els.runsPager.appendChild(mk(
      "span", "pager-page", `${pageIndex + 1} / ${pages}`));

    const next = mk("button", "btn bobi-btn small", "Next");
    next.type = "button";
    next.disabled = offset + (runs.runs || []).length >= total;
    next.addEventListener("click", () => { pageIndex += 1; pollRuns(); });
    els.runsPager.appendChild(next);
  }

  els.runsSearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      query = els.runsSearch.value.trim();
      pageIndex = 0;
      pollRuns();
    }, 250);
  });

  /** Transcript stays visible on every row. Awaiting workflows add a closure
      action, which ends the run without advancing the approval gate. */
  function rowActions(row) {
    const actions = mk("div", "row-actions");
    const transcript = mk("button", "btn bobi-btn small", "Transcript");
    transcript.type = "button";
    transcript.disabled = !row.session_id;
    if (!row.session_id) transcript.title = "No transcript was recorded for this run";
    transcript.addEventListener("click", (e) => {
      e.stopPropagation();
      openSlab(row);
    });
    actions.appendChild(transcript);

    if (!row.session_id && row.kind !== "session") {
      const details = mk("button", "btn bobi-btn small", "Details");
      details.type = "button";
      details.addEventListener("click", (e) => {
        e.stopPropagation();
        openSlab(row);
      });
      actions.appendChild(details);
    }

    if (row.status === "awaiting_action") {
      const closeButton = mk("button", "btn bobi-btn small quiet", "Close");
      closeButton.type = "button";
      closeButton.addEventListener("click", (e) => {
        e.stopPropagation();
        closeRun(row, closeButton);
      });
      actions.appendChild(closeButton);
    }
    return actions;
  }

  async function closeRun(row, button) {
    const awaited = ((row.detail && row.detail.await_event) || "action")
      .replaceAll("_", " ");
    const confirmed = window.confirm(
      `Close "${row.title}"?\n\nIt is awaiting ${awaited}. ` +
      "Closing ends this workflow without approving it or running later steps.");
    if (!confirmed) return;
    button.disabled = true;
    button.textContent = "Closing…";
    const { ok, data } = await api(
      `${base}/workflows/runs/${encodeURIComponent(row.run_id)}/close`,
      { method: "POST", body: "{}" });
    if (!ok) {
      button.disabled = false;
      button.textContent = "Close";
      showReport("close failed", (data && data.error) || "");
      return;
    }
    pollRuns();
  }

  /* --- the dark slab ------------------------------------------------ */

  // Bumped whenever the slab opens or closes. The composer's send outlives
  // its own click (a submit, then a poll the server may hold for minutes),
  // so every continuation checks the token it started under and drops out
  // if the operator has moved on. Without it a reply lands in a slab now
  // showing a different run.
  let slabToken = 0;

  function closeSlab() {
    slabToken += 1;
    els.backdrop.classList.remove("open");
    els.slabComposer.hidden = true;
    els.slabComposer.innerHTML = "";
  }
  els.slabClose.addEventListener("click", closeSlab);
  els.backdrop.addEventListener("click", (e) => {
    if (e.target === els.backdrop) closeSlab();
  });
  const onKey = (e) => { if (e.key === "Escape") closeSlab(); };
  document.addEventListener("keydown", onKey);

  async function openSlab(row) {
    const token = ++slabToken;
    els.backdrop.classList.add("open");
    els.slabTitle.textContent = row.title || "";
    els.slabMeta.textContent = "";
    els.slabBody.innerHTML = "";
    els.slabBody.appendChild(mk("div", "tr-empty", "Loading…"));
    els.slabComposer.hidden = true;
    els.slabComposer.innerHTML = "";

    // Rows with a session get a transcript; rows without get details.
    // That is the rule, and it is decided by data rather than by kind.
    if (row.session_id) {
      els.slabKind.textContent = "transcript";
      const { ok, data } = await api(
        `${base}/subagents/${encodeURIComponent(row.session_id)}/transcript`);
      // Open one row, then another, and the slower read must not paint over
      // the row the operator is actually looking at.
      if (token !== slabToken) return;
      if (!ok || !data) return slabError("Could not read that transcript.");
      renderTranscript(row, data);
      renderComposer(row);
      return;
    }

    els.slabKind.textContent = "details";
    // The details endpoint serves MONITOR run records. A session-less
    // workflow run suspended before it ever spawned has
    // no such record, and needs no fetch either: its row already carries
    // the whole story (what step, what event, how long).
    if (row.kind !== "monitor") {
      renderRowDetails(row);
      return;
    }
    const { ok, data } = await api(
      `${base}/runs/${encodeURIComponent(row.run_id)}/details`);
    if (!ok || !data) return slabError("That run's record is gone.");
    renderDetails(row, data);
  }

  function slabError(msg) {
    els.slabBody.innerHTML = "";
    els.slabBody.appendChild(mk("div", "tr-empty", msg));
  }

  function renderTranscript(row, data) {
    const usage = data.usage || {};
    const parts = [];
    if (usage.started_at && usage.ended_at) {
      parts.push(fmtDur(usage.ended_at - usage.started_at));
    }
    if (usage.tokens) parts.push(fmtTok(usage.tokens) + " tok");
    const cost = usage.cost_usd > 0 ? fmtUsd(usage.cost_usd)
                                    : fmtEst(row.est_cost_usd);
    if (cost) parts.push(cost);
    els.slabMeta.textContent = parts.join(" · ");

    els.slabBody.innerHTML = "";
    const entries = data.entries || [];
    if (!entries.length) {
      els.slabBody.appendChild(mk("div", "tr-empty",
        "No transcript on disk for this run."));
      return;
    }
    for (const entry of entries) {
      const line = mk("div", "tr-line" +
        (entry.kind === "tool" ? " tool" : "") +
        (entry.is_error ? " err" : ""));
      line.appendChild(mk("span", "ts", fmtStamp(entry.at)));
      const who = entry.kind === "message" ? entry.role : "tool";
      line.appendChild(mk("span", "who " + who, who));
      const text = entry.kind === "tool" && entry.tool
        ? `${entry.tool}: ${entry.text}`
        : entry.text + (entry.truncated ? " …" : "");
      line.appendChild(mk("span", "txt", text));
      els.slabBody.appendChild(line);
    }
    els.slabBody.scrollTop = els.slabBody.scrollHeight;
  }

  /* --- the composer -------------------------------------------------- */

  /** The reply box under a transcript.

      Two branches, and the read model picks between them rather than the
      row's kind: `detail.live` says whether anyone is behind this session
      right now.

        live      the text is delivered to that session, through the same
                  `/chat` endpoint (and the same `inbox.deliver` underneath)
                  that `bobi agent <name> message` reaches from a terminal.
        not live  there is no process to receive it, on this surface or in a
                  terminal, so the text is relayed to the team manager as a
                  request to continue the work in a FRESH session.

      Neither branch resumes a workflow run. A suspended run records the step
      AFTER its gate, so resuming one skips the approval it is waiting for.
      There is no resume call on this page and there must never be one. */
  function renderComposer(row) {
    const live = !!(row.detail && row.detail.live);
    const box = els.slabComposer;
    box.innerHTML = "";
    box.hidden = false;

    const input = mk("textarea", "composer-input");
    input.rows = 2;
    input.placeholder = live ? "Reply to this session…"
                             : "Say what should happen next…";
    input.setAttribute("aria-label",
      live ? "Reply to this session" : "Continue this run in a new session");

    const foot = mk("div", "composer-foot");
    foot.appendChild(mk("span", "composer-note", live
      ? "Delivered to this session, the same way the CLI delivers a message."
      : "This session has ended, so nothing here can answer. Sending asks "
        + "the manager to start a fresh session that picks up this run's "
        + "context."));
    const send = mk("button", "btn bobi-btn small primary",
                    live ? "Send" : "Continue in a new session");
    send.type = "button";
    foot.appendChild(send);

    const status = mk("p", "composer-status");
    status.hidden = true;

    box.appendChild(input);
    box.appendChild(foot);
    box.appendChild(status);

    const ui = { input, send, status, live,
                 label: live ? "Send" : "Continue in a new session" };
    send.addEventListener("click", () => sendComposer(row, ui));
    input.addEventListener("keydown", (e) => {
      // Enter sends and Shift+Enter breaks the line: the chat idiom, and the
      // reason the control is a textarea rather than an input.
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendComposer(row, ui);
      }
    });
  }

  /** Say something under the box. Inline, never a toast: this modal is what
      the operator is reading, and a send's outcome belongs beside the box
      that produced it rather than floating over the page. */
  function composerSays(ui, text, bad) {
    ui.status.textContent = text;
    ui.status.className = "composer-status" + (bad ? " bad" : "");
    ui.status.hidden = !text;
  }

  function composerBusy(ui, busy) {
    ui.input.disabled = busy;
    ui.send.disabled = busy;
    ui.send.textContent = busy ? "Sending…" : ui.label;
  }

  async function sendComposer(row, ui) {
    const text = ui.input.value.trim();
    if (!text || ui.send.disabled) return;
    const token = slabToken;
    composerBusy(ui, true);
    composerSays(ui, "", false);

    // The non-live branch addresses the manager, which an empty `subagent`
    // already means on both runtimes. The operator's words travel inside the
    // relay, never as the whole message: the manager has to know which run
    // they were typed on.
    const payload = ui.live
      ? { subagent: row.session_id, text }
      : { subagent: "", text: continuationRelay(row, text) };
    const { ok, data } = await api(`${base}/chat`,
      { method: "POST", body: JSON.stringify(payload) });
    if (token !== slabToken) return;
    if (!ok || !data || !data.message_id) {
      composerBusy(ui, false);
      composerSays(ui, (data && data.error) || "The message was not accepted.",
                   true);
      return;
    }

    const job = await awaitChatJob(data.message_id, token);
    if (token !== slabToken) return;
    composerBusy(ui, false);

    if (!job) {
      // Past the server's own budget. The turn may still land, so say that
      // rather than calling it a failure we did not observe.
      composerSays(ui, "Still waiting past the server's 5 minute budget. "
        + "Reopen this run to see whether the reply arrived.", true);
      return;
    }
    if (job.status === "error") {
      composerSays(ui, job.error || "The message was not delivered.", true);
      return;
    }

    ui.input.value = "";
    if (!ui.live) {
      // The reply landed in the MANAGER's transcript, not this dead
      // session's, so re-rendering this one would read as the message having
      // been swallowed. The new run shows up in the table on its own.
      composerSays(ui, "Sent to the manager. Starting the session is its "
        + "call; a new run will appear in the table if it does.", false);
      return;
    }
    // The slab is otherwise one-shot: the 4s timers refresh the table, not
    // this. Without a re-fetch the answer is invisible until it is reopened.
    const fresh = await api(
      `${base}/subagents/${encodeURIComponent(row.session_id)}/transcript`);
    if (token !== slabToken) return;
    if (!fresh.ok || !fresh.data) {
      composerSays(ui, "Delivered, but the transcript could not be re-read.",
                   true);
      return;
    }
    renderTranscript(row, fresh.data);
  }

  /** Poll a submitted chat job until it resolves. Null means it never did
      inside the window, which is not the same thing as an error. */
  async function awaitChatJob(messageId, token) {
    const deadline = Date.now() + CHAT_WAIT_MS;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, CHAT_POLL_MS));
      if (token !== slabToken) return null;
      const { ok, data } = await api(
        `${base}/chat/${encodeURIComponent(messageId)}`);
      if (ok && data && data.status && data.status !== "pending") return data;
    }
    return null;
  }

  /** Details for a run whose story is entirely in its row — a workflow run
      that suspended without a session behind it. */
  function renderRowDetails(row) {
    els.slabMeta.textContent = STATUS_LABEL(row.status);
    els.slabBody.innerHTML = "";
    const d = row.detail || {};
    slabLine("status", STATUS_LABEL(row.status));
    slabLine("started", fmtIso(row.started_at));
    if (row.duration_seconds != null) {
      slabLine("ran for", fmtDur(row.duration_seconds));
    }
    slabLine("origin", row.origin);
    const step = d.suspended_at_step >= 0 ? d.suspended_at_step : "";
    slabLine("step", step);
    slabLine("awaiting", d.await_event);
    slabLine("run key", d.run_key);
    slabLine("repo", d.repo);
    if (row.error) slabLine("why", row.error);
  }

  /** One `label  value` row in the Details slab. It borrows the transcript's
      line, but NOT its speaker column: these labels are data (a monitor
      definition's own keys), so the column has to be sized for them and has
      to wrap rather than paint over the value — hence `field`. */
  function slabLine(label, value) {
    if (value === "" || value == null) return;
    const l = mk("div", "tr-line field");
    l.appendChild(mk("span", "who tool", label));
    l.appendChild(mk("span", "txt", String(value)));
    els.slabBody.appendChild(l);
  }

  function renderDetails(row, data) {
    const rec = data.run || {};
    const def = data.definition || {};
    els.slabMeta.textContent = rec.outcome || "";
    els.slabBody.innerHTML = "";

    const line = slabLine;
    line("outcome", rec.outcome);
    line("reason", rec.reason);
    line("started", fmtIso(rec.started_at));
    line("ended", fmtIso(rec.ended_at));
    line("flavor", rec.flavor);
    line("cache", rec.script_cache_mode);
    line("published", rec.published);

    if (Object.keys(def).length) {
      els.slabBody.appendChild(mk("div", "tr-line"));
      els.slabBody.appendChild(mk("div", "tr-empty", "— definition —"));
      for (const [k, v] of Object.entries(def)) {
        line(k, Array.isArray(v) ? v.join(", ") : v);
      }
    } else {
      els.slabBody.appendChild(mk("div", "tr-empty",
        "This monitor no longer exists — only its record remains."));
    }
  }

  /* --- polling ------------------------------------------------------- */

  async function pollHealth() {
    const { ok, status, data } = await api(base + "/health");
    if (status === 404) return showMissing();
    if (ok && data) { health = data; renderBand(); }
  }
  async function pollRuns() {
    const request = ++runsRequest;
    const params = new URLSearchParams({
      limit: "100",
      offset: String(pageIndex * 100),
    });
    if (tab !== "all") params.set("status", tab);
    if (query) params.set("query", query);
    const { ok, status, data } = await api(base + "/runs?" + params);
    if (request !== runsRequest) return;
    if (status === 404) return showMissing();
    if (ok && data) {
      if (pageIndex > 0 && data.offset >= data.total) {
        pageIndex = Math.max(0, Math.ceil(data.total / 100) - 1);
        pollRuns();
        return;
      }
      runs = data;
      runsError = "";
      renderTabs();
      renderRuns();
      renderPager();
      return;
    }
    // A read that failed is not a read that is still running. Left saying
    // "Loading…" the table claims work is coming that never will.
    runsError = status === 0
      ? "Lost the app server — the table stopped updating."
      : "Could not read this agent's runs.";
    renderRuns();
    renderPager();
  }
  async function pollOverview() {
    const { ok, data } = await api(base + "/overview");
    if (ok && data) { overview = data; renderIdentity(); }
  }
  async function pollSpend() {
    const { ok, data } = await api(base + "/spend");
    if (ok && data) { spend = data; renderSaved(); }
  }

  renderBand();
  renderTabs();
  renderRuns();
  renderPager();
  pollHealth();
  pollRuns();
  pollOverview();
  pollSpend();

  // The strip and the runs table are the live surfaces. Composition only
  // changes in setup, and spend moves slowly, so both poll far slower.
  timers = [
    setInterval(pollHealth, 4000),
    setInterval(pollRuns, 4000),
    setInterval(pollSpend, 10000),
    setInterval(pollOverview, 30000),
  ];
  return () => {
    timers.forEach(clearInterval);
    clearTimeout(searchTimer);
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("click", closePopovers);
  };
}
