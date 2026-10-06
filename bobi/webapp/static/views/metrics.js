import { api } from "../shell.js";

const TOKEN_FIELDS = [
  ["input_tokens", "Input tokens"], ["uncached_input_tokens", "Uncached input"],
  ["output_tokens", "Output tokens"], ["cache_read_input_tokens", "Cache read"],
  ["cache_write_input_tokens", "Cache write"], ["cache_write_5m_input_tokens", "Cache write · 5m"],
  ["cache_write_1h_input_tokens", "Cache write · 1h"],
  ["cache_write_unknown_ttl_input_tokens", "Cache write · unknown TTL"],
  ["reasoning_output_tokens", "Reasoning output (included in output)"],
];

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function number(value) {
  return value == null ? "not recorded" : Number(value).toLocaleString();
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function pair(container, label, value) {
  const row = node("div", "", "metrics-pair");
  row.append(node("span", label), node("span", value == null ? "not recorded" : String(value), "bobi-tnum"));
  container.append(row);
}

function createSectionHead(icon, title, count, description) {
  const head = node("div", "", "section-panel-head");
  const row = node("div", "", "sph-title-row");
  if (icon) row.append(node("span", icon, "sph-icon"));
  row.append(node("h3", title, "sph-title"));
  if (count != null) row.append(node("span", String(count), "sph-count-pill"));
  head.append(row);
  if (description) head.append(node("p", description, "sph-desc"));
  return head;
}

function renderEmptyCard(icon, title, desc) {
  const card = node("div", "", "turn-empty-card");
  card.innerHTML = `
    <span class="tec-empty-icon">${icon}</span>
    <div class="tec-empty-body">
      <h5>${title}</h5>
      <p>${desc}</p>
    </div>
  `;
  return card;
}

function table(container, headers, rows) {
  const wrap = node("div", "", "runs-scroll");
  const element = node("table", "", "runs metrics-table");
  const head = node("thead");
  const headings = node("tr");
  headers.forEach(label => headings.append(node("th", label)));
  head.append(headings);
  const body = node("tbody");
  rows.forEach(rowDef => {
    const row = node("tr");
    const isObj = !Array.isArray(rowDef) && rowDef && rowDef.cells;
    const cells = isObj ? rowDef.cells : rowDef;
    if (isObj && rowDef.className) row.className = rowDef.className;
    if (isObj && typeof rowDef.onClick === "function") {
      row.classList.add("clickable-row");
      row.addEventListener("click", rowDef.onClick);
    }
    cells.forEach(value => {
      const cell = node("td", "", "bobi-tnum");
      cell.append(value instanceof Node ? value : document.createTextNode(value == null ? "not recorded" : String(value)));
      row.append(cell);
    });
    body.append(row);
  });
  element.append(head, body);
  wrap.append(element);
  container.append(wrap);
}

function policyState(row) {
  if (!row.router_decision_id) return row.session_fallback_reason || "No decision recorded";
  if (row.route_reused) return "Reused session route";
  return row.policy_status === "not_called" ? "Policy not called" : row.policy_status || "Policy status not recorded";
}

function toolIcon(name = "", kind = "") {
  const n = (name || "").toLowerCase();
  const k = (kind || "").toLowerCase();
  if (n.includes("bash") || n.includes("terminal") || n.includes("sh") || k === "shell") return "🐚";
  if (n.includes("read") || n.includes("view") || k === "filesystem" || k === "fs") return "📄";
  if (n.includes("write") || n.includes("edit") || n.includes("replace") || n.includes("create")) return "✏️";
  if (n.includes("glob") || n.includes("grep") || n.includes("search") || k === "search") return "🔍";
  if (n.includes("web") || n.includes("fetch") || n.includes("url") || n.includes("http")) return "🌐";
  return "🔧";
}

function chart(container, buckets) {
  if (!buckets || !buckets.length) return;
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 700 140");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Input and output tokens by UTC bucket");
  svg.classList.add("metrics-chart");

  const maximum = Math.max(10, ...buckets.flatMap(b => [b.input_tokens || 0, b.output_tokens || 0]));
  
  // Background grid lines
  [0.25, 0.5, 0.75, 1.0].forEach(fraction => {
    const y = 115 - fraction * 95;
    const line = document.createElementNS(namespace, "line");
    line.setAttribute("x1", "40");
    line.setAttribute("x2", "680");
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("stroke", "var(--border-hairline)");
    line.setAttribute("stroke-dasharray", "3 3");
    svg.append(line);

    const label = document.createElementNS(namespace, "text");
    label.setAttribute("x", "35");
    label.setAttribute("y", y + 3);
    label.setAttribute("text-anchor", "end");
    label.setAttribute("font-size", "10");
    label.setAttribute("fill", "var(--text-faint)");
    label.setAttribute("font-family", "var(--font-mono)");
    label.textContent = Math.round(fraction * maximum);
    svg.append(label);
  });

  const availableWidth = 640;
  const slotWidth = availableWidth / buckets.length;
  const barWidth = Math.min(28, Math.max(6, slotWidth / 2 - 4));

  buckets.forEach((bucket, index) => {
    const centerX = 40 + (index + 0.5) * slotWidth;
    
    // Input bar (clay)
    if (bucket.input_tokens != null) {
      const height = (bucket.input_tokens / maximum) * 95;
      const bar = document.createElementNS(namespace, "rect");
      bar.setAttribute("x", centerX - barWidth - 1);
      bar.setAttribute("y", 115 - height);
      bar.setAttribute("width", barWidth);
      bar.setAttribute("height", Math.max(2, height));
      bar.setAttribute("rx", "3");
      bar.setAttribute("ry", "3");
      bar.setAttribute("fill", "var(--bobi-clay)");
      
      const title = document.createElementNS(namespace, "title");
      title.textContent = `Input: ${number(bucket.input_tokens)} tokens`;
      bar.append(title);
      svg.append(bar);
    }

    // Output bar (violet / ink)
    if (bucket.output_tokens != null) {
      const height = (bucket.output_tokens / maximum) * 95;
      const bar = document.createElementNS(namespace, "rect");
      bar.setAttribute("x", centerX + 1);
      bar.setAttribute("y", 115 - height);
      bar.setAttribute("width", barWidth);
      bar.setAttribute("height", Math.max(2, height));
      bar.setAttribute("rx", "3");
      bar.setAttribute("ry", "3");
      bar.setAttribute("fill", "var(--bobi-acc)");
      
      const title = document.createElementNS(namespace, "title");
      title.textContent = `Output: ${number(bucket.output_tokens)} tokens`;
      bar.append(title);
      svg.append(bar);
    }

    // X-axis time label
    const timeText = document.createElementNS(namespace, "text");
    timeText.setAttribute("x", centerX);
    timeText.setAttribute("y", "132");
    timeText.setAttribute("text-anchor", "middle");
    timeText.setAttribute("font-size", "11");
    timeText.setAttribute("fill", "var(--text-secondary)");
    timeText.setAttribute("font-family", "var(--font-mono)");
    const date = new Date(bucket.started_at_us / 1000);
    timeText.textContent = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    svg.append(timeText);
  });

  container.append(svg);
}

function turnRows(container, turns, open) {
  table(container, [
    "Started", "Session / lifecycle", "Routing Arm", "Policy / State",
    "Recommendation", "Model (selected / provider)", "Tokens (in / out)", "Status"
  ], turns.map((turn, idx) => {
    const d = new Date(turn.started_at_us / 1000);
    const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const dateStr = d.toLocaleDateString([], { month: "short", day: "numeric" });
    const startedCell = node("div", "", "cell-started");
    startedCell.append(node("span", `${dateStr}, ${timeStr}`, "started-time-text"));

    const isRouted = Boolean(turn.router_decision_id);
    const confidence = turn.confidence == null ? null : Number(turn.confidence).toFixed(3);
    const models = [...new Set((turn.invocations || []).map(inv => inv.model_requested || inv.model_selected || "unknown"))];

    const sessionCell = node("div", "", "cell-session");
    const sessName = turn.session_name || "session";
    const shortId = turn.session_id ? (turn.session_id.length > 20 ? turn.session_id.slice(0, 16) + "…" : turn.session_id) : "";
    const strongName = node("strong", sessName, "sess-name");
    const sep = node("span", " / ", "subtle-sep");
    const idSpan = node("code", shortId, "subtle-id");
    if (turn.session_id) idSpan.title = turn.session_id;
    sessionCell.append(strongName, sep, idSpan);

    let variantCell;
    let polCell;
    let recCell;
    const modelCell = node("div", "", "cell-model");

    if (isRouted) {
      const mode = (turn.policy_mode || "").toLowerCase();
      const isFallback = Boolean(turn.fallback_reason);
      if (isFallback) {
        variantCell = node("span", "⚠️ JEV Fallback", "badge badge-jev-fallback");
        variantCell.title = `JEV fallback: ${turn.fallback_reason}`;
      } else if (mode === "enforce" || turn.variant_id === "treatment_jev" || turn.variant_id === "treatment") {
        variantCell = node("span", "⚡ JEV Enforce", "badge badge-jev-enforce");
        variantCell.title = "JEV policy enforced: dynamically selected optimal model for this turn";
      } else {
        variantCell = node("span", "👁️ JEV Shadow", "badge badge-jev-shadow");
        variantCell.title = "JEV shadow mode: evaluated recommendation without execution override";
      }
      const polText = confidence != null ? `${policyState(turn)} · conf ${confidence}` : policyState(turn);
      polCell = node("span", polText, "policy-text");
      recCell = turn.recommended_model
        ? node("span", turn.recommended_model, "model-badge")
        : node("span", "—", "dash-empty");
      const sel = turn.model_selected || "";
      const prov = models.join(", ");
      if (sel && prov && sel !== prov && !prov.endsWith("/" + sel) && !sel.endsWith("/" + prov)) {
        modelCell.append(node("span", sel, "model-badge"), node("span", " → ", "subtle-arrow"), node("span", prov, "model-badge"));
      } else {
        modelCell.append(node("span", prov || sel || "unknown", "model-badge"));
      }
    } else {
      variantCell = node("span", "➡️ Direct", "badge badge-direct");
      variantCell.title = "Direct routing: executed using default agent baseline model (no JEV policy was invoked)";
      polCell = node("span", "Baseline (No JEV)", "badge-subtle-policy");
      polCell.title = "Turn executed without JEV policy override";
      recCell = node("span", "—", "dash-empty");
      const modelName = models.join(", ") || turn.model_selected || "default model";
      modelCell.append(node("span", modelName, "model-badge"));
    }

    const inTok = turn.usage?.input_tokens;
    const outTok = turn.usage?.output_tokens;
    const cacheRead = turn.usage?.cache_read_input_tokens || 0;
    const cachePct = inTok ? Math.round((cacheRead / inTok) * 100) : 0;
    const tokStr = inTok != null && cacheRead > 0
      ? `${number(inTok)} / ${number(outTok)} (${cachePct}% cached)`
      : (inTok != null ? `${number(inTok)} / ${number(outTok)}` : "—");

    const statusBadge = node("span", turn.status, `status-badge ${turn.status}`);
    statusBadge.prepend(node("span", "", "status-dot"));

    return {
      turn,
      onClick: () => open(turn, idx),
      cells: [
        startedCell,
        sessionCell,
        variantCell,
        polCell,
        recCell,
        modelCell,
        tokStr,
        statusBadge
      ]
    };
  }));
}

function matchToolsWithTranscript(tools, entries, turn) {
  if (!entries || !entries.length) return tools.map(t => ({ tool: t, command: "", result: "" }));
  
  const toolEntries = [];
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    if (e.kind === "tool") {
      let resultText = "";
      if (i + 1 < entries.length && entries[i + 1].kind === "tool_result") {
        resultText = entries[i + 1].text || "";
      }
      toolEntries.push({
        tool: (e.tool || "").toLowerCase(),
        command: e.text || "",
        result: resultText,
        at: e.at ? Date.parse(e.at) : null,
      });
    }
  }

  const turnStart = turn.started_at_us ? turn.started_at_us / 1000 : null;
  const turnEnd = turn.ended_at_us ? turn.ended_at_us / 1000 : null;

  return tools.map((t, idx) => {
    const tStart = t.started_at_us ? t.started_at_us / 1000 : null;
    let match = null;
    if (tStart) {
      let bestDiff = Infinity;
      for (const te of toolEntries) {
        if (!te.at) continue;
        const diff = Math.abs(te.at - tStart);
        if (diff < bestDiff && diff < 8000) {
          bestDiff = diff;
          match = te;
        }
      }
    }
    if (!match && turnStart && turnEnd) {
      const inWindow = toolEntries.filter(te => te.at && te.at >= turnStart - 2000 && te.at <= turnEnd + 2000);
      if (inWindow[idx]) match = inWindow[idx];
    }
    return {
      tool: t,
      command: match?.command || "",
      result: match?.result || "",
    };
  });
}

function renderToolExecutionsList(container, tools, invocations, setTab, transcriptPromise = null, turn = {}) {
  if (!tools || !tools.length) {
    container.append(renderEmptyCard("🔧", "No Tool Executions Recorded", "No external tools or commands were invoked during this turn."));
    return;
  }

  const counts = {};
  let totalDur = 0;
  let errCount = 0;
  let totalIn = 0;
  let totalOut = 0;

  tools.forEach(t => {
    const name = t.tool_name || t.tool_kind || "tool";
    counts[name] = (counts[name] || 0) + 1;
    if (t.started_at_us && t.ended_at_us) totalDur += (t.ended_at_us - t.started_at_us) / 1000;
    if (t.is_error || t.status === "failed" || t.status === "error") errCount++;
    if (t.input_bytes) totalIn += t.input_bytes;
    if (t.output_bytes) totalOut += t.output_bytes;
  });

  const summaryBar = node("div", "", "turn-summary-banner tool-summary-banner");
  const pills = Object.entries(counts).map(([name, count]) =>
    `<span class="tool-count-pill">${toolIcon(name)} ${name} ×${count}</span>`
  ).join(" ");

  const durDisplay = totalDur >= 1000 ? `${(totalDur / 1000).toFixed(2)}s` : `${Math.round(totalDur)}ms`;

  summaryBar.innerHTML = `
    <div class="ts-item"><span class="ts-lbl">Tools Fired</span><span class="ts-val">${pills}</span></div>
    <div class="ts-item"><span class="ts-lbl">Total Latency</span><span class="ts-val bobi-tnum">${durDisplay}</span></div>
    <div class="ts-item"><span class="ts-lbl">Payload Exchange</span><span class="ts-val bobi-tnum">${totalIn ? number(totalIn) + ' B in' : '0 B'} ➔ ${totalOut ? number(totalOut) + ' B out' : '0 B'}</span></div>
    <div class="ts-item"><span class="ts-lbl">Reliability</span><span class="ts-val ${errCount > 0 ? 'text-failed' : 'text-success'}">${errCount === 0 ? '100% success' : `${errCount} error${errCount > 1 ? 's' : ''}`}</span></div>
  `;
  container.append(summaryBar);

  const invMap = new Map();
  invocations.forEach((inv, idx) => {
    const step = `#${inv.invocation_index ? inv.invocation_index : idx + 1}`;
    if (inv.invocation_id) invMap.set(inv.invocation_id, step);
  });

  const cardsContainer = node("div", "", "tec-cards-list");
  container.append(cardsContainer);

  function renderCards(matchedTools) {
    cardsContainer.replaceChildren();
    matchedTools.forEach(({ tool: t, command, result }, idx) => {
      const card = node("div", "", "tec-card");
      const icon = toolIcon(t.tool_name, t.tool_kind);
      const trigStep = t.triggering_invocation_id ? invMap.get(t.triggering_invocation_id) : null;
      const dur = (t.started_at_us && t.ended_at_us) ? `${Math.round((t.ended_at_us - t.started_at_us) / 1000)}ms` : "—";
      const isErr = t.is_error || t.status === "failed" || t.status === "error";

      card.innerHTML = `
        <div class="tec-header">
          <div class="tec-header-left">
            <span class="tec-step">#${idx + 1}</span>
            <span class="tec-tool-name"><span class="tool-icon">${icon}</span> <strong>${t.tool_name || "tool"}</strong></span>
            <span class="tec-kind-pill kind-${t.tool_kind || 'generic'}">${t.tool_kind || 'tool'}</span>
            <span class="tec-duration-pill">⏱️ ${dur}</span>
            ${trigStep ? `<button type="button" class="tec-origin-pill" title="View triggering invocation ${trigStep}">Invoked by ${trigStep}</button>` : ''}
          </div>
          <div class="tec-header-right">
            <span class="status-badge ${isErr ? 'failed' : t.status}">● ${isErr ? 'failed' : t.status}</span>
          </div>
        </div>
        <div class="tec-body">
          ${command ? `
          <div class="tec-section">
            <div class="tec-sec-head">
              <span class="tec-sec-title">COMMAND / INPUT</span>
              <button type="button" class="btn bobi-btn small quiet tec-copy-btn" title="Copy command">Copy</button>
            </div>
            <pre class="tec-code"><code>${escapeHtml(command)}</code></pre>
          </div>` : ''}
          ${result ? `
          <div class="tec-section">
            <div class="tec-sec-head">
              <span class="tec-sec-title">OUTPUT / RESULT</span>
            </div>
            <pre class="tec-output"><code>${escapeHtml(result)}</code></pre>
          </div>` : ''}
          ${!command && !result ? `
          <div class="tec-fallback-info">
            <span>Input Payload: <strong>${t.input_bytes != null ? number(t.input_bytes) + ' B' : '—'}</strong></span>
            <span>Output Payload: <strong>${t.output_bytes != null ? number(t.output_bytes) + ' B' : '—'}</strong></span>
            <span>Call ID: <code>${t.provider_tool_call_id || 'none'}</code></span>
          </div>` : ''}
        </div>
        <div class="tec-footer">
          <span>Latency: <strong>${dur}</strong></span>
          <span>Payload: <strong>${t.input_bytes != null ? number(t.input_bytes) + ' B' : '0 B'} in ➔ ${t.output_bytes != null ? number(t.output_bytes) + ' B' : '0 B'} out</strong></span>
          ${t.provider_tool_call_id ? `<span>Call ID: <code>${t.provider_tool_call_id}</code></span>` : ''}
        </div>
      `;

      const copyBtn = card.querySelector(".tec-copy-btn");
      if (copyBtn && command) {
        copyBtn.addEventListener("click", () => {
          navigator.clipboard?.writeText(command);
          copyBtn.textContent = "Copied! ✓";
          setTimeout(() => { copyBtn.textContent = "Copy"; }, 1200);
        });
      }

      const originBtn = card.querySelector(".tec-origin-pill");
      if (originBtn) {
        originBtn.addEventListener("click", () => {
          if (setTab) setTab("routing");
        });
      }

      cardsContainer.append(card);
    });
  }

  // Initial render with DB data
  renderCards(tools.map(t => ({ tool: t, command: "", result: "" })));

  // If transcriptPromise provided, enrich cards once resolved
  if (transcriptPromise) {
    transcriptPromise.then(res => {
      if (res && res.ok && res.data) {
        const entries = res.data.entries || res.data.transcript?.entries || [];
        if (entries.length) {
          const matched = matchToolsWithTranscript(tools, entries, turn);
          renderCards(matched);
        }
      }
    }).catch(() => {});
  }
}

export function renderTurnMetrics(container, data, section = "all", row = {}, onBack = null, onTabChange = null, agentName = "") {
  container.replaceChildren();

  const isSlab = container.classList.contains("transcript") || !!container.closest(".modal");
  const head = node("div", "", "metrics-detail-head");
  head.append(node("h3", `Turn Detail · ${data.turn.turn_id}`));
  if (onBack) {
    const backBtn = node("button", "← Back to turns list", "btn bobi-btn small back-turns-btn");
    backBtn.type = "button";
    backBtn.addEventListener("click", onBack);
    head.append(backBtn);
  } else if (!isSlab) {
    const closeBtn = node("button", "✕ Close", "btn bobi-btn small");
    closeBtn.type = "button";
    closeBtn.addEventListener("click", () => { container.hidden = true; });
    head.append(closeBtn);
  }
  container.append(head);

  // Turn summary banner
  const banner = node("div", "", "turn-summary-banner");
  const durMs = data.turn.wall_duration_ms;
  const durStr = durMs != null ? `${(durMs / 1000).toFixed(1)}s` : "—";
  const sessName = row.session_name || (data.turn.session_id ? data.turn.session_id.slice(0, 16) + '…' : "session");
  const targetSession = row.session_name || data.turn.session_name || data.turn.session_id;

  const sessToLoad = targetSession || row.session_name || data.turn.session_id;
  let transcriptPromise = null;
  if (sessToLoad && agentName) {
    transcriptPromise = api(`/api/agents/${encodeURIComponent(agentName)}/subagents/${encodeURIComponent(sessToLoad)}/transcript`);
  }

  const decisions = data.router_decisions || [];
  const invocations = data.invocations || [];
  const isJevRouted = decisions.length > 0;
  const primaryDecision = decisions[0];
  let routingBadgeHtml = `<span class="badge badge-direct">➡️ Direct</span>`;
  if (isJevRouted) {
    const pMode = (primaryDecision?.policy_mode || row.policy_mode || "").toLowerCase();
    if (primaryDecision?.fallback_reason) {
      routingBadgeHtml = `<span class="badge badge-jev-fallback">⚠️ JEV Fallback</span>`;
    } else if (pMode === "enforce" || primaryDecision?.variant_id === "treatment_jev" || primaryDecision?.variant_id === "treatment") {
      routingBadgeHtml = `<span class="badge badge-jev-enforce">⚡ JEV Enforce</span>`;
    } else {
      routingBadgeHtml = `<span class="badge badge-jev-shadow">👁️ JEV Shadow</span>`;
    }
  }

  banner.innerHTML = `
    <div class="ts-item"><span class="ts-lbl">Session</span><span class="ts-val"><strong>${sessName}</strong></span></div>
    <div class="ts-item"><span class="ts-lbl">Routing Arm</span><span class="ts-val">${routingBadgeHtml}</span></div>
    <div class="ts-item"><span class="ts-lbl">Duration</span><span class="ts-val bobi-tnum">${durStr}</span></div>
    <div class="ts-item"><span class="ts-lbl">Invocations</span><span class="ts-val bobi-tnum">${(data.invocations || []).length} calls</span></div>
    <div class="ts-item"><span class="ts-lbl">Status</span><span class="ts-val"><span class="status-badge ${data.turn.status}">● ${data.turn.status}</span></span></div>
  `;
  container.append(banner);

  const routingCount = (data.router_decisions?.length || 0) + (data.invocations?.length || 0);
  const toolCount = data.tool_executions?.length || 0;
  const usageCount = data.usage_measurements?.length || 0;
  const costCount = data.cost_measurements?.length || 0;

  const tabDefs = [
    { id: "all", label: "All" },
    { id: "routing", label: `Routing & Calls (${routingCount})` },
    { id: "tools", label: `Tools Executed (${toolCount})` },
    { id: "transcript", label: "Transcript" },
    { id: "usage", label: `Token Usage (${usageCount})` },
    { id: "cost", label: `Cost (${costCount})` },
  ];

  const panelRouting = node("div", "", "detail-tab-panel panel-routing");
  const panelTools = node("div", "", "detail-tab-panel panel-tools");
  const panelTranscript = node("div", "", "detail-tab-panel panel-transcript");
  const panelUsage = node("div", "", "detail-tab-panel panel-usage");
  const panelCost = node("div", "", "detail-tab-panel panel-cost");

  let transcriptLoaded = false;
  async function loadTranscript() {
    if (transcriptLoaded) return;
    transcriptLoaded = true;
    panelTranscript.replaceChildren(node("p", "Loading session transcript…", "metrics-note"));
    const sessToLoad = targetSession || row.session_name || data.turn.session_id;
    if (!sessToLoad || !agentName) {
      panelTranscript.replaceChildren(node("p", "No transcript identifier recorded for this turn.", "metrics-note"));
      return;
    }
    try {
      const res = await api(`/api/agents/${encodeURIComponent(agentName)}/subagents/${encodeURIComponent(sessToLoad)}/transcript`);
      if (!res.ok || !res.data) {
        panelTranscript.replaceChildren(renderEmptyCard("📄", "No Transcript Recorded", "No session transcript was found on disk for this session."));
        return;
      }
      panelTranscript.replaceChildren();
      const headBar = node("div", "", "transcript-panel-head");
      headBar.innerHTML = `<span class="tph-title">Session Transcript · <strong>${sessToLoad}</strong></span>`;
      if (!isSlab) {
        const fullLink = node("a", "Open in Agent Slab ↗", "btn bobi-btn small quiet td-slab-jump-btn");
        fullLink.href = `#/agents/${encodeURIComponent(agentName)}?session=${encodeURIComponent(sessToLoad)}`;
        headBar.append(fullLink);
      }
      panelTranscript.append(headBar);

      const entries = res.data?.entries || res.data?.transcript?.entries || [];
      if (!entries.length) {
        panelTranscript.append(renderEmptyCard("📄", "No Transcript Entries", "The session transcript exists but contains no recorded events."));
        return;
      }
      const list = node("div", "", "transcript-inline-list");
      for (const entry of entries) {
        const line = node("div", "", "tr-line" + (entry.kind === "tool" ? " tool" : "") + (entry.is_error ? " err" : ""));
        line.append(node("span", entry.at ? new Date(entry.at).toLocaleTimeString() : "", "ts"));
        const who = entry.kind === "message" ? entry.role : "tool";
        line.append(node("span", who, "who " + who));
        const text = entry.kind === "tool" && entry.tool
          ? `${entry.tool}: ${entry.text}`
          : entry.text + (entry.truncated ? " …" : "");
        line.append(node("span", text, "txt"));
        list.append(line);
      }
      panelTranscript.append(list);
    } catch (err) {
      console.error("loadTranscript error:", err);
      panelTranscript.replaceChildren(node("p", "Could not load transcript.", "tr-empty bad"));
    }
  }

  const tabsBar = node("div", "", "tabs detail-nav-tabs");
  let currentTab = section === "usage" ? "usage" : (section === "routing" ? "routing" : "all");

  const tabButtons = tabDefs.map(def => {
    const btn = node("button", def.label, "tab");
    btn.type = "button";
    btn.dataset.tab = def.id;
    btn.addEventListener("click", () => setTab(def.id));
    return btn;
  });

  function setTab(tabId) {
    currentTab = tabId;
    tabButtons.forEach(btn => {
      btn.classList.toggle("active", btn.dataset.tab === tabId);
    });
    panelRouting.hidden = !(tabId === "all" || tabId === "routing");
    panelTools.hidden = !(tabId === "all" || tabId === "tools");
    panelTranscript.hidden = !(tabId === "transcript");
    panelUsage.hidden = !(tabId === "all" || tabId === "usage");
    panelCost.hidden = !(tabId === "all" || tabId === "cost");
    if (tabId === "transcript") loadTranscript();
    if (container.scrollTo) container.scrollTo({ top: 0, behavior: "instant" });
    if (typeof onTabChange === "function") onTabChange(tabId);
  }

  tabButtons.forEach(btn => tabsBar.append(btn));
  container.append(tabsBar);
  setTab(currentTab);

  // Index tools and usages by invocation_id:
  const toolMap = new Map();
  (data.tool_executions || []).forEach(t => {
    if (t.triggering_invocation_id) {
      const list = toolMap.get(t.triggering_invocation_id) || [];
      list.push(t);
      toolMap.set(t.triggering_invocation_id, list);
    }
  });

  const usageMap = new Map();
  (data.usage_measurements || []).forEach(u => {
    if (u.invocation_id) {
      const existing = usageMap.get(u.invocation_id);
      if (!existing) {
        usageMap.set(u.invocation_id, u);
      } else {
        const score = (m) => (m.measurement_source === "claude_transcript" ? 10 : 0) + (m.output_tokens > 0 ? 5 : 0) + (m.input_tokens > 0 ? 1 : 0);
        if (score(u) > score(existing)) {
          usageMap.set(u.invocation_id, u);
        }
      }
    }
  });

  // 1. Routing panel
  panelRouting.append(createSectionHead("🧭", "Router Decisions & Policy", data.router_decisions?.length || null, "TypeSafe JEV routing policy evaluations, variant bindings, and provider selection."));
  if (!decisions.length) {
    const directCard = node("div", "", "direct-routing-notice");
    const baselineModel = invocations[0]?.model_requested || invocations[0]?.model_selected || data.turn.model_selected || "baseline model";
    directCard.innerHTML = `
      <div class="dr-header">
        <span class="dr-icon">➡️</span>
        <div class="dr-title-wrap">
          <h4>Direct Execution · Baseline Model</h4>
          <p>This turn was executed directly using the configured model (<code>${baselineModel}</code>) without JEV policy intervention.</p>
        </div>
      </div>
      <div class="dr-details">
        <div class="dr-item"><span class="dr-lbl">Session Role</span><span class="dr-val"><code>${row.role || "director"}</code></span></div>
        <div class="dr-item"><span class="dr-lbl">Routing Arm</span><span class="dr-val"><span class="badge badge-direct">Direct / Baseline</span></span></div>
        <div class="dr-item"><span class="dr-lbl">Execution Reason</span><span class="dr-val">${row.session_fallback_reason ? `Fallback: ${row.session_fallback_reason}` : "Default direct routing (outside policy scope or JEV disabled)"}</span></div>
      </div>
    `;
    panelRouting.append(directCard);
  } else {
    table(panelRouting, [
      "Routing Arm", "Policy / State", "Recommendation", "Confidence", "Selected Model", "Fallback", "Latency"
    ], decisions.map(decision => {
      const mode = (decision.policy_mode || "").toLowerCase();
      let armBadge;
      if (decision.fallback_reason) {
        armBadge = node("span", "⚠️ JEV Fallback", "badge badge-jev-fallback");
      } else if (mode === "enforce" || decision.variant_id === "treatment_jev" || decision.variant_id === "treatment") {
        armBadge = node("span", "⚡ JEV Enforce", "badge badge-jev-enforce");
      } else {
        armBadge = node("span", "👁️ JEV Shadow", "badge badge-jev-shadow");
      }

      const modeSpan = node("span", decision.policy_mode || "not recorded", `badge-policy-mode ${mode}`);
      const polState = policyState({ ...row, ...decision });
      const stateSpan = node("span", polState, `badge-policy-state state-${polState.toLowerCase().replace(/[^a-z0-9]/g, "-")}`);
      const polModeCell = node("div", "", "cell-mode-status");
      polModeCell.append(modeSpan, stateSpan);

      const recModel = decision.recommended_model
        ? node("span", decision.recommended_model, "model-badge")
        : node("span", "—", "dash-empty");

      let confCell;
      if (decision.confidence != null) {
        const cNum = Number(decision.confidence);
        confCell = node("span", `${Math.round(cNum * 100)}% (${cNum.toFixed(2)})`, `confidence-badge ${cNum < 0.6 ? "low" : "high"}`);
      } else {
        confCell = node("span", "—", "dash-empty");
      }

      const selModel = decision.model_selected
        ? node("span", decision.model_selected, "model-badge")
        : node("span", "—", "dash-empty");

      const fb = decision.fallback_reason || row.session_fallback_reason;
      let fbCell;
      if (fb) {
        const cleanFb = fb.replace(/^policy_/, "").replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
        fbCell = node("span", cleanFb, "badge badge-fallback-reason");
        fbCell.title = `Fallback triggered: ${fb}`;
      } else {
        fbCell = node("span", "None", "badge-none");
      }

      const rLat = decision.router_latency_ms != null ? Math.round(decision.router_latency_ms) : null;
      const pLat = decision.policy_latency_ms != null ? Math.round(decision.policy_latency_ms) : null;
      let latText = "—";
      if (rLat != null && pLat != null) {
        latText = `${rLat}ms`;
      } else if (rLat != null) {
        latText = `${rLat}ms`;
      } else if (pLat != null) {
        latText = `${pLat}ms`;
      }
      const latCell = node("span", latText, "bobi-tnum lat-val");
      if (rLat != null || pLat != null) {
        latCell.title = `Router: ${rLat != null ? rLat + 'ms' : '—'}, Policy: ${pLat != null ? pLat + 'ms' : '—'}`;
      }

      return [
        armBadge,
        polModeCell,
        recModel,
        confCell,
        selModel,
        fbCell,
        latCell
      ];
    }));
  }

  // LLM Invocations section
  panelRouting.append(createSectionHead("🤖", "LLM Invocations", `${invocations.length} calls`, "Sequential model generation rounds within this turn (tool execution loop, thinking steps, and final response)."));

  if (!invocations.length) {
    panelRouting.append(renderEmptyCard("🤖", "No LLM Invocations Recorded", "No sequential model generation rounds were executed in this turn."));
  } else {
    table(panelRouting, [
      "#", "Provider", "Model (selected / req)", "Latency", "Tokens (in / out)", "Action / Stop Reason", "Status"
    ], invocations.map((inv, idx) => {
      const stepNum = `#${inv.invocation_index ? inv.invocation_index : idx + 1}`;
      const lat = inv.provider_latency_ms ?? inv.wall_duration_ms;
      const ttft = inv.time_to_first_token_ms;
      const latStr = lat != null ? (lat >= 1000 ? `${(lat / 1000).toFixed(2)}s` : `${Math.round(lat)}ms`) : "—";
      const ttftStr = ttft != null ? ` (${Math.round(ttft)}ms TTFT)` : "";
      const u = usageMap.get(inv.invocation_id);
      const tokStr = u ? `${number(u.input_tokens)} / ${number(u.output_tokens)}` : "—";
      const tools = toolMap.get(inv.invocation_id);

      const modelReq = inv.model_requested || "";
      const modelSel = inv.model_selected || "";
      const displayModel = modelReq || modelSel || "not recorded";
      const modelCell = node("div", "", "cell-model");
      modelCell.append(node("span", displayModel, "model-badge"));
      if (modelSel && modelReq && modelSel !== modelReq && !modelReq.endsWith("/" + modelSel)) {
        const selSpan = node("span", ` (sel: ${modelSel})`, "routed-from-badge");
        selSpan.title = `Provider selected ${modelSel}`;
        modelCell.append(selSpan);
      }

      let actionCell;
      if (tools && tools.length) {
        const counts = {};
        tools.forEach(t => {
          const name = t.tool_name || t.tool_kind || "tool";
          counts[name] = (counts[name] || 0) + 1;
        });
        const summaryParts = Object.entries(counts).map(([name, count]) =>
          count > 1 ? `${name} ×${count}` : name
        );
        const actionBtn = node("button", `🔧 ${summaryParts.join(", ")}`, "tool-badge-btn");
        actionBtn.type = "button";
        actionBtn.title = `Triggered ${tools.length} tool execution(s). Click to view details in Tools Executed tab.`;
        actionBtn.addEventListener("click", () => setTab("tools"));
        actionCell = actionBtn;
      } else if (idx === invocations.length - 1 && inv.status === "completed") {
        actionCell = node("span", "💬 Final Response", "final-badge");
      } else {
        actionCell = document.createTextNode(inv.stop_reason || "—");
      }

      const provBadge = node("span", inv.provider || "gateway", "provider-tag");
      const statusBadge = node("span", inv.status || "completed", `status-badge ${inv.status || "completed"}`);
      statusBadge.prepend(node("span", "", "status-dot"));

      return [
        stepNum,
        provBadge,
        modelCell,
        `${latStr}${ttftStr}`,
        tokStr,
        actionCell,
        statusBadge
      ];
    }));
  }

  // 2. Tool executions panel
  panelTools.append(createSectionHead("🔧", "Tool Executions", toolCount, "Tools called by the agent during this turn (terminal commands, file reading/writing, web searches). Click any tool row to inspect detailed inputs, outputs, and execution results."));
  renderToolExecutionsList(panelTools, data.tool_executions || [], invocations, setTab, transcriptPromise, data.turn);

  // 3. Usage panel
  panelUsage.append(createSectionHead("📊", "Token Usage & Cache Performance", data.usage_measurements?.length ? `${data.usage_measurements.length} records` : null, "Step-by-step token consumption, prompt cache hit rate, and total context footprint for this turn."));
  if (data.usage_measurements && data.usage_measurements.length) {
    const turnUsage = data.usage_measurements.find(u => u.scope === "turn") || {};
    const totalIn = turnUsage.input_tokens || data.usage_measurements.reduce((acc, u) => u.scope === "invocation" ? acc + (u.input_tokens || 0) : acc, 0);
    const totalOut = turnUsage.output_tokens || data.usage_measurements.reduce((acc, u) => u.scope === "invocation" ? acc + (u.output_tokens || 0) : acc, 0);
    const cacheRead = turnUsage.cache_read_input_tokens || data.usage_measurements.reduce((acc, u) => u.scope === "invocation" ? acc + (u.cache_read_input_tokens || 0) : acc, 0);
    const cachePct = totalIn ? ((cacheRead / totalIn) * 100).toFixed(1) : "0.0";
    const totalTokens = totalIn + totalOut;

    const kpiGrid = node("div", "", "quick-kpi-grid");
    kpiGrid.innerHTML = `
      <div class="kpi-card">
        <span class="kpi-lbl">Total Input Tokens</span>
        <span class="kpi-val bobi-tnum">${number(totalIn)}</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-lbl">Prompt Cache Hit</span>
        <span class="kpi-val bobi-tnum text-green">${cachePct}% <small>(${number(cacheRead)} cached)</small></span>
      </div>
      <div class="kpi-card">
        <span class="kpi-lbl">Total Output Tokens</span>
        <span class="kpi-val bobi-tnum">${number(totalOut)}</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-lbl">Total Turn Tokens</span>
        <span class="kpi-val bobi-tnum">${number(totalTokens)}</span>
      </div>
    `;
    panelUsage.append(kpiGrid);

    // Step-by-Step Invocation Usage Breakdown
    const stepRows = invocations.map((inv, idx) => {
      const stepNum = `#${inv.invocation_index ? inv.invocation_index : idx + 1}`;
      const u = usageMap.get(inv.invocation_id) || {};
      const inTok = u.input_tokens || 0;
      const outTok = u.output_tokens || 0;
      const cache = u.cache_read_input_tokens || 0;
      const stepTotal = inTok + outTok;
      const cacheRatio = inTok > 0 && cache > 0 ? ` (${((cache / inTok) * 100).toFixed(1)}%)` : "";
      const tools = toolMap.get(inv.invocation_id);

      const modelName = inv.model_requested || inv.model_selected || "—";
      const modelCell = node("span", modelName, "model-badge");

      let actionDesc;
      if (tools && tools.length) {
        const counts = {};
        tools.forEach(t => {
          const name = t.tool_name || t.tool_kind || "tool";
          counts[name] = (counts[name] || 0) + 1;
        });
        const summaryParts = Object.entries(counts).map(([name, count]) =>
          count > 1 ? `${name} ×${count}` : name
        );
        actionDesc = node("span", `🔧 ${summaryParts.join(", ")}`, "tool-badge-btn");
      } else if (idx === invocations.length - 1 && inv.status === "completed") {
        actionDesc = node("span", "💬 Final Response", "final-badge");
      } else {
        actionDesc = document.createTextNode(inv.stop_reason || "completed");
      }

      const cachedCell = cache > 0
        ? node("span", `${number(cache)}${cacheRatio}`, "text-green")
        : node("span", "—", "dash-empty");

      return {
        cells: [
          stepNum,
          modelCell,
          number(inTok),
          number(outTok),
          cachedCell,
          number(stepTotal),
          actionDesc
        ]
      };
    });

    if (stepRows.length) {
      const invModels = [...new Set(invocations.map(i => i.model_requested || i.model_selected).filter(Boolean))];
      const totalTurnModel = invModels.length === 1
        ? invModels[0]
        : (invModels.length > 1
           ? invModels.join(", ")
           : (invocations[0]?.model_requested || invocations[0]?.model_selected || data.turn.model_selected || "—"));

      const totalStatus = node("span", `${data.turn.status} (${invocations.length} calls)`, `status-badge ${data.turn.status}`);
      totalStatus.prepend(node("span", "", "status-dot"));

      stepRows.push({
        className: "highlight-turn-row",
        cells: [
          node("strong", "Total (Turn)"),
          node("span", totalTurnModel, "model-badge"),
          node("strong", number(totalIn)),
          node("strong", number(totalOut)),
          cacheRead > 0 ? node("span", `${number(cacheRead)} (${cachePct}%)`, "text-green") : node("span", "—", "dash-empty"),
          node("strong", number(totalTokens)),
          totalStatus
        ]
      });

      table(panelUsage, [
        "Step", "Model", "Input Tokens", "Output Tokens", "Prompt Cached", "Step Total", "Action / Outcome"
      ], stepRows);
    }

    // Expandable Raw Telemetry Records
    const rawDrawer = node("details", "", "raw-measurements-drawer");
    rawDrawer.innerHTML = `
      <summary class="raw-drawer-summary">
        <span>🔍 Inspect Raw Telemetry Records (${data.usage_measurements.length} database entries)</span>
        <span class="raw-drawer-hint">Click to expand audit provenance & stream telemetry</span>
      </summary>
      <div class="raw-drawer-body"></div>
    `;
    const rawBody = rawDrawer.querySelector(".raw-drawer-body");
    table(rawBody, [
      "Scope", "Model", "Source", "Basis", "Input", "Output", "Cache Read", "Cache Write", "Reasoning"
    ], data.usage_measurements.map(usage => ({
      className: usage.scope === "turn" ? "highlight-turn-row" : "",
      cells: [
        node("span", usage.supersedes_measurement_id ? `${usage.scope} (#${usage.supersedes_measurement_id})` : usage.scope, `badge badge-scope ${usage.scope}`),
        node("span", usage.model || "—", "model-badge"),
        node("span", (usage.measurement_source || "unknown").replace(/_/g, " "), "source-tag"),
        node("span", usage.is_estimated ? "Estimated" : "Reported", `badge ${usage.is_estimated ? "badge-estimated" : "badge-reported"}`),
        number(usage.input_tokens),
        number(usage.output_tokens),
        usage.cache_read_input_tokens > 0 ? node("span", number(usage.cache_read_input_tokens), "text-green") : "0",
        number(usage.cache_write_input_tokens || 0),
        number(usage.reasoning_output_tokens || 0)
      ]
    })));
    panelUsage.append(rawDrawer);
  } else {
    panelUsage.append(renderEmptyCard("📊", "No Usage Measurements Recorded", "No step-by-step token consumption was reported for this turn."));
  }

  // 4. Cost panel
  panelCost.append(createSectionHead("💳", "Cost Accounting", data.cost_measurements?.length ? `${data.cost_measurements.length} records` : null, "Financial cost attribution per model pricing schedule."));
  if (data.cost_measurements && data.cost_measurements.length) {
    table(panelCost, [
      "Scope", "Model", "Spend (USD)", "Measurement Source", "Pricing Basis"
    ], data.cost_measurements.map(cost => {
      const usdNum = Number(cost.amount_usd || 0);
      const formattedUsd = `$${usdNum.toFixed(4)}`;
      return [
        node("span", cost.scope || "turn", `badge badge-scope ${cost.scope}`),
        node("span", cost.model || "—", "model-badge"),
        node("strong", formattedUsd, "bobi-tnum text-cost"),
        node("span", (cost.measurement_source || "unknown").replace(/_/g, " "), "source-tag"),
        node("span", cost.is_estimated ? "Estimated" : "Reported", `badge ${cost.is_estimated ? "badge-estimated" : "badge-reported"}`)
      ];
    }));
  } else {
    panelCost.append(renderEmptyCard("💳", "No Cost Schedule Recorded", "No cost schedule or financial pricing rules were populated for this provider model."));
  }

  const metaFooter = node("div", "", "turn-meta-footer");
  metaFooter.innerHTML = `
    <span>Turn: <code>${data.turn.turn_id}</code></span>
    ${targetSession ? `<span> · Session: <code>${targetSession}</code></span>` : ''}
  `;

  container.append(tabsBar, panelRouting, panelTools, panelTranscript, panelUsage, panelCost, metaFooter);
  setTab(currentTab);
}


export async function renderRunMetrics(container, { api, name, row, section, signal }) {
  container.replaceChildren(node("p", "Loading metrics…"));
  const end = new Date();
  const params = new URLSearchParams({
    from: new Date(end - 31 * 86400000).toISOString(),
    to: end.toISOString(),
    session_name: row.session_id,
    limit: "50"
  });
  const base = `/api/agents/${encodeURIComponent(name)}/metrics`;
  let cursor = "";
  let detailRequest = 0;
  const load = async () => {
    if (cursor) params.set("cursor", cursor);
    const result = await api(`${base}/turns?${params}`, { signal });
    if (signal.aborted) return;
    container.replaceChildren();
    if (!result.ok) {
      container.append(node("p", result.data?.error || "Could not read metrics."));
      return;
    }
    container.append(node("p", "Recorded turns for this session name across lifecycles, within the last 31 days. Select a turn to inspect its telemetry ID.", "metrics-note"));
    if (!result.data.turns.length) container.append(node("p", "No recorded turns in this window."));
    turnRows(container, result.data.turns, async (turn, idx) => {
      const request = ++detailRequest;
      const detail = await api(`${base}/turns/${encodeURIComponent(turn.turn_id)}`, { signal });
      if (signal.aborted || request !== detailRequest) return;
      if (!detail.ok) { container.replaceChildren(node("p", detail.data?.error || "Could not read turn.")); return; }
      renderTurnMetrics(container, detail.data, section, turn, () => load(), null, name);
    }, (sessName) => {
      location.hash = `#/agents/${encodeURIComponent(name)}/metrics/${encodeURIComponent(sessName)}`;
    }, name);
    if (result.data.next_cursor) {
      const next = node("button", "older turns", "btn bobi-btn small");
      next.type = "button";
      next.addEventListener("click", () => { cursor = result.data.next_cursor; load(); });
      container.append(next);
    }
  };
  await load();
}

export function mountMetrics(element, { api, name, session = "" }) {
  const page = node("div", "", "agent-page metrics-page");

  // Modern agent page header matching Single-Agent view
  const header = node("header", "", "agent-page-header");
  const ahBody = node("div", "", "ah-body");
  
  const ahName = node("div", "", "ah-name");
  const breadcrumbs = node("div", "", "metrics-breadcrumbs");
  const backLink = node("a", `← ${name}`, "metrics-back-link");
  backLink.href = `#/agents/${encodeURIComponent(name)}`;
  breadcrumbs.append(backLink, node("span", "/", "sep"), node("span", "metrics & routing", "curr"));
  ahName.append(breadcrumbs);
  ahName.append(node("h1", `${name} · metrics & routing`));
  ahName.append(node("p", "Token accounting, stream telemetry, and TypeSafe JEV routing decisions", "desc"));

  const ahRight = node("div", "", "ah-right");
  const collectorChip = node("span", "● collector: live", "chip");
  const jevHeaderBtn = node("button", "⚙️ JEV: Loading…", "btn bobi-btn small jev-header-btn");
  jevHeaderBtn.type = "button";
  jevHeaderBtn.title = `Configure TypeSafe JEV dynamic model routing for ${name}`;
  jevHeaderBtn.addEventListener("click", openJevConfigModal);
  const refreshBtn = node("button", "refresh", "btn bobi-btn small");
  refreshBtn.type = "button";
  refreshBtn.addEventListener("click", () => load(true));
  ahRight.append(collectorChip, jevHeaderBtn, refreshBtn);

  ahBody.append(ahName, ahRight);
  header.append(ahBody);

  // Main content container
  const content = node("div", "", "metrics-content");

  // Status note bar
  const statusBar = node("div", "", "metrics-status-bar");
  const note = node("p", "Loading metrics…", "metrics-note");
  note.setAttribute("role", "status");
  statusBar.append(note);

  // Summary sections
  const summary = node("section", "", "metrics-summary");
  const turns = node("section", "", "panel metrics-table-panel");
  const pager = node("div", "", "runs-pager");

  // Modern Turn Detail Slide-over Modal Drawer
  const drawerBackdrop = node("div", "", "modal-backdrop turn-drawer-backdrop");
  drawerBackdrop.hidden = true;
  const drawerModal = node("div", "", "modal turn-drawer-modal");
  drawerModal.setAttribute("role", "dialog");
  drawerModal.setAttribute("aria-modal", "true");
  drawerModal.setAttribute("aria-label", "Turn detail");

  const drawerHead = node("div", "", "modal-head td-head");
  const drawerBody = node("div", "", "turn-drawer-body");
  drawerModal.append(drawerHead, drawerBody);
  drawerBackdrop.append(drawerModal);

  // TypeSafe JEV Configuration Modal Dialog (Agent-wide)
  const jevModalBackdrop = node("div", "", "modal-backdrop jev-modal-backdrop");
  jevModalBackdrop.hidden = true;
  const jevModal = node("div", "", "modal jev-config-modal");
  jevModal.setAttribute("role", "dialog");
  jevModal.setAttribute("aria-modal", "true");
  jevModal.setAttribute("aria-label", "JEV Routing Configuration");

  const jevModalHead = node("div", "", "modal-head");
  const jevModalLeft = node("div", "", "td-head-left");
  jevModalLeft.append(node("span", "Agent Policy", "td-badge-turn"), node("h3", `TypeSafe JEV Dynamic Routing · ${name}`));
  const jevModalCloseBtn = node("button", "✕", "btn bobi-btn quiet small jev-modal-close-btn");
  jevModalCloseBtn.type = "button";
  jevModalCloseBtn.title = "Close dialog";
  jevModalCloseBtn.addEventListener("click", closeJevConfigModal);
  jevModalHead.append(jevModalLeft, jevModalCloseBtn);

  const jevModalNavTabs = node("div", "", "tabs jev-modal-nav-tabs");
  jevModalNavTabs.innerHTML = `
    <button type="button" class="tab active" data-jev-tab="status">⚡ Overview & Status</button>
    <button type="button" class="tab" data-jev-tab="guide">📖 .env Configuration Guide</button>
  `;

  function setJevModalTab(tabId) {
    const tabs = jevModalNavTabs.querySelectorAll(".tab");
    tabs.forEach(t => t.classList.toggle("active", t.dataset.jevTab === tabId));
    const paneStatus = jevModalBody.querySelector("#jev-pane-status");
    const paneGuide = jevModalBody.querySelector("#jev-pane-guide");
    if (paneStatus && paneGuide) {
      if (tabId === "guide") {
        paneStatus.hidden = true;
        paneGuide.hidden = false;
      } else {
        paneStatus.hidden = false;
        paneGuide.hidden = true;
      }
    }
  }

  jevModalNavTabs.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => setJevModalTab(btn.dataset.jevTab));
  });

  const jevModalBody = node("div", "", "modal-body jev-modal-body");

  const jevConfirmOverlay = node("div", "", "jev-confirm-overlay");
  jevConfirmOverlay.hidden = true;
  jevConfirmOverlay.innerHTML = `
    <div class="jev-confirm-card">
      <div class="jev-confirm-icon">🔄</div>
      <div class="jev-confirm-body">
        <h4 class="jev-confirm-title">Agent Restart Required</h4>
        <p class="jev-confirm-text">
          Saving this configuration requires an agent restart to apply to active sessions (such as the <code>director</code>).
        </p>
        <p class="jev-confirm-subtext">
          Do you want to restart the agent now to apply changes immediately?
        </p>
        <div class="jev-confirm-status-msg" role="status"></div>
      </div>
      <div class="jev-confirm-footer">
        <button type="button" class="btn bobi-btn quiet jev-confirm-btn-cancel">Cancel</button>
        <button type="button" class="btn bobi-btn primary jev-confirm-btn-restart">Save & Restart Agent</button>
      </div>
    </div>
  `;

  function openConfirmPopup() {
    jevConfirmOverlay.hidden = false;
    const msg = jevConfirmOverlay.querySelector(".jev-confirm-status-msg");
    if (msg) {
      msg.textContent = "";
      msg.className = "jev-confirm-status-msg";
    }
    const cCancel = jevConfirmOverlay.querySelector(".jev-confirm-btn-cancel");
    const cRestart = jevConfirmOverlay.querySelector(".jev-confirm-btn-restart");
    if (cCancel) cCancel.disabled = false;
    if (cRestart) cRestart.disabled = false;
  }

  function closeConfirmPopup() {
    jevConfirmOverlay.hidden = true;
  }

  jevConfirmOverlay.addEventListener("click", (e) => {
    if (e.target === jevConfirmOverlay) closeConfirmPopup();
  });

  jevModal.append(jevModalHead, jevModalNavTabs, jevModalBody, jevConfirmOverlay);
  jevModalBackdrop.append(jevModal);

  jevModalBackdrop.addEventListener("click", (e) => {
    if (e.target === jevModalBackdrop) closeJevConfigModal();
  });

  content.append(statusBar, summary, turns, pager);
  page.append(header, content, drawerBackdrop, jevModalBackdrop);
  element.replaceChildren(page);

  let stopped = false;
  let pending = false;
  let queued = false;
  let queuedClear = false;
  let timer;
  const controller = new AbortController();
  let detailController;
  let params;
  let cursor = "";
  const base = `/api/agents/${encodeURIComponent(name)}/metrics`;

  let activeTurnIndex = -1;
  let currentTurns = [];
  let currentDrawerTab = "all";

  function closeDrawer() {
    drawerBackdrop.hidden = true;
    drawerBackdrop.classList.remove("open");
    activeTurnIndex = -1;
    turns.querySelectorAll("tbody tr.turn-row-selected").forEach(r => r.classList.remove("turn-row-selected"));
    document.removeEventListener("keydown", handleDrawerKey);
  }

  function handleDrawerKey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      closeDrawer();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      if (activeTurnIndex > 0) {
        e.preventDefault();
        openAtIndex(activeTurnIndex - 1);
      }
    } else if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      if (activeTurnIndex < currentTurns.length - 1) {
        e.preventDefault();
        openAtIndex(activeTurnIndex + 1);
      }
    }
  }

  drawerBackdrop.addEventListener("click", (e) => {
    if (e.target === drawerBackdrop) closeDrawer();
  });

  async function openAtIndex(index) {
    if (index < 0 || index >= currentTurns.length) return;
    activeTurnIndex = index;
    const turn = currentTurns[index];

    // Highlight row in table
    const tableRows = turns.querySelectorAll("tbody tr");
    tableRows.forEach((r, idx) => {
      r.classList.toggle("turn-row-selected", idx === index);
    });

    drawerBackdrop.hidden = false;
    drawerBackdrop.classList.add("open");
    document.addEventListener("keydown", handleDrawerKey);

    renderDrawerHead(turn, index, currentTurns.length);

    detailController?.abort();
    detailController = new AbortController();
    const signal = detailController.signal;

    drawerBody.replaceChildren(node("div", "Loading turn telemetry…", "tr-empty"));

    const result = await api(`${base}/turns/${encodeURIComponent(turn.turn_id)}`, { signal });
    if (stopped || signal.aborted) return;
    if (!result.ok) {
      drawerBody.replaceChildren(node("p", result.data?.error || "Could not read turn.", "tr-empty bad"));
      return;
    }

    renderTurnMetrics(drawerBody, result.data, currentDrawerTab || "all", turn, null, (tabId) => {
      currentDrawerTab = tabId;
    }, name);
  }

  function renderDrawerHead(turn, index, total) {
    drawerHead.replaceChildren();

    const left = node("div", "", "td-head-left");
    const eyebrow = node("span", "Turn Detail", "td-badge-turn");
    const turnIdChip = node("code", turn.turn_id, "td-id-chip");
    turnIdChip.title = "Click to copy Turn ID";
    turnIdChip.addEventListener("click", () => {
      navigator.clipboard?.writeText(turn.turn_id);
      turnIdChip.textContent = "Copied! ✓";
      setTimeout(() => { turnIdChip.textContent = turn.turn_id; }, 1200);
    });
    left.append(eyebrow, turnIdChip);

    const center = node("div", "", "td-head-nav");
    const prevBtn = node("button", "↑ Earlier", "btn bobi-btn quiet small");
    prevBtn.type = "button";
    prevBtn.disabled = index <= 0;
    prevBtn.title = "Previous turn in timeline";
    prevBtn.addEventListener("click", () => openAtIndex(index - 1));

    const counter = node("span", `Turn ${index + 1} of ${total}`, "td-nav-counter");

    const nextBtn = node("button", "↓ Later", "btn bobi-btn quiet small");
    nextBtn.type = "button";
    nextBtn.disabled = index >= total - 1;
    nextBtn.title = "Next turn in timeline";
    nextBtn.addEventListener("click", () => openAtIndex(index + 1));

    center.append(prevBtn, counter, nextBtn);

    const right = node("div", "", "td-head-right");
    const closeBtn = node("button", "✕ Close", "btn bobi-btn small");
    closeBtn.type = "button";
    closeBtn.addEventListener("click", closeDrawer);
    right.append(closeBtn);

    drawerHead.append(left, center, right);
  }

  function renderSummary(data) {
    summary.replaceChildren();

    // 1. KPI Metric Tiles
    const tiles = node("div", "", "metrics-tiles");
    const reportedCost = data.totals.reported_cost_usd;
    const estCost = data.totals.estimated_cost_usd;
    const hasSpend = reportedCost != null || estCost != null;
    let costDisplay = "$0.00";
    let costSub = "no spend recorded";
    let costBadge = "";
    if (hasSpend) {
      const sum = (reportedCost || 0) + (estCost || 0);
      costDisplay = `$${sum.toFixed(4)}`;
      if (reportedCost != null && estCost != null && estCost > 0) {
        costSub = `$${reportedCost.toFixed(4)} exact + $${estCost.toFixed(4)} est`;
        costBadge = "EXACT+EST";
      } else if (reportedCost != null) {
        costSub = "provider stream · exact";
        costBadge = "EXACT";
      } else {
        costSub = "model pricing estimate";
        costBadge = "EST";
      }
    }

    const fields = [
      ["input_tokens", "Input tokens", "clay", data.totals.cache_read_input_tokens ? `+ ${number(data.totals.cache_read_input_tokens)} cache read` : "canonical input", "tile-input", number(data.totals.input_tokens)],
      ["output_tokens", "Output tokens", "accent", "provider stream · exact", "tile-output", number(data.totals.output_tokens)],
      ["cache_read_input_tokens", "Prompt Cache Hits", "accent", data.totals.input_tokens ? `${((data.totals.cache_read_input_tokens || 0) / data.totals.input_tokens * 100).toFixed(1)}% cache hit ratio` : "prompt cache hits", "tile-cache-read", number(data.totals.cache_read_input_tokens)],
      ["total_cost_usd", "Total Spend (USD)", costBadge, costSub, "tile-cost", costDisplay]
    ];

    fields.forEach(([field, label, badgeText, subtext, modClass, displayVal]) => {
      const tile = node("div", "", `metrics-tile ${modClass}`);
      const tileHead = node("div", "", "tile-head");
      tileHead.append(node("span", label, "tile-label"));
      if (badgeText) {
        const isClay = badgeText === "RAW";
        tileHead.append(node("span", badgeText, `tile-badge ${isClay ? "clay" : "accent"}`));
      }
      const value = node("strong", displayVal, "bobi-tnum");
      if (field === "total_cost_usd") value.classList.add("text-green");
      const sub = node("span", subtext, "tile-sub");
      tile.append(tileHead, value, sub);
      tiles.append(tile);
    });
    summary.append(tiles);

    // 2. Analytics 2-Col Grid (Routing Distribution & Provenance)
    const grid = node("div", "", "metrics-analytics-grid");

    // Card A: Model Routing Distribution
    const routingCard = node("div", "", "analytics-card");
    const rHead = node("div", "", "analytics-card-head");
    const rTitle = node("span", "", "analytics-card-title");
    rTitle.innerHTML = `<svg viewBox="0 0 24 24"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path></svg> Model Routing Distribution (JEV)`;
    const rBadge = node("span", data.routing.routed_turns > 0 ? "JEV active" : "passthrough", "chip");
    rHead.append(rTitle, rBadge);
    routingCard.append(rHead);

    // Ratio Meter
    const totalTurns = data.routing.turns || 1;
    const meterWrap = node("div", "", "ratio-meter-wrap");
    const models = data.routing.models || [];

    if (models.length > 0 || data.routing.fallback_turns) {
      const meterBar = node("div", "", "ratio-meter-bar");
      const meterLegend = node("div", "", "ratio-meter-legend");

      models.forEach(grp => {
        const pct = Math.round((grp.turns / totalTurns) * 100);
        const isFlash = grp.model_selected.includes("flash");
        const seg = node("div", "", `ratio-meter-seg ${isFlash ? "flash" : "pro"}`);
        seg.style.width = `${pct}%`;
        meterBar.append(seg);

        const leg = node("span", "", "legend-item");
        leg.append(node("span", "", `legend-dot ${isFlash ? "flash" : "pro"}`), document.createTextNode(` ${grp.model_selected}: ${grp.turns} turns (${pct}%)`));
        meterLegend.append(leg);
      });

      if (data.routing.fallback_turns) {
        const fbPct = Math.round((data.routing.fallback_turns / totalTurns) * 100);
        const seg = node("div", "", "ratio-meter-seg fallback");
        seg.style.width = `${fbPct}%`;
        meterBar.append(seg);

        const leg = node("span", "", "legend-item");
        leg.innerHTML = `<span class="legend-dot fallback"></span> Fallback: ${data.routing.fallback_turns} turns (${fbPct}%)`;
        meterLegend.append(leg);
      }
      meterWrap.append(meterBar, meterLegend);
    } else {
      const standby = node("div", "", "ratio-standby-state");
      standby.innerHTML = `<span class="standby-dot"></span><span class="standby-text">Default session routing · no policy overrides in window</span>`;
      meterWrap.append(standby);
    }
    routingCard.append(meterWrap);

    // Routing key-values
    pair(routingCard, "Turns with / without a route", `${data.routing.routed_turns} / ${data.routing.turns - data.routing.routed_turns}`);
    pair(routingCard, "Distinct recorded policy calls / fallback turns", `${data.routing.policy_calls} / ${data.routing.fallback_turns || 0}`);
    pair(routingCard, "Mean router latency (ms)", number(data.routing.mean_router_latency_ms));
    routingCard.append(node("p", "Router decisions evaluate TypeSafe JEV policies with fallback protection on initial session turns.", "metrics-note"));
    grid.append(routingCard);

    // Card B: Stream Coverage & Telemetry Health
    const healthCard = node("div", "", "analytics-card");
    const hHead = node("div", "", "analytics-card-head");
    const hTitle = node("span", "", "analytics-card-title");
    hTitle.innerHTML = `<svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg> Telemetry Health & Coverage`;
    const hStatus = node("span", data.collector.status === "running" ? "● live" : `● ${data.collector.status || "idle"}`, "chip");
    hHead.append(hTitle, hStatus);
    healthCard.append(hHead);

    const coverage = data.coverage;
    pair(healthCard, "Invocation coverage · exact / estimated / unknown", `${coverage.exact_invocations} / ${coverage.estimated_invocations} / ${coverage.unknown_invocations}`);
    pair(healthCard, "Usage granularity", coverage.usage_granularity);
    pair(healthCard, "Reported cost (USD)", data.totals.reported_cost_usd);
    pair(healthCard, "Estimated cost (USD)", data.totals.estimated_cost_usd);
    pair(healthCard, "Collector status / import lag (ms)", `${data.collector.status || "not recorded"} / ${number(data.collector.import_lag_ms)}`);
    pair(healthCard, "Global reconciliation errors / uncovered turns", `${number(data.collector.reconciliation_errors)} / ${number(data.collector.uncovered_turns)}`);
    healthCard.append(node("p", "Totals use canonical usage, which may include estimates. Unknown counters stay unknown. Call IDs may be reused across turns; routing selects once per fresh session.", "metrics-note"));
    grid.append(healthCard);

    summary.append(grid);
  }

  async function load(reset = false, clearDetail = reset) {
    if (stopped) return;
    if (pending) {
      if (reset) { queued = true; queuedClear ||= clearDetail; }
      return;
    }
    pending = true;
    refreshBtn.disabled = true;

    if (reset || !params) {
      cursor = "";
      const end = new Date();
      // Default to 30-day window, no time buttons needed
      params = new URLSearchParams({
        from: new Date(end.getTime() - 30 * 86400000).toISOString(),
        to: end.toISOString(),
      });
      if (clearDetail) { detailController?.abort(); closeDrawer(); }
    }

    const query = new URLSearchParams(params);
    if (cursor) query.set("cursor", cursor);

    const results = await Promise.all([
      api(`${base}/summary?${params}`, { signal: controller.signal }),
      api(`${base}/turns?${query}`, { signal: controller.signal })
    ]);

    pending = false;
    if (stopped) return;
    refreshBtn.disabled = false;

    if (queued) {
      const clear = queuedClear;
      queued = false;
      queuedClear = false;
      load(true, clear);
      return;
    }

    const failure = results.find(r => !r.ok);
    if (failure) {
      note.textContent = `${failure.data?.error || "Could not read metrics."} Previous values, if any, are stale.`;
    } else {
      renderSummary(results[0].data);

      const allTurns = results[1].data.turns || [];
      currentTurns = allTurns;

      turns.replaceChildren();
      const tHead = node("div", "", "metrics-table-head");
      tHead.append(node("h3", `Recent turns across fleet (${allTurns.length})`));
      turns.append(tHead);

      turnRows(turns, currentTurns, (turn, idx) => {
        openAtIndex(idx);
      }, name);

      if (!currentTurns.length) {
        turns.append(node("p", "No recorded turns in this window.", "runs-empty"));
      }
      note.textContent = `Showing ${currentTurns.length} turns · Click any row to inspect turn details`;

      pager.replaceChildren();
      const nextCursor = results[1].data.next_cursor;
      if (cursor) {
        const first = node("button", "latest turns", "btn bobi-btn small");
        first.type = "button";
        first.addEventListener("click", () => load(true));
        pager.append(first);
      }
      if (nextCursor) {
        const next = node("button", "older turns", "btn bobi-btn small");
        next.type = "button";
        next.addEventListener("click", () => { if (!pending) { cursor = nextCursor; load(); } });
        pager.append(next);
      }
    }

    clearTimeout(timer);
    timer = setTimeout(() => { if (!document.hidden && !cursor) load(true, false); }, 10000);
  }

  let currentRoutingConfig = null;
  async function fetchRoutingConfig() {
    try {
      const res = await api(`/api/agents/${encodeURIComponent(name)}/routing/config`, { signal: controller.signal });
      if (res.ok) {
        currentRoutingConfig = res.data;
        updateJevHeaderButton();
      }
    } catch (e) {
      console.warn("Could not load routing config:", e);
    }
  }

  function updateJevHeaderButton() {
    if (!currentRoutingConfig) return;
    const isEn = Boolean(currentRoutingConfig.enabled);
    const mode = currentRoutingConfig.mode || "shadow";
    jevHeaderBtn.className = `btn bobi-btn small jev-header-btn ${isEn ? `active mode-${mode}` : "disabled"}`;
    if (!isEn) {
      jevHeaderBtn.textContent = "⚙️ JEV: Disabled";
      jevHeaderBtn.title = `TypeSafe JEV Dynamic Routing is disabled for ${name}. Click to configure.`;
    } else if (mode === "enforce") {
      jevHeaderBtn.textContent = "⚡ JEV: Enforce";
      jevHeaderBtn.title = `TypeSafe JEV is actively enforcing model routing for ${name}. Click to configure.`;
    } else {
      jevHeaderBtn.textContent = "👁️ JEV: Shadow";
      jevHeaderBtn.title = `TypeSafe JEV is in shadow observer mode for ${name}. Click to configure.`;
    }
  }

  function openJevConfigModal() {
    renderJevConfigModalBody();
    setJevModalTab("status");
    jevModalBackdrop.hidden = false;
    jevModalBackdrop.classList.add("open");
  }

  function closeJevConfigModal() {
    closeConfirmPopup();
    jevModalBackdrop.hidden = true;
    jevModalBackdrop.classList.remove("open");
  }

  function renderJevConfigModalBody() {
    const cfg = currentRoutingConfig || {
      enabled: false,
      mode: "shadow",
      control_model: "ds/deepseek-flash",
      candidate_models: ["ds/deepseek-flash", "ds/deepseek-v4-pro"],
      roles: ["director", "engineer"],
      config_path: "run/.env",
      raw_json: ""
    };

    jevModalBody.innerHTML = `
      <form class="jev-config-form">
        <div class="jev-modal-body-scroll">
          <!-- Tab Pane 1: Overview & Status -->
          <div class="jev-tab-pane pane-status" id="jev-pane-status">
          <!-- Main Toggle Box -->
          <div class="jev-form-group jev-toggle-box">
            <label class="jev-toggle-label">
              <input type="checkbox" id="jev-input-enabled" ${cfg.enabled ? "checked" : ""}>
              <span class="jev-switch-slider"></span>
              <div>
                <span class="jev-switch-title">Enable TypeSafe JEV Dynamic Model Routing</span>
                <p class="jev-switch-subtext">Toggle JEV routing on or off for this agent fleet. Model instruction trust, task criteria, and candidate models are configured in <code>run/.env</code>.</p>
              </div>
            </label>
          </div>

          <!-- Policy Summary Card -->
          <div class="jev-summary-card">
            <div class="jev-summary-row">
              <span class="jev-summary-label">Current Status</span>
              <span class="jev-summary-val">
                <span class="jev-status-pill ${cfg.enabled ? (cfg.mode === 'enforce' ? 'enforce' : 'shadow') : 'disabled'}">
                  ${cfg.enabled ? (cfg.mode === 'enforce' ? '⚡ Enforce Active' : '👁️ Shadow Active') : '⚙️ Disabled'}
                </span>
              </span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Fallback Model</span>
              <span class="jev-summary-val font-mono">${escapeHtml(cfg.control_model || "ds/deepseek-flash")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Candidate Models</span>
              <span class="jev-summary-val font-mono">${escapeHtml((cfg.candidate_models || []).join(", ") || "—")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Scoped Roles</span>
              <span class="jev-summary-val">${escapeHtml((cfg.roles || []).join(", ") || "—")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Config Location</span>
              <span class="jev-summary-val font-mono text-muted">${escapeHtml(cfg.config_path || "run/.env")}</span>
            </div>
          </div>

          <!-- Raw JSON Inspector / Preview -->
          <div class="jev-form-group">
            <div class="jev-code-head">
              <label class="jev-field-label">Active JEV Policy Configuration (<code>BOBI_METRICS_EXPERIMENT_JSON</code>)</label>
              <div class="jev-code-head-actions">
                <button type="button" class="btn bobi-btn quiet jev-btn-jump-to-guide">📖 How to configure? View Guide →</button>
                <button type="button" class="btn bobi-btn quiet jev-btn-copy-json" title="Copy raw JSON configuration">Copy JSON</button>
              </div>
            </div>
            <pre class="jev-code-preview"><code>${escapeHtml(cfg.raw_json || '{\n  "note": "No BOBI_METRICS_EXPERIMENT_JSON found in run/.env"\n}')}</code></pre>
            <p class="jev-form-hint">💡 <strong>Manual Customization:</strong> To tune individual model trust criteria, prompt instructions, candidate models, or confidence thresholds, edit <code>BOBI_METRICS_EXPERIMENT_JSON</code> in your <code>run/.env</code>.</p>
          </div>

          <div class="jev-restart-notice">
            <span class="jev-notice-icon">💡</span>
            <span><strong>Note on applying changes:</strong> Persistent sessions (such as the active <code>director</code>) keep their model throughout execution. After saving, restart the agent to re-initialize active sessions under the new policy.</span>
          </div>
        </div>

        <!-- Tab Pane 2: .env Configuration Guide -->
        <div class="jev-tab-pane pane-guide" id="jev-pane-guide" hidden>
          <div class="jev-guide-container">
            <div class="jev-guide-intro">
              <div class="jev-guide-badge">Architecture & Manual Config</div>
              <h4>Configuring TypeSafe JEV in <code>run/.env</code></h4>
              <p>Because TypeSafe JEV uses custom model instruction trust, reasoning depth thresholds, and confidence boundaries that vary per model family, real-world policies are configured directly in your agent's <code>run/.env</code> file.</p>
            </div>

            <div class="jev-guide-section">
              <h5>1. Required Environment Variables</h5>
              <div class="jev-guide-table-wrap">
                <table class="jev-guide-table">
                  <thead>
                    <tr>
                      <th>Variable Name</th>
                      <th>Description</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td><code>BOBI_METRICS_EXPERIMENT_JSON</code></td>
                      <td>JSON specification defining control/candidate models, routing mode, scoped roles, and criteria.</td>
                      <td><span class="badge-req">Required</span></td>
                    </tr>
                    <tr>
                      <td><code>TYPESAFE_API_KEY</code></td>
                      <td>API key authorizing classification requests with TypeSafe JEV routing service.</td>
                      <td><span class="badge-req">Required</span></td>
                    </tr>
                    <tr>
                      <td><code>BOBI_METRICS_ASSIGNMENT_SECRET</code></td>
                      <td>Salt string used for consistent deterministic routing hashes across turns.</td>
                      <td><span class="badge-opt">Recommended</span></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div class="jev-guide-section">
              <h5>2. Key Policy Properties Explained</h5>
              <ul class="jev-guide-keys-list">
                <li>
                  <strong><code>variants.control</code></strong>: The default fallback model (e.g. <code>ds/deepseek-flash</code>). All sessions start with this model or fall back to it if JEV is disabled or unreachable.
                </li>
                <li>
                  <strong><code>variants.treatment_jev</code></strong>: Dynamic router configuration with candidate models (e.g. <code>ds/deepseek-v4-pro</code>) evaluated by the TypeSafe classifier.
                </li>
                <li>
                  <strong><code>mode</code></strong>:
                  <ul>
                    <li><code>"enforce"</code>: Actively switches session to chosen candidate model when complexity criteria are met.</li>
                    <li><code>"shadow"</code>: Runs background evaluation and records telemetry without switching the active model (ideal for dry-runs).</li>
                  </ul>
                </li>
                <li>
                  <strong><code>scope.roles</code></strong>: Specifies which agent session roles participate in JEV routing (e.g. <code>["director", "engineer"]</code>). Exclude <code>"director"</code> if you only want subagents dynamically routed.
                </li>
                <li>
                  <strong><code>options.criteria</code> (Instruction Trust)</strong>: Define model-specific trust instructions (e.g. <code>instruction_trust: "strict"</code>, <code>min_confidence: 0.75</code>) instructing JEV when to trust a lighter model vs when task complexity warrants upgrading to a heavier model.
                </li>
              </ul>
            </div>

            <div class="jev-guide-section">
              <div class="jev-guide-sec-head">
                <h5>3. Ready-to-Use <code>run/.env</code> Template</h5>
                <button type="button" class="btn bobi-btn small quiet jev-btn-copy-template" title="Copy template configuration">Copy Template</button>
              </div>
              <pre class="jev-code-preview jev-guide-code"><code>BOBI_METRICS_EXPERIMENT_JSON='{
  "experiment_id": "jev_prod_v1",
  "mode": "enforce",
  "variants": {
    "control": { "model": "ds/deepseek-flash" },
    "treatment_jev": {
      "provider": "typesafe",
      "candidate_models": ["ds/deepseek-flash", "ds/deepseek-v4-pro"],
      "options": {
        "criteria": {
          "min_confidence": 0.75,
          "instruction_trust": "strict"
        }
      }
    }
  },
  "scope": {
    "roles": ["director", "engineer"],
    "entry_points": ["cli", "chat"]
  }
}'
TYPESAFE_API_KEY=your_typesafe_api_key_here
BOBI_METRICS_ASSIGNMENT_SECRET=production_salt_secret</code></pre>
            </div>

            <div class="jev-guide-footer-tip">
              <span class="jev-notice-icon">🔄</span>
              <div>
                <strong>Applying Changes:</strong> After updating <code>run/.env</code>, click <strong>Save & Restart Agent</strong> (or run <code>bobi agent restart ${name}</code>) to reinitialize sessions with your updated policy.
              </div>
            </div>
          </div>
        </div>
        </div>

        <!-- Pinned Form Actions Bar -->
        <div class="jev-modal-footer">
          <span class="jev-save-msg" role="status"></span>
          <div class="jev-actions-right">
            <button type="button" class="btn bobi-btn quiet jev-btn-cancel">Cancel</button>
            <button type="submit" class="btn bobi-btn primary jev-btn-save">Save Changes</button>
          </div>
        </div>
      </form>
    `;

    const form = jevModalBody.querySelector(".jev-config-form");
    const cancelBtn = form.querySelector(".jev-btn-cancel");
    cancelBtn.addEventListener("click", closeJevConfigModal);

    const jumpToGuideBtn = form.querySelector(".jev-btn-jump-to-guide");
    if (jumpToGuideBtn) {
      jumpToGuideBtn.addEventListener("click", () => {
        setJevModalTab("guide");
      });
    }

    const copyBtn = form.querySelector(".jev-btn-copy-json");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        const textToCopy = cfg.raw_json || "";
        if (!textToCopy) return;
        navigator.clipboard.writeText(textToCopy).then(() => {
          copyBtn.textContent = "Copied! ✓";
          setTimeout(() => { copyBtn.textContent = "Copy JSON"; }, 1500);
        });
      });
    }

    const copyTemplateBtn = form.querySelector(".jev-btn-copy-template");
    if (copyTemplateBtn) {
      copyTemplateBtn.addEventListener("click", () => {
        const tpl = `BOBI_METRICS_EXPERIMENT_JSON='{
  "experiment_id": "jev_prod_v1",
  "mode": "enforce",
  "variants": {
    "control": { "model": "ds/deepseek-flash" },
    "treatment_jev": {
      "provider": "typesafe",
      "candidate_models": ["ds/deepseek-flash", "ds/deepseek-v4-pro"],
      "options": {
        "criteria": {
          "min_confidence": 0.75,
          "instruction_trust": "strict"
        }
      }
    }
  },
  "scope": {
    "roles": ["director", "engineer"],
    "entry_points": ["cli", "chat"]
  }
}'
TYPESAFE_API_KEY=your_typesafe_api_key_here
BOBI_METRICS_ASSIGNMENT_SECRET=production_salt_secret`;
        navigator.clipboard?.writeText(tpl).then(() => {
          copyTemplateBtn.textContent = "Copied! ✓";
          setTimeout(() => { copyTemplateBtn.textContent = "Copy Template"; }, 1500);
        });
      });
    }

    const saveMsg = form.querySelector(".jev-save-msg");
    const saveBtn = form.querySelector(".jev-btn-save");

    const confirmCancelBtn = jevConfirmOverlay.querySelector(".jev-confirm-btn-cancel");
    const confirmRestartBtn = jevConfirmOverlay.querySelector(".jev-confirm-btn-restart");
    const confirmStatusMsg = jevConfirmOverlay.querySelector(".jev-confirm-status-msg");

    if (confirmCancelBtn) confirmCancelBtn.onclick = closeConfirmPopup;
    if (confirmRestartBtn) confirmRestartBtn.onclick = () => handleSave(true);

    async function handleSave(restartAfter = true) {
      saveBtn.disabled = true;
      if (confirmCancelBtn) confirmCancelBtn.disabled = true;
      if (confirmRestartBtn) confirmRestartBtn.disabled = true;

      const actionText = restartAfter ? "Saving changes & restarting agent…" : "Saving changes to .env…";
      saveMsg.textContent = actionText;
      saveMsg.className = "jev-save-msg";
      if (confirmStatusMsg) {
        confirmStatusMsg.textContent = actionText;
        confirmStatusMsg.className = "jev-confirm-status-msg";
      }

      const enabled = form.querySelector("#jev-input-enabled").checked;

      try {
        const res = await api(`/api/agents/${encodeURIComponent(name)}/routing/config`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled })
        });

        if (res.ok) {
          currentRoutingConfig = res.data;
          updateJevHeaderButton();

          if (restartAfter) {
            const restartMsg = "Saved! Restarting agent…";
            saveMsg.textContent = restartMsg;
            if (confirmStatusMsg) confirmStatusMsg.textContent = restartMsg;
            try {
              const restartRes = await api(`/api/agents/${encodeURIComponent(name)}/restart`, {
                method: "POST"
              });
              const okMsg = "Saved changes & Agent restarted! ✓";
              saveMsg.textContent = okMsg;
              saveMsg.className = "jev-save-msg text-green";
              if (confirmStatusMsg) {
                confirmStatusMsg.textContent = okMsg;
                confirmStatusMsg.className = "jev-confirm-status-msg text-green";
              }
            } catch (rErr) {
              const warnMsg = "Saved to .env (Restart agent via header/CLI). ✓";
              saveMsg.textContent = warnMsg;
              saveMsg.className = "jev-save-msg text-green";
              if (confirmStatusMsg) {
                confirmStatusMsg.textContent = warnMsg;
                confirmStatusMsg.className = "jev-confirm-status-msg text-green";
              }
            }
          } else {
            const savedMsg = "Saved changes to .env! (Restart agent later to apply). ✓";
            saveMsg.textContent = savedMsg;
            saveMsg.className = "jev-save-msg text-green";
            if (confirmStatusMsg) {
              confirmStatusMsg.textContent = savedMsg;
              confirmStatusMsg.className = "jev-confirm-status-msg text-green";
            }
          }

          setTimeout(() => {
            closeConfirmPopup();
            closeJevConfigModal();
            load(true);
          }, 850);
        } else {
          const errText = res.data?.error || "Failed to update configuration.";
          saveMsg.textContent = errText;
          saveMsg.className = "jev-save-msg text-red";
          if (confirmStatusMsg) {
            confirmStatusMsg.textContent = errText;
            confirmStatusMsg.className = "jev-confirm-status-msg text-red";
          }
          saveBtn.disabled = false;
          if (confirmCancelBtn) confirmCancelBtn.disabled = false;
          if (confirmLaterBtn) confirmLaterBtn.disabled = false;
          if (confirmRestartBtn) confirmRestartBtn.disabled = false;
        }
      } catch (err) {
        const netErr = "Network error updating configuration.";
        saveMsg.textContent = netErr;
        saveMsg.className = "jev-save-msg text-red";
        if (confirmStatusMsg) {
          confirmStatusMsg.textContent = netErr;
          confirmStatusMsg.className = "jev-confirm-status-msg text-red";
        }
        saveBtn.disabled = false;
        if (confirmCancelBtn) confirmCancelBtn.disabled = false;
        if (confirmLaterBtn) confirmLaterBtn.disabled = false;
        if (confirmRestartBtn) confirmRestartBtn.disabled = false;
      }
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      openConfirmPopup();
    });
  }

  fetchRoutingConfig();
  load(true);

  return () => {
    stopped = true;
    controller.abort();
    detailController?.abort();
    clearTimeout(timer);
    document.removeEventListener("keydown", handleDrawerKey);
  };
}
