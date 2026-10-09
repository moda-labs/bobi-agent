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
  return `${humanSessionName(session)} · ${started}`;
}

function humanSessionName(session) {
  const role = session.role || "";
  let name = session.session_name || role || "session";
  name = name.replace(/^(curator)[-_:]curator(?:[-_:]|$)/i, "$1:");
  if (role && name.toLowerCase() === `${role}-${role}`.toLowerCase()) name = role;
  if (/^curator[:_-]/i.test(name)) return `curator:${(name.replace(/^curator[:_-]/i, "") || (session.session_id || "").replace(/^ses_/, "")).slice(0, 6)}`;
  if (/^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(name)) return role || "session";
  name = name.replace(/[\s:_-]+(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{12,})$/i, "");
  const cleaned = name.replace(/[_-]+/g, " ").trim();
  if (/^bobi\s+[a-z0-9-]+\s+(director|worker|curator)$/i.test(cleaned)) {
    return cleaned.replace(/^bobi\s+[a-z0-9-]+\s+/i, "");
  }
  if (/^bobi\s+(director|worker|curator)$/i.test(cleaned)) {
    return cleaned.replace(/^bobi\s+/i, "");
  }
  return cleaned || role || "session";
}

function icon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const paths = {
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    clear: '<path d="m6 6 12 12M18 6 6 18"/>',
    refresh: '<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
    copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    external: '<path d="M13 5H5v14h14v-8"/><path d="M14 3h7v7M21 3l-11 11"/>',
    bolt: '<path d="m14 3-9 11h6l-1 7 9-11h-6z"/>',
  };
  svg.innerHTML = paths[kind] || paths.copy;
  return svg;
}

function money(value, precision = 4) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount <= 0) return "—";
  const digits = Math.min(10, Math.max(precision, Math.ceil(-Math.log10(amount)) + 1));
  return "$" + amount.toFixed(digits);
}

function conversationBadge(origin = {}) {
  if (!origin || origin.source !== "slack") {
    return null;
  }
  const label = origin.source === "slack"
    ? `Slack ${origin.channel_name ? "#" + origin.channel_name.replace(/^#/, "") : origin.channel_id || ""}${origin.thread_id ? " · Thread " + String(origin.thread_id).split(".")[0] : ""}`
    : origin.source;
  if (origin.source === "slack" && origin.url && /^https:\/\/(?:app\.slack\.com\/client\/|[a-z0-9-]+\.slack\.com\/archives\/)[^\s<>"']+$/i.test(origin.url)) {
    const link = node("a", label, "metrics-conversation-badge");
    link.href = origin.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.append(icon("external"));
    link.setAttribute("aria-label", `${label} (opens in a new tab)`);
    link.addEventListener("click", event => event.stopPropagation());
    return link;
  }
  return null;
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

function promptPreview(text, formatted = false) {
  const preview = node("div", "", "metrics-prompt-preview is-collapsed");
  const content = formatted ? markdown(text) : node("pre", text);
  content.classList.add("metrics-prompt-content");
  content.id = `metrics-prompt-${crypto.randomUUID()}`;
  const expand = node("button", `Show full prompt (${text.split("\n").length} lines)`, "metrics-tool-expand");
  expand.type = "button";
  expand.hidden = true;
  expand.setAttribute("aria-expanded", "false");
  expand.setAttribute("aria-controls", content.id);
  expand.addEventListener("click", () => {
    const collapsed = preview.classList.toggle("is-collapsed");
    expand.setAttribute("aria-expanded", String(!collapsed));
    expand.textContent = collapsed ? `Show full prompt (${text.split("\n").length} lines)` : "Collapse";
  });
  preview.append(content, expand);
  requestAnimationFrame(() => {
    const long = content.scrollHeight > 180;
    expand.hidden = !long;
    preview.classList.toggle("is-collapsed", long);
  });
  return preview;
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
  const isWorkflow = conversation.origin?.source === "workflow_step";
  const isMaintenance = /sleep cycle|memory compaction|maintenance/i.test(conversation.semantic_title || "")
    || ["sleep_cycle", "compaction", "sleep-cycle"].includes(conversation.origin?.source);

  if (hasUserMessage) {
    const block = node("section", "", "metrics-io-block io-block-user");
    const bar = node("div", "", "metrics-io-head");
    const labelRow = node("div", "", "io-role-row");
    if (isWorkflow) {
      labelRow.append(node("h4", "Workflow Objective"));
    } else {
      labelRow.append(node("h4", "User Message"));
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
    block.append(bar, promptPreview(conversation.user_message, true));

    if (conversation.input && conversation.input !== conversation.user_message) {
      const details = node("details", "", "metrics-full-prompt-details");
      const summaryText = isWorkflow
        ? `View workflow input & orchestration context (${number(conversation.input.length)} chars)`
        : `System prompt & raw context (${number(conversation.input.length)} chars)`;
      details.append(node("summary", summaryText), promptPreview(conversation.input));
      details.addEventListener("toggle", () => {
        if (details.open) {
          const preview = details.querySelector(".metrics-prompt-preview");
          const long = preview.querySelector(".metrics-prompt-content").scrollHeight > 180;
          preview.querySelector("button").hidden = !long;
          preview.classList.toggle("is-collapsed", long);
        }
      });
      block.append(details);
    }
    panel.append(block);
  } else if (conversation.input) {
    const block = node("section", "", "metrics-io-block io-block-system");
    const bar = node("div", "", "metrics-io-head");
    const labelRow = node("div", "", "io-role-row");
    if (isMaintenance) {
      labelRow.append(icon("bolt"), node("h4", "System / Maintenance Prompt"));
    } else {
      labelRow.append(node("h4", conversation.semantic_title === "Agent Startup & Initialization"
        ? "Agent Startup & System Context" : "System Context / Instructions"));
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
    block.append(promptPreview(conversation.input));
    panel.append(block);
  }

  if (conversation.response) {
    const block = node("section", "", "metrics-io-block io-block-assistant");
    const bar = node("div", "", "metrics-io-head");
    const labelRow = node("div", "", "io-role-row");
    labelRow.append(node("h4", "Assistant Response"));
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

// Recorded fallback reasons in operator words. Decision-level reasons send the
// turn to the control model; routing_* reasons mean no decision was made and
// the turn ran on its configured model.
const FALLBACK_TEXT = {
  policy_low_confidence: "policy confidence was below the minimum",
  policy_timeout: "policy call timed out",
  policy_unauthenticated: "TypeSafe API key is missing or was rejected",
  policy_unavailable: "policy service was unavailable",
  policy_circuit_open: "circuit breaker open after repeated policy failures",
  policy_invalid_response: "policy returned an unusable answer",
  policy_version_drift: "policy reported a different version than configured",
  route_persistence_failed: "session route could not be saved",
  routing_config_missing: "assignment secret is set but no routing policy is saved",
  routing_config_invalid: "routing policy failed validation",
  routing_assignment_secret_missing: "BOBI_METRICS_ASSIGNMENT_SECRET is not set",
  routing_policy_unavailable: "routing policy could not be loaded",
  routing_metrics_unavailable: "metrics store was unavailable",
  routing_unavailable: "router failed unexpectedly",
  routing_brain_mismatch: "policy is pinned to a different brain than this agent runs",
  routing_sticky_stale: "resumed session's saved route no longer matches the policy",
  routing_sticky_invalid: "resumed session's saved route could not be read",
  admission_rejected: "metrics store did not accept the route",
};

function fallbackText(reason) {
  return FALLBACK_TEXT[reason] || String(reason || "").replace(/^(policy|routing)_/, "").replace(/_/g, " ");
}

function routeExplanation(route = {}) {
  const model = route.model_selected || route.model_requested || "the configured model";
  if (model === "<synthetic>" || route.model_requested === "<synthetic>") {
    return ["System Turn", "Executed locally without upstream LLM API call"];
  }
  const fallback = route.fallback_reason || route.session_fallback_reason;
  if (fallback) {
    return [/breaker|circuit/.test(fallback) ? "Fallback (Breaker)" : "Fallback", fallbackText(fallback) + "; executed " + model];
  }
  if (route.route_reused) return ["Sticky Session", "Maintained the recorded session route with " + model];
  if (route.policy_mode === "shadow") return ["Shadow Evaluation", "Recommended " + (route.recommended_model || "—") + "; kept the configured model " + model];
  if (route.policy_mode === "enforce" && route.policy_status !== "not_called") return ["Router Decision", "Policy selected " + model + (route.recommended_model && route.recommended_model !== model ? "; recommendation: " + route.recommended_model : "")];
  return ["Default Config", "Executed the configured model without a policy override"];
}

// Recorded dollars win; the server's list-price figure is a labelled fallback.
function costText(costs = {}) {
  if (costs?.reported_cost_usd != null) return money(costs.reported_cost_usd);
  if (costs?.estimated_cost_usd != null) return money(costs.estimated_cost_usd);
  if (costs?.list_price_usd != null && costs.list_price_usd > 0) return "~" + money(costs.list_price_usd);
  return "—";
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
    const cleanReason = fallbackText(fallback);
    if (suggested) {
      details.append(node("div", `Suggest: ${suggested} → Fallback`, "jev-meta-models"));
    } else {
      details.append(node("div", "Fallback triggered", "jev-meta-models"));
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
  }

  container.append(details);
  return container;
}

function percent(part, whole) {
  return whole > 0 ? Math.round((100 * part) / whole) : 0;
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function usd(value) {
  return Number(value) === 0 ? "$0.00" : money(value, 2);
}

function rateText(prices) {
  return prices ? `$${prices.input_per_mtok} / $${prices.output_per_mtok}` : "no list price";
}

function kpi(label, value, sub, className = "") {
  const cell = node("div", "", `jev-kpi ${className}`);
  cell.append(node("span", label, "jev-kpi-label"), node("strong", value, "jev-kpi-value bobi-tnum"));
  const detail = node("span", "", "jev-kpi-sub");
  detail.append(...(Array.isArray(sub) ? sub : [sub]));
  cell.append(detail);
  return cell;
}

// One row per model the router picked, merged across route sets, busiest
// first. The distribution bar and the breakdown table share it, so a model
// keeps the same colour in both.
function modelRows(savings) {
  const rows = new Map();
  (savings.route_sets || []).forEach(set => set.candidates.forEach(candidate => {
    if (!candidate.turns) return;
    const row = rows.get(candidate.model) || { ...candidate, turns: 0, routed: 0, baseline: 0, fallback: 0, reasons: {}, isBaseline: true };
    row.turns += candidate.turns;
    row.routed += candidate.routed_cost_usd || 0;
    row.baseline += candidate.ceiling_cost_usd || 0;
    row.fallback += candidate.fallback_turns || 0;
    Object.entries(candidate.fallback_reasons || {}).forEach(([reason, count]) => { row.reasons[reason] = (row.reasons[reason] || 0) + count; });
    row.isBaseline &&= candidate.model === set.ceiling_model;
    rows.set(candidate.model, row);
  }));
  return [...rows.values()]
    .sort((a, b) => b.turns - a.turns || a.model.localeCompare(b.model))
    .map((row, index) => ({ ...row, swatch: `jev-swatch-${index % 6}` }));
}

function modelDistribution(rows, total) {
  const wrap = node("div", "", "jev-dist");
  if (!total) return wrap;
  const head = node("div", "", "jev-dist-head");
  head.append(node("span", "Model Distribution", "jev-section-title"), node("span", `Share of ${plural(total, "priced routed turn")}`, "jev-section-sub"));
  const bar = node("div", "", "jev-dist-bar");
  bar.setAttribute("role", "img");
  const legend = node("div", "", "jev-dist-legend");
  const parts = [];
  rows.forEach(row => {
    const share = percent(row.turns, total);
    const seg = node("span", "", `jev-dist-seg ${row.swatch}`);
    seg.style.flexGrow = String(row.turns);
    seg.title = `${row.model}: ${plural(row.turns, "turn")} (${share}%)`;
    bar.append(seg);
    const entry = node("div", "", "jev-dist-item");
    entry.append(node("span", "", `jev-dist-swatch ${row.swatch}`), node("code", row.model, "jev-dist-name"),
      node("span", `${plural(row.turns, "turn")} (${share}%)`, "jev-dist-share bobi-tnum"));
    legend.append(entry);
    parts.push(`${row.model} ${share}%`);
  });
  bar.setAttribute("aria-label", `Routed turns by model: ${parts.join(", ")}`);
  wrap.append(head, bar, legend);
  return wrap;
}

function breakdownTable(rows, savings) {
  const total = savings.priced_turns || 0;
  const wrap = node("div", "", "jev-table-wrap");
  const head = node("div", "", "jev-dist-head");
  head.append(node("span", "Model Breakdown & Cost Savings", "jev-section-title"),
    node("span", "List prices over the tokens each turn used", "jev-section-sub"));
  const table = node("table", "", "jev-table");
  const headRow = node("tr");
  [["Model", ""], ["Share / Turns", "num"], ["Actual Spend", "num"], ["Baseline Cost (Flagship)", "num"], ["Net Savings", "num"]].forEach(([label, cls]) => {
    const th = node("th", label, cls);
    th.scope = "col";
    headRow.append(th);
  });
  const thead = node("thead");
  thead.append(headRow);

  const shareCell = turns => {
    const cell = node("td", "", "num bobi-tnum");
    cell.append(node("span", `${percent(turns, total)}%`, "jev-share"), node("span", ` / ${turns}`, "jev-share-turns"));
    return cell;
  };
  const savingsCell = (saved, base, isBaseline) => {
    const cell = node("td", "", "num bobi-tnum jev-cell-saved");
    if (saved > 0) {
      cell.append(node("span", `-${usd(saved)}`, "jev-saved"), node("span", `(${(100 * saved / base).toFixed(1)}%)`, "jev-saved-pct"));
    } else {
      cell.append(node("span", "—", "jev-saved-none"), node("span", isBaseline ? "(Baseline)" : "", "jev-saved-pct"));
    }
    return cell;
  };
  const tbody = node("tbody");
  rows.forEach(item => {
    const row = node("tr");
    const model = node("td", "", "jev-cell-model");
    const code = node("code", item.model);
    code.title = `${rateText(item.prices)} per MTok in / out`;
    model.append(node("span", "", `jev-dist-swatch ${item.swatch}`), code);
    if (item.fallback) {
      const flag = node("span", `Fallback ×${item.fallback}`, "jev-flag is-fallback");
      const reasons = Object.entries(item.reasons).map(([reason, count]) => `${count} × ${fallbackText(reason)}`);
      flag.title = `${plural(item.fallback, "turn")} sent to the control model: ${reasons.join("; ") || "reason not recorded"}`;
      model.append(flag);
    }
    if (item.off_list) model.append(node("span", "Off list", "jev-flag"));
    row.append(model, shareCell(item.turns),
      node("td", usd(item.routed), "num bobi-tnum"),
      node("td", usd(item.baseline), "num bobi-tnum jev-cell-baseline"),
      savingsCell(item.baseline - item.routed, item.baseline, item.isBaseline));
    tbody.append(row);
  });
  const footRow = node("tr");
  footRow.append(node("td", "Total"), shareCell(total),
    node("td", usd(savings.routed_cost_usd), "num bobi-tnum"), node("td", usd(savings.ceiling_cost_usd), "num bobi-tnum jev-cell-baseline"),
    savingsCell(savings.saved_usd, savings.ceiling_cost_usd, false));
  const tfoot = node("tfoot");
  tfoot.append(footRow);
  table.append(thead, tbody, tfoot);
  const scroll = node("div", "", "jev-table-scroll");
  scroll.append(table);
  wrap.append(head, scroll);
  return wrap;
}

function jevValueCard(routing) {
  const card = node("div", "", "analytics-card jev-summary-card");
  const totalTurns = routing.turns || 0;
  const routedTurns = routing.routed_turns || 0;
  const fallbackTurns = routing.fallback_turns || 0;
  const savings = routing.savings || {};
  const priced = savings.saved_pct != null;

  const head = node("div", "", "analytics-card-head");
  const title = node("span", "", "analytics-card-title");
  title.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h10M4 12h16M4 18h7"/><circle cx="17" cy="6" r="2"/><circle cx="14" cy="18" r="2"/></svg>`;
  title.append(document.createTextNode(" Model Routing Value (JEV)"));
  head.append(title, node("span", routedTurns > 0 ? "routing active" : "passthrough", "chip " + (routedTurns > 0 ? "chip-accent" : "")));
  card.append(head);

  if (!routedTurns) {
    const standby = node("div", "", "ratio-standby-state");
    standby.append(node("span", "", "standby-dot"), node("span", "No routed turns in this window. Every turn ran on its configured model.", "standby-text"));
    card.append(standby);
    return card;
  }

  const kpis = node("div", "", "jev-kpis");
  if (priced) {
    const saved = savings.saved_pct;
    const strongest = savings.ceiling_model ? node("code", savings.ceiling_model) : document.createTextNode("the strongest candidate");
    kpis.append(kpi(saved < 0 ? "net extra spend" : "net saved", usd(Math.abs(savings.saved_usd)),
      [node("span", `${Math.abs(saved).toFixed(1)}% ${saved < 0 ? "costlier" : "cheaper"}`, "jev-kpi-emph"), document.createTextNode(" than always "), strongest],
      saved < 0 ? "is-negative" : "is-saved"));
  } else {
    const missing = savings.unpriced_models || [];
    kpis.append(kpi("net saved", "—", missing.length ? `no list price for ${missing.join(", ")}` : "no routed usage recorded yet", "is-unknown"));
  }
  kpis.append(
    kpi("routing rate", `${percent(routedTurns, totalTurns)}%`, `${routedTurns} of ${totalTurns} turns routed`),
    kpi("safety fallbacks", String(fallbackTurns), `${fallbackTurns === 1 ? "turn" : "turns"} returned to the control model`),
    kpi("router overhead", routing.mean_router_latency_ms == null ? "—" : `${Number(routing.mean_router_latency_ms).toFixed(0)} ms`, "mean per decision"),
  );
  card.append(kpis);

  if (priced) {
    const rows = modelRows(savings);
    card.append(modelDistribution(rows, savings.priced_turns || 0), breakdownTable(rows, savings));
  }

  const notes = [];
  if (savings.unpriced_turns) notes.push(`${plural(savings.unpriced_turns, "routed turn")} left out: no list price for ${(savings.unpriced_models || []).join(", ") || "a model"}.`);
  const unmeasured = routedTurns - (savings.priced_turns || 0) - (savings.unpriced_turns || 0);
  if (unmeasured > 0) notes.push(`${plural(unmeasured, "routed turn")} without recorded usage.`);
  if (priced) notes.unshift("* Baseline Cost (Flagship) prices every turn on the most expensive model its router could pick. Fallback turns ran the configured Control Model.");
  card.append(node("p", notes.join(" "), "metrics-note jev-footnote"));
  return card;
}

// Telemetry health folds into one strip; it only draws attention when
// something needs it (unknown usage, import lag, reconciliation errors).
function telemetryStrip(data) {
  const coverage = data.coverage || {};
  const collector = data.collector || {};
  const strip = node("div", "", "metrics-health-strip");
  strip.setAttribute("aria-label", "telemetry health");
  const live = collector.status === "running";
  strip.append(node("span", live ? "live" : (collector.status || "idle"), `metrics-health-state${live ? " is-live" : ""}`));
  const items = [
    ["coverage", `${number(coverage.exact_invocations)} exact · ${number(coverage.estimated_invocations)} derived · ${number(coverage.unknown_invocations)} unknown`, coverage.unknown_invocations > 0],
    ["granularity", coverage.usage_granularity || "none", false],
    ["import lag", collector.import_lag_ms == null ? "not recorded" : `${number(collector.import_lag_ms)} ms`, collector.import_lag_ms > 60000],
    ["reconciliation", plural(collector.reconciliation_errors || 0, "error") + (collector.uncovered_turns ? ` · ${plural(collector.uncovered_turns, "uncovered turn")}` : ""), collector.reconciliation_errors > 0],
  ];
  items.forEach(([label, value, warn]) => {
    const item = node("span", "", `metrics-health-item${warn ? " is-warn" : ""}`);
    item.append(node("span", label, "metrics-health-label"), node("span", value, "bobi-tnum"));
    strip.append(item);
  });
  strip.title = "Totals use canonical usage. Unknown counters stay unknown. Call IDs may be reused across turns; routing selects once per fresh session.";
  return strip;
}

function groupTurnsIntoThreads(turns) {
  const map = new Map();
  turns.forEach(turn => {
    const sid = turn.session_id || `ses_unassigned_${turn.turn_id}`;
    if (!map.has(sid)) {
      map.set(sid, {
        sessionId: sid,
        role: turnRole(turn),
        agent: turn.agent || "agent",
        latestAtUs: turn.started_at_us || 0,
        totalTokens: 0,
        totalCostUsd: 0,
        hasCost: false,
        approximateCost: false,
        turns: [],
      });
    }
    const thread = map.get(sid);
    thread.turns.push(turn);
    thread.latestAtUs = Math.max(thread.latestAtUs, turn.started_at_us || 0);
    if (turn.usage?.input_tokens != null && turn.usage?.output_tokens != null) {
      thread.totalTokens += Number(turn.usage.input_tokens) + Number(turn.usage.output_tokens);
    }
    const recorded = turn.costs?.reported_cost_usd ?? turn.costs?.estimated_cost_usd;
    const cost = recorded ?? turn.costs?.list_price_usd;
    if (cost != null) {
      thread.totalCostUsd += cost;
      thread.hasCost = true;
      thread.approximateCost ||= recorded == null;
    }
  });

  const threads = Array.from(map.values());
  threads.sort((a, b) => b.latestAtUs - a.latestAtUs);
  threads.forEach(th => {
    th.turns.sort((a, b) => (a.turn_index ?? 0) - (b.turn_index ?? 0) || (a.started_at_us || 0) - (b.started_at_us || 0));
    // Turns arrive newest-first; the thread's origin is its earliest linkable one.
    const withOrigin = th.turns.filter(turn => turn.conversation?.origin);
    th.origin = (withOrigin.find(turn => conversationBadge(turn.conversation.origin)) || withOrigin[0])?.conversation.origin || null;
  });
  return threads;
}

// One classifier for thread headers and flat rows: a turn with a recorded user
// message is a person talking to the agent; anything else is runtime work.
function turnTopic(turn = {}) {
  const conversation = turn.conversation || {};
  const firstLine = text => (text || "").split("\n").find(line => line.trim())?.trim() || "";
  if (conversation.user_message) {
    return { title: firstLine(conversation.user_message) || "message", kind: "user" };
  }
  const semantic = conversation.semantic_title || turn.semantic_title;
  const context = `${turn.session_name || ""} ${conversation.origin?.source || ""} ${conversation.input || ""}`;
  const title = semantic
    || (/sleep|curator|memory compaction/i.test(context) ? "Sleep Cycle & Memory Compaction"
      : /inbox/i.test(context) ? "Inbox Event Processing"
      : /idle|heartbeat/i.test(context) ? "Idle Standby (No new events)"
      : "Agent Startup & Initialization");
  return { title, kind: "system" };
}

function kindTag(kind) {
  const tag = node("span", kind === "user" ? "user prompt" : "system lifecycle", `metrics-kind-tag kind-${kind}`);
  tag.title = kind === "user" ? "A person asked the agent something" : "Runtime maintenance, startup or background processing";
  return tag;
}

function relativeTime(startedAtUs, long = false) {
  const date = new Date(startedAtUs / 1000);
  const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
  const element = node("span", minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago`
    : long ? date.toLocaleString() : date.toLocaleTimeString(), "metrics-provenance");
  element.title = date.toISOString();
  return element;
}

function agentRoleBadge(agent, role) {
  const badge = node("span", "", "mth-agent-badge");
  badge.append(node("span", agent, "mth-agent-name"), node("span", role, "mth-agent-role"));
  badge.title = `agent ${agent} · role ${role}`;
  return badge;
}

function turnRole(turn, fallback = "agent") {
  return turn.role || turn.session_name?.split(/[-_:]/)[0] || fallback;
}

function modelCell(turn) {
  const cell = node("div", "", "cell-model-decision");
  const models = [...new Set((turn.invocations || []).map(item => item.model_selected || item.model_requested).filter(Boolean))];
  const raw = models.join(", ") || turn.model_selected || "—";
  cell.append(node("code", raw === "<synthetic>" ? "System / Local" : raw, "table-code-model"), jevStatusBadge(turn));
  return cell;
}

function tokenCostCell(turn) {
  const total = turn.usage?.input_tokens != null && turn.usage?.output_tokens != null
    ? Number(turn.usage.input_tokens) + Number(turn.usage.output_tokens) : null;
  const cell = node("span", (total == null ? "—" : number(total) + " tok") + " · " + costText(turn.costs), "bobi-tnum");
  cell.title = "Input + output tokens. A ~ cost is a list-price estimate; recorded dollars show without it.";
  return cell;
}

function latencyText(turn) {
  return turn.wall_duration_ms == null ? "—" : (turn.wall_duration_ms / 1000).toFixed(1) + "s";
}

function getTurnActionSummary(turn, isFirstRequest = true) {
  const conversation = turn.conversation || {};
  const toolCount = turn.tool_count || (turn.tool_executions || []).length;
  const snippet = text => {
    const line = (text || "").split("\n").find(item => item.trim())?.trim() || "";
    return line ? `"${line.slice(0, 85)}${line.length > 85 ? "…" : ""}"` : "";
  };
  if (toolCount > 0) {
    const first = (turn.tool_executions || [])[0]?.tool_name;
    const title = first ? `tool: ${first}${toolCount > 1 ? ` +${toolCount - 1}` : ""}` : `${toolCount} tool call${toolCount > 1 ? "s" : ""}`;
    const detail = `${toolCount} tool execution${toolCount > 1 ? "s" : ""}${turn.wall_duration_ms != null ? ` · ${(turn.wall_duration_ms / 1000).toFixed(1)}s` : ""}`;
    return { title, detail, kind: "tool" };
  }
  if (conversation.user_message) {
    return { title: isFirstRequest ? "initial request" : "follow-up", detail: snippet(conversation.user_message), kind: "prompt" };
  }
  if (conversation.response) {
    return { title: "assistant response", detail: snippet(conversation.response), kind: "response" };
  }
  return { title: turnTopic(turn).title, detail: "runtime lifecycle step", kind: "system" };
}

function renderThreadGroups(container, turns, open, agentName = "") {
  const threads = groupTurnsIntoThreads(turns);
  const threadsContainer = node("div", "", "metrics-threads-container");

  threads.forEach(thread => {
    const card = node("div", "", "metrics-thread-card");
    card.dataset.sessionId = thread.sessionId;
    const topic = turnTopic(thread.turns.find(turn => turn.conversation?.user_message) || thread.turns[0]);
    card.classList.add(`kind-${topic.kind}`);

    const head = node("div", "", "metrics-thread-head");
    const left = node("div", "", "mth-left");
    const toggleBtn = node("button", "", "mth-toggle-btn");
    toggleBtn.type = "button";
    toggleBtn.setAttribute("aria-label", "Toggle thread");
    toggleBtn.setAttribute("aria-expanded", "true");
    toggleBtn.innerHTML = `<svg class="mth-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`;

    const title = node("span", topic.title, "mth-title");
    title.title = topic.title;
    const agent = agentName || thread.agent || "agent";
    left.append(toggleBtn, kindTag(topic.kind), title, agentRoleBadge(agent, thread.role || "agent"));

    const origin = conversationBadge(thread.origin);
    if (origin) left.append(origin);

    if (thread.sessionId && !thread.sessionId.startsWith("ses_unassigned_")) {
      const sessionPill = node("button", shortSessionId(thread.sessionId), "session-id-pill mth-session-pill");
      sessionPill.type = "button";
      sessionPill.title = "Copy session ID: " + thread.sessionId;
      sessionPill.addEventListener("click", async event => {
        event.stopPropagation();
        try {
          await navigator.clipboard.writeText(thread.sessionId);
          sessionPill.textContent = "copied";
          setTimeout(() => { sessionPill.textContent = shortSessionId(thread.sessionId); }, 1200);
        } catch {
          sessionPill.title = "Copy unavailable: " + thread.sessionId;
        }
      });
      left.append(sessionPill);
    }

    const right = node("div", "", "mth-right");
    right.append(node("span", `${thread.turns.length} turn${thread.turns.length > 1 ? "s" : ""}`, "mth-pill-turns"));
    if (thread.totalTokens > 0) right.append(node("span", `${number(thread.totalTokens)} tok`, "mth-stat bobi-tnum"));
    if (thread.hasCost && thread.totalCostUsd > 0) {
      right.append(node("span", (thread.approximateCost ? "~" : "") + money(thread.totalCostUsd), "mth-stat mth-cost bobi-tnum"));
    }
    right.append(relativeTime(thread.latestAtUs));
    head.append(left, right);

    const toggle = () => {
      const collapsed = card.classList.toggle("is-collapsed");
      toggleBtn.setAttribute("aria-expanded", String(!collapsed));
    };
    head.addEventListener("click", event => { if (!event.target.closest("button, a")) toggle(); });
    toggleBtn.addEventListener("click", event => { event.stopPropagation(); toggle(); });
    card.append(head);

    const body = node("div", "", "mth-body");
    const firstRequest = thread.turns.find(turn => turn.conversation?.user_message);
    table(body, ["Turn", "Step", "Model & Decision", "Confidence", "Tokens / Cost", "Latency"], thread.turns.map((turn, tIdx) => {
      const identity = node("div", "", "metrics-turn-identity");
      identity.append(node("span", "#" + (turn.turn_index ?? tIdx + 1), "bobi-tnum"), relativeTime(turn.started_at_us));

      const action = getTurnActionSummary(turn, turn === firstRequest);
      const topicCell = node("div", "", "metrics-topic-cell");
      const topicHeader = node("div", "", "metrics-topic-header");
      // Thread header already names agent, role and origin; repeat only what differs.
      const role = turnRole(turn, thread.role);
      if (role !== thread.role || (turn.agent && turn.agent !== thread.agent)) {
        topicHeader.append(node("span", role, "mth-subrole-badge"));
      }
      const headline = node("span", action.title, `metrics-topic-title action-${action.kind}`);
      headline.title = action.title;
      topicHeader.append(headline);

      const topicSub = node("div", "", "metrics-topic-meta");
      const turnOrigin = turn.conversation?.origin;
      if (turnOrigin && JSON.stringify(turnOrigin) !== JSON.stringify(thread.origin)) {
        const badge = conversationBadge(turnOrigin);
        if (badge) topicSub.append(badge);
      }
      if (action.detail) {
        const detail = node("span", action.detail, "metrics-provenance");
        detail.title = action.detail;
        topicSub.append(detail);
      }
      if (turn.error_kind) topicSub.append(node("span", turn.error_kind, "mth-turn-error-tag"));
      topicCell.append(topicHeader, topicSub);

      return {
        turn,
        onClick: () => open(turn),
        cells: [identity, topicCell, modelCell(turn), confidenceCell(turn.confidence), tokenCostCell(turn), latencyText(turn)]
      };
    }));

    card.append(body);
    threadsContainer.append(card);
  });

  container.append(threadsContainer);
}

function turnRows(container, turns, open, agentName = "", onSessionClick = null, viewMode = "threads") {
  if (viewMode === "threads") {
    renderThreadGroups(container, turns, open, agentName);
    return;
  }

  // Per-session turn counts decide whether "#n" carries information: a lone
  // turn of a single-turn session would only ever read "#1".
  const perSession = new Map();
  turns.forEach(turn => perSession.set(turn.session_id, (perSession.get(turn.session_id) || 0) + 1));

  table(container, ["When", "Session / Topic", "Model & Decision", "Confidence", "Tokens / Cost", "Latency"], turns.map((turn, index) => {
    const identity = node("div", "", "metrics-turn-identity");
    identity.append(relativeTime(turn.started_at_us, true));
    if (perSession.get(turn.session_id) > 1) identity.append(node("span", "turn #" + (turn.turn_index ?? index + 1), "metrics-turn-index bobi-tnum"));

    const topic = turnTopic(turn);
    const topicCell = node("div", "", "metrics-topic-cell");
    const topicHeader = node("div", "", "metrics-topic-header");
    const headline = node("span", topic.title, "metrics-topic-title");
    headline.title = topic.title;
    topicHeader.append(kindTag(topic.kind), headline);

    const topicSub = node("div", "", "metrics-topic-meta");
    const role = turnRole(turn);
    topicSub.append(agentRoleBadge(agentName || turn.agent || "agent", role));
    const sessionName = humanSessionName(turn);
    if (turn.session_id) {
      const session = node(onSessionClick ? "button" : "span", sessionName === role ? shortSessionId(turn.session_id) : sessionName, "metrics-session-chip-inline");
      session.title = `filter to session ${turn.session_name || ""} (${turn.session_id})`.replace(" ()", "");
      if (onSessionClick) {
        session.type = "button";
        session.addEventListener("click", event => { event.stopPropagation(); onSessionClick(turn.session_id); });
      }
      topicSub.append(session);
    }
    const origin = conversationBadge(turn.conversation?.origin);
    if (origin) topicSub.append(origin);
    topicCell.append(topicHeader, topicSub);

    return {turn, onClick: () => open(turn, index), cells: [identity, topicCell, modelCell(turn), confidenceCell(turn.confidence), tokenCostCell(turn), latencyText(turn)]};
  }));
}

function matchToolsWithTranscript(tools, entries, turn) {
  if (!entries || !entries.length) return tools.map(t => ({ tool: t, command: "", result: "" }));
  
  const toolEntries = [];
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    if (e.kind === "tool") {
      let resultText = "";
      let resultEntry = null;
      if (i + 1 < entries.length && entries[i + 1].kind === "tool_result") {
        resultEntry = entries[i + 1];
        resultText = resultEntry.text || "";
      }
      toolEntries.push({
        index: toolEntries.length,
        tool: (e.tool || "").toLowerCase(),
        command: e.text || "",
        result: resultText,
        inputPreview: e,
        resultPreview: resultEntry,
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
      inputPreview: bestMatch?.inputPreview,
      resultPreview: bestMatch?.resultPreview,
    };
  });
}

function renderToolExecutionsList(container, tools, entries, turn) {
  const matched = matchToolsWithTranscript(tools, entries, turn);
  matched.forEach(({tool, command, result, inputPreview, resultPreview}, index) => {
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
    for (const [label, text, preview] of [["Input", command, inputPreview], ["Result", result, resultPreview]]) {
      if (!text) continue;
      hasBody = true;
      const block = node("div", "", "metrics-tool-body");
      const code = node("div", "", "metrics-tool-code");
      const pre = node("pre", text);
      pre.tabIndex = 0;
      pre.setAttribute("aria-label", `Tool ${label.toLowerCase()} code`);
      const copy = node("button", "", "metrics-tool-copy");
      copy.type = "button";
      const copyLabel = "Copy " + label.toLowerCase();
      copy.title = copyLabel;
      copy.setAttribute("aria-label", copyLabel);
      copy.append(icon("copy"));
      const feedback = node("span", "", "metrics-copy-feedback");
      feedback.setAttribute("role", "status");
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          copy.replaceChildren(icon("check"));
          copy.title = "Copied";
          copy.setAttribute("aria-label", "Copied " + label.toLowerCase());
          feedback.textContent = "Copied";
          setTimeout(() => {
            copy.replaceChildren(icon("copy"));
            copy.title = copyLabel;
            copy.setAttribute("aria-label", copyLabel);
            feedback.textContent = "";
          }, 1500);
        } catch {
          feedback.textContent = "Copy unavailable";
          copy.title = "Copy unavailable";
        }
      });
      code.append(pre, copy, feedback);
      block.append(node("span", label), code);
      const lineCount = text.split("\n").length;
      let expanded = lineCount <= 10;
      code.classList.toggle("is-collapsed", !expanded);
      if (!expanded) pre.textContent = text.split("\n").slice(0, 10).join("\n");
      const previewNote = node("p", "", "metrics-tool-truncated");
      function updatePreviewNote() {
        if (!preview?.truncated) return;
        const shownBytes = new TextEncoder().encode(pre.textContent).length;
        const totalBytes = Number(preview.total_bytes);
        previewNote.textContent = `Truncated · showing ${number(pre.textContent.length)} characters (${number(shownBytes)} bytes)${Number.isFinite(totalBytes) && totalBytes > shownBytes ? ` of ${number(totalBytes)} bytes` : ""}. Copy includes the available preview only.`;
      }
      {
        const expandLabel = `Expand full ${label.toLowerCase()} (${lineCount} lines)`;
        const expand = node("button", expandLabel, "metrics-tool-expand");
        expand.type = "button";
        expand.hidden = expanded;
        expand.setAttribute("aria-expanded", String(expanded));
        pre.id = `metrics-tool-${index}-${label.toLowerCase()}`;
        expand.setAttribute("aria-controls", pre.id);
        card.addEventListener("toggle", () => {
          if (card.open && expand.hidden && pre.scrollHeight > 220) {
            expanded = false;
            expand.hidden = false;
            expand.setAttribute("aria-expanded", "false");
            code.classList.add("is-collapsed");
          }
        });
        expand.addEventListener("click", () => {
          expanded = !expanded;
          code.classList.toggle("is-expanded", expanded);
          code.classList.toggle("is-collapsed", !expanded);
          pre.textContent = expanded ? text : text.split("\n").slice(0, 10).join("\n");
          expand.setAttribute("aria-expanded", String(expanded));
          expand.textContent = expanded ? "Collapse" : expandLabel;
          updatePreviewNote();
        });
        block.append(expand);
      }
      if (preview?.truncated) {
        updatePreviewNote();
        block.append(previewNote);
      }
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
    pair(banner, "Total tokens", totalTokens == null ? "—" : number(totalTokens));
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
      "Canonical usage only. Reasoning is included in output. Turn totals can supersede invocation usage; rows are not blindly summed."));
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
      const latText = latencyMs == null ? "not recorded" : (latencyMs < 1000 ? Math.round(latencyMs) + "ms" : (latencyMs / 1000).toFixed(1) + "s");
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

      // 8. Cost Cell
      const terminalCost = invocations.length === 1 ? (data.cost_measurements || []).find(item => item.scope === "turn") : null;
      const turnLevelCost = (data.cost_measurements || []).find(item => item.scope === "turn");
      const cost = (data.cost_measurements || []).filter(item => item.invocation_id === invocation.invocation_id)
        .sort((first, second) => first.is_estimated - second.is_estimated || second.observed_at_us - first.observed_at_us)[0] || terminalCost;
      const amountCell = node("div", "", "token-cell");
      if (cost?.amount_usd != null) {
        amountCell.append(node("span", money(cost.amount_usd, 6), "bobi-tnum token-main-val text-green"));
      } else {
        if (usage.list_price_usd > 0) {
          amountCell.append(node("span", "~" + money(usage.list_price_usd, 6), "bobi-tnum token-main-val"));
          amountCell.append(node("span", "list price", "token-sub-val"));
        } else if (turnLevelCost?.amount_usd != null && invocations.length > 1) {
          amountCell.append(node("span", "—", "bobi-tnum token-main-val"));
          amountCell.append(node("span", "billed per turn", "token-sub-val"));
        } else {
          amountCell.append(node("span", "—", "bobi-tnum token-main-val"));
        }
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
        amountCell
      ];
    }));
  } else {
    panelUsage.append(node("p", "No model invocations recorded for this turn.", "metrics-note"));
  }
  const totalLine = node("div", "", "metrics-turn-totals");
  pair(totalLine, "Turn input / output", `${number(totals.input_tokens)} / ${number(totals.output_tokens)}`);
  pair(totalLine, "Cache read / write", `${number(totals.cache_read_input_tokens)} / ${number(totals.cache_write_input_tokens)}`);
  let turnCostForTotal = (data.cost_measurements || []).find(item => item.scope === "turn") || (costs.length ? costs[0] : null);
  const listPrices = (data.best_usage || []).map(item => item.list_price_usd);
  if (!turnCostForTotal && listPrices.length && listPrices.every(value => value != null)) {
    turnCostForTotal = { amount_usd: listPrices.reduce((sum, value) => sum + value, 0), scope: "turn", measurement_source: "list price" };
  }
  if (turnCostForTotal?.amount_usd != null) {
    pair(totalLine, "Turn cost", money(turnCostForTotal.amount_usd, 6));
  }
  panelUsage.append(totalLine);

  technical.append(panelUsage);
  if (costs.length || turnCostForTotal) {
    const costPanel = node("div", "", "panel-cost");
    const displayCosts = costs.length ? costs : [turnCostForTotal];
    costPanel.append(createSectionHead("", "Turn Invoiced / Reconciled Spend", displayCosts.length,
      "Recorded cost for the overall turn."));
    table(costPanel, ["Scope", "Model", "USD"], displayCosts.map(cost => [
      cost.scope || "turn",
      cost.model || (invocations[0]?.model_selected || "—"),
      node("span", money(cost.amount_usd, 6), "bobi-tnum text-green")
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
  const jevHeaderBtn = node("button", "JEV: Disabled", "btn bobi-btn small jev-header-btn disabled");
  jevHeaderBtn.type = "button";
  jevHeaderBtn.title = `Configure TypeSafe JEV dynamic model routing for ${name}`;
  jevHeaderBtn.addEventListener("click", openJevConfigModal);
  const refreshBtn = node("button", "refresh", "btn bobi-btn small");
  refreshBtn.type = "button";
  refreshBtn.id = "metrics-refresh";
  refreshBtn.prepend(icon("refresh"));
  refreshBtn.setAttribute("aria-label", "Refresh metrics");
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
  modelInput.id = "metrics-model";
  modelInput.type = "text";
  modelInput.setAttribute("aria-label", "Provider model filter");
  modelInput.placeholder = "filter by model…";
  modelInput.addEventListener("input", () => {
    modelRevision++;
    clearTimeout(modelDebounce);
    modelDebounce = setTimeout(() => {
      modelDebounce = null;
      load(true);
    }, 300);
  });
  const sessionInput = node("select", "", "metrics-search-input");
  sessionInput.id = "metrics-session";
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
  timeRange.id = "metrics-time-range";
  timeRange.setAttribute("aria-label", "Time range");
  for (const [value, label] of [["1", "Last 1h"], ["24", "24h"], ["168", "7d"], ["744", "All (up to 31d)"]]) {
    const option = node("option", label); option.value = value; timeRange.append(option);
  }
  timeRange.value = "24";
  timeRange.addEventListener("change", () => load(true));
  const search = node("input", "", "metrics-search-input");
  search.id = "metrics-search";
  search.type = "search";
  search.placeholder = "Search loaded turns…";
  search.setAttribute("aria-label", "Search loaded turns");
  search.addEventListener("input", () => { searchClear.hidden = !search.value; drawTurns(); });
  const searchBox = node("div", "", "metrics-search-box search-turns-box");
  const searchIcon = icon("search");
  searchIcon.classList.add("search-icon");
  const searchClear = node("button", "", "metrics-search-clear");
  searchClear.id = "metrics-search-clear";
  searchClear.type = "button";
  searchClear.hidden = true;
  searchClear.title = "Clear search";
  searchClear.setAttribute("aria-label", "Clear search");
  searchClear.append(icon("clear"));
  searchClear.addEventListener("click", () => {
    search.value = "";
    searchClear.hidden = true;
    search.focus();
    drawTurns();
  });
  searchBox.append(searchIcon, search, searchClear);

  const auto = node("input");
  auto.type = "checkbox";
  auto.id = "metrics-live";
  auto.setAttribute("role", "switch");
  auto.setAttribute("aria-label", "Auto-refresh metrics");
  auto.checked = true;
  const liveState = node("span", "Live", "metrics-live-state");
  auto.addEventListener("change", () => {
    liveState.textContent = auto.checked ? "Live" : "Paused";
    if (auto.checked) load(true, false);
  });
  const autoLabel = node("label", "", "metrics-auto-refresh");
  autoLabel.append(auto, node("span", "", "metrics-switch-track"), liveState);

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
  jevModalCloseBtn.setAttribute("aria-label", "Close dialog");
  jevModalCloseBtn.addEventListener("click", closeJevConfigModal);
  jevModalHead.append(jevModalLeft, jevModalCloseBtn);

  const jevModalNavTabs = node("div", "", "tabs jev-modal-nav-tabs");
  jevModalNavTabs.innerHTML = `
    <button type="button" class="tab active" data-jev-tab="status">Overview</button>
    <button type="button" class="tab" data-jev-tab="guide">Configure</button>
    <button type="button" class="tab" data-jev-tab="sandbox">Test Routing</button>
  `;
  jevModalNavTabs.setAttribute("role", "tablist");
  jevModalNavTabs.setAttribute("aria-label", "JEV configuration tabs");

  function setJevModalTab(tabId) {
    const tabs = jevModalNavTabs.querySelectorAll(".tab");
    tabs.forEach(tab => {
      const active = tab.dataset.jevTab === tabId;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    jevModalBody.querySelectorAll(".jev-tab-pane").forEach(pane => { pane.hidden = pane.id !== `jev-pane-${tabId}`; });
    const footer = jevModalBody.querySelector(".jev-modal-footer");
    if (footer) footer.hidden = tabId === "sandbox";
  }

  jevModalNavTabs.querySelectorAll(".tab").forEach(btn => {
    btn.id = `jev-tab-${btn.dataset.jevTab}`;
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-controls", `jev-pane-${btn.dataset.jevTab}`);
    btn.addEventListener("click", () => setJevModalTab(btn.dataset.jevTab));
    btn.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...jevModalNavTabs.querySelectorAll(".tab")];
      const index = tabs.indexOf(btn);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      setJevModalTab(tabs[next].dataset.jevTab);
      tabs[next].focus();
    });
  });

  const jevModalBody = node("div", "", "modal-body jev-modal-body");

  const jevConfirmOverlay = node("div", "", "jev-confirm-overlay");
  jevConfirmOverlay.hidden = true;
  jevConfirmOverlay.innerHTML = `
    <div class="jev-confirm-card" role="alertdialog" aria-modal="true" aria-labelledby="jev-confirm-title" aria-describedby="jev-confirm-description">
      <div class="jev-confirm-icon"></div>
      <div class="jev-confirm-body">
        <h4 class="jev-confirm-title" id="jev-confirm-title">Reset / delete JEV configuration?</h4>
        <p class="jev-confirm-text" id="jev-confirm-description">
          Remove the saved JEV policy and disable routing for this agent.
        </p>
        <p class="jev-confirm-subtext">
          Restart the agent after deletion to apply changes to production sessions.
        </p>
        <div class="jev-confirm-status-msg" role="status"></div>
      </div>
      <div class="jev-confirm-footer">
        <button type="button" class="btn bobi-btn quiet jev-confirm-btn-cancel">Cancel</button>
        <button type="button" id="jev-config-confirm" class="btn bobi-btn primary jev-confirm-btn-restart">Reset / Delete configuration</button>
      </div>
    </div>
  `;

  let configDirty = false;
  let configPending = false;
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
    if (cRestart) cRestart.textContent = "Reset / Delete configuration";
    cCancel?.focus();
  }

  function closeConfirmPopup() {
    if (configPending) return;
    jevConfirmOverlay.hidden = true;
    jevModalBody.querySelector("#jev-config-delete")?.focus();
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
      const legacy = node("option", `Selected session · ${currentSessionFilter.startsWith("ses_") ? "outside recent window" : "all matching lifecycles"}`);
      legacy.title = currentSessionFilter;
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
  let modelDebounce;
  let modelRevision = 0;
  const controller = new AbortController();
  let detailController;
  let params;
  let cursor = "";
  const base = `/api/agents/${encodeURIComponent(name)}/metrics`;

  let activeTurnIndex = -1;
  let currentTurns = [];
  let currentDrawerTab = "overview";
  let currentTurnsView = "threads";

  function closeDrawer() {
    drawerBackdrop.hidden = true;
    drawerBackdrop.classList.remove("open");
    activeTurnIndex = -1;
    turns.querySelector("tbody tr.turn-row-selected")?.focus();
    turns.querySelectorAll("tbody tr.turn-row-selected").forEach(r => r.classList.remove("turn-row-selected"));
    document.removeEventListener("keydown", handleDrawerKey);
  }

  function getThreadTurns(turn) {
    if (!turn?.session_id) return [turn];
    const matching = currentTurns.filter(t => t.session_id === turn.session_id);
    if (!matching.length) return [turn];
    return [...matching].sort((a, b) => {
      if (a.turn_index != null && b.turn_index != null) return a.turn_index - b.turn_index;
      return (a.started_at_us || 0) - (b.started_at_us || 0);
    });
  }

  function handleDrawerKey(e) {
    if (e.key.startsWith("Arrow") && e.target.closest(".metrics-tool-code")) return;
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
      return;
    }
    const currentTurn = currentTurns[activeTurnIndex];
    if (!currentTurn) return;
    const threadTurns = getThreadTurns(currentTurn);
    const pos = threadTurns.findIndex(t => t.turn_id === currentTurn.turn_id);
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      if (pos > 0) {
        e.preventDefault();
        const prevTurn = threadTurns[pos - 1];
        const idx = currentTurns.findIndex(t => t.turn_id === prevTurn.turn_id);
        openAtIndex(idx, prevTurn);
      }
    } else if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      if (pos >= 0 && pos < threadTurns.length - 1) {
        e.preventDefault();
        const nextTurn = threadTurns[pos + 1];
        const idx = currentTurns.findIndex(t => t.turn_id === nextTurn.turn_id);
        openAtIndex(idx, nextTurn);
      }
    }
  }

  drawerBackdrop.addEventListener("click", (e) => {
    if (e.target === drawerBackdrop) closeDrawer();
  });

  async function openAtIndex(index, explicitTurn = null) {
    const turn = explicitTurn || currentTurns[index];
    if (!turn) return;
    activeTurnIndex = index >= 0 ? index : currentTurns.findIndex(t => t.turn_id === turn.turn_id);

    // Highlight row in table
    const tableRows = turns.querySelectorAll("tbody tr");
    tableRows.forEach(r => {
      r.classList.toggle("turn-row-selected", r.dataset.turnId === turn.turn_id);
    });

    drawerBackdrop.hidden = false;
    drawerBackdrop.classList.add("open");
    document.addEventListener("keydown", handleDrawerKey);

    renderDrawerHead(turn);
    drawerHead.querySelector(".td-head-right button")?.focus();

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

  function renderDrawerHead(turn) {
    drawerHead.replaceChildren();

    const left = node("div", "", "td-head-left");
    const turnIndex = turn.turn_index != null ? turn.turn_index : 1;
    const eyebrow = node("span", `Turn #${turnIndex}`, "td-badge-turn");
    left.append(eyebrow);

    if (turn.turn_id && turn.turn_id !== turn.session_id) {
      const turnIdChip = node("code", shortSessionId(turn.turn_id), "td-id-chip");
      turnIdChip.title = `Turn ID: ${turn.turn_id} (Click to copy)`;
      turnIdChip.addEventListener("click", () => {
        navigator.clipboard?.writeText(turn.turn_id);
        turnIdChip.textContent = "copied";
        setTimeout(() => { turnIdChip.textContent = shortSessionId(turn.turn_id); }, 1200);
      });
      left.append(turnIdChip);
    }

    if (turn.session_id) {
      const sessGroup = node("div", "", "td-head-session");
      sessGroup.append(node("span", "Session", "td-badge-session"));
      const sessChip = node("code", shortSessionId(turn.session_id), "td-id-chip td-sess-chip");
      sessChip.title = `Session: ${turn.session_id} (Click to copy)`;
      sessChip.addEventListener("click", () => {
        navigator.clipboard?.writeText(turn.session_id);
        sessChip.textContent = "copied";
        setTimeout(() => { sessChip.textContent = shortSessionId(turn.session_id); }, 1200);
      });
      sessGroup.append(sessChip);
      const isGenericName = !turn.session_name || /^bobi[-_].*[-_](director|worker|curator)$/i.test(turn.session_name) || turn.session_name === "session";
      if (turn.session_name && !isGenericName) {
        const sessName = node("span", `(${turn.session_name})`, "td-sess-name");
        sessName.title = "Session name: " + turn.session_name;
        sessGroup.append(sessName);
      }
      left.append(sessGroup);
    }

    const threadTurns = getThreadTurns(turn);
    const threadIndex = threadTurns.findIndex(t => t.turn_id === turn.turn_id);
    const pos = threadIndex >= 0 ? threadIndex : 0;
    const threadTotal = threadTurns.length;

    const center = node("div", "", "td-head-nav");
    const prevBtn = node("button", "← Earlier", "btn bobi-btn quiet small td-nav-btn td-nav-prev");
    prevBtn.type = "button";
    prevBtn.disabled = pos <= 0;
    prevBtn.title = pos > 0 ? `Go to Turn #${threadTurns[pos - 1].turn_index ?? pos} in this thread` : "No earlier turns in this thread";
    prevBtn.addEventListener("click", () => {
      if (pos > 0) {
        const prevTurn = threadTurns[pos - 1];
        const idx = currentTurns.findIndex(t => t.turn_id === prevTurn.turn_id);
        openAtIndex(idx, prevTurn);
      }
    });

    const counterText = threadTotal > 1
      ? `Turn ${pos + 1} of ${threadTotal}`
      : `Turn #${turn.turn_index ?? 1}`;
    const counter = node("span", counterText, "td-nav-counter");
    if (threadTotal > 1) {
      counter.title = `Turn ${pos + 1} of ${threadTotal} in this thread`;
    } else {
      counter.title = "Single turn thread";
    }

    const nextBtn = node("button", "Later →", "btn bobi-btn quiet small td-nav-btn td-nav-next");
    nextBtn.type = "button";
    nextBtn.disabled = pos >= threadTotal - 1;
    nextBtn.title = pos < threadTotal - 1 ? `Go to Turn #${threadTurns[pos + 1].turn_index ?? (pos + 2)} in this thread` : "No later turns in this thread";
    nextBtn.addEventListener("click", () => {
      if (pos < threadTotal - 1) {
        const nextTurn = threadTurns[pos + 1];
        const idx = currentTurns.findIndex(t => t.turn_id === nextTurn.turn_id);
        openAtIndex(idx, nextTurn);
      }
    });

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
    let costSub = "No spend recorded";
    if (hasSpend) {
      costDisplay = costText(data.totals);
      if (costDisplay !== "—") costSub = "Recorded provider cost";
    }

    const cacheRead = data.totals.cache_read_input_tokens;
    const cachePercent = data.totals.input_tokens > 0 && cacheRead != null
      ? `${(cacheRead / data.totals.input_tokens * 100).toFixed(1)}% ` : "";
    const fields = [
      ["input_tokens", "Total Input Tokens", "", cacheRead != null ? `${number(cacheRead)} (${cachePercent}cache read)` : "Canonical input", "tile-input", number(data.totals.input_tokens)],
      ["output_tokens", "Total Output Tokens", "", "Canonical output", "tile-output", number(data.totals.output_tokens)],
      ["total_cost_usd", "Total Spend (USD)", "", costSub, "tile-cost", costDisplay]
    ];

    fields.forEach(([field, label, badgeText, subtext, modClass, displayVal]) => {
      const tile = node("div", "", `metrics-tile ${modClass}`);
      const tileHead = node("div", "", "tile-head");
      tileHead.append(node("span", label, "tile-label"));
      if (badgeText) {
        tileHead.append(node("span", badgeText, "tile-badge accent"));
      }
      const value = node("strong", displayVal, "bobi-tnum");
      if (field === "total_cost_usd") value.classList.add("text-green");
      const sub = node("span", subtext, "tile-sub");
      tile.append(tileHead, value, sub);
      tiles.append(tile);
    });
    summary.append(tiles);

    summary.append(telemetryStrip(data), jevValueCard(data.routing || {}));
  }

  function drawTurns() {
    const query = search.value.trim().toLowerCase();
    const visible = currentTurns.filter(turn => !query || [turn.semantic_title, turn.conversation?.semantic_title, turn.conversation?.user_message, turn.conversation?.prompt_snippet, turn.conversation?.response_snippet, turn.session_name, turn.role, turn.session_id, turn.model_selected, turn.fallback_reason].filter(Boolean).join(" ").toLowerCase().includes(query));
    turns.replaceChildren();

    const head = createSectionHead("", "Recent turns", visible.length, "Individual turns, model execution lifecycle, and token consumption");
    const viewSwitch = node("div", "", "metrics-view-switch");
    const btnThreads = node("button", "By Thread", "btn bobi-btn small" + (currentTurnsView === "threads" ? " active" : ""));
    btnThreads.type = "button";
    btnThreads.title = "Group turns by conversation thread & session";
    btnThreads.addEventListener("click", () => {
      currentTurnsView = "threads";
      drawTurns();
    });
    const btnFlat = node("button", "Flat", "btn bobi-btn small" + (currentTurnsView === "flat" ? " active" : ""));
    btnFlat.type = "button";
    btnFlat.title = "Show flat chronological turn list";
    btnFlat.addEventListener("click", () => {
      currentTurnsView = "flat";
      drawTurns();
    });
    viewSwitch.append(btnThreads, btnFlat);
    head.querySelector(".sph-title-row")?.append(viewSwitch);

    turns.append(head);
    turnRows(turns, visible, turn => openAtIndex(currentTurns.indexOf(turn)), name, setSessionFilter, currentTurnsView);
    if (!visible.length) turns.append(node("p", "No recorded turns in this window.", "runs-empty"));
  }

  async function load(reset = false, clearDetail = reset) {
    if (stopped) return;
    if (pending) {
      if (reset) { queued = true; queuedClear ||= clearDetail; }
      return;
    }
    pending = true;
    const revision = modelRevision;
    refreshBtn.disabled = true;
    refreshBtn.classList.add("is-refreshing");
    refreshBtn.setAttribute("aria-busy", "true");

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
    if (!stopped && !queued && revision === modelRevision && results.every(result => result.ok)) {
      results.push(await api(`${base}/sessions?${new URLSearchParams({from: params.get("from"), to: params.get("to")})}`,
        {signal: controller.signal}));
    }

    pending = false;
    if (stopped) return;
    refreshBtn.disabled = false;
    refreshBtn.classList.remove("is-refreshing");
    refreshBtn.setAttribute("aria-busy", "false");

    if (queued) {
      const clear = queuedClear;
      queued = false;
      queuedClear = false;
      if (!modelDebounce) load(true, clear);
      return;
    }
    if (revision !== modelRevision) return;

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
      if (selectedTurnId) {
        activeTurnIndex = currentTurns.findIndex(turn => turn.turn_id === selectedTurnId);
        if (activeTurnIndex >= 0 && !drawerBackdrop.hidden) {
          renderDrawerHead(currentTurns[activeTurnIndex]);
        }
      }

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
  let sandboxController = null;
  let routingController = null;
  async function fetchRoutingConfig() {
    routingController?.abort();
    const request = new AbortController();
    routingController = request;
    currentRoutingConfig = null;
    updateJevHeaderButton();
    const cancel = () => request.abort();
    controller.signal.addEventListener("abort", cancel, {once: true});
    let cancelled;
    const aborted = new Promise(resolve => {
      cancelled = () => resolve(null);
      request.signal.addEventListener("abort", cancelled, {once: true});
    });
    const timeout = setTimeout(cancel, 2500);
    try {
      const res = await Promise.race([api(`${base}/jev-config`, {signal: request.signal}), aborted]);
      if (stopped || request.signal.aborted || routingController !== request) return false;
      if (res?.ok && typeof res.data?.enabled === "boolean" && ["off", "shadow", "enforce"].includes(res.data.mode)) {
        currentRoutingConfig = res.data;
        updateJevHeaderButton();
        return true;
      }
    } catch (e) {
      console.warn("Could not load routing config:", e);
    } finally {
      clearTimeout(timeout);
      controller.signal.removeEventListener("abort", cancel);
      request.signal.removeEventListener("abort", cancelled);
      if (routingController === request) {
        routingController = null;
        updateJevHeaderButton();
      }
    }
    return false;
  }

  function updateJevHeaderButton() {
    const mode = currentRoutingConfig?.mode || "off";
    const isEn = Boolean(currentRoutingConfig?.enabled) && mode !== "off";
    jevHeaderBtn.className = `btn bobi-btn small jev-header-btn ${isEn ? `active mode-${mode}` : "disabled"}`;
    if (!isEn) {
      jevHeaderBtn.textContent = "JEV: Disabled";
      jevHeaderBtn.title = `TypeSafe JEV Dynamic Routing is disabled for ${name}. Click to configure.`;
    } else if (mode === "enforce") {
      jevHeaderBtn.textContent = "JEV: Enforce";
      jevHeaderBtn.title = `TypeSafe JEV is actively enforcing model routing for ${name}. Click to configure.`;
    } else {
      jevHeaderBtn.textContent = "JEV: Shadow";
      jevHeaderBtn.title = `TypeSafe JEV is in shadow observer mode for ${name}. Click to configure.`;
    }
  }

  async function openJevConfigModal() {
    jevModalBody.replaceChildren(node("p", "Loading JEV configuration…", "metrics-note"));
    jevModalBackdrop.hidden = false;
    jevModalBackdrop.classList.add("open");
    jevModalCloseBtn.focus();
    document.addEventListener("keydown", handleJevModalKey);
    const loaded = await fetchRoutingConfig();
    if (stopped || jevModalBackdrop.hidden) return;
    if (!loaded) currentRoutingConfig = null;
    configDirty = false;
    renderJevConfigModalBody();
    setJevModalTab("status");
  }

  function closeJevConfigModal() {
    if (configPending) return;
    if (configDirty && !window.confirm("Discard unsaved JEV configuration changes?")) return;
    configDirty = false;
    routingController?.abort();
    sandboxController?.abort();
    const keyInput = jevModalBody.querySelector("#jev-test-key");
    if (keyInput) { keyInput.type = "password"; keyInput.value = ""; }
    closeConfirmPopup();
    jevModalBackdrop.hidden = true;
    jevModalBackdrop.classList.remove("open");
    document.removeEventListener("keydown", handleJevModalKey);
    jevHeaderBtn.focus();
  }

  function handleJevModalKey(event) {
    if (event.key === "Escape") { event.preventDefault(); if (!jevConfirmOverlay.hidden) closeConfirmPopup(); else closeJevConfigModal(); }
    if (event.key !== "Tab") return;
    const controls = [...(jevConfirmOverlay.hidden ? jevModal : jevConfirmOverlay).querySelectorAll('button:not(:disabled), input, select, textarea, summary')]
      .filter(control => !control.disabled && control.tabIndex >= 0 && control.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }

  function renderJevConfigModalBody() {
    jevModalNavTabs.hidden = !currentRoutingConfig;
    if (!currentRoutingConfig) {
      const unavailable = node("p", "JEV configuration unavailable. Could not load saved settings; close and reopen to retry. No configuration was changed.", "metrics-note");
      unavailable.setAttribute("role", "alert");
      jevModalBody.replaceChildren(unavailable);
      return;
    }
    const cfg = currentRoutingConfig;
    const scopedRoles = [...new Set(["director", "engineer", "curator", ...(cfg.available_roles || []), ...(cfg.roles || [])])];
    const EGRESS_TEXT = {
      redacted: "Redacted task text (secrets and paths removed)",
      none: "Metadata only (size, role, entry point)",
    };
    const brainText = `${cfg.brain && cfg.brain !== "auto" ? cfg.brain : "auto"}; this agent runs ${cfg.agent_brain || "claude"}${cfg.gateway ? " through a gateway" : " natively"}`;

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
                <p class="jev-switch-subtext">Enable or disable this agent's routing policy. Edit mode, models, roles, and instructions in the config editor.</p>
              </div>
            </label>
          </div>

          ${cfg.config_error ? `<p class="jev-config-error" role="alert"><strong>Saved policy is not active:</strong> ${escapeHtml(cfg.config_error)}. Fix it in Configure and save, or reset it.</p>` : ""}
          ${(cfg.config_warnings || []).map(warning => `<p class="jev-config-error" role="alert"><strong>Cannot route this agent:</strong> ${escapeHtml(warning)}.</p>`).join("")}
          <!-- Policy Summary Card -->
          <div class="jev-summary-card">
            <div class="jev-summary-row">
              <span class="jev-summary-label">Current Status</span>
              <span class="jev-summary-val">
                <span class="jev-status-pill ${cfg.enabled ? (cfg.mode === 'enforce' ? 'enforce' : 'shadow') : 'disabled'}">
                  ${cfg.enabled ? (cfg.mode === 'enforce' ? 'Enforce Active' : 'Shadow Active') : 'Disabled'}
                </span>
              </span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Control / Fallback Model</span>
              <span class="jev-summary-val font-mono">${escapeHtml(cfg.control_model || "Not set")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Candidate Models</span>
              <span class="jev-summary-val font-mono">${escapeHtml((cfg.candidate_models || []).join(", ") || "—")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Brain</span>
              <span class="jev-summary-val">${escapeHtml(brainText)}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Prompt to JEV</span>
              <span class="jev-summary-val">${escapeHtml(EGRESS_TEXT[cfg.prompt_egress] || "—")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Scoped Roles</span>
              <span class="jev-summary-val">${escapeHtml((cfg.roles || []).join(", ") || "—")}</span>
            </div>
            <div class="jev-summary-row">
              <span class="jev-summary-label">Config Location</span>
              <span class="jev-summary-val font-mono text-muted">run/.env</span>
            </div>
          </div>

          <!-- Raw JSON Inspector / Preview -->
          <div class="jev-form-group">
            <div class="jev-code-head">
              <label class="jev-field-label">Active JEV Policy Configuration (<code>BOBI_METRICS_EXPERIMENT_JSON</code>)</label>
              <div class="jev-code-head-actions">
                <button type="button" class="btn bobi-btn quiet jev-btn-jump-to-guide">Edit configuration →</button>
                <button type="button" class="btn bobi-btn quiet jev-btn-copy-json" title="Copy raw JSON configuration">Copy JSON</button>
              </div>
            </div>
            <pre class="jev-code-preview"><code>${escapeHtml(cfg.raw_json || '{\n  "note": "No BOBI_METRICS_EXPERIMENT_JSON found in run/.env"\n}')}</code></pre>
            <p class="jev-form-hint">Use the config editor to update this policy. Existing advanced policy fields are preserved when saving.</p>
          </div>

          <div class="jev-restart-notice">
            <span><strong>Note on applying changes:</strong> Persistent sessions (such as the active <code>director</code>) keep their model throughout execution. After saving, restart the agent to re-initialize active sessions under the new policy.</span>
          </div>
        </div>

        <div class="jev-tab-pane pane-guide" id="jev-pane-guide" hidden>
          <h4>Configure routing</h4>
          <p class="jev-form-hint">Saved to agent environment (<code>run/.env</code>). Test Routing uses it at once; restart the agent to apply it to production sessions.</p>
          <div class="jev-editor-grid">
            <div class="jev-form-group">
              <label class="jev-field-label" for="jev-config-mode">Routing mode</label>
              <select id="jev-config-mode" aria-describedby="jev-mode-hint"><option value="off">Disabled</option><option value="shadow">Shadow</option><option value="enforce">Enforce</option></select>
              <p class="jev-form-hint" id="jev-mode-hint"></p>
            </div>
            <div class="jev-form-group">
              <label class="jev-field-label" for="jev-config-control">Control / fallback model</label>
              <select id="jev-config-control" aria-describedby="jev-control-hint"></select>
              <p class="jev-form-hint" id="jev-control-hint">Runs when the policy is unsure, slow or unreachable. Pick from the candidates below.</p>
            </div>
          </div>
          <div class="jev-form-group">
            <span class="jev-field-label" id="jev-candidates-label">Candidate models</span>
            <div class="jev-candidate-editor">
              <div class="jev-candidate-tags" role="list" aria-labelledby="jev-candidates-label"></div>
              <input id="jev-model-filter" type="search" class="jev-model-filter" aria-controls="jev-model-options" autocomplete="off"
                placeholder="Filter ${escapeHtml(cfg.agent_brain || "claude")} models" aria-label="Filter ${escapeHtml(cfg.agent_brain || "claude")} models">
              <div id="jev-model-options" class="jev-model-options" role="listbox" aria-multiselectable="true" aria-label="${escapeHtml(cfg.agent_brain || "claude")} models"></div>
              <p class="jev-model-empty jev-form-hint" hidden>No ${escapeHtml(cfg.agent_brain || "claude")} model matches.</p>
              <details class="jev-custom-model" ${(cfg.candidate_models || []).some(model => !(cfg.native_models || []).includes(model)) ? "open" : ""}>
                <summary>+ Add custom gateway model</summary>
                <div class="jev-candidate-entry"><input id="jev-config-candidates" maxlength="8192" aria-describedby="jev-candidate-hint" placeholder="e.g. cx/gpt-6.1-sol, ds/deepseek-flash" autocomplete="off"><button type="button" id="jev-config-candidate-add" class="btn bobi-btn quiet">Add</button></div>
                <p class="jev-form-hint" id="jev-candidate-hint">${cfg.gateway
                  ? "This agent dials a gateway, so any routing id it serves can run. A dashed tag has no list price; the dashboard cannot measure its savings."
                  : `This agent calls ${cfg.agent_brain === "codex" ? "OpenAI" : "Anthropic"} directly. Custom ids need <code>BOBI_GATEWAY_BASE_URL</code> in run/.env; without it, saving rejects them.`}</p>
              </details>
            </div>
            <p class="jev-form-hint">Click models this agent's ${escapeHtml(cfg.agent_brain || "claude")} brain can run natively.</p>
          </div>
          <fieldset class="jev-form-group jev-editor-roles">
            <legend class="jev-field-label">Scoped roles</legend>
            <div class="jev-role-options">${scopedRoles.map(role => `<label class="jev-role-option"><input type="checkbox" name="jev-role" value="${escapeHtml(role)}" ${(cfg.roles || []).includes(role) ? "checked" : ""}><span>${escapeHtml(role)}</span></label>`).join("")}</div>
          </fieldset>
          <div class="jev-form-group">
            <label class="jev-field-label" for="jev-config-instructions">Routing instructions</label>
            <textarea id="jev-config-instructions" rows="5" maxlength="32768" placeholder="Explain when to choose each candidate model">${escapeHtml(cfg.instructions || "")}</textarea>
          </div>
          <div class="jev-form-group">
            <label class="jev-field-label" for="jev-config-egress">Prompt sent to JEV</label>
            <select id="jev-config-egress" aria-describedby="jev-egress-hint">
              <option value="redacted">Redacted task text (recommended)</option>
              <option value="none">Metadata only (strict privacy)</option>
            </select>
            <p class="jev-form-hint" id="jev-egress-hint"></p>
          </div>
          <details class="jev-editor-advanced">
            <summary>Advanced policy fields</summary>
            <div class="jev-editor-grid">
              <div class="jev-form-group"><label class="jev-field-label" for="jev-config-confidence">Minimum confidence</label><input id="jev-config-confidence" type="number" min="0" max="1" step="0.01" value="${escapeHtml(String(cfg.min_confidence ?? 0.85))}"></div>
            </div>
            <p class="jev-form-hint">Per-model criteria, the experiment id and other existing advanced fields stay as saved.</p>
          </details>
          <div class="jev-editor-tools">
            <button type="button" id="jev-config-delete" class="btn bobi-btn quiet">Reset / Delete</button>
          </div>
        </div>
        <div class="jev-tab-pane" id="jev-pane-sandbox" hidden>
          <div class="jev-sandbox-intro">
            <h4>Test a routing decision</h4>
            <p class="jev-form-hint">Try the saved policy without creating sessions or telemetry. ${cfg.prompt_egress === "redacted"
              ? "Like production, TypeSafe receives your test prompt with secrets and paths redacted."
              : "Like production, TypeSafe receives only metadata (prompt size, role, entry point), never the prompt text."} Vendor charges may apply.</p>
          </div>
          <div class="jev-sandbox-grid">
            <div class="jev-form-group">
              <label class="jev-field-label" for="jev-test-key">TYPESAFE_API_KEY</label>
              <div class="jev-sandbox-key">
                <input id="jev-test-key" type="password" autocomplete="off" spellcheck="false" placeholder="Use configured key if blank">
                <button type="button" class="btn bobi-btn quiet jev-test-show-key" aria-controls="jev-test-key" aria-pressed="false">Show key</button>
              </div>
            </div>
            <div class="jev-form-group">
              <label class="jev-field-label" for="jev-test-role">Role</label>
              <select id="jev-test-role">${scopedRoles.map(role => `<option ${role === ((cfg.roles || []).includes("engineer") ? "engineer" : (cfg.roles || [])[0]) ? "selected" : ""}>${escapeHtml(role)}</option>`).join("")}</select>
            </div>
            <div class="jev-form-group">
              <label class="jev-field-label" for="jev-test-entry">Entry Point</label>
              <select id="jev-test-entry">${[...new Set(["session_start", ...(cfg.entry_points || []), "subagent_phase"])].map(entry => `<option ${entry === "session_start" ? "selected" : ""}>${escapeHtml(entry)}</option>`).join("")}</select>
            </div>
            <p class="jev-form-hint jev-sandbox-credentials">The key is used for this request only and cleared on close.</p>
          </div>
          <div class="jev-form-group">
            <label class="jev-field-label" for="jev-test-prompt">User Prompt / Task</label>
            <textarea id="jev-test-prompt" rows="5" maxlength="65536" placeholder="Write a distributed Raft consensus algorithm"></textarea>
          </div>
          <div class="jev-test-actions">
            <button type="button" class="btn bobi-btn primary jev-test-run">Simulate JEV Routing</button>
            <button type="button" class="btn bobi-btn quiet jev-test-abort" hidden>Cancel request</button>
          </div>
          <p class="jev-test-status jev-form-hint" role="status" aria-live="polite"></p>
          <div class="jev-test-results" hidden></div>
        </div>
        </div>

        <!-- Pinned Form Actions Bar -->
        <div class="jev-modal-footer">
          <span class="jev-save-msg" role="status"></span>
          <div class="jev-actions-right">
            <button type="button" class="btn bobi-btn quiet jev-btn-cancel">Cancel</button>
            <button type="submit" id="jev-config-save" class="btn bobi-btn primary jev-btn-save">Save and Apply</button>
          </div>
        </div>
      </form>
    `;

    const form = jevModalBody.querySelector(".jev-config-form");
    form.noValidate = true;
    const modeInput = form.querySelector("#jev-config-mode");
    const enabledInput = form.querySelector("#jev-input-enabled");
    const egressInput = form.querySelector("#jev-config-egress");
    const egressHint = form.querySelector("#jev-egress-hint");
    const EGRESS_HINTS = {
      redacted: "JEV reads the task with secrets, credentials and local paths removed, so it can judge difficulty. Test Routing sends the same.",
      none: "JEV sees only prompt size, role and entry point, never the task text. Routing is less accurate. Test Routing sends the same.",
    };
    egressInput.value = cfg.prompt_egress === "none" ? "none" : "redacted";
    const syncEgress = () => { egressHint.textContent = EGRESS_HINTS[egressInput.value]; };
    syncEgress();
    egressInput.addEventListener("change", syncEgress);
    const modeHint = form.querySelector("#jev-mode-hint");
    const MODE_HINTS = {
      off: "No policy calls. Every turn runs its configured model.",
      shadow: "The policy recommends a model and the dashboard records it, but turns keep the control model.",
      enforce: "Turns run the recommended model; low-confidence or failed calls fall back to the control model.",
    };
    const syncMode = () => { modeHint.textContent = MODE_HINTS[modeInput.value]; };
    modeInput.value = cfg.enabled ? (cfg.mode === "enforce" ? "enforce" : "shadow") : "off";
    syncMode();
    modeInput.addEventListener("change", () => { enabledInput.checked = modeInput.value !== "off"; syncMode(); });
    enabledInput.addEventListener("change", () => {
      const restored = cfg.mode === "enforce" || cfg.configured_mode === "enforce" ? "enforce" : "shadow";
      modeInput.value = enabledInput.checked ? restored : "off";
      syncMode();
    });
    let candidateModels = [...new Set([...(cfg.candidate_models || []), ...(cfg.control_model ? [cfg.control_model] : [])])];
    let controlModel = cfg.control_model || candidateModels[0] || "";
    const candidateInput = form.querySelector("#jev-config-candidates");
    const candidateTags = form.querySelector(".jev-candidate-tags");
    const candidateAdd = form.querySelector("#jev-config-candidate-add");
    const controlInput = form.querySelector("#jev-config-control");
    const unpriced = new Set(cfg.unpriced_models || []);
    const known = new Set([...(cfg.priced_models || []), ...(cfg.native_models || [])]);
    const modelFilter = form.querySelector("#jev-model-filter");
    const modelOptions = form.querySelector("#jev-model-options");
    const modelEmpty = form.querySelector(".jev-model-empty");
    function drawOptions() {
      const query = modelFilter.value.trim().toLowerCase();
      const visible = (cfg.native_models || []).filter(model => model.toLowerCase().includes(query));
      modelOptions.replaceChildren(...visible.map(model => {
        const option = node("button", "", "jev-model-option");
        option.type = "button";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(candidateModels.includes(model)));
        option.dataset.model = model;
        option.append(node("code", model));
        option.addEventListener("click", () => {
          if (configPending) return;
          candidateModels = candidateModels.includes(model)
            ? candidateModels.filter(value => value !== model) : [...candidateModels, model];
          configDirty = true;
          drawCandidates();
          modelOptions.querySelector(`[data-model="${CSS.escape(model)}"]`)?.focus();
        });
        return option;
      }));
      modelEmpty.hidden = visible.length > 0;
    }
    modelFilter.addEventListener("input", drawOptions);
    modelFilter.addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      modelOptions.querySelector(".jev-model-option")?.click();
    });
    controlInput.addEventListener("change", () => { controlModel = controlInput.value; drawCandidates(); });
    function drawControl() {
      if (!candidateModels.includes(controlModel)) controlModel = candidateModels[0] || "";
      controlInput.replaceChildren(...(candidateModels.length
        ? candidateModels.map(model => { const option = node("option", model); option.value = model; return option; })
        : [Object.assign(node("option", "Add a candidate first"), {value: ""})]));
      controlInput.value = controlModel;
    }
    function drawCandidates() {
      drawControl();
      drawOptions();
      candidateTags.replaceChildren();
      candidateModels.forEach(model => {
        const unlisted = unpriced.has(model) || (!known.has(model) && !(cfg.candidate_models || []).includes(model) && model !== cfg.control_model);
        const tag = node("span", "", `jev-candidate-tag${unlisted ? " is-unlisted" : ""}`);
        if (unlisted) tag.title = `${model} has no list price; check the spelling. The dashboard cannot price its turns.`;
        if (model === controlModel) tag.append(node("span", "control", "jev-candidate-role"));
        tag.setAttribute("role", "listitem");
        const remove = node("button", "", "jev-candidate-remove");
        remove.type = "button";
        remove.title = `Remove ${model}`;
        remove.setAttribute("aria-label", `Remove ${model}`);
        remove.append(icon("clear"));
        remove.addEventListener("click", () => {
          if (configPending) return;
          candidateModels = candidateModels.filter(value => value !== model);
          configDirty = true;
          drawCandidates();
          modelFilter.focus();
        });
        tag.append(node("code", model), remove);
        candidateTags.append(tag);
      });
    }
    function addCandidates() {
      const values = candidateInput.value.split(/[\n,]+/).map(value => value.trim()).filter(Boolean);
      if (!values.length) return;
      candidateModels = [...new Set([...candidateModels, ...values])];
      candidateInput.value = "";
      configDirty = true;
      drawCandidates();
      candidateInput.focus();
    }
    candidateAdd.addEventListener("click", addCandidates);
    candidateInput.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === ",") { event.preventDefault(); addCandidates(); }
    });
    drawCandidates();
    form.addEventListener("input", event => {
      if (event.target.closest("#jev-pane-sandbox")) return;
      configDirty = true;
    });
    form.querySelectorAll(".jev-tab-pane").forEach(pane => {
      pane.setAttribute("role", "tabpanel");
      pane.setAttribute("aria-labelledby", pane.id.replace("pane", "tab"));
    });
    const keyInput = form.querySelector("#jev-test-key");
    const showKey = form.querySelector(".jev-test-show-key");
    showKey.addEventListener("click", () => {
      const show = keyInput.type === "password";
      keyInput.type = show ? "text" : "password";
      showKey.textContent = show ? "Hide key" : "Show key";
      showKey.setAttribute("aria-pressed", String(show));
    });
    const simulate = form.querySelector(".jev-test-run");
    const abort = form.querySelector(".jev-test-abort");
    const testStatus = form.querySelector(".jev-test-status");
    const results = form.querySelector(".jev-test-results");
    abort.addEventListener("click", () => {
      sandboxController?.abort();
      testStatus.textContent = "Request cancelled. You can run another simulation.";
    });
    simulate.addEventListener("click", async () => {
      const prompt = form.querySelector("#jev-test-prompt").value;
      results.hidden = true;
      results.replaceChildren();
      if (!prompt.trim()) { testStatus.textContent = "Enter a test prompt first."; return; }
      sandboxController?.abort();
      const requestController = new AbortController();
      sandboxController = requestController;
      simulate.disabled = true;
      abort.hidden = false;
      results.setAttribute("aria-busy", "true");
      simulate.textContent = "Simulating…";
      testStatus.textContent = "Calling TypeSafe JEV…";
      try {
        const response = await api(`/api/agents/${encodeURIComponent(name)}/metrics/test-route`, {
          method: "POST", signal: requestController.signal,
          body: JSON.stringify({prompt, api_key: keyInput.value,
            features: {role: form.querySelector("#jev-test-role").value,
              entry_point: form.querySelector("#jev-test-entry").value}}),
        });
        if (requestController.signal.aborted) return;
        if (!response.ok) {
          testStatus.textContent = `${response.data?.error || "Simulation failed"}${response.data?.code ? ` (${response.data.code})` : ""}`;
          return;
        }
        const data = response.data;
        const banner = node("div", "", "jev-decision-banner");
        banner.append(node("span", "JEV recommendation", "jev-field-label"), node("strong", data.selected_model));
        results.append(banner);
        function probabilityMeter(label, value, className) {
          const percent = Math.round(Math.min(1, Math.max(0, Number(value) || 0)) * 100);
          const row = node("div", "", className);
          const heading = node("div", "", "jev-meter-heading");
          heading.append(node("span", label), node("strong", `${percent}%`, "bobi-tnum"));
          const meter = node("div", "", "jev-meter-track");
          meter.setAttribute("role", "meter");
          meter.setAttribute("aria-label", label);
          meter.setAttribute("aria-valuemin", "0");
          meter.setAttribute("aria-valuemax", "100");
          meter.setAttribute("aria-valuenow", String(percent));
          meter.setAttribute("aria-valuetext", `${percent}% ${label}`);
          const fill = node("span", "", "jev-meter-fill");
          fill.style.width = `${percent}%`;
          meter.append(fill);
          row.append(heading, meter);
          return row;
        }
        banner.append(probabilityMeter("Confidence", data.confidence,
          `jev-confidence-meter${data.fallback_reason ? " low-confidence" : ""}`));
        const distribution = node("div", "", "jev-probabilities");
        distribution.append(node("h4", "Model probabilities"));
        for (const [model, probability] of Object.entries(data.probabilities)) {
          distribution.append(probabilityMeter(model, probability,
            `jev-probability-row${model === data.selected_model ? " is-selected" : ""}`));
        }
        results.append(distribution);
        const drift = data.fallback_reason === "policy_version_drift" ? ` (answered ${data.model_version}, pinned ${data.pinned_version})` : "";
        results.append(node("p", `Policy preview (${data.mode}): ${data.effective_model}${data.fallback_reason ? ` · fallback to the control model: ${fallbackText(data.fallback_reason)}${drift}` : ""}`, "jev-form-hint"));
        const wire = node("details", "", "jev-wire-inspector");
        wire.append(node("summary", "Context & Wire Inspector"));
        for (const [title, value] of [["Extracted features", data.features], ["Outgoing Payload", data.outgoing_payload], ["Raw JEV Response", data.raw_response]]) {
          wire.append(node("h4", title), node("pre", JSON.stringify(value, null, 2), "jev-code-preview"));
        }
        results.append(wire); results.hidden = false;
        testStatus.textContent = `Completed in ${Math.round(data.latency_ms)} ms. No metrics recorded.`;
      } catch (error) {
        if (!requestController.signal.aborted) testStatus.textContent = "Network error. Simulation did not complete.";
      } finally {
        simulate.disabled = false;
        simulate.textContent = "Simulate JEV Routing";
        if (document.activeElement === abort) simulate.focus();
        abort.hidden = true;
        results.setAttribute("aria-busy", "false");
      }
    });
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
      copyBtn.addEventListener("click", async () => {
        const textToCopy = cfg.raw_json || "";
        if (!textToCopy) return;
        try {
          await navigator.clipboard.writeText(textToCopy);
          copyBtn.textContent = "copied";
          setTimeout(() => { copyBtn.textContent = "Copy JSON"; }, 1500);
        } catch { copyBtn.textContent = "Copy unavailable"; }
      });
    }

    const saveMsg = form.querySelector(".jev-save-msg");
    const saveBtn = form.querySelector(".jev-btn-save");
    const deleteBtn = form.querySelector("#jev-config-delete");
    const confirmCancelBtn = jevConfirmOverlay.querySelector(".jev-confirm-btn-cancel");
    const confirmDeleteBtn = jevConfirmOverlay.querySelector(".jev-confirm-btn-restart");
    const confirmStatusMsg = jevConfirmOverlay.querySelector(".jev-confirm-status-msg");
    const editorControls = [...form.querySelectorAll("#jev-pane-guide input, #jev-pane-guide select, #jev-pane-guide textarea, #jev-pane-guide button"), enabledInput, modeInput, saveBtn, deleteBtn];

    function draftConfig(validate = true) {
      const mode = modeInput.value;
      addCandidates();
      const controlModel = controlInput.value;
      const candidates = [...candidateModels];
      const roles = [...form.querySelectorAll('input[name="jev-role"]:checked')].map(input => input.value);
      const instructions = form.querySelector("#jev-config-instructions").value.trim();
      const confidenceInput = form.querySelector("#jev-config-confidence");
      if (validate && mode !== "off") {
        const invalid = !candidates.length ? ["Add at least one candidate model.", "#jev-model-filter"]
          : !controlModel ? ["Choose a control model.", "#jev-config-control"]
          : !roles.length ? ["Select at least one scoped role.", 'input[name="jev-role"]']
          : !instructions ? ["Enter routing instructions.", "#jev-config-instructions"]
          : !confidenceInput.value || !confidenceInput.checkValidity() ? ["Minimum confidence must be between 0 and 1.", "#jev-config-confidence"] : null;
        if (invalid) {
          saveMsg.textContent = invalid[0];
          saveMsg.className = "jev-save-msg text-red";
          setJevModalTab("guide");
          const field = form.querySelector(invalid[1]);
          field.closest("details")?.setAttribute("open", "");
          field.focus();
          return null;
        }
      }
      return {mode, control_model: controlModel, candidate_models: candidates, roles, instructions,
        min_confidence: Number(confidenceInput.value), prompt_egress: egressInput.value};
    }

    deleteBtn.addEventListener("click", openConfirmPopup);
    confirmCancelBtn.onclick = closeConfirmPopup;
    confirmDeleteBtn.onclick = () => updateConfig("DELETE");
    async function updateConfig(method) {
      if (configPending || !currentRoutingConfig) return;
      const draft = method === "POST" ? draftConfig() : null;
      if (method === "POST" && !draft) return;
      configPending = true;
      sandboxController?.abort();
      editorControls.forEach(control => { control.disabled = true; });
      confirmDeleteBtn.disabled = true;
      confirmCancelBtn.disabled = true;
      saveMsg.textContent = method === "DELETE" ? "Removing saved JEV configuration…" : "Saving JEV configuration…";
      saveMsg.className = "jev-save-msg";
      confirmStatusMsg.textContent = saveMsg.textContent;
      try {
        const response = await api(base + "/jev-config", {
          method, signal: controller.signal,
          ...(draft ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(draft.mode === "off" ? {mode: "off"} : draft)} : {}),
        });
        if (!response.ok) throw new Error((response.data?.error || "Could not update configuration.") + (response.data?.code ? " (" + response.data.code + ")" : ""));
        if (stopped) return;
        currentRoutingConfig = response.data;
        updateJevHeaderButton();
        configDirty = false;
        configPending = false;
        closeConfirmPopup();
        const activeTab = jevModalNavTabs.querySelector('[aria-selected="true"]').dataset.jevTab;
        renderJevConfigModalBody();
        setJevModalTab(activeTab);
        const status = jevModalBody.querySelector(".jev-save-msg");
        status.className = "jev-save-msg text-green";
        status.textContent = (method === "DELETE" ? "Configuration deleted; JEV disabled." : "Saved. Policy is available to Sandbox.")
          + (response.data.restart_required !== false ? " Restart agent to apply to production; existing sessions retain their model." : " Configuration applied.");
      } catch (error) {
        if (stopped) return;
        saveMsg.textContent = error.message || "Network error updating configuration.";
        saveMsg.className = "jev-save-msg text-red";
        confirmStatusMsg.textContent = saveMsg.textContent;
        confirmStatusMsg.className = "jev-confirm-status-msg text-red";
      } finally {
        configPending = false;
        editorControls.forEach(control => { control.disabled = false; });
        confirmDeleteBtn.disabled = false;
        confirmCancelBtn.disabled = false;
      }
    }

    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!form.querySelector("#jev-pane-sandbox").hidden) { if (!simulate.disabled) simulate.click(); return; }
      updateConfig("POST");
    });
  }

  fetchRoutingConfig();
  load(true);

  return () => {
    stopped = true;
    controller.abort();
    detailController?.abort();
    sandboxController?.abort();
    routingController?.abort();
    clearTimeout(timer);
    clearTimeout(modelDebounce);
    document.removeEventListener("keydown", handleDrawerKey);
    document.removeEventListener("keydown", handleJevModalKey);
  };
}
