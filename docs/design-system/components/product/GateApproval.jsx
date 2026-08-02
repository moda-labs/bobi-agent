import React from "react";

// The human gate, surfaced as UI. This is the most important object in the
// product: the violet rail + wash says "the run is stopped here until a person
// decides." Never render an approval as a plain dialog or a toast.
export function GateApproval({ title, workflow, step, detail, children, onApprove, onReject, approveLabel = "Approve", rejectLabel = "Reject", decided, decidedBy, expiresIn, className = "", style }) {
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
