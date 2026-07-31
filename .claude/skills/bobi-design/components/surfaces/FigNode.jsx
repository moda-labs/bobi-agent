import React from "react";

// A node card in a Bobi figure: 36px icon chip + title + mono subtitle, on a
// 10px-radius paper card with a soft warm shadow. The atom of every Bobi
// diagram — the loop, the team flows, the sleep cycle.
export function FigNode({ icon, title, sub, gate = false, absolute, className = "", style }) {
  const pos = absolute ? { position: "absolute", left: absolute.left, top: absolute.top, width: absolute.width, height: absolute.height } : null;
  return (
    <div className={className} style={{
      display: "flex", alignItems: "center", gap: 12, padding: absolute ? "0 16px" : "11px 16px",
      background: gate ? "color-mix(in srgb, var(--bobi-acc) 4%, var(--bobi-paper))" : "var(--bobi-paper)",
      border: `1px solid ${gate ? "var(--border-gate)" : "var(--border-node)"}`,
      borderRadius: "var(--radius-lg)", boxShadow: "var(--shadow-node)", ...pos, ...style,
    }}>
      {icon && (
        <span aria-hidden="true" style={{ width: 36, height: 36, flexShrink: 0, borderRadius: 9, background: "var(--surface-inset)", border: "1px solid rgba(54,46,37,0.10)", display: "flex", alignItems: "center", justifyContent: "center" }}>{icon}</span>
      )}
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontFamily: "var(--font-sans)", fontSize: "var(--text-node-title)", fontWeight: 500, letterSpacing: "-0.01em", color: "var(--bobi-ink)", lineHeight: 1.25, whiteSpace: "nowrap" }}>{title}</span>
        {sub && <span style={{ display: "block", marginTop: 2, fontFamily: "var(--font-mono)", fontSize: "var(--text-node-sub)", color: "var(--text-secondary)", lineHeight: 1.4, whiteSpace: "nowrap" }}>{sub}</span>}
      </span>
    </div>
  );
}

// The vertical dashed wire connecting stacked FigNodes.
export function FigWire({ height = 12 }) {
  return <div aria-hidden="true" style={{ width: 1.5, height, margin: "0 auto", backgroundImage: "repeating-linear-gradient(to bottom, rgba(54,46,37,0.35) 0 3px, transparent 3px 7px)" }} />;
}

// A vertical flow of nodes joined by wires — the team mini-flow pattern.
export function FigFlow({ nodes = [], className = "", style }) {
  return (
    <div className={className} style={{ display: "flex", flexDirection: "column", ...style }}>
      {nodes.map((n, i) => (
        <React.Fragment key={i}>
          {i > 0 && <FigWire />}
          <FigNode {...n} />
        </React.Fragment>
      ))}
    </div>
  );
}
