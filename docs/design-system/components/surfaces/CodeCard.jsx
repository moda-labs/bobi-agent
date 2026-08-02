import React from "react";

// A titled config card: uppercase mono header with a tag on the right, then a
// mono body. This is how Bobi shows YAML, markdown, and config of any kind.
export function CodeCard({ title, tag, tagAccent = false, rounded = false, children, className = "", style }) {
  return (
    <div className={className} style={{ border: "1px solid var(--border-card)", background: "var(--bobi-paper)", borderRadius: rounded ? "var(--radius-md)" : 0, overflow: "hidden", boxShadow: rounded ? "var(--shadow-card)" : "none", ...style }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "10px 16px", borderBottom: "1px solid var(--border-card)", fontFamily: "var(--font-mono)", fontSize: "var(--text-plate)", letterSpacing: "var(--track-label)", textTransform: "uppercase", color: "var(--text-secondary)" }}>
        <span>{title}</span>
        {tag && <span style={{ fontSize: "var(--text-plate-sm)", color: tagAccent ? "var(--bobi-acc)" : "var(--bobi-clay)" }}>{tag}</span>}
      </div>
      <pre style={{ margin: 0, padding: "16px", overflowX: "auto", fontFamily: "var(--font-mono)", fontSize: 13, lineHeight: 1.7, color: "var(--bobi-ink)" }}>{children}</pre>
    </div>
  );
}

// YAML/markdown token spans — the four-color scheme used throughout Bobi.
export function Tok({ kind = "v", children }) {
  const c = { k: "var(--bobi-clay)", v: "var(--bobi-ink)", c: "var(--text-secondary)", a: "var(--bobi-acc)" }[kind];
  return <span style={{ color: c }}>{children}</span>;
}

// A gated/enforced line: violet left border and a 5% violet wash, bleeding to
// the padding edges. The single most important visual claim Bobi makes.
export function GateLine({ children }) {
  return (
    <span style={{ display: "block", margin: "0 -16px", padding: "0 16px 0 14px", borderLeft: "2px solid var(--bobi-acc)", background: "var(--bobi-acc-wash)" }}>{children}</span>
  );
}
