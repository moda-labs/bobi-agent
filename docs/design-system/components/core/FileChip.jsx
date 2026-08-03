import React from "react";

// A bordered mono filename chip — Bobi's way of naming a file inline, in a
// heading, or in a table cell. Sized relative to surrounding text in headings.
export function FileChip({ children, inHeading = false, accent = false, className = "", style }) {
  return (
    <span className={className} style={{
      display: "inline-flex", alignItems: "baseline", position: "relative",
      margin: "0 2px", padding: "0 8px 1px",
      border: `1px solid ${accent ? "var(--border-gate)" : "var(--border-strong)"}`,
      borderRadius: "var(--radius-sm)", background: "var(--bobi-paper)",
      fontFamily: "var(--font-mono)", fontWeight: 400, letterSpacing: 0,
      color: accent ? "var(--bobi-acc)" : "var(--bobi-ink)", whiteSpace: "nowrap",
      top: inHeading ? -1 : 0, fontSize: inHeading ? "0.62em" : "12.5px", ...style,
    }}>{children}</span>
  );
}
