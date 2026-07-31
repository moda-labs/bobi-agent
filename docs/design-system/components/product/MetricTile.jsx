import React from "react";

// One number and its label. Sentence-case sans label (mono is for data), and the
// label reserves two lines so a row of tiles shares one value baseline whatever
// labels the caller passes.
export function MetricTile({ label, value, unit, delta, deltaTone = "muted", accent = false, className = "", style }) {
  const dc = { up: "var(--bobi-acc)", down: "var(--status-failed)", muted: "var(--text-secondary)" }[deltaTone];
  return (
    <div className={className} style={{
      background: "var(--surface-card)", border: "1px solid var(--border-card)",
      borderRadius: "var(--radius-lg)", boxShadow: "var(--elev-1)", padding: "14px 16px 16px", ...style,
    }}>
      <span style={{ display: "block", minHeight: 32, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, lineHeight: 1.4, letterSpacing: "-0.005em", color: "var(--text-secondary)" }}>{label}</span>
      <span style={{ display: "flex", alignItems: "baseline", gap: 5 }}>
        <span className="bobi-tnum" style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 26, lineHeight: 1, letterSpacing: "-0.025em", color: accent ? "var(--bobi-acc)" : "var(--bobi-ink)" }}>{value}</span>
        {unit && <span style={{ fontFamily: "var(--font-sans)", fontSize: 12.5, color: "var(--text-secondary)" }}>{unit}</span>}
      </span>
      {/* delta sits on its own line: a long change string must never push past
          the card edge, and tiles keep a predictable height. */}
      {delta && (
        <span className="bobi-tnum" style={{ display: "block", marginTop: 6, fontFamily: "var(--font-sans)", fontSize: 12, lineHeight: 1.3, color: dc, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{delta}</span>
      )}
    </div>
  );
}
