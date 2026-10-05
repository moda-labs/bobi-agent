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

function pair(container, label, value) {
  const row = node("div", "", "metrics-pair");
  row.append(node("span", label), node("span", value == null ? "not recorded" : String(value), "bobi-tnum"));
  container.append(row);
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
    "Started", "Session / lifecycle", "Variant", "Policy",
    "Recommendation", "Selected / provider model", "Tokens (in / out)", "Status"
  ], turns.map(turn => {
    const d = new Date(turn.started_at_us / 1000);
    const timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const dateStr = d.toLocaleDateString([], { month: "short", day: "numeric" });
    const button = node("button", "", "turn-inspect-btn");
    button.type = "button";
    button.setAttribute("aria-label", `Inspect turn from ${dateStr}, ${timeStr}`);
    button.innerHTML = `<span class="tib-time">${dateStr}, ${timeStr}</span><span class="tib-arrow">↗</span>`;
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      open(turn);
    });
    const confidence = turn.confidence == null ? "not recorded" : Number(turn.confidence).toFixed(3);
    const models = [...new Set(turn.invocations.map(inv => inv.model_selected || "unknown"))];

    const sessionCell = node("div", "", "cell-session");
    sessionCell.append(node("strong", turn.session_name || "session"), " / ");
    const shortId = turn.session_id ? (turn.session_id.length > 20 ? turn.session_id.slice(0, 16) + "…" : turn.session_id) : "";
    const idSpan = node("span", shortId, "subtle-id");
    if (turn.session_id) idSpan.title = turn.session_id;
    sessionCell.append(idSpan);

    const pol = `${policyState(turn)} · confidence ${confidence}`;
    const inTok = turn.usage?.input_tokens;
    const outTok = turn.usage?.output_tokens;
    const cacheRead = turn.usage?.cache_read_input_tokens || 0;
    const cachePct = inTok ? Math.round((cacheRead / inTok) * 100) : 0;
    const tokStr = inTok != null && cacheRead > 0
      ? `${number(inTok)} / ${number(outTok)} (${cachePct}% cached)`
      : `${number(inTok)} / ${number(outTok)}`;

    const statusBadge = node("span", turn.status, `status-badge ${turn.status}`);

    return {
      turn,
      onClick: () => open(turn),
      cells: [
        button,
        sessionCell,
        turn.variant_id,
        pol,
        turn.recommended_model,
        `${turn.model_selected || "not recorded"} / ${models.join(", ") || "not recorded"}`,
        tokStr,
        statusBadge
      ]
    };
  }));
}

export function renderTurnMetrics(container, data, section = "all", row = {}, onBack = null) {
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
  banner.innerHTML = `
    <div class="ts-item"><span class="ts-lbl">Session</span><span class="ts-val">${row.session_name || data.turn.session_id.slice(0, 16) + '…'}</span></div>
    <div class="ts-item"><span class="ts-lbl">Duration</span><span class="ts-val bobi-tnum">${durStr}</span></div>
    <div class="ts-item"><span class="ts-lbl">Invocations</span><span class="ts-val bobi-tnum">${(data.invocations || []).length} calls</span></div>
    <div class="ts-item"><span class="ts-lbl">Status</span><span class="ts-val"><span class="status-badge ${data.turn.status}">${data.turn.status}</span></span></div>
  `;
  container.append(banner);

  pair(container, "Telemetry session ID", data.turn.session_id);
  pair(container, "Telemetry turn ID", data.turn.turn_id);

  const routingCount = (data.router_decisions?.length || 0) + (data.invocations?.length || 0);
  const toolCount = data.tool_executions?.length || 0;
  const usageCount = data.usage_measurements?.length || 0;
  const costCount = data.cost_measurements?.length || 0;

  const tabDefs = [
    { id: "all", label: "All" },
    { id: "routing", label: `Routing & Calls (${routingCount})` },
    { id: "tools", label: `Tools Executed (${toolCount})` },
    { id: "usage", label: `Token Usage (${usageCount})` },
    { id: "cost", label: `Cost (${costCount})` },
  ];

  const panelRouting = node("div", "", "detail-tab-panel panel-routing");
  const panelTools = node("div", "", "detail-tab-panel panel-tools");
  const panelUsage = node("div", "", "detail-tab-panel panel-usage");
  const panelCost = node("div", "", "detail-tab-panel panel-cost");

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
    panelUsage.hidden = !(tabId === "all" || tabId === "usage");
    panelCost.hidden = !(tabId === "all" || tabId === "cost");
  }

  tabButtons.forEach(btn => tabsBar.append(btn));

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
      usageMap.set(u.invocation_id, u);
    }
  });

  // Routing panel
  panelRouting.append(node("h3", "Router decision"));
  const decisions = data.router_decisions || [];
  if (!decisions.length) {
    pair(panelRouting, "Policy", row.session_fallback_reason || "No decision recorded; routing may be disabled or out of scope.");
  } else {
    table(panelRouting, [
      "Assignment", "Policy mode / status", "Recommendation", "Confidence", "Selected model", "Fallback", "Latency (ms)"
    ], decisions.map(decision => [
      decision.variant_id,
      `${decision.policy_mode || "not recorded"} / ${policyState({ ...row, ...decision })}`,
      decision.recommended_model,
      decision.confidence == null ? "not recorded" : String(decision.confidence),
      decision.model_selected,
      decision.fallback_reason || row.session_fallback_reason || "none recorded",
      `${decision.router_latency_ms ?? "—"} router / ${decision.policy_latency_ms ?? "—"} policy`
    ]));
  }

  // LLM Invocations section
  const invocations = data.invocations || [];
  panelRouting.append(node("h3", `LLM Invocations (${invocations.length} calls in this turn)`));
  panelRouting.append(node("p", "Sequential model generation rounds within this turn (tool execution loop, thinking steps, and final response).", "metrics-note"));

  if (!invocations.length) {
    panelRouting.append(node("p", "No LLM invocations recorded."));
  } else {
    table(panelRouting, [
      "#", "Provider", "Model (selected / req)", "Latency", "Tokens (in / out)", "Action / Stop reason", "Status"
    ], invocations.map((inv, idx) => {
      const stepNum = `#${inv.invocation_index != null ? inv.invocation_index + 1 : idx + 1}`;
      const lat = inv.provider_latency_ms ?? inv.wall_duration_ms;
      const ttft = inv.time_to_first_token_ms;
      const latStr = lat != null ? `${lat}ms` : "—";
      const ttftStr = ttft != null ? ` (${ttft}ms TTFT)` : "";
      const u = usageMap.get(inv.invocation_id);
      const tokStr = u ? `${number(u.input_tokens)} / ${number(u.output_tokens)}` : "—";
      const tools = toolMap.get(inv.invocation_id);

      const modelReq = inv.model_requested || "";
      const modelSel = inv.model_selected || "";
      const modelCell = node("div", "", "cell-model");
      modelCell.append(node("span", modelSel || "not recorded", "model-badge"));
      if (modelReq) {
        const isMatch = modelReq === modelSel || modelReq.replace(/^[^/]+\//, "") === modelSel;
        const reqSpan = node("span", isMatch ? ` (${modelReq})` : ` (req: ${modelReq})`, isMatch ? "subtle-id" : "routed-from-badge");
        reqSpan.title = isMatch ? `Requested as ${modelReq}` : `Router chose ${modelSel} (requested: ${modelReq})`;
        modelCell.append(reqSpan);
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

      return [
        stepNum,
        inv.provider || "not recorded",
        modelCell,
        `${latStr}${ttftStr}`,
        tokStr,
        actionCell,
        inv.status
      ];
    }));
  }

  // Tool executions panel
  panelTools.append(node("h3", `Tool Executions (${toolCount})`));
  panelTools.append(node("p", "Tools called by the agent during this turn (terminal commands, file reading/writing, web searches).", "metrics-note"));
  if (data.tool_executions && data.tool_executions.length) {
    table(panelTools, [
      "#", "Tool name", "Kind", "Duration", "Payload (in / out)", "Output tokens", "Status"
    ], data.tool_executions.map((t, idx) => {
      const dur = (t.started_at_us && t.ended_at_us) ? `${Math.round((t.ended_at_us - t.started_at_us) / 1000)}ms` : "—";
      const hasIn = t.input_bytes != null;
      const hasOut = t.output_bytes != null;
      let bytes = "—";
      if (hasIn || hasOut) {
        bytes = `${hasIn ? number(t.input_bytes) + " B" : "0 B"} in / ${hasOut ? number(t.output_bytes) + " B" : "0 B"} out`;
      }
      const tokens = t.output_estimated_tokens != null ? number(t.output_estimated_tokens) : "—";
      return [
        `#${idx + 1}`,
        t.tool_name,
        t.tool_kind || "—",
        dur,
        bytes,
        tokens,
        t.status
      ];
    }));
  } else {
    panelTools.append(node("p", "No tool executions recorded in this turn."));
  }

  // Usage panel
  panelUsage.append(node("h3", "Usage measurements"));
  panelUsage.append(node("p", "Provenance rows below are not additive totals. Cache counters are included in normalized input; reasoning is included in output.", "metrics-note"));
  if (data.usage_measurements && data.usage_measurements.length) {
    const turnUsage = data.usage_measurements.find(u => u.scope === "turn") || {};
    const totalIn = turnUsage.input_tokens || data.usage_measurements.reduce((acc, u) => u.scope === "invocation" ? acc + (u.input_tokens || 0) : acc, 0);
    const totalOut = turnUsage.output_tokens || data.usage_measurements.reduce((acc, u) => u.scope === "invocation" ? acc + (u.output_tokens || 0) : acc, 0);
    const cacheRead = turnUsage.cache_read_input_tokens || data.usage_measurements.reduce((acc, u) => u.scope === "invocation" ? acc + (u.cache_read_input_tokens || 0) : acc, 0);
    const cachePct = totalIn ? ((cacheRead / totalIn) * 100).toFixed(1) : "0.0";

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
        <span class="kpi-lbl">Invocations</span>
        <span class="kpi-val bobi-tnum">${(data.invocations || []).length}</span>
      </div>
    `;
    panelUsage.append(kpiGrid);

    table(panelUsage, [
      "Scope", "Model", "Source", "Basis", "Input", "Output", "Cache read", "Cache write", "Reasoning"
    ], data.usage_measurements.map(usage => {
      const isTurn = usage.scope === "turn";
      return {
        className: isTurn ? "highlight-turn-row" : "",
        cells: [
          usage.supersedes_measurement_id ? `${usage.scope} (replaces #${usage.supersedes_measurement_id})` : usage.scope,
          usage.model,
          usage.measurement_source,
          usage.is_estimated ? "estimated" : "provider-reported",
          number(usage.input_tokens),
          number(usage.output_tokens),
          number(usage.cache_read_input_tokens),
          number(usage.cache_write_input_tokens),
          number(usage.reasoning_output_tokens)
        ]
      };
    }));
  } else {
    panelUsage.append(node("p", "No usage measurements recorded."));
  }

  // Cost panel
  panelCost.append(node("h3", "Cost measurements"));
  if (data.cost_measurements && data.cost_measurements.length) {
    table(panelCost, ["Scope", "Model", "USD", "Source", "Basis"], data.cost_measurements.map(cost =>
      [cost.scope, cost.model, cost.amount_usd, cost.measurement_source, cost.is_estimated ? "estimated" : "reported"]));
  } else {
    panelCost.append(node("p", "No cost measurements recorded."));
  }

  container.append(tabsBar, panelRouting, panelTools, panelUsage, panelCost);
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
    turnRows(container, result.data.turns, async turn => {
      const request = ++detailRequest;
      const detail = await api(`${base}/turns/${encodeURIComponent(turn.turn_id)}`, { signal });
      if (signal.aborted || request !== detailRequest) return;
      if (!detail.ok) { container.replaceChildren(node("p", detail.data?.error || "Could not read turn.")); return; }
      renderTurnMetrics(container, detail.data, section, turn, () => load());
    });
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
  const refreshBtn = node("button", "refresh", "btn bobi-btn small");
  refreshBtn.type = "button";
  ahRight.append(collectorChip, refreshBtn);

  ahBody.append(ahName, ahRight);
  header.append(ahBody);

  // Main content container
  const content = node("div", "", "metrics-content");

  // Toolbar card
  const toolbarCard = node("div", "", "metrics-toolbar-card");
  const filters = node("div", "", "metrics-filters");

  const modelField = node("div", "", "metrics-filter-field");
  modelField.innerHTML = `<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"></circle><path d="m16 16 4 4"></path></svg>`;
  const modelInput = node("input");
  modelInput.placeholder = "Provider model filter";
  modelInput.setAttribute("aria-label", "Provider model filter");
  modelField.append(modelInput);

  const sessionField = node("div", "", "metrics-filter-field");
  sessionField.innerHTML = `<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h12"></path></svg>`;
  const sessionInput = node("input");
  sessionInput.value = session;
  sessionInput.placeholder = "Session name (all lifecycles)";
  sessionInput.setAttribute("aria-label", "Session name");
  sessionField.append(sessionInput);

  const windowSelect = node("select");
  windowSelect.setAttribute("aria-label", "Time window");
  [["24 hours", 1], ["7 days", 7], ["31 days", 31]].forEach(([lbl, d]) => {
    const opt = node("option", lbl); opt.value = d; windowSelect.append(opt);
  });
  windowSelect.style.display = "none";

  const timeTabs = node("div", "", "metrics-time-tabs");
  [["24h", 1], ["7d", 7], ["31d", 31]].forEach(([lbl, d], idx) => {
    const tab = node("button", lbl, `metrics-time-tab${idx === 0 ? " active" : ""}`);
    tab.type = "button";
    tab.addEventListener("click", () => {
      windowSelect.value = d;
      timeTabs.querySelectorAll(".metrics-time-tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      load(true);
    });
    timeTabs.append(tab);
  });

  filters.append(modelField, sessionField, timeTabs, windowSelect);
  toolbarCard.append(filters, refreshBtn);

  // Scope indicator pill
  const scopeBar = node("div", "", "metrics-scope-bar");

  // Status note bar
  const statusBar = node("div", "", "metrics-status-bar");
  const note = node("p", "Loading metrics…", "metrics-note");
  note.setAttribute("role", "status");
  statusBar.append(note);

  // Summary sections
  const summary = node("section", "", "metrics-summary");
  const turns = node("section", "", "panel metrics-table-panel");
  const detail = node("section", "", "panel metrics-detail");
  detail.hidden = true;
  const pager = node("div", "", "runs-pager");

  content.append(toolbarCard, scopeBar, statusBar, summary, turns, pager, detail);
  page.append(header, content);
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

  async function open(turn) {
    detailController?.abort();
    detailController = new AbortController();
    const signal = detailController.signal;
    const result = await api(`${base}/turns/${encodeURIComponent(turn.turn_id)}`, { signal });
    if (stopped || signal.aborted) return;
    detail.hidden = false;
    if (!result.ok) {
      detail.replaceChildren(node("p", result.data?.error || "Could not read turn."));
    } else {
      renderTurnMetrics(detail, result.data, "all", turn);
    }
    detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
      params = new URLSearchParams({
        from: new Date(end - Number(windowSelect.value) * 86400000).toISOString(),
        to: end.toISOString(),
        model: modelInput.value.trim(),
        session_name: sessionInput.value.trim()
      });
      if (clearDetail) { detailController?.abort(); detail.hidden = true; }
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

      const sess = sessionInput.value.trim();
      const winDays = windowSelect.value || "1";
      const winStr = winDays === "1" ? "24h" : (winDays === "7" ? "7d" : "31d");
      const mod = modelInput.value.trim();

      scopeBar.innerHTML = `
        <div class="metrics-scope-pill ${sess ? "scope-single" : "scope-fleet"}">
          <span class="scope-icon">${sess ? "🎯" : "🌐"}</span>
          <span class="scope-label"><strong>Scope:</strong> ${sess ? `Session "${sess}" (all lifecycles)` : `Entire Fleet · all sessions in ${name}`}</span>
          <span class="scope-meta">Window: <strong>${winStr}</strong>${mod ? ` · Model: <strong>${mod}</strong>` : ""}</span>
        </div>
      `;

      turns.replaceChildren();
      const tHead = node("div", "", "metrics-table-head");
      const turnsLabel = sess
        ? `Recent turns for session "${sess}" (${results[1].data.turns.length})`
        : `Recent turns across fleet (${results[1].data.turns.length})`;
      tHead.append(node("h3", turnsLabel));
      turns.append(tHead);

      turnRows(turns, results[1].data.turns, open);
      if (!results[1].data.turns.length) turns.append(node("p", "No recorded turns in this window.", "runs-empty"));
      note.textContent = `Updated ${new Date().toLocaleTimeString()}. Null means not recorded, not zero.`;

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

  const visibility = () => { if (!document.hidden && !cursor) load(true, false); };
  document.addEventListener("visibilitychange", visibility);
  refreshBtn.addEventListener("click", () => load(true));
  windowSelect.addEventListener("change", () => load(true));
  modelInput.addEventListener("change", () => load(true));
  sessionInput.addEventListener("change", () => load(true));

  load(true);

  return () => {
    stopped = true;
    controller.abort();
    detailController?.abort();
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", visibility);
  };
}
