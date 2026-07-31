import React from "react";

// The dark inset. #0F1226 is a fixed value — it is the one cool-dark surface in
// an otherwise warm system, reserved for real shell output.
export function Terminal({ children, className = "", style }) {
  return (
    <div className={className} style={{ background: "var(--surface-terminal)", color: "var(--bobi-term-fg)", fontFamily: "var(--font-mono)", fontSize: "var(--text-mono-sm)", lineHeight: 1.8, padding: "14px 16px", overflowX: "auto", ...style }}>{children}</div>
  );
}

// A prompt line: violet "$" then the command.
export function TermCmd({ children }) {
  return (<><span style={{ color: "var(--bobi-acc-bright)", userSelect: "none" }} aria-hidden="true">$ </span>{children}</>);
}

// Program output. tone: dim (default), ok (warm amber), acc (violet).
export function TermOut({ tone = "dim", children }) {
  const c = { dim: "var(--bobi-term-dim)", ok: "var(--bobi-term-ok)", acc: "var(--bobi-acc-bright)" }[tone];
  return <span style={{ color: c }}>{children}</span>;
}
