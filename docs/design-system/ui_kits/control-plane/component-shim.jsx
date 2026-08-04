// AUTO-ASSEMBLED from components/**/<Name>.jsx so this UI kit runs without a
// bundler. Each file is wrapped in its own IIFE and its exports attached to
// window. When consuming this design system from a real app, import directly.

/* ==================== brand/MarkProbe ==================== */
(function () {

// The Bobi mark: an ink body, a dashed orbit, and a probe dot out on the orbit
// in violet. Canonical geometry — 32x32 viewBox, do not redraw or re-proportion.
function MarkProbe({ size = 32, ink = "var(--bobi-paper)", accent = "var(--bobi-acc-bright)", className = "", style }) {
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} className={className} style={style} aria-hidden="true">
      <circle cx="14" cy="18" r="6" fill={ink} />
      <circle cx="14" cy="18" r="12.5" fill="none" stroke={ink} strokeWidth="1.2" strokeDasharray="2 4" opacity="0.6" />
      <circle cx="23.5" cy="9.5" r="3" fill={accent} />
    </svg>
  );
}

Object.assign(window, { MarkProbe });
})();

/* ==================== brand/BobiLockup ==================== */
(function () {

// Mark + lowercase "bobi" in Geist semibold at -0.045em. Everything is sized in
// ems off fontSize, so one component covers 22px footer, 27px header, and the
// 64px hero moment. Pass a clamp() string for responsive sizing.
function BobiLockup({ fontSize = 27, ink = "var(--bobi-paper)", accent = "var(--bobi-acc-bright)", className = "" }) {
  return (
    <span className={className} style={{ fontSize, display: "inline-flex", alignItems: "center", gap: "0.32em" }}>
      <MarkProbe size="0.62em" ink={ink} accent={accent} style={{ width: "0.62em", height: "0.62em", flexShrink: 0 }} />
      <span style={{ fontFamily: "var(--font-display)", fontWeight: 600, lineHeight: 1, letterSpacing: "var(--track-wordmark)", color: ink }}>bobi</span>
    </span>
  );
}

Object.assign(window, { BobiLockup });
})();

/* ==================== brand/Constellation ==================== */
(function () {

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

function Constellation({ width = 700, height = 610, accent = "var(--bobi-acc-bright)", animate = true, className = "" }) {
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

Object.assign(window, { Constellation });
})();

/* ==================== core/Button ==================== */
(function () {

// Bobi buttons. Hover, active, and focus live in tokens/app.css (.bobi-btn) —
// pseudo-states cannot be expressed inline, and a button without them reads
// broken. Never reintroduce JS mouse handlers to fake a press.
const BASE = {
  display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8,
  border: "1px solid transparent", cursor: "pointer", textDecoration: "none",
  fontFamily: "var(--font-sans)", fontWeight: 500, letterSpacing: "-0.006em",
  borderRadius: "var(--radius-sm)", textShadow: "none", whiteSpace: "nowrap",
};
const SIZES = {
  sm: { height: 30, padding: "0 12px", fontSize: 13 },
  md: { height: 36, padding: "0 16px", fontSize: 14 },
  lg: { height: 44, padding: "0 24px", fontSize: 15 },
};
const VARIANTS = {
  primary: { background: "var(--bobi-acc)", color: "var(--bobi-paper)", boxShadow: "var(--elev-1)" },
  inverse: { background: "var(--bobi-paper)", color: "var(--bobi-ink)" },
  quiet: { background: "var(--bobi-paper-alt)", color: "var(--bobi-ink)" },
  glass: { background: "var(--glass-bg)", color: "var(--bobi-paper)", borderColor: "var(--glass-border)", backdropFilter: "blur(var(--glass-blur))" },
  ghost: { background: "var(--surface-card)", color: "var(--bobi-ink)", borderColor: "var(--border-strong)", boxShadow: "var(--elev-1)" },
};

function Button({ variant = "primary", size = "md", as = "button", icon, trailing, children, disabled, className = "", style, ...rest }) {
  const Tag = as;
  return (
    <Tag
      {...rest}
      data-variant={variant}
      disabled={Tag === "button" ? disabled : undefined}
      className={`bobi-btn ${className}`}
      style={{ ...BASE, ...SIZES[size], ...VARIANTS[variant], ...style }}
    >
      {icon}{children}{trailing}
    </Tag>
  );
}

Object.assign(window, { Button });
})();

/* ==================== core/Eyebrow ==================== */
(function () {

// The section eyebrow: a 10px violet square + uppercase mono label. Bobi's most
// repeated structural signal — every major section opens with one.
function Eyebrow({ children, color = "var(--bobi-acc)", className = "", style }) {
  return (
    <p className={className} style={{ display: "flex", alignItems: "center", margin: "0 0 var(--stack-eyebrow-heading)", fontFamily: "var(--font-mono)", fontSize: "var(--text-eyebrow)", fontWeight: 500, letterSpacing: "var(--track-tightest)", color: "var(--text-secondary)", ...style }}>
      <span aria-hidden="true" style={{ display: "inline-block", width: 10, height: 10, background: color, marginRight: 8, flexShrink: 0 }}></span>
      {children}
    </p>
  );
}

Object.assign(window, { Eyebrow });
})();

/* ==================== core/FileChip ==================== */
(function () {

// A bordered mono filename chip — Bobi's way of naming a file inline, in a
// heading, or in a table cell. Sized relative to surrounding text in headings.
function FileChip({ children, inHeading = false, accent = false, className = "", style }) {
  return (
    <span className={className} style={{
      display: "inline-flex", alignItems: "baseline", position: "relative",
      margin: "0 2px", padding: "0 8px 1px",
      border: `1px solid ${accent ? "var(--border-gate)" : "var(--border-strong)"}`,
      borderRadius: "var(--radius-sm)", background: "var(--bobi-paper)",
      fontFamily: "var(--font-mono)", fontWeight: 400, letterSpacing: 0,
      color: accent ? "var(--bobi-acc)" : "var(--bobi-ink)", whiteSpace: "nowrap",
      top: inHeading ? -1 : 0, fontSize: inHeading ? "0.62em" : "12.5px", ...style,
    }}>{children}</span>
  );
}

Object.assign(window, { FileChip });
})();

/* ==================== core/Badge ==================== */
(function () {

// Small uppercase mono tag. Bobi's four established tones map to meaning:
// muted = neutral metadata, clay = label/category, accent = enforced/live/gate,
// dark = a count or an inverted chip.
const TONES = {
  muted: { color: "var(--text-secondary)", background: "var(--status-idle-soft)", edge: "var(--status-idle-edge)" },
  clay: { color: "var(--bobi-clay)", background: "var(--status-waiting-soft)", edge: "var(--status-waiting-edge)" },
  accent: { color: "var(--bobi-acc)", background: "var(--status-live-soft)", edge: "var(--status-live-edge)" },
  dark: { color: "var(--bobi-paper)", background: "var(--bobi-void)", edge: "transparent" },
};

function Badge({ children, tone = "muted", plain = false, dot = false, pill = false, caps = true, className = "", style }) {
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

Object.assign(window, { Badge });
})();

/* ==================== forms/Input ==================== */
(function () {

// Text field. Label is sentence-case SANS (mono is for data); the VALUE stays
// mono when it holds a path, id, cron, or command. The whole field lights up on
// focus via .bobi-field in tokens/app.css, not just the inner input.
function Input({ label, hint, mono = true, invalid = false, prefix, suffix, className = "", style, ...rest }) {
  return (
    <label className={className} style={{ display: "block", ...style }}>
      {label && <span style={{ display: "block", marginBottom: 6, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, letterSpacing: "-0.005em", color: "var(--bobi-ink)" }}>{label}</span>}
      <span className="bobi-field" style={{ display: "flex", alignItems: "center", gap: 8, height: 36, padding: "0 11px", background: "var(--surface-card)", border: `1px solid ${invalid ? "var(--status-failed)" : "var(--border-strong)"}`, borderRadius: "var(--radius-sm)" }}>
        {prefix && <span style={{ fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--bobi-acc)", flexShrink: 0 }}>{prefix}</span>}
        <input {...rest} style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)", fontSize: mono ? 13 : 14, color: "var(--bobi-ink)" }} />
        {suffix}
      </span>
      {hint && <span style={{ display: "block", marginTop: 6, fontFamily: "var(--font-sans)", fontSize: 12, color: invalid ? "var(--status-failed)" : "var(--text-secondary)" }}>{hint}</span>}
    </label>
  );
}

Object.assign(window, { Input });
})();

/* ==================== forms/Select ==================== */
(function () {

// Native select in Bobi clothing. Sentence-case sans label; mono value because
// select values here are config identifiers (runtimes, models, modes).
function Select({ label, hint, options = [], className = "", style, ...rest }) {
  return (
    <label className={className} style={{ display: "block", ...style }}>
      {label && <span style={{ display: "block", marginBottom: 6, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, letterSpacing: "-0.005em", color: "var(--bobi-ink)" }}>{label}</span>}
      <span className="bobi-field" style={{ display: "flex", alignItems: "center", height: 36, background: "var(--surface-card)", border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)", padding: "0 9px 0 11px" }}>
        <select {...rest} style={{ flex: 1, minWidth: 0, height: "100%", border: "none", outline: "none", background: "transparent", fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--bobi-ink)", appearance: "none", cursor: "pointer" }}>
          {options.map((o) => {
            const v = typeof o === "string" ? o : o.value;
            const l = typeof o === "string" ? o : o.label;
            return <option key={v} value={v}>{l}</option>;
          })}
        </select>
        <svg viewBox="0 0 20 20" width="15" height="15" fill="none" aria-hidden="true" style={{ flexShrink: 0, color: "var(--text-secondary)" }}>
          <path d="M6 8.5 10 12.5 14 8.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      {hint && <span style={{ display: "block", marginTop: 6, fontFamily: "var(--font-sans)", fontSize: 12, color: "var(--text-secondary)" }}>{hint}</span>}
    </label>
  );
}

Object.assign(window, { Select });
})();

/* ==================== forms/Switch ==================== */
(function () {

// Track switches to the violet accent when on. 6px radius track — Bobi does not
// use fully-round pill switches.
function Switch({ checked = false, onChange, label, sub, disabled, className = "", style }) {
  return (
    <label className={className} style={{ display: "flex", alignItems: "center", gap: 12, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1, ...style }}>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled}
        onClick={() => onChange && onChange(!checked)}
        className="bobi-switch"
        style={{ position: "relative", width: 38, height: 22, flexShrink: 0, padding: 0, cursor: "inherit",
          border: `1px solid ${checked ? "var(--bobi-acc)" : "var(--border-strong)"}`, borderRadius: "var(--radius-sm)",
          background: checked ? "var(--bobi-acc)" : "var(--bobi-paper-alt)",
          transition: "background var(--dur-fast) ease-out, border-color var(--dur-fast) ease-out" }}>
        <span style={{ position: "absolute", top: 2, left: checked ? 18 : 2, width: 16, height: 16, borderRadius: 4,
          background: "var(--bobi-paper)", boxShadow: "0 1px 3px rgba(54,46,37,0.25)",
          transition: "left var(--dur-fast) var(--ease)" }} />
      </button>
      {(label || sub) && (
        <span style={{ minWidth: 0 }}>
          {label && <span style={{ display: "block", fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: 500, color: "var(--bobi-ink)" }}>{label}</span>}
          {sub && <span style={{ display: "block", marginTop: 1, fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>{sub}</span>}
        </span>
      )}
    </label>
  );
}

Object.assign(window, { Switch });
})();

/* ==================== icons/Icon ==================== */
(function () {

// Bobi's icon set, ported verbatim from the product source (bobi-icons.tsx).
// Hand-drawn architect line art: ink strokes at 1.5, round caps and joins, and
// AT MOST ONE violet accent per glyph — echoing the probe mark's ink-body-plus-
// violet-dot signature. Never mix in a third-party icon pack alongside these.
const INK = "var(--bobi-ink)";
const ACC = "var(--bobi-acc)";
const S = { fill: "none", strokeWidth: 1.5, strokeLinecap: "round", strokeLinejoin: "round" };

const PATHS = {
  // ---- sources / events ----
  ticket: (<><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M8 9.5h8M8 13h5" /></>),
  bell: (<><path d="M12 3a5.5 5.5 0 0 0-5.5 5.5V12l-1.8 3h14.6l-1.8-3V8.5A5.5 5.5 0 0 0 12 3Z" /><path d="M10 18a2 2 0 0 0 4 0" /></>),
  mail: (<><rect x="3.5" y="5.5" width="17" height="13" rx="2" /><path d="m4.5 7.5 7.5 5.5 7.5-5.5" /></>),
  chat: (<><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v6a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 12.5Z" /><path d="M8.5 8.5h7M8.5 11.5h4" /></>),
  queue: <path d="M5 7.5h14M5 12h14M5 16.5h9" />,
  // ---- agents / roles ----
  code: <path d="m8.5 8-4 4 4 4M15.5 8l4 4-4 4" />,
  pulse: <path d="M3 12h4l2.5-6 4 12 2.5-6h5" />,
  headset: <path d="M4 12a8 8 0 0 1 16 0v4a2 2 0 0 1-2 2h-2v-6h4M4 12v4a2 2 0 0 0 2 2h2v-6H4" />,
  checklist: <path d="M4 6h2M4 12h2M4 18h2M9 6h11M9 12h11M9 18h7" />,
  // ---- outcomes ----
  checkCircle: (<><circle cx="12" cy="12" r="8.5" /><path d="M8.5 12.5l2.4 2.4 4.6-5.4" /></>),
  plane: <path d="M21 3 11.5 12.5M21 3l-6.5 18-3-8-8-3L21 3Z" />,
  // ---- cycle ----
  sessions: (<><rect x="4" y="13" width="7" height="7" /><rect x="13" y="13" width="7" height="7" /><rect x="8.5" y="4" width="7" height="7" /></>),
  tray: (<><path d="M4 13v6h16v-6" /><path d="M12 4v9M8.5 10.5 12 14l3.5-3.5" /></>),
  sunrise: (<><path d="M4 18h16" /><path d="M8 15a4 4 0 0 1 8 0" /><path d="M12 6v3M6.6 9.6l1.6 1.6M17.4 9.6l-1.6 1.6" /></>),
  // ---- benefits / config ----
  workflow: (<><rect x="3.5" y="3.5" width="7" height="7" /><rect x="13.5" y="13.5" width="7" height="7" /><path d="M10.5 7h4.5a2 2 0 0 1 2 2v4.5" /><circle cx="17" cy="9" r="1.7" style={{ fill: ACC }} stroke="none" /></>),
  parallel: (<><path d="M6 4v16M12 4v16M18 4v16" /><circle cx="6" cy="9" r="1.8" style={{ fill: ACC }} stroke="none" /><circle cx="12" cy="14.5" r="1.8" style={{ fill: ACC }} stroke="none" /><circle cx="18" cy="7" r="1.8" style={{ fill: ACC }} stroke="none" /></>),
  bus: (<><path d="M6 4.5v6.5M12 4.5v6.5M18 4.5v6.5" /><path d="M3.5 16.5h17" /><circle cx="6" cy="16.5" r="1.1" fill={INK} stroke="none" /><circle cx="18" cy="16.5" r="1.1" fill={INK} stroke="none" /><circle cx="12" cy="16.5" r="2" style={{ fill: ACC }} stroke="none" /></>),
  coin: (<><circle cx="12" cy="12" r="8.5" /><path d="M12 7v10M15 9.2c-.6-.9-1.7-1.4-3-1.4-1.7 0-3 .9-3 2.1s1.2 1.8 3 2.1 3 .9 3 2.1-1.3 2.1-3 2.1c-1.3 0-2.4-.5-3-1.4" style={{ stroke: ACC }} /></>),
  sliders: (<><path d="M3.5 7h17M3.5 12h17M3.5 17h17" /><circle cx="9" cy="7" r="2.2" fill="var(--bobi-paper)" /><circle cx="15.5" cy="12" r="2.2" fill="var(--bobi-paper)" style={{ stroke: ACC }} /><circle cx="6.5" cy="17" r="2.2" fill="var(--bobi-paper)" /></>),
  chip: (<><rect x="7.5" y="7.5" width="9" height="9" rx="1.5" /><path d="M10 4.5v3M14 4.5v3M10 16.5v3M14 16.5v3M4.5 10h3M4.5 14h3M16.5 10h3M16.5 14h3" /><circle cx="12" cy="12" r="1.7" style={{ fill: ACC }} stroke="none" /></>),
  human: (<><circle cx="11" cy="8.5" r="3.2" /><path d="M4.5 19.5a6.5 6.5 0 0 1 13 0" /><rect x="16.4" y="3.4" width="4.2" height="4.2" transform="rotate(45 18.5 5.5)" style={{ stroke: ACC }} /></>),
  // ---- deployment ----
  local: (<><rect x="3.5" y="4.5" width="17" height="11.5" rx="2" /><path d="M12 16v3.5M8 19.5h8" /><circle cx="17" cy="7.5" r="1.5" style={{ fill: ACC }} stroke="none" /></>),
  cloud: (<><path d="M7 17.5a4.5 4.5 0 1 1 .9-8.9 5.5 5.5 0 0 1 10.5 1.6 3.9 3.9 0 0 1-.9 7.3H7Z" /><path d="M12 15.5v-5M9.8 12.7 12 10.5l2.2 2.2" style={{ stroke: ACC }} /></>),
  control: (<><path d="m12 3.5 8.5 5L12 13.5l-8.5-5L12 3.5Z" style={{ stroke: ACC }} /><path d="m3.5 13 8.5 5 8.5-5" /><path d="m3.5 17 8.5 5 8.5-5" /></>),
};

// Solid/rotated glyphs that don't take the shared round-cap stroke treatment.
const SPECIAL = {
  moon: { style: { stroke: ACC }, path: <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8Z" /> },
  diamond: { flat: true, path: <rect x="6.2" y="6.2" width="11.6" height="11.6" transform="rotate(45 12 12)" /> },
  moonAcc: { path: (<><path d="M20 13.4A8 8 0 1 1 10.6 4 6.4 6.4 0 0 0 20 13.4Z" /><circle cx="17.5" cy="5.5" r="1.6" style={{ fill: ACC }} stroke="none" /></>) },
  plus: { path: <path d="M12 5v14M5 12h14" /> },
  chevronRight: { path: <path d="M9.5 6 15.5 12 9.5 18" /> },
  chevronDown: { path: <path d="M6 9.5 12 15.5 18 9.5" /> },
  arrowRight: { path: (<><path d="M6.24 12h11.52" /><path d="M12.96 7.2 17.76 12l-4.8 4.8" /></>) },
  close: { path: <path d="M6 6l12 12M18 6 6 18" /> },
  search: { path: (<><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></>) },
  clock: { path: (<><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>) },
};

function Icon({ name, size = 20, color = INK, className = "", style, title }) {
  const special = SPECIAL[name];
  const body = special ? special.path : PATHS[name];
  if (!body) return null;
  const base = special && special.flat
    ? { fill: "none", strokeWidth: 1.5 }
    : S;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} className={className}
      {...base} stroke={color} style={{ ...(special && special.style), ...style }}
      role={title ? "img" : undefined} aria-hidden={title ? undefined : "true"} aria-label={title}>
      {title && <title>{title}</title>}
      {body}
    </svg>
  );
}

// GitHub is the one third-party mark in the set — it must stay the official
// silhouette, so it is a filled path rather than line art.
function GithubGlyph({ size = 16, className = "" }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} className={className} fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

const ICON_NAMES = [...Object.keys(PATHS), ...Object.keys(SPECIAL)];

Object.assign(window, { Icon, GithubGlyph, ICON_NAMES });
})();

/* ==================== surfaces/CodeCard ==================== */
(function () {

// A titled config card: uppercase mono header with a tag on the right, then a
// mono body. This is how Bobi shows YAML, markdown, and config of any kind.
function CodeCard({ title, tag, tagAccent = false, rounded = false, children, className = "", style }) {
  return (
    <div className={className} style={{ border: "1px solid var(--border-card)", background: "var(--bobi-paper)", borderRadius: rounded ? "var(--radius-md)" : 0, overflow: "hidden", boxShadow: rounded ? "var(--shadow-card)" : "none", ...style }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "10px 16px", borderBottom: "1px solid var(--border-card)", fontFamily: "var(--font-mono)", fontSize: "var(--text-plate)", letterSpacing: "var(--track-label)", textTransform: "uppercase", color: "var(--text-secondary)" }}>
        <span>{title}</span>
        {tag && <span style={{ fontSize: "var(--text-plate-sm)", color: tagAccent ? "var(--bobi-acc)" : "var(--bobi-clay)" }}>{tag}</span>}
      </div>
      <pre style={{ margin: 0, padding: "16px", overflowX: "auto", fontFamily: "var(--font-mono)", fontSize: 13, lineHeight: 1.7, color: "var(--bobi-ink)" }}>{children}</pre>
    </div>
  );
}

// YAML/markdown token spans — the four-color scheme used throughout Bobi.
function Tok({ kind = "v", children }) {
  const c = { k: "var(--bobi-clay)", v: "var(--bobi-ink)", c: "var(--text-secondary)", a: "var(--bobi-acc)" }[kind];
  return <span style={{ color: c }}>{children}</span>;
}

// A gated/enforced line: violet left border and a 5% violet wash, bleeding to
// the padding edges. The single most important visual claim Bobi makes.
function GateLine({ children }) {
  return (
    <span style={{ display: "block", margin: "0 -16px", padding: "0 16px 0 14px", borderLeft: "2px solid var(--bobi-acc)", background: "var(--bobi-acc-wash)" }}>{children}</span>
  );
}

Object.assign(window, { CodeCard, Tok, GateLine });
})();

/* ==================== surfaces/Terminal ==================== */
(function () {

// The dark inset. #0F1226 is a fixed value — it is the one cool-dark surface in
// an otherwise warm system, reserved for real shell output.
function Terminal({ children, className = "", style }) {
  return (
    <div className={className} style={{ background: "var(--surface-terminal)", color: "var(--bobi-term-fg)", fontFamily: "var(--font-mono)", fontSize: "var(--text-mono-sm)", lineHeight: 1.8, padding: "14px 16px", overflowX: "auto", ...style }}>{children}</div>
  );
}

// A prompt line: violet "$" then the command.
function TermCmd({ children }) {
  return (<><span style={{ color: "var(--bobi-acc-bright)", userSelect: "none" }} aria-hidden="true">$ </span>{children}</>);
}

// Program output. tone: dim (default), ok (warm amber), acc (violet).
function TermOut({ tone = "dim", children }) {
  const c = { dim: "var(--bobi-term-dim)", ok: "var(--bobi-term-ok)", acc: "var(--bobi-acc-bright)" }[tone];
  return <span style={{ color: c }}>{children}</span>;
}

Object.assign(window, { Terminal, TermCmd, TermOut });
})();

/* ==================== surfaces/FigNode ==================== */
(function () {

// A node card in a Bobi figure: 36px icon chip + title + mono subtitle, on a
// 10px-radius paper card with a soft warm shadow. The atom of every Bobi
// diagram — the loop, the team flows, the sleep cycle.
function FigNode({ icon, title, sub, gate = false, absolute, className = "", style }) {
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
function FigWire({ height = 12 }) {
  return <div aria-hidden="true" style={{ width: 1.5, height, margin: "0 auto", backgroundImage: "repeating-linear-gradient(to bottom, rgba(54,46,37,0.35) 0 3px, transparent 3px 7px)" }} />;
}

// A vertical flow of nodes joined by wires — the team mini-flow pattern.
function FigFlow({ nodes = [], className = "", style }) {
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

Object.assign(window, { FigNode, FigWire, FigFlow });
})();

/* ==================== surfaces/FileTree ==================== */
(function () {

// The agent workspace panel: a rounded paper card listing the agent's real files.
// The current row gets a tan wash; badges call out generated/enforced/secret files.
function FileTree({ root = "my-agent/", status, statusAccent = false, rows = [], current, onSelect, className = "", style }) {
  return (
    <div className={className} style={{ border: "1px solid rgba(54,46,37,0.14)", borderRadius: "var(--radius-xl)", background: "var(--bobi-paper)", boxShadow: "var(--shadow-panel)", padding: 12, fontFamily: "var(--font-mono)", ...style }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 10px 12px" }}>
        <span style={{ fontSize: 13, fontWeight: 500, color: "var(--bobi-ink)" }}>{root}</span>
        {status && <span style={{ fontFamily: "var(--font-sans)", fontSize: 12, color: statusAccent ? "var(--bobi-acc)" : "var(--text-secondary)" }}>{status}</span>}
      </div>
      {rows.map((r) => {
        const cur = r.label === current;
        return (
          <div key={r.label} onClick={onSelect ? () => onSelect(r.label) : undefined}
            role={onSelect ? "button" : undefined} tabIndex={onSelect ? 0 : undefined}
            onKeyDown={onSelect ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(r.label); } } : undefined}
            data-current={cur ? "true" : "false"}
            className="bobi-file-row"
            style={{ display: "flex", alignItems: "center", gap: 8, minHeight: 34, padding: "6px 10px 6px 24px", borderRadius: "var(--radius-sm)", fontSize: 12.5, color: "var(--bobi-ink)", cursor: onSelect ? "pointer" : "default" }}>
            <span style={{ minWidth: 0, wordBreak: "break-word" }}>{r.label}</span>
            {r.badge && <span style={{ marginLeft: "auto", flexShrink: 0, fontFamily: "var(--font-sans)", fontSize: 11.5, color: r.badgeAccent ? "var(--bobi-acc)" : "var(--text-secondary)" }}>{r.badge}</span>}
          </div>
        );
      })}
    </div>
  );
}

Object.assign(window, { FileTree });
})();

/* ==================== product/StatusBadge ==================== */
(function () {

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

function StatusBadge({ state = "idle", label, bare = false, className = "", style }) {
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

Object.assign(window, { StatusBadge });
})();

/* ==================== product/MetricTile ==================== */
(function () {

// One number and its label. Sentence-case sans label (mono is for data), and the
// label reserves two lines so a row of tiles shares one value baseline whatever
// labels the caller passes.
function MetricTile({ label, value, unit, delta, deltaTone = "muted", accent = false, className = "", style }) {
  const dc = { up: "var(--bobi-acc)", down: "var(--status-failed)", muted: "var(--text-secondary)" }[deltaTone];
  return (
    <div className={className} style={{
      background: "var(--surface-card)", border: "1px solid var(--border-card)",
      borderRadius: "var(--radius-lg)", boxShadow: "var(--elev-1)", padding: "14px 16px 16px", ...style,
    }}>
      <span style={{ display: "block", minHeight: 32, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, lineHeight: 1.4, letterSpacing: "-0.005em", color: "var(--text-secondary)" }}>{label}</span>
      <span style={{ display: "flex", alignItems: "baseline", gap: 5 }}>
        <span className="bobi-tnum" style={{ fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 26, lineHeight: 1, letterSpacing: "-0.025em", color: accent ? "var(--bobi-acc)" : "var(--bobi-ink)" }}>{value}</span>
        {unit && <span style={{ fontFamily: "var(--font-sans)", fontSize: 12.5, color: "var(--text-secondary)" }}>{unit}</span>}
      </span>
      {/* delta sits on its own line: a long change string must never push past
          the card edge, and tiles keep a predictable height. */}
      {delta && (
        <span className="bobi-tnum" style={{ display: "block", marginTop: 6, fontFamily: "var(--font-sans)", fontSize: 12, lineHeight: 1.3, color: dc, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{delta}</span>
      )}
    </div>
  );
}

Object.assign(window, { MetricTile });
})();

/* ==================== product/SectionLabel ==================== */
(function () {

// A small label that sits ABOVE a card or card group, outside its border. This
// is the quiet way to group an operational screen: the label belongs to the
// page, not to the card, so cards stay clean white objects with no tinted
// header bars competing for attention.
function SectionLabel({ children, action, className = "", style }) {
  return (
    <div className={className} style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, marginBottom: 10, ...style }}>
      <span style={{ fontFamily: "var(--font-sans)", fontSize: 13.5, fontWeight: 500, letterSpacing: "-0.01em", color: "var(--bobi-ink)" }}>{children}</span>
      {action}
    </div>
  );
}

Object.assign(window, { SectionLabel });
})();

/* ==================== product/Toolbar ==================== */
(function () {

// The operational toolbar: a search field that grows, optional icon buttons, and
// one primary action on the right. Keyboard hints live inside the field — a
// small enterprise affordance that signals the app is meant to be driven fast.
function Toolbar({ placeholder = "Search", value, onChange, keyHint, tools, primary, className = "", style }) {
  return (
    <div className={className} style={{ display: "flex", alignItems: "center", gap: 10, ...style }}>
      <span className="bobi-field" style={{ display: "flex", alignItems: "center", gap: 9, flex: 1, minWidth: 0, padding: "0 11px", height: 36, background: "var(--surface-card)", border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)", boxShadow: "var(--elev-1)" }}>
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="var(--text-secondary)" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true" style={{ flexShrink: 0 }}>
          <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" />
        </svg>
        <input value={value} onChange={onChange} placeholder={placeholder}
          style={{ flex: 1, minWidth: 0, border: "none", outline: "none", background: "transparent", fontFamily: "var(--font-sans)", fontSize: 14, color: "var(--bobi-ink)" }} />
        {keyHint && <KeyHint>{keyHint}</KeyHint>}
      </span>
      {tools}
      {primary}
    </div>
  );
}

// A keyboard shortcut chip.
function KeyHint({ children }) {
  return (
    <span aria-hidden="true" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 19, height: 19, padding: "0 5px", flexShrink: 0, border: "1px solid var(--border-hairline)", borderRadius: 5, background: "var(--surface-inset)", fontFamily: "var(--font-mono)", fontSize: 10.5, color: "var(--text-secondary)" }}>{children}</span>
  );
}

// A square icon-only button, for filters and view toggles beside the search.
function IconButton({ icon, label, onClick, active = false, className = "", style }) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label} className={`bobi-icon-btn ${className}`}
      style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 36, height: 36, flexShrink: 0, cursor: "pointer",
        background: active ? "var(--surface-inset)" : "var(--surface-card)",
        border: "1px solid var(--border-strong)", borderRadius: "var(--radius-sm)",
        boxShadow: "var(--elev-1)", color: "var(--bobi-ink)" }}>{icon}</button>
  );
}

Object.assign(window, { Toolbar, KeyHint, IconButton });
})();

/* ==================== product/PageHeader ==================== */
(function () {

// App page header. The title sets in DISPLAY CAPS with open tracking — caps read
// as chrome rather than editorial copy, which is what keeps a dense enterprise
// screen from feeling like a marketing page. Marketing headings stay sentence
// case; this treatment is product-only.
function PageHeader({ title, sub, breadcrumb, actions, tabs, activeTab, onTab, className = "", style }) {
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

Object.assign(window, { PageHeader });
})();

/* ==================== product/EventRow ==================== */
(function () {

// A row in the event queue. Hover/selected/focus states live in tokens/app.css
// (.bobi-row) — a table row that doesn't respond to the cursor reads broken.
// Rows are real buttons for the keyboard: tabbable, Enter/Space activate.
//
// Column model: source glyph · event (grows) · agent · workflow · state · time.
// The event title is the ONLY cell allowed to truncate. Agent and workflow are
// sized to their real content (agent names and workflow filenames are short and
// known) — three ellipses in one row means the column model is wrong, not the
// content.
const GRID = "20px minmax(120px,1fr) 82px 140px 74px 60px 16px";

function EventRow({ icon, source, title, agent, workflow, status, time, selected = false, onClick, className = "", style }) {
  const activate = (e) => {
    if (!onClick) return;
    if (e.type === "click" || e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); }
  };
  return (
    <div
      role={onClick ? "button" : "row"}
      tabIndex={onClick ? 0 : undefined}
      data-selected={selected ? "true" : "false"}
      onClick={activate}
      onKeyDown={activate}
      className={`bobi-row ${className}`}
      style={{
        display: "grid", gridTemplateColumns: GRID, alignItems: "center", gap: 12,
        minHeight: 52, padding: "8px 14px", borderTop: "1px solid var(--border-hairline)",
        cursor: onClick ? "pointer" : "default", ...style,
      }}
    >
      <span aria-hidden="true" style={{ display: "flex", justifyContent: "center", color: "var(--text-secondary)" }}>{icon}</span>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: 500, letterSpacing: "-0.006em", color: "var(--bobi-ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
        {source && <span style={{ display: "block", marginTop: 1, fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{source}</span>}
      </span>
      <span style={{ fontFamily: "var(--font-sans)", fontSize: 13, color: agent ? "var(--bobi-ink)" : "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent || "—"}</span>
      <span title={typeof workflow === "string" ? workflow : undefined} style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{workflow || "—"}</span>
      <span>{status}</span>
      <span className="bobi-tnum" style={{ fontFamily: "var(--font-sans)", fontSize: 12.5, color: "var(--text-secondary)", textAlign: "right" }}>{time}</span>
      <span className="bobi-row-go" aria-hidden="true" style={{ display: "flex", justifyContent: "flex-end", color: "var(--text-secondary)" }}>
        <svg viewBox="0 0 20 20" width="14" height="14" fill="none"><path d="M8 6l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </span>
    </div>
  );
}

// Column header. Sentence-case sans at 11px — one uppercase treatment per app
// (the page title), so headers stay quiet.
function EventRowHeader({ columns = ["", "Event", "Agent", "Workflow", "State", "Time", ""], className = "", style }) {
  return (
    <div role="row" className={className} style={{
      display: "grid", gridTemplateColumns: GRID, alignItems: "center", gap: 12,
      padding: "9px 14px", background: "var(--surface-inset)",
      borderBottom: "1px solid var(--border-hairline)",
      fontFamily: "var(--font-sans)", fontSize: 11.5, fontWeight: 500,
      letterSpacing: "0.01em", color: "var(--text-secondary)", ...style,
    }}>
      {columns.map((c, i) => <span key={i} style={{ textAlign: i === 5 ? "right" : "left" }}>{c}</span>)}
    </div>
  );
}

Object.assign(window, { EventRow, EventRowHeader });
})();

/* ==================== product/GateApproval ==================== */
(function () {

// The human gate, surfaced as UI. This is the most important object in the
// product: the violet rail + wash says "the run is stopped here until a person
// decides." Never render an approval as a plain dialog or a toast.
function GateApproval({ title, workflow, step, detail, children, onApprove, onReject, approveLabel = "Approve", rejectLabel = "Reject", decided, decidedBy, expiresIn, className = "", style }) {
  return (
    <div className={className} style={{
      border: "1px solid var(--border-gate)", borderLeft: "2px solid var(--bobi-acc)",
      background: "color-mix(in srgb, var(--bobi-acc) 4%, var(--bobi-paper))",
      borderRadius: "var(--radius-lg)", padding: "20px 22px", ...style,
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontFamily: "var(--font-sans)", fontSize: 13, fontWeight: 500, color: "var(--bobi-acc)" }}>
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <rect x="6.2" y="6.2" width="11.6" height="11.6" transform="rotate(45 12 12)" />
          </svg>
          Awaiting approval
        </span>
        {workflow && <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--text-secondary)" }}>{workflow}{step ? ` · ${step}` : ""}</span>}
      </div>
      {title && <h4 style={{ marginTop: 12, fontFamily: "var(--font-display)", fontWeight: 600, fontSize: 20, lineHeight: 1.14, letterSpacing: "var(--track-tightest)", color: "var(--bobi-ink)" }}>{title}</h4>}
      {detail && <p style={{ marginTop: 8, fontSize: 14.5, lineHeight: 1.6, color: "var(--text-secondary)" }}>{detail}</p>}
      {children && <div style={{ marginTop: 16 }}>{children}</div>}
      {decided ? (
        <p style={{ marginTop: 16, fontFamily: "var(--font-sans)", fontSize: 13, color: decided === "approved" ? "var(--bobi-acc)" : "var(--status-failed)" }}>
          {decided === "approved" ? "Approved" : "Rejected"}
          <span style={{ color: "var(--text-secondary)" }}>
            {decidedBy ? ` by ${decidedBy}` : ""} · the run {decided === "approved" ? "continued" : "halted"}
          </span>
        </p>
      ) : (
        <div style={{ marginTop: 18, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <button type="button" onClick={onApprove} className="bobi-btn" data-variant="primary"
            style={{ display: "inline-flex", alignItems: "center", gap: 8, height: 36, padding: "0 18px", border: "1px solid transparent", borderRadius: "var(--radius-sm)", background: "var(--bobi-acc)", color: "var(--bobi-paper)", boxShadow: "var(--elev-1)", fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: 500, letterSpacing: "-0.006em", cursor: "pointer" }}>{approveLabel}</button>
          <button type="button" onClick={onReject} className="bobi-btn" data-variant="ghost"
            style={{ display: "inline-flex", alignItems: "center", gap: 8, height: 36, padding: "0 18px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-strong)", background: "var(--surface-card)", color: "var(--bobi-ink)", fontFamily: "var(--font-sans)", fontSize: 14, fontWeight: 500, letterSpacing: "-0.006em", cursor: "pointer" }}>{rejectLabel}</button>
          {expiresIn && (
            <span style={{ marginLeft: "auto", fontFamily: "var(--font-sans)", fontSize: 12.5, color: "var(--text-secondary)" }}>
              Halted {expiresIn} · nothing proceeds until you decide
            </span>
          )}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { GateApproval });
})();

/* ==================== product/SideNav ==================== */
(function () {

// Product sidebar. tone="light" (default) keeps chrome in the same family as the
// content so a dense app reads as one calm surface; tone="void" is the
// brand-forward pairing for login and demo shells.
//
// Labels are SANS, not mono: mono is for data (ids, paths, timestamps), and a
// mono nav makes an app read as terminal cosplay. Hover/current/focus come from
// .bobi-nav-item in tokens/app.css.
function SideNav({ items = [], active, onSelect, footer, agent, tone = "light", className = "", style }) {
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

Object.assign(window, { SideNav });
})();

