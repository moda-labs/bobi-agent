import React from "react";

// The editorial eyebrow: a 10px clay square + uppercase mono label. Clay keeps
// this decorative marketing signature distinct from violet product state.
export function Eyebrow({ children, color = "var(--bobi-clay)", className = "", style }) {
  return (
    <p className={className} style={{ display: "flex", alignItems: "center", margin: "0 0 var(--stack-eyebrow-heading)", fontFamily: "var(--font-mono)", fontSize: "var(--text-eyebrow)", fontWeight: 500, letterSpacing: "var(--track-tightest)", color: "var(--text-secondary)", ...style }}>
      <span aria-hidden="true" style={{ display: "inline-block", width: 10, height: 10, background: color, marginRight: 8, flexShrink: 0 }}></span>
      {children}
    </p>
  );
}
