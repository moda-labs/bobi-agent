import { api } from "../shell.js";

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

function shortSessionId(id = "") {
  return id.length > 18 ? id.slice(0, 10) + "…" + id.slice(-4) : id;
}

function sessionLabel(session) {
  const started = new Date(session.started_at_us / 1000).toLocaleString();
  return `${session.session_name} (${shortSessionId(session.session_id)}) · ${started}`;
}

function conversationBadge(origin = {}) {
  if (!origin || !origin.source || !origin.source.trim()) {
    return null;
  }
  const label = origin.source === "slack"
    ? `Slack ${origin.channel_name ? "#" + origin.channel_name : origin.channel_id || ""}${origin.thread_id ? " · Thread " + origin.thread_id : ""}`
    : origin.source;
  const badge = node("span", label, "metrics-conversation-badge");
  if (origin.url && /^https:\/\/app\.slack\.com\/client\/T[A-Z0-9]+\/[CDG][A-Z0-9]+\/thread\/[CDG][A-Z0-9]+-\d+\.\d+$/.test(origin.url)) {
    const link = node("a", label, "metrics-conversation-badge");
    link.href = origin.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.addEventListener("click", event => event.stopPropagation());
    return link;
  }
  return badge;
}

function markdown(text) {
  const body = node("div", "", "metrics-markdown");
  let code = null;
  for (const line of text.split("\n")) {
    if (/^\s*```/.test(line)) {
      if (code) code = null;
      else { code = node("code"); const block = node("pre"); block.append(code); body.append(block); }
    } else if (code) {
      code.append(document.createTextNode(line + "\n"));
    } else {
      const block = node("p");
      const heading = line.match(/^#{1,6}\s+(.+)/);
      const content = heading ? heading[1] : line;
      for (const part of content.split(/(`[^`]+`|\*\*[^*]+\*\*)/g)) {
        block.append(part.startsWith("`") && part.endsWith("`")
          ? node("code", part.slice(1, -1))
          : part.startsWith("**") && part.endsWith("**")
            ? node("strong", part.slice(2, -2)) : document.createTextNode(part));
      }
      if (heading) block.classList.add("metrics-markdown-heading");
      body.append(block);
    }
  }
  return body;
}

function turnIO(conversation = {}) {
  if (!conversation.input && !conversation.response) {
    const system = ["cron", "monitor", "workflow", "system", "heartbeat", "sleep-cycle"].includes(conversation.origin?.source);
    return node("div", system
      ? "System execution turn — no direct user chat input recorded."
      : "No input or response recorded for this turn.", "metrics-note metrics-turn-empty-io");
  }
  const panel = node("section", "", "metrics-turn-io");
  const head = node("div", "", "metrics-io-head");
  head.append(node("h3", "Turn conversation transcript"));
  const badge = conversationBadge(conversation.origin);
  if (badge) head.append(badge);
  panel.append(head);

  const hasUserMessage = Boolean(conversation.user_message);
  const isWorkflow = conversation.origin?.source === "workflow_step" || (conversation.input && conversation.input.includes("Workflow ") && (conversation.input.includes("background for run") || conversation.input.includes("steps:")));
  const isMaintenance = (conversation.input && /sleep cycle|curator|memory compaction|maintainer/i.test(conversation.input)) || ["cron", "monitor", "system", "heartbeat", "sleep-cycle"].includes(conversation.origin?.source);

  if (hasUserMessage) {
    const block = node("section", "", "metrics-io-block io-block-user");
    const bar = node("div", "", "metrics-io-head");
    const labelRow = node("div", "", "io-role-row");
    if (isWorkflow) {
      labelRow.append(node("span", "⚙️", "io-role-icon"), node("h4", "Workflow Objective"));
    } else {
      labelRow.append(node("span", "👤", "io-role-icon"), node("h4", "User Message"));
    }
    bar.append(labelRow);
    const copy = node("button", "copy", "btn bobi-btn small quiet");
    copy.type = "button";
    copy.setAttribute("aria-label", "Copy Input / Prompt");
    copy.title = isWorkflow ? "Copy workflow prompt" : "Copy user message";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(conversation.input || conversation.user_message); copy.textContent = "copied"; }
      catch { copy.textContent = "copy unavailable"; }
    });
    bar.append(copy);
    block.append(bar, markdown(conversation.user_message));

    if (conversation.input && conversation.input !== conversation.user_message) {
      const details = node("details", "", "metrics-full-prompt-details");
      const summaryText = isWorkflow
        ? `View workflow input & orchestration context (${number(conversation.input.length)} chars)`
        : `System prompt & raw context (${number(conversation.input.length)} chars)`;
      details.append(node("summary", summaryText), node("pre", conversation.input));
      block.append(details);
    }
    panel.append(block);
  } else if (conversation.input) {
    const block = node("section", "", "metrics-io-block io-block-system");
    const bar = node("div", "", "metrics-io-head");
    const labelRow = node("div", "", "io-role-row");
    if (isMaintenance) {
      labelRow.append(node("span", "⚡", "io-role-icon"), node("h4", "System Maintenance / Sleep Cycle"));
    } else {
      labelRow.append(node("span", "⚡", "io-role-icon"), node("h4", "Agent Startup & System Context"));
    }
    bar.append(labelRow);
    const copy = node("button", "copy", "btn bobi-btn small quiet");
    copy.type = "button";
    copy.setAttribute("aria-label", "Copy Input / Prompt");
    copy.title = "Copy system prompt";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(conversation.input); copy.textContent = "copied"; }
      catch { copy.textContent = "copy unavailable"; }
    });
    bar.append(copy);
    const descText = isMaintenance
      ? "Scheduled background maintenance and memory compaction cycle."
      : "This turn was initiated by the agent runtime without a direct user chat message.";
    block.append(bar, node("p", descText, "metrics-note"));
    const details = node("details", "", "metrics-full-prompt-details");
    const summaryLabel = isMaintenance
      ? `View maintenance instructions (${number(conversation.input.length)} chars)`
      : `View startup role prompt (${number(conversation.input.length)} chars)`;
    details.append(node("summary", summaryLabel), node("pre", conversation.input));
    block.append(details);
    panel.append(block);
  }

  if (conversation.response) {
    const block = node("section", "", "metrics-io-block io-block-assistant");
    const bar = node("div", "", "metrics-io-head");
    const labelRow = node("div", "", "io-role-row");
    labelRow.append(node("span", "🤖", "io-role-icon"), node("h4", "Assistant Response"));
    bar.append(labelRow);
    const copy = node("button", "copy", "btn bobi-btn small quiet");
    copy.type = "button";
    copy.setAttribute("aria-label", "Copy Assistant Response");
    copy.title = "Copy assistant response";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(conversation.response); copy.textContent = "copied"; }
      catch { copy.textContent = "copy unavailable"; }
    });
    bar.append(copy);
    block.append(bar, markdown(conversation.response));
    panel.append(block);
  }

  if (conversation.truncated) panel.append(node("p", "Transcript preview is truncated; copy contains the displayed text only.", "metrics-note"));
  return panel;
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
    if (isObj && rowDef.turn) row.dataset.turnId = rowDef.turn.turn_id;
    if (isObj && typeof rowDef.onClick === "function") {
      row.classList.add("clickable-row");
      row.tabIndex = 0;
      row.addEventListener("click", rowDef.onClick);
      row.addEventListener("keydown", event => {
        if (event.target === row && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          rowDef.onClick();
        }
      });
    }
    cells.forEach(value => {
      const numeric = typeof value === "number" || (typeof value === "string" && /^(?:[\d.,]+(?:ms|s|%)?|—|\$[\d.]+)$/.test(value));
      const cell = node("td", "", numeric ? "bobi-tnum" : "");
      cell.append(value instanceof Node ? value : document.createTextNode(value == null ? "not recorded" : String(value)));
      row.append(cell);
    });
    body.append(row);
  });
  element.append(head, body);
  wrap.append(element);
  container.append(wrap);
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

function confidenceCell(value, detail = false) {
  const score = value == null ? null : Number(value);
  const valid = score != null && Number.isFinite(score) && score >= 0 && score <= 1;
  const tone = valid && score >= 0.8 ? "confidence-high" : valid && score >= (detail ? 0 : 0.5) && score < (detail ? 0.6 : 0.8) ? "confidence-warning" : "";
  const cell = node("span", valid ? Math.round(score * 100) + "%" : "—", "conf-pct bobi-tnum " + tone);
  if (valid) cell.title = "Raw score: " + score.toFixed(3);
  return cell;
}

function routeExplanation(route = {}) {
  const model = route.model_selected || route.model_requested || "the configured model";
  if (model === "<synthetic>" || route.model_requested === "<synthetic>") {
    return ["System Turn", "Executed locally without upstream LLM API call"];
  }
  const fallback = route.fallback_reason || route.session_fallback_reason;
  if (fallback) {
    const reason = String(fallback).replace(/^policy_/, "").replace(/_/g, " ");
    return [/breaker|circuit/.test(fallback) ? "Fallback (Breaker)" : "Fallback", reason + "; executed " + model];
  }
  if (route.route_reused) return ["Sticky Session", "Maintained the recorded session route with " + model];
  if (route.policy_mode === "shadow") return ["Shadow Evaluation", "Recommended " + (route.recommended_model || "—") + "; kept the configured model " + model];
  if (route.policy_mode === "enforce" && route.policy_status !== "not_called") return ["Router Decision", "Policy selected " + model + (route.recommended_model && route.recommended_model !== model ? "; recommendation: " + route.recommended_model : "")];
  return ["Default Config", "Executed the configured model without a policy override"];
}

function costText(costs = {}) {
  const parts = [];
  if (costs.reported_cost_usd != null) parts.push("$" + Number(costs.reported_cost_usd).toFixed(4));
  if (costs.estimated_cost_usd != null) parts.push("$" + Number(costs.estimated_cost_usd).toFixed(4) + " est");
  return parts.join(" / ") || "—";
}

function jevStatusBadge(turn = {}) {
  const isSynthetic = turn.model_selected === "<synthetic>" || turn.model_requested === "<synthetic>" || (turn.invocations?.length === 1 && turn.invocations[0].model_selected === "<synthetic>");
  if (isSynthetic) {
    const badge = node("span", "System Turn", "arm-tag arm-tag-system");
    badge.title = "Turn executed locally without upstream LLM API call";
    return badge;
  }
  const [label, explanation] = routeExplanation(turn);
  let badgeClass = "arm-tag-direct";
  if (label.startsWith("Fallback")) badgeClass = "arm-tag-fallback";
  else if (label === "Sticky Session") badgeClass = "arm-tag-sticky";
  else if (label === "Router Decision") badgeClass = "arm-tag-enforce";
  else if (label === "Shadow Evaluation") badgeClass = "arm-tag-shadow";
  const badge = node("span", label, "arm-tag " + badgeClass);
  badge.title = explanation;
  return badge;
}

function jevDetailBlock(route = {}) {
  const container = node("div", "", "cell-jev-breakdown");
  const isSynthetic = route.model_selected === "<synthetic>" || route.model_requested === "<synthetic>";
  if (isSynthetic) {
    container.append(node("span", "System Turn", "arm-tag arm-tag-system"));
    container.append(node("div", "No upstream LLM API call", "model-provider-status"));
    return container;
  }

  const badge = jevStatusBadge(route);
  container.append(badge);

  const fallback = route.fallback_reason || route.session_fallback_reason;
  const suggested = route.recommended_model;
  const selected = route.model_selected || route.model_requested;
  const conf = route.confidence != null ? Math.round(Number(route.confidence) * 100) + "%" : null;
  const minConf = route.min_confidence != null ? Math.round(Number(route.min_confidence) * 100) + "%" : null;

  const details = node("div", "", "jev-routing-details");

  if (fallback) {
    const cleanReason = String(fallback).replace(/^policy_/, "").replace(/_/g, " ");
    if (suggested && selected) {
      details.append(node("div", `Suggest: ${suggested} → Selected: ${selected}`, "jev-meta-models"));
    }
    const sub = node("div", "", "jev-meta-sub");
    if (conf) sub.append(node("span", `Conf ${conf}${minConf ? ` (< ${minConf})` : ""}`, "jev-meta-conf"));
    if (cleanReason) sub.append(node("span", `${conf ? " · " : ""}${cleanReason}`, "jev-meta-reason"));
    details.append(sub);
  } else if (route.route_reused) {
    details.append(node("div", "Sticky session route", "jev-meta-models"));
    details.append(node("div", `Maintained ${selected || "model"} from turn 1`, "jev-meta-sub"));
  } else if (route.policy_mode === "enforce" && route.policy_status !== "not_called") {
    if (suggested && suggested !== selected) {
      details.append(node("div", `Enforced: ${selected} (suggest: ${suggested})`, "jev-meta-models"));
    } else {
      details.append(node("div", `Enforced: ${selected || "model"}`, "jev-meta-models"));
    }
    if (conf) details.append(node("div", `Confidence: ${conf} (enforced)`, "jev-meta-sub jev-meta-conf-good"));
  } else if (route.policy_mode === "shadow") {
    details.append(node("div", `Suggest: ${suggested || "—"} (conf ${conf || "—"})`, "jev-meta-models"));
    details.append(node("div", `Kept configured: ${selected || "model"}`, "jev-meta-sub"));
  } else {
    details.append(node("div", "Configured model directly executed", "jev-meta-sub"));
  }

  container.append(details);
  return container;
}

function turnRows(container, turns, open, agentNameOrHandler = "", onFilterSession = null) {
  const onSessionClick = typeof agentNameOrHandler === "function" ? agentNameOrHandler : onFilterSession;
  table(container, ["Turn", "Session / Topic", "Model & Decision", "Confidence", "Tokens / Cost", "Latency"], turns.map((turn, index) => {
    const identity = node("div", "", "metrics-turn-identity");
    const date = new Date(turn.started_at_us / 1000);
    const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    const time = node("span", minutes < 1 ? "just now" : minutes < 60 ? minutes + "m ago" : date.toLocaleString(), "metrics-provenance");
    time.title = date.toISOString();
    const session = node("button", shortSessionId(turn.session_id || ""), "session-id-pill");
    session.type = "button";
    session.title = "Copy session ID: " + (turn.session_id || "");
    session.addEventListener("click", async event => {
      event.stopPropagation();
      try { await navigator.clipboard.writeText(turn.session_id); session.title = "Copied session ID"; }
      catch { session.title = "Copy unavailable: " + turn.session_id; }
    });
    identity.append(node("span", "#" + (turn.turn_index ?? index + 1), "bobi-tnum"), time, session);

    // Consolidated Session / Topic column
    const topicCell = node("div", "", "metrics-topic-cell");
    const userMsg = turn.conversation?.user_message;
    const hasUserMsg = Boolean(userMsg);
    const titleText = userMsg
      || turn.conversation?.response_snippet
      || (turn.session_name ? `${turn.session_name} · Startup` : "Agent Session Startup");
    const topicTitle = node("div", titleText, "metrics-topic-title" + (hasUserMsg ? "" : " metrics-system-trigger"));
    topicTitle.title = titleText;

    const topicMeta = node("div", "", "metrics-topic-meta");
    if (onSessionClick && turn.session_id) {
      const filter = node("button", turn.session_name || shortSessionId(turn.session_id), "sess-name clickable-session");
      filter.type = "button";
      filter.title = "Filter lifecycle: " + turn.session_id;
      filter.addEventListener("click", event => { event.stopPropagation(); onSessionClick(turn.session_id); });
      topicMeta.append(filter);
    }
    const origin = conversationBadge(turn.conversation?.origin);
    if (origin) topicMeta.append(origin);
    if (turn.tool_count) {
      topicMeta.append(node("span", `${turn.tool_count} tool calls`, "metrics-conversation-badge"));
    }
    topicCell.append(topicTitle, topicMeta);

    const model = node("div", "", "cell-model-decision");
    const rawModels = [...new Set((turn.invocations || []).map(item => item.model_selected || item.model_requested).filter(Boolean))];
    const rawModel = rawModels.join(", ") || turn.model_selected || "—";
    const displayModel = rawModel === "<synthetic>" ? "System / Local" : rawModel;
    const badge = jevStatusBadge(turn);
    model.append(node("code", displayModel, "table-code-model"), badge);

    const total = turn.usage?.input_tokens != null && turn.usage?.output_tokens != null ? Number(turn.usage.input_tokens) + Number(turn.usage.output_tokens) : null;
    const tokens = node("span", (total == null ? "—" : number(total) + " tok" + (turn.usage.is_estimated ? " est" : "")) + " · " + costText(turn.costs), "bobi-tnum");
    tokens.title = "Input + output tokens. Cache and reasoning dimensions are shown separately in telemetry.";
    return {turn, onClick: () => open(turn, index), cells: [identity, topicCell, model, confidenceCell(turn.confidence), tokens, turn.wall_duration_ms == null ? "—" : (turn.wall_duration_ms / 1000).toFixed(1) + "s"]};
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
        index: toolEntries.length,
        tool: (e.tool || "").toLowerCase(),
        command: e.text || "",
        result: resultText,
        at: e.at ? Date.parse(e.at) : null,
        used: false,
      });
    }
  }

  return tools.map((t, idx) => {
    const tStart = t.started_at_us ? t.started_at_us / 1000 : null;
    const tName = (t.tool_name || t.tool_kind || "").toLowerCase();
    let bestMatch = null;
    let bestDiff = Infinity;

    if (tStart) {
      for (const te of toolEntries) {
        if (!te.used && te.at) {
          const diff = Math.abs(te.at - tStart);
          const nameMatches = !tName || !te.tool || te.tool.includes(tName) || tName.includes(te.tool);
          if (diff < (nameMatches ? 15000 : 5000) && diff < bestDiff) {
            bestDiff = diff;
            bestMatch = te;
          }
        }
      }
    }

    if (!bestMatch) {
      bestMatch = toolEntries.find(te => !te.used && (!tName || !te.tool || te.tool.includes(tName) || tName.includes(te.tool)));
    }
    if (!bestMatch && toolEntries[idx] && !toolEntries[idx].used) {
      bestMatch = toolEntries[idx];
    }

    if (bestMatch) bestMatch.used = true;

    return {
      tool: t,
      command: bestMatch?.command || "",
      result: bestMatch?.result || "",
    };
  });
}

function renderToolExecutionsList(container, tools, entries, turn) {
  const matched = matchToolsWithTranscript(tools, entries, turn);
  matched.forEach(({tool, command, result}, index) => {
    const card = node("details", "", "metrics-tool-card");
    const summary = node("summary", "", "metrics-tool-summary");
    const latency = tool.started_at_us != null && tool.ended_at_us != null ? (tool.ended_at_us - tool.started_at_us) / 1000 : null;
    summary.append(
      node("span", `#${index + 1}`, "tool-idx-badge bobi-tnum"),
      node("code", tool.tool_name || tool.tool_kind || "tool"),
      node("span", tool.status || "—", "metrics-provenance"),
      node("span", latency == null ? "—" : Math.round(latency) + "ms", "bobi-tnum")
    );
    card.append(summary);

    let hasBody = false;
    for (const [label, text] of [["Input", command], ["Result", result]]) {
      if (!text) continue;
      hasBody = true;
      const block = node("div", "", "metrics-tool-body");
      const copy = node("button", "copy", "btn bobi-btn small quiet");
      copy.type = "button";
      copy.setAttribute("aria-label", "Copy tool " + label.toLowerCase());
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(text); copy.textContent = "copied"; }
        catch { copy.textContent = "copy unavailable"; }
      });
      block.append(node("span", label), copy, node("pre", text));
      card.append(block);
    }
    if (!hasBody) {
      const block = node("div", "", "metrics-tool-body");
      const meta = [];
      if (tool.input_bytes != null) meta.push(`Input payload: ${number(tool.input_bytes)} bytes`);
      if (tool.output_bytes != null) meta.push(`Output payload: ${number(tool.output_bytes)} bytes`);
      block.append(node("p", meta.join(" · ") || "Execution metadata recorded in telemetry.", "metrics-note"));
      card.append(block);
    }
    container.append(card);
  });
}

export function renderTurnMetrics(container, data, section = "overview", row = {}, onBack = null, onTabChange = null, agentName = "", onFilterSession = null) {
  try {
    container.replaceChildren();
    if (onBack) {
      const back = node("button", "Back to turns", "btn bobi-btn small");
      back.type = "button";
      back.addEventListener("click", onBack);
      container.append(back);
    }
    const decisions = data.router_decisions || [];
    const invocations = data.invocations || [];
    const primary = {...row, ...decisions[0], route_reused: row.route_reused ?? decisions[0]?.route_reused};
    const totals = data.usage_totals || row.usage || {};
    const costs = data.cost_measurements || [];
    const banner = node("div", "", "turn-summary-banner metrics-kpi-strip");
    pair(banner, "Model", invocations[0]?.model_selected || primary.model_selected || invocations[0]?.model_requested);
    pair(banner, "Status", data.turn?.status || "—");
    const totalTokens = totals.input_tokens != null && totals.output_tokens != null
      ? Number(totals.input_tokens) + Number(totals.output_tokens) : null;
    pair(banner, "Total tokens", totalTokens == null ? "—" : number(totalTokens) + (totals.is_estimated ? " est" : ""));
    pair(banner, "Cost", costText(row.costs));
    pair(banner, "Latency", data.turn?.wall_duration_ms == null ? "—" : (data.turn.wall_duration_ms / 1000).toFixed(1) + "s");
  const confidence = node("div", "", "metrics-pair");
  confidence.append(node("span", "Confidence"), confidenceCell(primary.confidence));
  banner.append(confidence);
  const overview = node("div", "", "detail-tab-panel panel-overview");
  const technical = node("div", "", "detail-tab-panel panel-technical");
  const tabs = node("div", "", "tabs detail-nav-tabs");
  tabs.setAttribute("role", "tablist");
  let active = ["technical", "usage", "cost", "transcript"].includes(section) ? "technical" : "overview";
  function setTab(id) {
    active = id === "technical" ? "technical" : "overview";
    overview.hidden = active !== "overview";
    technical.hidden = active !== "technical";
    tabs.querySelectorAll("button").forEach(button => {
      const selected = button.dataset.tab === active;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    if (onTabChange) onTabChange(active);
  }
  for (const [id, label] of [["overview", "Overview & Execution"], ["technical", "Technical Telemetry"]]) {
    const button = node("button", label, "tab");
    button.type = "button";
    button.dataset.tab = id;
    button.setAttribute("role", "tab");
    button.addEventListener("click", () => setTab(id));
    tabs.append(button);
  }
  overview.append(banner, turnIO(data.conversation));
  if (data.tool_executions?.length) {
    const panelTools = node("div", "", "panel-tools");
    panelTools.append(createSectionHead("", "Tool executions", data.tool_executions.length));
    renderToolExecutionsList(panelTools, data.tool_executions, data.conversation?.entries || [], data.turn);
    overview.append(panelTools);
  }
  const panelRouting = node("div", "", "panel-routing");
  panelRouting.append(createSectionHead("", "Router Decisions & Policy"));
  const routes = decisions.length ? decisions : [primary];
  table(panelRouting, ["Model Used", "Routing Decision & Reason", "Confidence", "Latency"], routes.map(decision => {
    const route = {...row, ...decision};
    const model = node("div", "", "cell-model-decision");
    model.append(node("code", invocations[0]?.model_selected || route.model_selected || invocations[0]?.model_requested || "—", "table-code-model"));
    model.append(node("span", [invocations[0]?.provider, route.policy_mode].filter(Boolean).join(" · "), "metrics-provenance"));
    const reason = node("div", "", "cell-decision-reason");
    const [label, explanation] = routeExplanation(route);
    reason.append(node("span", label, "arm-tag"), node("span", explanation, "decision-reason-text"));
    const latency = route.router_latency_ms ?? route.policy_latency_ms;
    return [model, reason, confidenceCell(route.confidence, true), latency == null ? "—" : Math.round(latency) + "ms"];
  }));
  overview.append(panelRouting);
  const panelUsage = node("div", "", "panel-usage");
  const usageMap = new Map();
  for (const invocation of invocations) {
    const candidates = (data.invocation_usage || []).filter(usage => usage.invocation_id === invocation.invocation_id);
    const usage = candidates.find(item => item.model === invocation.model_selected)
      || candidates.find(item => item.model === invocation.model_requested)
      || (candidates.length === 1 ? candidates[0] : null);
    const terminal = invocations.length === 1 ? (data.best_usage || []).find(item => item.scope === "turn") : null;
    usageMap.set(invocation.invocation_id, terminal && (!usage || (!terminal.is_estimated && usage.is_estimated)) ? terminal : usage);
  }

  panelUsage.append(createSectionHead("", "Model Invocations & Token Breakdown", invocations.length,
    "Canonical usage only. Reasoning is included in output. Reported turn totals can supersede invocation estimates; rows are not blindly summed."));
  if (invocations.length) {
    table(panelUsage, [
      "#",
      "Model",
      "JEV Routing & Policy",
      "Latency",
      "Input Tokens",
      "Output Tokens",
      "Cache Read",
      "Cache Write",
      "Total Cached",
      "Cost ($)"
    ], invocations.map((invocation, index) => {
      const usage = usageMap.get(invocation.invocation_id) || {};
      
      // 1. Model Cell
      const modelCell = node("div", "", "cell-model-breakdown");
      const executedModel = invocation.model_selected || invocation.model_requested || "not recorded";
      const modelDisplay = executedModel === "<synthetic>" ? "System / Local" : executedModel;
      const modelCode = node("code", modelDisplay, "table-code-model");
      modelCell.append(modelCode);
      if (invocation.status && invocation.status !== "completed") {
        modelCell.append(node("span", invocation.status, "badge-status-warn"));
      }

      // 2. JEV Routing & Policy Cell
      const decision = (data.router_decisions || []).find(d => d.turn_id === invocation.turn_id) || data.router_decisions?.[0] || row;
      const jevCell = jevDetailBlock({...row, ...decision, ...invocation});

      // 3. Latency Cell
      const latencyMs = invocation.provider_latency_ms ?? invocation.wall_duration_ms;
      const latencyCell = node("div", "", "token-cell");
      const latText = latencyMs == null ? "—" : (latencyMs < 1000 ? Math.round(latencyMs) + "ms" : (latencyMs / 1000).toFixed(1) + "s");
      latencyCell.append(node("span", latText, "bobi-tnum token-main-val"));

      // 4. Input Tokens Cell
      const inputCell = node("div", "", "token-cell");
      inputCell.append(node("span", number(usage.input_tokens), "bobi-tnum token-main-val"));

      // 5. Output Tokens Cell
      const outputCell = node("div", "", "token-cell");
      outputCell.append(node("span", number(usage.output_tokens), "bobi-tnum token-main-val"));
      if (usage.reasoning_output_tokens) {
        outputCell.append(node("span", `+${number(usage.reasoning_output_tokens)} reasoning`, "token-sub-val reasoning-sub"));
      }

      // 6. Cache Read Cell
      const cacheReadCell = node("div", "", "token-cell");
      cacheReadCell.append(node("span", number(usage.cache_read_input_tokens), "bobi-tnum token-main-val"));
      if (usage.input_tokens > 0 && usage.cache_read_input_tokens != null && usage.cache_read_input_tokens > 0) {
        const hitRate = (100 * usage.cache_read_input_tokens / usage.input_tokens).toFixed(1);
        cacheReadCell.append(node("span", `${hitRate}% hit`, "token-sub-val hit-sub"));
      }

      // 7. Cache Write Cell
      const cacheWriteCell = node("div", "", "token-cell");
      cacheWriteCell.append(node("span", number(usage.cache_write_input_tokens), "bobi-tnum token-main-val"));

      // 8. Total Cached Cell
      const totalCached = (usage.cache_read_input_tokens != null || usage.cache_write_input_tokens != null)
        ? (usage.cache_read_input_tokens || 0) + (usage.cache_write_input_tokens || 0) : null;
      const totalCachedCell = node("div", "", "token-cell");
      totalCachedCell.append(node("span", totalCached == null ? "—" : number(totalCached), "bobi-tnum token-main-val"));

      // 9. Cost Cell
      const terminalCost = invocations.length === 1 ? (data.cost_measurements || []).find(item => item.scope === "turn") : null;
      const turnLevelCost = (data.cost_measurements || []).find(item => item.scope === "turn");
      const cost = (data.cost_measurements || []).filter(item => item.invocation_id === invocation.invocation_id)
        .sort((first, second) => first.is_estimated - second.is_estimated || second.observed_at_us - first.observed_at_us)[0] || terminalCost;
      const amountCell = node("div", "", "token-cell");
      if (cost?.amount_usd != null) {
        amountCell.append(node("span", "$" + Number(cost.amount_usd).toFixed(6), "bobi-tnum token-main-val text-green"));
        amountCell.append(node("span", cost.is_estimated ? "estimated list price" : (cost.scope === "turn" ? "turn total" : "provider reported"), "token-sub-val"));
      } else if (turnLevelCost?.amount_usd != null && invocations.length > 1) {
        amountCell.append(node("span", "—", "bobi-tnum token-main-val"));
        amountCell.append(node("span", "billed per turn", "token-sub-val"));
      } else {
        amountCell.append(node("span", "not recorded", "bobi-tnum token-main-val"));
      }

      return [
        index + 1,
        modelCell,
        jevCell,
        latencyCell,
        inputCell,
        outputCell,
        cacheReadCell,
        cacheWriteCell,
        totalCachedCell,
        amountCell
      ];
    }));
  } else {
    panelUsage.append(node("p", "No model invocations recorded for this turn.", "metrics-note"));
  }
  const totalLine = node("div", "", "metrics-turn-totals");
  pair(totalLine, "Turn input / output", `${number(totals.input_tokens)} / ${number(totals.output_tokens)}`);
  pair(totalLine, "Cache read / write", `${number(totals.cache_read_input_tokens)} / ${number(totals.cache_write_input_tokens)}`);
  const turnCostForTotal = (data.cost_measurements || []).find(item => item.scope === "turn") || (costs.length ? costs[0] : null);
  if (turnCostForTotal?.amount_usd != null) {
    pair(totalLine, "Turn cost", "$" + Number(turnCostForTotal.amount_usd).toFixed(6));
  }
  pair(totalLine, "Basis", totals.is_estimated == null ? null : totals.is_estimated ? "estimated" : "reported");
  panelUsage.append(totalLine);

  technical.append(panelUsage);
  if (costs.length) {
    const costPanel = node("div", "", "panel-cost");
    costPanel.append(createSectionHead("", "Recorded costs (not additive)", costs.length,
      "Official provider invoices or estimates recorded at the turn or session level. Rows are non-additive with invocation totals."));
    table(costPanel, ["Scope", "Model", "USD", "Basis"], costs.map(cost => [
      cost.scope,
      cost.model,
      node("span", "$" + Number(cost.amount_usd).toFixed(6), "bobi-tnum text-green"),
      cost.is_estimated ? "Estimated" : "Reported"
    ]));
    technical.append(costPanel);
  }
  container.append(tabs, overview, technical);
  setTab(active);
  } catch (err) {
    console.error("renderTurnMetrics failed:", err);
    container.replaceChildren();
    const errBox = node("div", "", "metrics-turn-empty-io");
    errBox.style.color = "var(--bad, #ef4444)";
    errBox.append(node("h4", "Unable to display turn telemetry"));
    errBox.append(node("p", err?.message || String(err)));
    container.append(errBox);
  }
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
      renderTurnMetrics(container, detail.data, section, turn, () => load(), null, name, (sessionId) => {
        location.hash = `#/agents/${encodeURIComponent(name)}/metrics?session=${encodeURIComponent(sessionId)}`;
      });
    }, name, (sessName) => {
      location.hash = `#/agents/${encodeURIComponent(name)}/metrics?session=${encodeURIComponent(sessName)}`;
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
  const jevHeaderBtn = node("button", " JEV: Loading…", "btn bobi-btn small jev-header-btn");
  jevHeaderBtn.type = "button";
  jevHeaderBtn.title = `Configure TypeSafe JEV dynamic model routing for ${name}`;
  jevHeaderBtn.addEventListener("click", openJevConfigModal);
  const refreshBtn = node("button", "refresh", "btn bobi-btn small");
  refreshBtn.type = "button";
  refreshBtn.addEventListener("click", () => load(true));
  ahRight.append(collectorChip, jevHeaderBtn);

  ahBody.append(ahName, ahRight);
  header.append(ahBody);

  // Main content container
  const content = node("div", "", "metrics-content");

  // Status note bar
  const statusBar = node("div", "", "metrics-status-bar");
  const note = node("p", "Loading metrics…", "metrics-note");
  note.setAttribute("role", "status");

  // Active Session Filter Chip (positioned cleanly in status bar)
  const sessionFilterBar = node("div", "", "metrics-session-chip");
  sessionFilterBar.hidden = true;
  statusBar.append(note, sessionFilterBar);

  const filters = node("div", "", "metrics-toolbar-card");
  const modelInput = node("input", "", "metrics-search-input");
  modelInput.type = "text";
  modelInput.setAttribute("aria-label", "Provider model filter");
  modelInput.placeholder = "filter by model…";
  modelInput.addEventListener("change", () => load(true));
  const sessionInput = node("select", "", "metrics-search-input");
  sessionInput.setAttribute("aria-label", "Session lifecycle");
  sessionInput.append(node("option", "all sessions"));
  sessionInput.firstChild.value = "";
  sessionInput.addEventListener("change", () => {
    if (sessionInput.value === "__more__") { loadMoreSessions(); return; }
    setSessionFilter(sessionInput.value);
  });
  const modelField = node("label", "", "metrics-search-box bobi-field metrics-model-field");
  const modelLabel = node("span", "Provider model", "field-label");
  modelField.append(modelLabel, modelInput);

  const sessionField = node("label", "", "metrics-search-box bobi-field metrics-session-field");
  const sessionLabelSpan = node("span", "Session", "field-label");
  sessionField.append(sessionLabelSpan, sessionInput);

  const timeRange = node("select", "", "metrics-time-select");
  timeRange.setAttribute("aria-label", "Time range");
  for (const [value, label] of [["1", "Last 1h"], ["24", "24h"], ["168", "7d"], ["744", "All (up to 31d)"]]) {
    const option = node("option", label); option.value = value; timeRange.append(option);
  }
  timeRange.value = "24";
  timeRange.addEventListener("change", () => load(true));
  const search = node("input", "", "metrics-search-input");
  search.type = "search";
  search.placeholder = "Search loaded turns…";
  search.setAttribute("aria-label", "Search loaded turns");
  search.addEventListener("input", () => drawTurns());
  const searchBox = node("div", "", "metrics-search-box search-turns-box");
  searchBox.append(search);

  const auto = node("input");
  auto.type = "checkbox";
  auto.checked = true;
  auto.addEventListener("change", () => { if (auto.checked) load(true, false); });
  const autoLabel = node("label", "Auto-refresh", "metrics-auto-refresh");
  autoLabel.prepend(auto);

  const actions = node("div", "", "metrics-toolbar-actions");
  actions.append(autoLabel, refreshBtn);
  filters.append(timeRange, modelField, sessionField, searchBox, actions);

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
    <button type="button" class="tab active" data-jev-tab="status">⚙️ Overview & Status</button>
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
      <div class="jev-confirm-icon"></div>
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

  content.append(statusBar, filters, summary, turns, pager);
  page.append(header, content, drawerBackdrop, jevModalBackdrop);
  element.replaceChildren(page);

  let currentSessionFilter = session || "";
  let recentSessions = [];
  let sessionsCursor = "";
  let sessionsRange = "";

  function updateSessionPicker() {
    const all = node("option", "all sessions");
    all.value = "";
    sessionInput.replaceChildren(all);
    if (currentSessionFilter && !recentSessions.some(item => item.session_id === currentSessionFilter)) {
      const legacy = node("option", `${currentSessionFilter} (${currentSessionFilter.startsWith("ses_") ? "outside recent window" : "all matching lifecycles"})`);
      legacy.value = currentSessionFilter;
      sessionInput.append(legacy);
    }
    recentSessions.forEach(item => {
      const option = node("option", sessionLabel(item));
      option.value = item.session_id;
      option.title = item.session_id;
      sessionInput.append(option);
    });
    if (sessionsCursor) {
      const more = node("option", "load older sessions…");
      more.value = "__more__";
      sessionInput.append(more);
    }
    sessionInput.value = currentSessionFilter;
  }

  async function loadMoreSessions() {
    sessionInput.value = currentSessionFilter;
    sessionInput.disabled = true;
    const range = new URLSearchParams({from: params.get("from"), to: params.get("to"), cursor: sessionsCursor});
    const result = await api(`${base}/sessions?${range}`, {signal: controller.signal});
    if (stopped) return;
    sessionInput.disabled = false;
    if (!result.ok) { note.textContent = result.data?.error || "Could not read older sessions."; return; }
    const known = new Set(recentSessions.map(item => item.session_id));
    recentSessions.push(...(result.data.sessions || []).filter(item => !known.has(item.session_id)));
    sessionsCursor = result.data.next_cursor || "";
    updateSessionPicker();
  }

  function updateSessionFilterUI() {
    updateSessionPicker();
    sessionFilterBar.hidden = !currentSessionFilter;
    sessionFilterBar.replaceChildren();
    if (!currentSessionFilter) return;
    const selected = recentSessions.find(item => item.session_id === currentSessionFilter);
    const chip = node("span", "Session: " + shortSessionId(currentSessionFilter));
    chip.title = selected ? sessionLabel(selected) : currentSessionFilter;
    const clear = node("button", "×", "btn bobi-btn quiet small");
    clear.type = "button";
    clear.setAttribute("aria-label", "Clear session filter");
    clear.addEventListener("click", () => setSessionFilter(""));
    sessionFilterBar.append(chip, clear);
  }

  function setSessionFilter(sess) {
    currentSessionFilter = sess;
    updateSessionFilterUI();
    const newHash = currentSessionFilter
      ? `#/agents/${encodeURIComponent(name)}/metrics?session=${encodeURIComponent(currentSessionFilter)}`
      : `#/agents/${encodeURIComponent(name)}/metrics`;
    history.replaceState(null, "", newHash);
    load(true);
  }

  updateSessionFilterUI();

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
  let currentDrawerTab = "overview";

  function closeDrawer() {
    drawerBackdrop.hidden = true;
    drawerBackdrop.classList.remove("open");
    activeTurnIndex = -1;
    turns.querySelector("tbody tr.turn-row-selected")?.focus();
    turns.querySelectorAll("tbody tr.turn-row-selected").forEach(r => r.classList.remove("turn-row-selected"));
    document.removeEventListener("keydown", handleDrawerKey);
  }

  function handleDrawerKey(e) {
    if (e.key === "Tab") {
      const controls = [...drawerModal.querySelectorAll('button:not(:disabled), a[href], summary, [tabindex="0"]')].filter(control => control.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      return;
    }
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
    tableRows.forEach(r => {
      r.classList.toggle("turn-row-selected", r.dataset.turnId === turn.turn_id);
    });

    drawerBackdrop.hidden = false;
    drawerBackdrop.classList.add("open");
    document.addEventListener("keydown", handleDrawerKey);

    renderDrawerHead(turn, index, currentTurns.length);
    drawerHead.querySelector(".td-head-right button").focus();

    detailController?.abort();
    detailController = new AbortController();
    const signal = detailController.signal;

    const loading = node("div", "Loading turn telemetry…", "tr-empty metrics-loading");
    loading.setAttribute("role", "status");
    loading.setAttribute("aria-busy", "true");
    drawerBody.replaceChildren(loading);

    const result = await api(`${base}/turns/${encodeURIComponent(turn.turn_id)}`, { signal });
    if (stopped || signal.aborted) return;
    if (!result.ok) {
      drawerBody.replaceChildren(node("p", result.data?.error || "Could not read turn.", "tr-empty bad"));
      return;
    }

    try {
      renderTurnMetrics(drawerBody, result.data, currentDrawerTab || "all", turn, null, (tabId) => {
        currentDrawerTab = tabId;
      }, name, (sessionId) => {
        closeDrawer();
        setSessionFilter(sessionId);
      });
    } catch (err) {
      console.error("Failed to render turn metrics in drawer:", err);
      drawerBody.replaceChildren();
      const errBox = node("div", "", "metrics-turn-empty-io");
      errBox.style.color = "var(--bad, #ef4444)";
      errBox.append(node("h4", "Unable to display turn telemetry"));
      errBox.append(node("p", err?.message || String(err)));
      drawerBody.append(errBox);
    }
  }

  function renderDrawerHead(turn, index, total) {
    drawerHead.replaceChildren();

    const left = node("div", "", "td-head-left");
    const turnIndex = turn.turn_index != null ? turn.turn_index : index + 1;
    const eyebrow = node("span", `Turn #${turnIndex}`, "td-badge-turn");
    left.append(eyebrow);

    const turnIdChip = node("code", shortSessionId(turn.turn_id), "td-id-chip");
    turnIdChip.title = `Turn ID: ${turn.turn_id} (Click to copy)`;
    turnIdChip.addEventListener("click", () => {
      navigator.clipboard?.writeText(turn.turn_id);
      turnIdChip.textContent = "Copied! ✓";
      setTimeout(() => { turnIdChip.textContent = shortSessionId(turn.turn_id); }, 1200);
    });
    left.append(turnIdChip);

    if (turn.session_id) {
      const sessGroup = node("div", "", "td-head-session");
      sessGroup.append(node("span", "Session", "td-badge-session"));
      const sessChip = node("code", shortSessionId(turn.session_id), "td-id-chip td-sess-chip");
      sessChip.title = `Session: ${turn.session_id} (Click to copy)`;
      sessChip.addEventListener("click", () => {
        navigator.clipboard?.writeText(turn.session_id);
        sessChip.textContent = "Copied! ✓";
        setTimeout(() => { sessChip.textContent = shortSessionId(turn.session_id); }, 1200);
      });
      sessGroup.append(sessChip);
      if (turn.session_name) {
        const sessName = node("span", `(${turn.session_name})`, "td-sess-name");
        sessName.title = "Session name: " + turn.session_name;
        sessGroup.append(sessName);
      }
      left.append(sessGroup);
    }

    const center = node("div", "", "td-head-nav");
    const prevBtn = node("button", "← Earlier", "btn bobi-btn quiet small td-nav-btn td-nav-prev");
    prevBtn.type = "button";
    prevBtn.disabled = index <= 0;
    prevBtn.title = "Previous turn in timeline";
    prevBtn.addEventListener("click", () => openAtIndex(index - 1));

    const counter = node("span", `Turn ${index + 1} of ${total}`, "td-nav-counter");

    const nextBtn = node("button", "Later →", "btn bobi-btn quiet small td-nav-btn td-nav-next");
    nextBtn.type = "button";
    nextBtn.disabled = index >= total - 1;
    nextBtn.title = "Next turn in timeline";
    nextBtn.addEventListener("click", () => openAtIndex(index + 1));

    center.append(prevBtn, counter, nextBtn);

    const right = node("div", "", "td-head-right");
    const closeBtn = node("button", "Close", "btn bobi-btn small td-close-btn");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.title = "Close (Esc)";
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
    let costDisplay = "—";
    let costSub = "no spend recorded";
    let costBadge = "";
    if (hasSpend) {
      costDisplay = costText(data.totals);
      if (reportedCost != null && estCost != null && estCost > 0) {
        costSub = "reported / estimated · kept separate";
        costBadge = "MIXED";
      } else if (reportedCost != null) {
        costSub = "provider-reported cost";
        costBadge = "EXACT";
      } else {
        costSub = "model pricing estimate";
        costBadge = "EST";
      }
    }

    const fields = [
      ["input_tokens", "Input tokens", "clay", data.totals.cache_read_input_tokens != null ? `${number(data.totals.cache_read_input_tokens)} cache read (included)` : "canonical input", "tile-input", number(data.totals.input_tokens)],
      ["output_tokens", "Output tokens", "accent", "canonical output", "tile-output", number(data.totals.output_tokens)],
      ["cache_read_input_tokens", "Prompt Cache Hits", "accent", data.totals.input_tokens > 0 && data.totals.cache_read_input_tokens != null ? `${(data.totals.cache_read_input_tokens / data.totals.input_tokens * 100).toFixed(1)}% cache hit ratio` : "cache usage not recorded", "tile-cache-read", number(data.totals.cache_read_input_tokens)],
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

  function drawTurns() {
    const query = search.value.trim().toLowerCase();
    const visible = currentTurns.filter(turn => !query || [turn.conversation?.prompt_snippet, turn.conversation?.response_snippet, turn.session_id, turn.model_selected, turn.fallback_reason].filter(Boolean).join(" ").toLowerCase().includes(query));
    turns.replaceChildren();
    turns.append(createSectionHead("", "Recent turns", visible.length, "Individual turns, model execution lifecycle, and token consumption"));
    turnRows(turns, visible, turn => openAtIndex(currentTurns.indexOf(turn)), name, setSessionFilter);
    if (!visible.length) turns.append(node("p", "No recorded turns in this window.", "runs-empty"));
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
        from: new Date(end.getTime() - Number(timeRange.value) * 3600000).toISOString(),
        to: end.toISOString(),
      });
      if (currentSessionFilter) {
        params.set(recentSessions.some(item => item.session_id === currentSessionFilter) || currentSessionFilter.startsWith("ses_")
          ? "session_id" : "session", currentSessionFilter);
      }
      if (modelInput.value.trim()) params.set("model", modelInput.value.trim());
      if (clearDetail) { detailController?.abort(); closeDrawer(); }
    }

    const query = new URLSearchParams(params);
    if (cursor) query.set("cursor", cursor);

    const results = await Promise.all([
      api(`${base}/summary?${params}`, { signal: controller.signal }),
      api(`${base}/turns?${query}`, { signal: controller.signal })
    ]);
    if (!stopped && !queued && results.every(result => result.ok)) {
      results.push(await api(`${base}/sessions?${new URLSearchParams({from: params.get("from"), to: params.get("to")})}`,
        {signal: controller.signal}));
    }

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

    const failure = results.slice(0, 2).find(r => !r.ok);
    if (failure) {
      note.textContent = `${failure.data?.error || "Could not read metrics."} Previous values, if any, are stale.`;
    } else {
      if (results[2]?.ok) {
        const range = `${params.get("from")}/${params.get("to")}`;
        if (sessionsRange !== range) {
          recentSessions = [];
          sessionsRange = range;
          sessionsCursor = results[2].data.next_cursor || "";
        }
        const known = new Set(results[2].data.sessions.map(item => item.session_id));
        recentSessions = [...results[2].data.sessions, ...recentSessions.filter(item => !known.has(item.session_id))];
        updateSessionFilterUI();
      }
      renderSummary(results[0].data);

      const allTurns = results[1].data.turns || [];
      const selectedTurnId = currentTurns[activeTurnIndex]?.turn_id;
      currentTurns = allTurns;
      if (selectedTurnId) activeTurnIndex = currentTurns.findIndex(turn => turn.turn_id === selectedTurnId);

      drawTurns();
      note.textContent = "Read-only metrics · refreshed " + new Date().toLocaleTimeString();

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
    timer = setTimeout(() => { if (auto.checked && !document.hidden && !cursor) load(true, false); }, 10000);
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
