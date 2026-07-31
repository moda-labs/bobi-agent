import React from "react";

// A row in the event queue. Hover/selected/focus states live in tokens/app.css
// (.bobi-row) — a table row that doesn't respond to the cursor reads broken.
// Rows are real buttons for the keyboard: tabbable, Enter/Space activate.
//
// Column model: source glyph · event (grows) · agent · workflow · state · time.
// The event title is the ONLY cell allowed to truncate. Agent and workflow are
// sized to their real content (agent names and workflow filenames are short and
// known) — three ellipses in one row means the column model is wrong, not the
// content.
const GRID = "20px minmax(120px,1fr) 82px 140px 74px 60px 16px";

export function EventRow({ icon, source, title, agent, workflow, status, time, selected = false, onClick, className = "", style }) {
  const activate = (e) => {
    if (!onClick) return;
    if (e.type === "click" || e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); }
  };
  return (
    <div
      role={onClick ? "button" : "row"}
      tabIndex={onClick ? 0 : undefined}
      data-selected={selected ? "true" : "false"}
      onClick={activate}
      onKeyDown={activate}
      className={`bobi-row ${className}`}
      style={{
        display: "grid", gridTemplateColumns: GRID, alignItems: "center", gap: 12,
        minHeight: 52, padding: "8px 14px", borderTop: "1px solid var(--border-hairline)",
        cursor: onClick ? "pointer" : "default", ...style,
      }}
    >
      <span aria-hidden="true" style={{ display: "flex", justifyContent: "center", color: "var(--text-secondary)" }}>{icon}</span>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: 500, letterSpacing: "-0.006em", color: "var(--bobi-ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
        {source && <span style={{ display: "block", marginTop: 1, fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{source}</span>}
      </span>
      <span style={{ fontFamily: "var(--font-sans)", fontSize: 13, color: agent ? "var(--bobi-ink)" : "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent || "—"}</span>
      <span title={typeof workflow === "string" ? workflow : undefined} style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{workflow || "—"}</span>
      <span>{status}</span>
      <span className="bobi-tnum" style={{ fontFamily: "var(--font-sans)", fontSize: 12.5, color: "var(--text-secondary)", textAlign: "right" }}>{time}</span>
      <span className="bobi-row-go" aria-hidden="true" style={{ display: "flex", justifyContent: "flex-end", color: "var(--text-secondary)" }}>
        <svg viewBox="0 0 20 20" width="14" height="14" fill="none"><path d="M8 6l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </span>
    </div>
  );
}

// Column header. Sentence-case sans at 11px — one uppercase treatment per app
// (the page title), so headers stay quiet.
export function EventRowHeader({ columns = ["", "Event", "Agent", "Workflow", "State", "Time", ""], className = "", style }) {
  return (
    <div role="row" className={className} style={{
      display: "grid", gridTemplateColumns: GRID, alignItems: "center", gap: 12,
      padding: "9px 14px", background: "var(--surface-inset)",
      borderBottom: "1px solid var(--border-hairline)",
      fontFamily: "var(--font-sans)", fontSize: 11.5, fontWeight: 500,
      letterSpacing: "0.01em", color: "var(--text-secondary)", ...style,
    }}>
      {columns.map((c, i) => <span key={i} style={{ textAlign: i === 5 ? "right" : "left" }}>{c}</span>)}
    </div>
  );
}
