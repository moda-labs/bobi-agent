import React from "react";

// Native select in Bobi clothing. Sentence-case sans label; mono value because
// select values here are config identifiers (runtimes, models, modes).
export function Select({ label, hint, options = [], className = "", style, ...rest }) {
  return (
    <label className={className} style={{ display: "block", ...style }}>
      {label && <span style={{ display: "block", marginBottom: 6, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, letterSpacing: "-0.005em", color: "var(--bobi-ink)" }}>{label}</span>}
      <span className="bobi-field" style={{ display: "flex", alignItems: "center", height: 36, background: "var(--surface-card)", border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)", padding: "0 9px 0 11px" }}>
        <select {...rest} style={{ flex: 1, minWidth: 0, height: "100%", border: "none", outline: "none", background: "transparent", fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--bobi-ink)", appearance: "none", cursor: "pointer" }}>
          {options.map((o) => {
            const v = typeof o === "string" ? o : o.value;
            const l = typeof o === "string" ? o : o.label;
            return <option key={v} value={v}>{l}</option>;
          })}
        </select>
        <svg viewBox="0 0 20 20" width="15" height="15" fill="none" aria-hidden="true" style={{ flexShrink: 0, color: "var(--text-secondary)" }}>
          <path d="M6 8.5 10 12.5 14 8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      {hint && <span style={{ display: "block", marginTop: 6, fontFamily: "var(--font-sans)", fontSize: 12, color: "var(--text-secondary)" }}>{hint}</span>}
    </label>
  );
}
