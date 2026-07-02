/* Agent view — one installed agent: subagent roster (left) + blocking chat
   (right). Ported from the standalone agentui SPA; same behavior, routed
   under #/agents/<name> with team-scoped endpoints. */

export function mountAgent(el, { api, name }) {
  const base = "/api/agents/" + encodeURIComponent(name);

  el.innerHTML = "";
  const shell = document.createElement("div");
  shell.className = "shell";
  shell.innerHTML = `
    <aside class="sidebar">
      <div class="side-head mono"><a class="side-back" href="#/">&larr; agents</a> · subagents</div>
      <div class="cards" data-el="cards"></div>
      <p class="empty" data-el="empty" hidden>No active subagents. Start the
        agent from the dashboard, then launch sub-agents.</p>
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
        <form class="composer" data-el="composer">
          <textarea data-el="input" rows="1" placeholder="Message this agent…"
                    autocomplete="off"></textarea>
          <button data-el="send" type="submit">Send</button>
        </form>
      </div>
    </section>`;
  el.appendChild(shell);

  const els = {};
  for (const n of shell.querySelectorAll("[data-el]")) {
    els[n.dataset.el] = n;
  }

  // name -> [{who, text, error, pending}] so a chat survives roster refreshes.
  const history = new Map();
  let selected = null;
  let lastAgents = [];
  let sending = false;
  let messagesLoading = false;

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
    renderTranscript();
    els.input.focus();
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
    const a = lastAgents.find((x) => x.name === selected) || { name: selected };
    els.chatName.textContent = a.name;
    els.chatRole.textContent = a.is_manager ? "manager" : (a.role || "agent");
    els.chatStatus.textContent = a.status || "";
    els.chatStatus.hidden = !a.status;
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
    }
  }

  function pollMessages() {
    if (selected) loadMessages(selected);
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
  const t1 = setInterval(poll, 3000);
  const t2 = setInterval(pollMessages, 3000);
  return () => { clearInterval(t1); clearInterval(t2); };
}
