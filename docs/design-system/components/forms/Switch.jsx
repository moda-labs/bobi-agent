import React from "react";

// Track switches to the violet accent when on. 6px radius track — Bobi does not
// use fully-round pill switches.
export function Switch({ checked = false, onChange, label, sub, disabled, className = "", style }) {
  return (
    <label className={className} style={{ display: "flex", alignItems: "center", gap: 12, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, ...style }}>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled}
        onClick={() => onChange && onChange(!checked)}
        className="bobi-switch"
        style={{ position: "relative", width: 38, height: 22, flexShrink: 0, padding: 0, cursor: "inherit",
          border: `1px solid ${checked ? "var(--bobi-acc)" : "var(--border-strong)"}`, borderRadius: "var(--radius-sm)",
          background: checked ? "var(--bobi-acc)" : "var(--bobi-paper-alt)",
          transition: "background var(--dur-fast) ease-out, border-color var(--dur-fast) ease-out" }}>
        <span style={{ position: "absolute", top: 2, left: checked ? 18 : 2, width: 16, height: 16, borderRadius: 4,
          background: "var(--bobi-paper)", boxShadow: "0 1px 3px rgba(54,46,37,0.25)",
          transition: "left var(--dur-fast) var(--ease)" }} />
      </button>
      {(label || sub) && (
        <span style={{ minWidth: 0 }}>
          {label && <span style={{ display: "block", fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: 500, color: "var(--bobi-ink)" }}>{label}</span>}
          {sub && <span style={{ display: "block", marginTop: 1, fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>{sub}</span>}
        </span>
      )}
    </label>
  );
}
