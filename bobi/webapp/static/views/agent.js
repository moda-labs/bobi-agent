/* Agent view — the single-agent page (#887 / plan
   plans/2026-07-31-single-agent-view.md).

   Three elements, in the order the questions get asked:

     1. a status strip — is this thing running, and recover it if not
     2. an identity header — what is it (SAVED / ABOUT popovers)
     3. one runs table — what did it do, and what failed

   This replaced the five-panel page (needs-attention, health, spend,
   roster, session log) plus the chat column. Chat lives in Slack and the
   CLI; this page observes and recovers.

   Every value is rendered from the read models behind /health, /overview,
   /runs and /spend. Formatting happens HERE on purpose: the server sends
   raw epochs and seconds because it does not know the viewer's timezone. */

import { openSetup, fmtUsd, fmtEst, fmtTok, EST_NOTE } from "../shell.js";

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

/* --- the view -------------------------------------------------------- */

export function mountAgent(el, { api, name }) {
  const base = "/api/agents/" + encodeURIComponent(name);

  el.innerHTML = "";
  const page = mk("div", "agent-page");
  page.innerHTML = `
    <div class="agent-header">
      <div class="status-band" data-el="band"></div>
      <div class="band-report" data-el="report" hidden></div>
      <div class="ah-body">
        <div class="ah-name">
          <h1 data-el="title"></h1>
          <p class="desc" data-el="desc"></p>
        </div>
        <div class="ah-right">
          <span class="stat-popover" data-el="savedWrap">
            <span class="chip" data-el="savedChip" tabindex="0"
                  role="button" aria-expanded="false">SAVED …</span>
            <span class="popover"><span class="pop-card" data-el="savedCard">
            </span></span>
          </span>
          <span class="stat-popover" data-el="aboutWrap">
            <span class="chip" data-el="aboutChip" tabindex="0"
                  role="button" aria-expanded="false">ABOUT</span>
            <span class="popover"><span class="pop-card" data-el="aboutCard">
            </span></span>
          </span>
          <button class="btn quiet" data-el="edit" type="button"
                  title="Open this team in the setup editor">Edit design</button>
        </div>
      </div>
    </div>

    <section class="panel">
      <div class="panel-head">
        <span class="eyebrow">Runs</span>
        <span class="count" data-el="runsCount"></span>
        <div class="tabs" data-el="tabs"></div>
      </div>
      <table class="runs">
        <thead><tr>
          <th style="width:104px">Status</th>
          <th>Run</th>
          <th style="width:130px">When</th>
          <th style="width:120px" class="r-tok">Tokens · cost</th>
          <th style="width:96px"></th>
        </tr></thead>
        <tbody data-el="runRows"></tbody>
      </table>
      <p class="runs-empty" data-el="runsEmpty" hidden></p>
    </section>

    <div class="modal-backdrop" data-el="backdrop">
      <div class="modal" role="dialog" aria-modal="true"
           aria-label="Run detail">
        <div class="modal-head">
          <span class="eyebrow" data-el="slabKind"></span>
          <span class="path" data-el="slabTitle"></span>
          <span class="meta" data-el="slabMeta"></span>
          <button class="btn small" data-el="slabClose" type="button">Close</button>
        </div>
        <div class="transcript" data-el="slabBody"></div>
      </div>
    </div>`;
  el.appendChild(page);

  const els = {};
  page.querySelectorAll("[data-el]").forEach((n) => {
    els[n.getAttribute("data-el")] = n;
  });

  els.title.textContent = name;

  let health = null;
  let overview = null;
  let runs = null;
  let spend = null;
  let tab = "all";          // all | running | failed
  let busyVerb = null;

  /* --- 1. status strip --------------------------------------------- */

  const STATE_WORD = {
    running: "RUNNING", stopped: "STOPPED", not_responding: "NOT RESPONDING",
  };
  const STATE_CLASS = {
    running: "band-running", stopped: "band-stopped",
    not_responding: "band-error",
  };

  function renderBand() {
    const state = (health && health.state) || "stopped";
    els.band.className = "status-band " + (STATE_CLASS[state] || "band-stopped");
    els.band.innerHTML = "";
    els.band.title = (health && health.detail) || "";

    els.band.appendChild(mk("span", "lamp"));
    els.band.appendChild(mk("span", "state", STATE_WORD[state] || "—"));

    // Whatever segments the runtime could actually produce. A reading it
    // could not is absent, never faked, so this renders the list given.
    for (const seg of (health && health.segments) || []) {
      const box = mk("span", "seg");
      box.appendChild(mk("span", "lbl", seg.label));
      const value = fmtSegment(seg);
      box.appendChild(mk("span", "val",
        seg.note ? `${value} · ${seg.note}` : value));
      els.band.appendChild(box);
    }

    const actions = mk("span", "band-actions");
    const btn = (label, cls, verb) => {
      const b = mk("button", "btn " + cls, busyVerb ? busyVerb + "…" : label);
      b.type = "button";
      if (busyVerb) b.disabled = true;
      else b.addEventListener("click", () => act(verb));
      actions.appendChild(b);
    };
    // Amber marks only the primary recovery action — never the state.
    if (state === "running") {
      btn("Restart", "small", "restart");
      btn("Stop", "small", "stop");
    } else if (state === "not_responding") {
      btn("Restart agent", "primary big", "restart");
    } else {
      btn("▸ Start agent", "primary big", "start");
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
      // A failed start carries a preflight report. Render it under the
      // strip in the strip's own grammar rather than dropping it.
      showReport((data && (data.report || data.error)) || verb + " failed");
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

  function showReport(text) {
    els.report.innerHTML = "";
    els.report.appendChild(mk("span", "rep-head", "start failed"));
    els.report.appendChild(document.createTextNode(text));
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

  /** SAVED — the value story. Estimates never present as a bill. */
  function renderSaved() {
    const card = els.savedCard;
    card.innerHTML = "";
    if (!spend) { card.appendChild(mk("div", "note", "…")); return; }

    const cache = spend.script_cache || {};
    const estimated = spend.estimated_cost_usd || 0;
    const recorded = spend.total_cost_usd || 0;
    const cacheSaved = cache.estimated_savings_usd || 0;
    const total = estimated + cacheSaved;

    els.savedChip.textContent = total > 0
      ? `SAVED ⌁ ~${fmtUsd(total)} · ${spend.sessions_counted || 0} runs`
      : `SAVED ⌁ ${spend.sessions_counted || 0} runs`;

    card.appendChild(mk("div", "eyebrow", "Saved"));
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
    card.appendChild(mk("div", "note",
      "Estimated at API list price for the tokens this team actually used." +
      " Lifetime, over runs still on disk." + EST_NOTE));
  }

  /** ABOUT — composition, read-only. Editing lives in setup. */
  function renderAbout() {
    const card = els.aboutCard;
    card.innerHTML = "";
    if (!overview) { card.appendChild(mk("div", "note", "…")); return; }

    if (overview.roles && overview.roles.length) {
      card.appendChild(mk("div", "eyebrow", "Roles"));
      for (const role of overview.roles.slice(0, 6)) {
        kv(card, role.name, role.description || "—");
      }
      card.appendChild(mk("hr"));
    }

    card.appendChild(mk("div", "eyebrow", "Reaches"));
    const chat = overview.chat || {};
    kv(card, "chat", chat.service
      ? chat.service + (chat.channels && chat.channels.length
          ? " · " + chat.channels.join(", ") : "")
      : "—");
    kv(card, "services",
       (overview.services || []).map((s) => s.name).join(" · ") || "—");

    const auto = overview.automations || {};
    card.appendChild(mk("hr"));
    card.appendChild(mk("div", "eyebrow", "Automations"));
    kv(card, "scheduled", `${auto.monitors || 0} monitors` +
      (auto.paused_monitors ? ` (${auto.paused_monitors} off)` : ""));
    kv(card, "event-triggered", `${auto.workflows || 0} workflows`);

    const brain = overview.brain || {};
    const cap = overview.spend_cap || {};
    card.appendChild(mk("hr"));
    card.appendChild(mk("div", "eyebrow", "Brain"));
    kv(card, [brain.kind, brain.model].filter(Boolean).join(" · ") || "—",
       brain.max_turns ? `max ${brain.max_turns} turns` : "");
    kv(card, "spend cap",
       `${cap.value || 0} inv/hr` + (cap.is_default ? " (default)" : ""));
    card.appendChild(mk("div", "note",
      "Composition is read-only here — change it in Edit design."));
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

  // Edit design is an ACTION, not an href: the setup mount is per-session,
  // so a hardcoded /setup/<slot> link is not guaranteed to resolve.
  els.edit.addEventListener("click", async () => {
    const err = await openSetup({ name, mode: "open" });
    if (err) showReport(err);
  });

  /* --- 3. runs table ------------------------------------------------ */

  const TABS = [
    { key: "all", label: "ALL" },
    { key: "running", label: "RUNNING" },
    { key: "failed", label: "FAILED" },
  ];

  function renderTabs() {
    const counts = (runs && runs.counts) || {};
    els.tabs.innerHTML = "";
    for (const t of TABS) {
      const n = counts[t.key];
      const b = mk("button", "tab" + (tab === t.key ? " active" : ""),
                   n == null ? t.label : `${t.label} · ${n}`);
      b.type = "button";
      b.addEventListener("click", () => {
        tab = t.key;
        renderTabs();
        renderRuns();
      });
      els.tabs.appendChild(b);
    }
  }

  const FAILED_SET = new Set(["failed", "crashed", "stalled"]);

  function visibleRows() {
    const rows = (runs && runs.runs) || [];
    if (tab === "running") return rows.filter((r) => r.status === "running");
    if (tab === "failed") return rows.filter((r) => FAILED_SET.has(r.status));
    return rows;
  }

  function renderRuns() {
    const rows = visibleRows();
    els.runRows.innerHTML = "";
    const counts = (runs && runs.counts) || {};
    els.runsCount.textContent = counts.all
      ? `⌁ ${counts.all} run${counts.all === 1 ? "" : "s"}` : "";

    if (!rows.length) {
      els.runsEmpty.hidden = false;
      els.runsEmpty.textContent = !runs
        ? "Loading…"
        : tab === "failed"
          ? "Nothing needs a human — no failed, crashed, or stalled runs."
          : tab === "running"
            ? "No live runs."
            : "No runs yet. Start the agent and its first work will appear here.";
      return;
    }
    els.runsEmpty.hidden = true;

    for (const row of rows) {
      const tr = mk("tr");

      const stat = mk("td");
      const chip = mk("span", "rstat " + row.status);
      chip.appendChild(mk("span", "rdot"));
      chip.appendChild(mk("span", null, row.status.toUpperCase()));
      stat.appendChild(chip);
      tr.appendChild(stat);

      const run = mk("td");
      run.appendChild(mk("div", "r-title", row.title || "—"));
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
      act.appendChild(rowAction(row));
      tr.appendChild(act);

      tr.addEventListener("click", () => openSlab(row));
      els.runRows.appendChild(tr);
    }
  }

  /** One action per row: Resume for a stalled run, else Open/Transcript
      when there is a session, else Details. */
  function rowAction(row) {
    if (row.status === "stalled" && row.detail && row.detail.resumable) {
      const b = mk("button", "btn small primary", "Resume");
      b.type = "button";
      b.addEventListener("click", (e) => { e.stopPropagation(); resume(row); });
      return b;
    }
    const label = row.session_id
      ? (row.status === "running" ? "Open" : "Transcript")
      : "Details";
    const b = mk("button", "btn small", label);
    b.type = "button";
    b.addEventListener("click", (e) => { e.stopPropagation(); openSlab(row); });
    return b;
  }

  /** Resume force-continues the suspended step — on an `await: approval`
      gate that proceeds AS IF APPROVED — so name what is being waved
      through before firing. */
  async function resume(row) {
    const awaited = (row.detail && row.detail.await_event) || "the awaited event";
    const ok = window.confirm(
      `Resume "${row.title}" from step ` +
      `${(row.detail && row.detail.suspended_at_step) ?? "?"}?\n\n` +
      `It is waiting for ${awaited}. Resuming continues as if that ` +
      `arrived — an approval gate proceeds as if approved.`);
    if (!ok) return;
    const { ok: sent, data } = await api(
      `${base}/workflows/runs/${encodeURIComponent(row.run_id)}/resume`,
      { method: "POST", body: "{}" });
    if (!sent) {
      showReport((data && data.error) || "resume failed");
      return;
    }
    pollRuns();
  }

  /* --- the dark slab ------------------------------------------------ */

  function closeSlab() { els.backdrop.classList.remove("open"); }
  els.slabClose.addEventListener("click", closeSlab);
  els.backdrop.addEventListener("click", (e) => {
    if (e.target === els.backdrop) closeSlab();
  });
  const onKey = (e) => { if (e.key === "Escape") closeSlab(); };
  document.addEventListener("keydown", onKey);

  async function openSlab(row) {
    els.backdrop.classList.add("open");
    els.slabTitle.textContent = row.title || "";
    els.slabMeta.textContent = "";
    els.slabBody.innerHTML = "";
    els.slabBody.appendChild(mk("div", "tr-empty", "Loading…"));

    // Rows with a session get a transcript; rows without get details.
    // That is the rule, and it is decided by data rather than by kind.
    if (row.session_id) {
      els.slabKind.textContent = "Transcript";
      const { ok, data } = await api(
        `${base}/subagents/${encodeURIComponent(row.session_id)}/transcript`);
      if (!ok || !data) return slabError("Could not read that transcript.");
      renderTranscript(row, data);
      return;
    }

    els.slabKind.textContent = "Details";
    // The details endpoint serves MONITOR run records. A session-less
    // workflow run — a stalled one, suspended before it ever spawned — has
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

  /** Details for a run whose story is entirely in its row — a workflow run
      that suspended without a session behind it. */
  function renderRowDetails(row) {
    els.slabMeta.textContent = row.status.toUpperCase();
    els.slabBody.innerHTML = "";
    const d = row.detail || {};
    slabLine("status", row.status);
    slabLine("started", fmtIso(row.started_at));
    if (row.duration_seconds != null) {
      slabLine("ran for", fmtDur(row.duration_seconds));
    }
    slabLine("origin", row.origin);
    slabLine("step", d.suspended_at_step >= 0 ? d.suspended_at_step : "");
    slabLine("awaiting", d.await_event);
    slabLine("run key", d.run_key);
    slabLine("repo", d.repo);
    if (row.error) slabLine("why", row.error);
    if (d.resumable) {
      els.slabBody.appendChild(mk("div", "tr-empty",
        "Resume continues from this step as if the awaited event arrived."));
    }
  }

  function slabLine(label, value) {
    if (value === "" || value == null) return;
    const l = mk("div", "tr-line");
    l.appendChild(mk("span", "who tool", label));
    l.appendChild(mk("span", "txt", String(value)));
    els.slabBody.appendChild(l);
  }

  function renderDetails(row, data) {
    const rec = data.run || {};
    const def = data.definition || {};
    els.slabMeta.textContent = rec.outcome ? rec.outcome.toUpperCase() : "";
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
    const { ok, data } = await api(base + "/health");
    if (ok && data) { health = data; renderBand(); }
  }
  async function pollRuns() {
    const { ok, data } = await api(base + "/runs");
    if (ok && data) { runs = data; renderTabs(); renderRuns(); }
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
  pollHealth();
  pollRuns();
  pollOverview();
  pollSpend();

  // The strip and the runs table are the live surfaces. Composition only
  // changes in setup, and spend moves slowly, so both poll far slower.
  const timers = [
    setInterval(pollHealth, 4000),
    setInterval(pollRuns, 4000),
    setInterval(pollSpend, 10000),
    setInterval(pollOverview, 30000),
  ];
  return () => {
    timers.forEach(clearInterval);
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("click", closePopovers);
  };
}
