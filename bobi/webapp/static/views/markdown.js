// Tiny, safe markdown for agent replies. No CDN, no deps.
//
// Everything is HTML-escaped FIRST, then a fixed set of inline/block transforms
// runs on the escaped text — so agent output can never inject markup.
//
// Extracted from the agent view (Q025): the renderer is the security-critical
// ~60 lines of a 900-line view file, and it was unreachable from any test while
// it lived inside that closure. It is a module so the escaping can be proven
// rather than reviewed.

// D008 — quotes MUST be escaped, not just the angle brackets.
//
// mdInline drops a URL into a double-quoted href attribute. Escaping only
// & < > leaves `"` intact, and the link regex's URL group excludes only `)` and
// whitespace — so agent output (drivable by prompt injection from any page,
// email, or tool result the agent reads) containing
//   [x](https://a"onmouseover="location=document.cookie//)
// closes the href early and renders a live event-handler attribute. Browsers
// accept an attribute with no separating whitespace after a quoted value, the
// payload needs no space, paren, or `)`, and the dashboard ships no CSP — so it
// executes in the operator's loopback origin on hover. Escaping the quote here,
// before any transform runs, closes it for every attribute sink at once.
export const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                           .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
                           .replace(/'/g, "&#39;");

export function mdInline(t) {
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

export function renderMarkdown(src) {
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
