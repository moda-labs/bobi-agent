import React from "react";

// Bobi buttons. Hover, active, and focus live in tokens/app.css (.bobi-btn) —
// pseudo-states cannot be expressed inline, and a button without them reads
// broken. Never reintroduce JS mouse handlers to fake a press.
const BASE = {
  display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8,
  border: "1px solid transparent", cursor: "pointer", textDecoration: "none",
  fontFamily: "var(--font-sans)", fontWeight: 500, letterSpacing: "-0.006em",
  borderRadius: "var(--radius-sm)", textShadow: "none", whiteSpace: "nowrap",
};
const SIZES = {
  sm: { height: 30, padding: "0 12px", fontSize: 13 },
  md: { height: 36, padding: "0 16px", fontSize: 14 },
  lg: { height: 44, padding: "0 24px", fontSize: 15 },
};
const VARIANTS = {
  primary: { background: "var(--bobi-acc)", color: "var(--bobi-paper)", boxShadow: "var(--elev-1)" },
  inverse: { background: "var(--bobi-paper)", color: "var(--bobi-ink)" },
  quiet: { background: "var(--bobi-paper-alt)", color: "var(--bobi-ink)" },
  glass: { background: "var(--glass-bg)", color: "var(--bobi-paper)", borderColor: "var(--glass-border)", backdropFilter: "blur(var(--glass-blur))" },
  ghost: { background: "var(--surface-card)", color: "var(--bobi-ink)", borderColor: "var(--border-strong)", boxShadow: "var(--elev-1)" },
};

export function Button({ variant = "primary", size = "md", as = "button", icon, trailing, children, disabled, className = "", style, ...rest }) {
  const Tag = as;
  return (
    <Tag
      {...rest}
      data-variant={variant}
      disabled={Tag === "button" ? disabled : undefined}
      className={`bobi-btn ${className}`}
      style={{ ...BASE, ...SIZES[size], ...VARIANTS[variant], ...style }}
    >
      {icon}{children}{trailing}
    </Tag>
  );
}
