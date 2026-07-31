import React from "react";

// Text field. Label is sentence-case SANS (mono is for data); the VALUE stays
// mono when it holds a path, id, cron, or command. The whole field lights up on
// focus via .bobi-field in tokens/app.css, not just the inner input.
export function Input({ label, hint, mono = true, invalid = false, prefix, suffix, className = "", style, ...rest }) {
  return (
    <label className={className} style={{ display: "block", ...style }}>
      {label && <span style={{ display: "block", marginBottom: 6, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, letterSpacing: "-0.005em", color: "var(--bobi-ink)" }}>{label}</span>}
      <span className="bobi-field" style={{ display: "flex", alignItems: "center", gap: 8, height: 36, padding: "0 11px", background: "var(--surface-card)", border: `1px solid ${invalid ? "var(--status-failed)" : "var(--border-strong)"}`, borderRadius: "var(--radius-sm)" }}>
        {prefix && <span style={{ fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--bobi-acc)", flexShrink: 0 }}>{prefix}</span>}
        <input {...rest} style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)", fontSize: mono ? 13 : 14, color: "var(--bobi-ink)" }} />
        {suffix}
      </span>
      {hint && <span style={{ display: "block", marginTop: 6, fontFamily: "var(--font-sans)", fontSize: 12, color: invalid ? "var(--status-failed)" : "var(--text-secondary)" }}>{hint}</span>}
    </label>
  );
}
