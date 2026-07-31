import React from "react";

// Run/agent state. Sentence-case SANS, not uppercase mono: mono is reserved for
// data (ids, paths, timestamps), and stacking dot + border + tint + caps + mono
// over-signals a single word. A soft tint, a hairline, and a dot only where the
// state is genuinely moving or broken.
const STATES = {
  live:    { label: "Live",    color: "var(--status-live)",    bg: "var(--status-live-soft)",    edge: "var(--status-live-edge)",    dot: true, pulse: true },
  waiting: { label: "Gate",    color: "var(--status-waiting)", bg: "var(--status-waiting-soft)", edge: "var(--status-waiting-edge)", dot: true },
  done:    { label: "Done",    color: "var(--text-secondary)", bg: "transparent",                edge: "var(--border-strong)" },
  idle:    { label: "Idle",    color: "var(--text-secondary)", bg: "transparent",                edge: "var(--border-hairline)" },
  failed:  { label: "Failed",  color: "var(--status-failed)",  bg: "var(--status-failed-soft)",  edge: "var(--status-failed-edge)",  dot: true },
};

export function StatusBadge({ state = "idle", label, bare = false, className = "", style }) {
  const s = STATES[state] || STATES.idle;
  return (
    <span className={className} style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      fontFamily: "var(--font-sans)", fontSize: 12, fontWeight: 500,
      letterSpacing: "-0.005em", color: s.color, lineHeight: 1.5, whiteSpace: "nowrap",
      background: bare ? "transparent" : s.bg,
      border: bare ? "none" : `1px solid ${s.edge}`,
      padding: bare ? 0 : "2px 8px",
      borderRadius: bare ? 0 : 999, ...style,
    }}>
      {s.dot && (
        <span aria-hidden="true" style={{ width: 5, height: 5, borderRadius: "50%", background: s.color, flexShrink: 0, animation: s.pulse ? "bobi-glow 3.6s var(--ease) infinite" : undefined }} />
      )}
      {label || s.label}
    </span>
  );
}
