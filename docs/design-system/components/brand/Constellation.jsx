import React from "react";

// The hero star chart: an origin EVENT fans through relay nodes to a GATE and an
// OUTCOME. Reads as a survey chart, not a flowchart. Decorative only.
const NODES = [
  { id: "o", x: 110, y: 540, r: 5, kind: "origin", label: "EVENT" },
  { id: "h", x: 190, y: 330, r: 2.5, kind: "open" },
  { id: "a", x: 285, y: 445, r: 3.5, kind: "filled" },
  { id: "b", x: 355, y: 295, r: 4, kind: "filled", ping: 0 },
  { id: "c", x: 500, y: 390, r: 3, kind: "open" },
  { id: "d", x: 545, y: 205, r: 5, kind: "gate", label: "GATE", ping: 1.4 },
  { id: "e", x: 665, y: 320, r: 3, kind: "open" },
  { id: "f", x: 620, y: 490, r: 3.5, kind: "filled", ping: 2.6 },
  { id: "g", x: 715, y: 130, r: 5, kind: "outcome", label: "OUTCOME" },
];
const EDGES = [["o","a"],["a","b"],["b","d"],["d","g"],["a","c"],["c","f"],["b","h"],["c","e"]];
const TICKS = [[240,160],[430,110],[640,560],[150,620],[690,230],[80,240]];
const MAIN = new Set(["oa","ab","bd","dg"]);
const TRAVEL = "M110 540 L285 445 L355 295 L545 205 L715 130";

export function Constellation({ width = 700, height = 610, accent = "var(--bobi-acc-bright)", animate = true, className = "" }) {
  const line = "rgba(250,247,238,0.32)";
  const mainLine = "rgba(250,247,238,0.5)";
  const open = "rgba(250,247,238,0.75)";
  const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));
  return (
    <svg viewBox="0 0 780 680" width={width} height={height} className={className} aria-hidden="true">
      <circle cx="430" cy="-130" r="420" fill="none" stroke={line} strokeWidth="1" strokeDasharray="2 7" opacity="0.7" />
      <circle cx="180" cy="760" r="300" fill="none" stroke={line} strokeWidth="1" strokeDasharray="2 7" opacity="0.45" />
      {TICKS.map(([x, y], i) => (
        <g key={i} stroke="rgba(250,247,238,0.4)" strokeWidth="1">
          <line x1={x - 4} y1={y} x2={x + 4} y2={y} />
          <line x1={x} y1={y - 4} x2={x} y2={y + 4} />
        </g>
      ))}
      {EDGES.map(([p, q]) => {
        const main = MAIN.has(p + q);
        return <line key={p + q} x1={byId[p].x} y1={byId[p].y} x2={byId[q].x} y2={byId[q].y} stroke={main ? mainLine : line} strokeWidth={main ? 1.2 : 1} />;
      })}
      {animate && (
        <circle cx={110} cy={540} r="3" fill={accent} style={{ offsetPath: `path('${TRAVEL}')`, offsetRotate: "0deg", animation: "bobi-travel 7s var(--ease) infinite" }} />
      )}
      {NODES.map((n) => (
        <g key={n.id}>
          {animate && n.ping !== undefined && (
            <circle cx={n.x} cy={n.y} r={n.r * 4.2} fill="none" stroke={accent} strokeWidth="1.2"
              style={{ transformBox: "fill-box", transformOrigin: "center", animation: `bobi-ping 4.2s var(--ease) ${n.ping}s infinite` }} />
          )}
          {n.kind === "gate" && <rect x={n.x - 7} y={n.y - 7} width={14} height={14} fill="rgba(250,247,238,0.06)" stroke={accent} strokeWidth="1.4" transform={`rotate(45 ${n.x} ${n.y})`} />}
          {n.kind === "outcome" && <><circle cx={n.x} cy={n.y} r={n.r + 6} fill="none" stroke={accent} strokeWidth="1" opacity="0.5" /><circle cx={n.x} cy={n.y} r={n.r} fill={accent} /></>}
          {n.kind === "origin" && <><circle cx={n.x} cy={n.y} r={n.r + 7} fill="none" stroke={open} strokeWidth="1" strokeDasharray="2 3" /><circle cx={n.x} cy={n.y} r={n.r} fill="var(--bobi-paper)" /></>}
          {n.kind === "open" && <circle cx={n.x} cy={n.y} r={n.r} fill="var(--bobi-void)" stroke={open} strokeWidth="1.2" />}
          {n.kind === "filled" && <circle cx={n.x} cy={n.y} r={n.r} fill="var(--bobi-paper)" />}
          {n.label && (
            <text x={n.kind === "outcome" ? n.x - 16 : n.x + 14} y={n.y + 4} textAnchor={n.kind === "outcome" ? "end" : "start"}
              fontFamily="var(--font-mono)" fontSize="11" letterSpacing="0.16em"
              fill={n.kind === "outcome" || n.kind === "gate" ? "rgba(250,247,238,0.92)" : "rgba(250,247,238,0.78)"}>{n.label}</text>
          )}
        </g>
      ))}
    </svg>
  );
}
