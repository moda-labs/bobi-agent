import React from "react";

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

export function Icon({ name, size = 20, color = INK, className = "", style, title }) {
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
export function GithubGlyph({ size = 16, className = "" }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} className={className} fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

export const ICON_NAMES = [...Object.keys(PATHS), ...Object.keys(SPECIAL)];
