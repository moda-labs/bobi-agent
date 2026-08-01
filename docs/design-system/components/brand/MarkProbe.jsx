import React from "react";

// The Bobi mark: an ink body, a dashed orbit, and a probe dot out on the orbit
// in violet. Canonical geometry — 32x32 viewBox, do not redraw or re-proportion.
export function MarkProbe({ size = 32, ink = "var(--bobi-paper)", accent = "var(--bobi-acc-bright)", className = "", style }) {
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} className={className} style={style} aria-hidden="true">
      <circle cx="14" cy="18" r="6" fill={ink} />
      <circle cx="14" cy="18" r="12.5" fill="none" stroke={ink} strokeWidth="1.2" strokeDasharray="2 4" opacity="0.6" />
      <circle cx="23.5" cy="9.5" r="3" fill={accent} />
    </svg>
  );
}
