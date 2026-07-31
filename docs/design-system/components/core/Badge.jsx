import React from "react";

// Small uppercase mono tag. Bobi's four established tones map to meaning:
// muted = neutral metadata, clay = label/category, accent = enforced/live/gate,
// dark = a count or an inverted chip.
const TONES = {
  muted: { color: "var(--text-secondary)", background: "var(--status-idle-soft)", edge: "var(--status-idle-edge)" },
  clay: { color: "var(--bobi-clay)", background: "var(--status-waiting-soft)", edge: "var(--status-waiting-edge)" },
  accent: { color: "var(--bobi-acc)", background: "var(--status-live-soft)", edge: "var(--status-live-edge)" },
  dark: { color: "var(--bobi-paper)", background: "var(--bobi-void)", edge: "transparent" },
};

export function Badge({ children, tone = "muted", plain = false, dot = false, pill = false, caps = true, className = "", style }) {
  const t = TONES[tone] || TONES.muted;
  return (
    <span className={className} style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      fontFamily: caps ? "var(--font-mono)" : "var(--font-sans)",
      fontSize: caps ? 10.5 : 11.5, fontWeight: 500,
      letterSpacing: caps ? "var(--track-caps-sm)" : "-0.005em",
      textTransform: caps ? "uppercase" : "none",
      color: t.color,
      background: plain ? "transparent" : t.background,
      border: plain ? "none" : `1px solid ${t.edge}`,
      padding: plain ? 0 : (pill ? "2px 9px" : "3px 8px"),
      borderRadius: plain ? 0 : (pill ? 999 : "var(--radius-sm)"),
      lineHeight: 1.5, whiteSpace: "nowrap", ...style,
    }}>
      {dot && <span aria-hidden="true" style={{ width: 5, height: 5, borderRadius: "50%", background: "currentColor", flexShrink: 0 }} />}
      {children}
    </span>
  );
}
