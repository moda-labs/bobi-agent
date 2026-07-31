import React from "react";

// A small label that sits ABOVE a card or card group, outside its border. This
// is the quiet way to group an operational screen: the label belongs to the
// page, not to the card, so cards stay clean white objects with no tinted
// header bars competing for attention.
export function SectionLabel({ children, action, className = "", style }) {
  return (
    <div className={className} style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, marginBottom: 10, ...style }}>
      <span style={{ fontFamily: "var(--font-sans)", fontSize: 13.5, fontWeight: 500, letterSpacing: "-0.01em", color: "var(--bobi-ink)" }}>{children}</span>
      {action}
    </div>
  );
}
