import React from "react";

// Product sidebar. tone="light" (default) keeps chrome in the same family as the
// content so a dense app reads as one calm surface; tone="void" is the
// brand-forward pairing for login and demo shells.
//
// Labels are SANS, not mono: mono is for data (ids, paths, timestamps), and a
// mono nav makes an app read as terminal cosplay. Hover/current/focus come from
// .bobi-nav-item in tokens/app.css.
export function SideNav({ items = [], active, onSelect, footer, agent, tone = "light", className = "", style }) {
  const dark = tone === "void";
  const t = dark
    ? { bg: "var(--surface-dark)", fg: "var(--bobi-paper)", dim: "rgba(250,247,238,0.68)", faint: "rgba(250,247,238,0.5)", edge: "rgba(250,247,238,0.14)", ink: "var(--bobi-paper)", acc: "var(--bobi-acc-bright)", border: "none", inset: "rgba(250,247,238,0.05)" }
    : { bg: "var(--surface-card)", fg: "var(--bobi-ink)", dim: "var(--text-secondary)", faint: "var(--text-secondary)", edge: "var(--border-hairline)", ink: "var(--bobi-ink)", acc: "var(--bobi-acc)", border: "1px solid var(--border-hairline)", inset: "var(--surface-inset)" };
  return (
    <nav className={className} style={{ display: "flex", flexDirection: "column", width: 236, flexShrink: 0, background: t.bg, color: t.fg, borderRight: t.border, padding: "20px 0 12px", ...style }}>
      <div style={{ padding: "0 16px 4px" }}>
        <span style={{ fontSize: 22, display: "inline-flex", alignItems: "center", gap: "0.32em" }}>
          <svg viewBox="0 0 32 32" style={{ width: "0.62em", height: "0.62em", flexShrink: 0 }} aria-hidden="true">
            <circle cx="14" cy="18" r="6" fill={t.ink} />
            <circle cx="14" cy="18" r="12.5" fill="none" stroke={t.ink} strokeWidth="1.2" strokeDasharray="2 4" opacity="0.6" />
            <circle cx="23.5" cy="9.5" r="3" fill={t.acc} />
          </svg>
          <span style={{ fontFamily: "var(--font-display)", fontWeight: 600, lineHeight: 1, letterSpacing: "var(--track-wordmark)", color: t.ink }}>bobi</span>
        </span>
      </div>
      <a href="#" style={{ margin: "8px 16px 18px", fontFamily: "var(--font-mono)", fontSize: 10, letterSpacing: "0.14em", color: t.faint, textDecoration: "none" }}>BY MODA LABS ↗</a>
      {agent && (
        <button type="button" style={{ margin: "0 10px 16px", padding: "0 10px", height: 38, cursor: "pointer", border: `1px solid ${t.edge}`, borderRadius: "var(--radius-sm)", background: t.inset, color: t.ink, display: "flex", alignItems: "center", gap: 8, textAlign: "left" }}>
          <span style={{ flex: 1, minWidth: 0, fontFamily: "var(--font-sans)", fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent}</span>
          <svg viewBox="0 0 20 20" width="13" height="13" fill="none" aria-hidden="true" style={{ color: t.dim, flexShrink: 0 }}>
            <path d="M7 8l3-3 3 3M7 12l3 3 3-3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 1, padding: "0 10px" }}>
        {items.map((it) => {
          const on = it.id === active;
          return (
            <button key={it.id} type="button" onClick={() => onSelect && onSelect(it.id)}
              aria-current={on ? "page" : undefined}
              className="bobi-nav-item"
              style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", height: 36, padding: "0 10px", border: "none", cursor: "pointer",
                borderRadius: "var(--radius-sm)", textAlign: "left", background: "transparent",
                color: on ? t.ink : t.dim,
                fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: on ? 500 : 400, letterSpacing: "-0.006em" }}>
              {it.icon && <span aria-hidden="true" style={{ display: "flex", flexShrink: 0, opacity: on ? 1 : 0.75 }}>{it.icon}</span>}
              <span style={{ flex: 1, minWidth: 0 }}>{it.label}</span>
              {it.count != null && (
                <span className="bobi-tnum" style={{ fontFamily: "var(--font-sans)", fontSize: 12, fontWeight: 500,
                  padding: it.countAccent ? "1px 7px" : 0, borderRadius: 999,
                  background: it.countAccent ? (dark ? "rgba(150,144,241,0.18)" : "var(--status-live-soft)") : "transparent",
                  border: it.countAccent ? `1px solid ${dark ? "rgba(150,144,241,0.35)" : "var(--status-live-edge)"}` : "none",
                  color: it.countAccent ? t.acc : t.faint }}>{it.count}</span>
              )}
              {it.expandable && (
                <svg viewBox="0 0 20 20" width="13" height="13" fill="none" aria-hidden="true" style={{ color: t.faint, flexShrink: 0 }}>
                  <path d="M8 6l4 4-4 4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
            </button>
          );
        })}
      </div>
      {footer && <div style={{ marginTop: "auto", padding: "12px 16px 0", borderTop: `1px solid ${t.edge}`, fontFamily: "var(--font-mono)", fontSize: 11, color: t.faint }}>{footer}</div>}
    </nav>
  );
}
