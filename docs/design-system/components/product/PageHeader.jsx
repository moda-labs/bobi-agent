import React from "react";

// App page header. The title sets in DISPLAY CAPS with open tracking — caps read
// as chrome rather than editorial copy, which is what keeps a dense enterprise
// screen from feeling like a marketing page. Marketing headings stay sentence
// case; this treatment is product-only.
export function PageHeader({ title, sub, breadcrumb, actions, tabs, activeTab, onTab, className = "", style }) {
  return (
    <header className={className} style={{ background: "var(--surface-card)", borderBottom: "1px solid var(--border-hairline)", ...style }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 20, padding: tabs && tabs.length ? "18px 28px 0" : "18px 28px 20px", flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 300px", minWidth: 0 }}>
          {breadcrumb && (
            <nav style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 8, fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>
              {breadcrumb.map((b, i) => (
                <React.Fragment key={i}>
                  {i > 0 && <span aria-hidden="true" style={{ opacity: 0.5 }}>/</span>}
                  <span style={{ color: i === breadcrumb.length - 1 ? "var(--bobi-ink)" : "var(--text-secondary)" }}>{b}</span>
                </React.Fragment>
              ))}
            </nav>
          )}
          {/* Lowercase, not caps: the wordmark is lowercase "bobi", agent names,
              roles, filenames and CLI verbs are all lowercase, so a lowercase
              page title puts the chrome in the product's own voice. It also
              removes the last shouty uppercase treatment from the app layer. */}
          <h1 style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 21, lineHeight: 1.25, letterSpacing: "-0.02em", textTransform: "lowercase", color: "var(--bobi-ink)" }}>{title}</h1>
          {sub && <p style={{ marginTop: 6, maxWidth: 620, fontSize: 13.5, lineHeight: 1.55, color: "var(--text-secondary)" }}>{sub}</p>}
        </div>
        {actions && <div style={{ display: "flex", alignItems: "center", gap: 9, paddingTop: 2, flexShrink: 0, whiteSpace: "nowrap" }}>{actions}</div>}
      </div>
      {tabs && tabs.length > 0 && (
        <div role="tablist" style={{ display: "flex", gap: 2, padding: "14px 28px 0", marginBottom: -1 }}>
          {tabs.map((t) => {
            const on = t.id === activeTab;
            return (
              <button key={t.id} type="button" role="tab" aria-selected={on} onClick={() => onTab && onTab(t.id)}
                className="bobi-tab"
                style={{ display: "inline-flex", alignItems: "center", gap: 7, height: 34, padding: "0 11px", border: "none", background: "transparent", cursor: "pointer",
                  fontFamily: "var(--font-sans)", fontSize: 13.5, fontWeight: 500, letterSpacing: "-0.006em", textTransform: "lowercase",
                  color: on ? "var(--bobi-ink)" : "var(--text-secondary)" }}>
                {t.label}
                {t.count != null && (
                  <span className="bobi-tnum" style={{ fontFamily: "var(--font-sans)", fontSize: 11.5, padding: "1px 6px", borderRadius: 999, background: "var(--surface-inset)", color: "var(--text-secondary)" }}>{t.count}</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </header>
  );
}
