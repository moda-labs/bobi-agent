import React from "react";

// The operational toolbar: a search field that grows, optional icon buttons, and
// one primary action on the right. Keyboard hints live inside the field — a
// small enterprise affordance that signals the app is meant to be driven fast.
export function Toolbar({ placeholder = "Search", value, onChange, keyHint, tools, primary, className = "", style }) {
  return (
    <div className={className} style={{ display: "flex", alignItems: "center", gap: 10, ...style }}>
      <span className="bobi-field" style={{ display: "flex", alignItems: "center", gap: 9, flex: 1, minWidth: 0, padding: "0 11px", height: 36, background: "var(--surface-card)", border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)", boxShadow: "var(--elev-1)" }}>
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="var(--text-secondary)" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true" style={{ flexShrink: 0 }}>
          <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" />
        </svg>
        <input value={value} onChange={onChange} placeholder={placeholder}
          style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", fontFamily: "var(--font-sans)", fontSize: 14, color: "var(--bobi-ink)" }} />
        {keyHint && <KeyHint>{keyHint}</KeyHint>}
      </span>
      {tools}
      {primary}
    </div>
  );
}

// A keyboard shortcut chip.
export function KeyHint({ children }) {
  return (
    <span aria-hidden="true" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 19, height: 19, padding: "0 5px", flexShrink: 0, border: "1px solid var(--border-hairline)", borderRadius: 5, background: "var(--surface-inset)", fontFamily: "var(--font-mono)", fontSize: 10.5, color: "var(--text-secondary)" }}>{children}</span>
  );
}

// A square icon-only button, for filters and view toggles beside the search.
export function IconButton({ icon, label, onClick, active = false, className = "", style }) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label} className={`bobi-icon-btn ${className}`}
      style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 36, height: 36, flexShrink: 0, cursor: "pointer",
        background: active ? "var(--surface-inset)" : "var(--surface-card)",
        border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)",
        boxShadow: "var(--elev-1)", color: "var(--bobi-ink)" }}>{icon}</button>
  );
}
