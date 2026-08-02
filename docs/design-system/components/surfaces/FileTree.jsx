import React from "react";

// The agent workspace panel: a rounded paper card listing the agent's real files.
// The current row gets a tan wash; badges call out generated/enforced/secret files.
export function FileTree({ root = "my-agent/", status, statusAccent = false, rows = [], current, onSelect, className = "", style }) {
  return (
    <div className={className} style={{ border: "1px solid rgba(54,46,37,0.14)", borderRadius: "var(--radius-xl)", background: "var(--bobi-paper)", boxShadow: "var(--shadow-panel)", padding: 12, fontFamily: "var(--font-mono)", ...style }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 10px 12px" }}>
        <span style={{ fontSize: 13, fontWeight: 500, color: "var(--bobi-ink)" }}>{root}</span>
        {status && <span style={{ fontFamily: "var(--font-sans)", fontSize: 12, color: statusAccent ? "var(--bobi-acc)" : "var(--text-secondary)" }}>{status}</span>}
      </div>
      {rows.map((r) => {
        const cur = r.label === current;
        return (
          <div key={r.label} onClick={onSelect ? () => onSelect(r.label) : undefined}
            role={onSelect ? "button" : undefined} tabIndex={onSelect ? 0 : undefined}
            onKeyDown={onSelect ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(r.label); } } : undefined}
            data-current={cur ? "true" : "false"}
            className="bobi-file-row"
            style={{ display: "flex", alignItems: "center", gap: 8, minHeight: 34, padding: "6px 10px 6px 24px", borderRadius: "var(--radius-sm)", fontSize: 12.5, color: "var(--bobi-ink)", cursor: onSelect ? "pointer" : "default" }}>
            <span style={{ minWidth: 0, wordBreak: "break-word" }}>{r.label}</span>
            {r.badge && <span style={{ marginLeft: "auto", flexShrink: 0, fontFamily: "var(--font-sans)", fontSize: 11.5, color: r.badgeAccent ? "var(--bobi-acc)" : "var(--text-secondary)" }}>{r.badge}</span>}
          </div>
        );
      })}
    </div>
  );
}
