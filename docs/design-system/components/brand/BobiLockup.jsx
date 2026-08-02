import React from "react";
import { MarkProbe } from "./MarkProbe.jsx";

// Mark + lowercase "bobi" in Geist semibold at -0.045em. Everything is sized in
// ems off fontSize, so one component covers 22px footer, 27px header, and the
// 64px hero moment. Pass a clamp() string for responsive sizing.
export function BobiLockup({ fontSize = 27, ink = "var(--bobi-paper)", accent = "var(--bobi-acc-bright)", className = "" }) {
  return (
    <span className={className} style={{ fontSize, display: "inline-flex", alignItems: "center", gap: "0.32em" }}>
      <MarkProbe size="0.62em" ink={ink} accent={accent} style={{ width: "0.62em", height: "0.62em", flexShrink: 0 }} />
      <span style={{ fontFamily: "var(--font-display)", fontWeight: 600, lineHeight: 1, letterSpacing: "var(--track-wordmark)", color: ink }}>bobi</span>
    </span>
  );
}
