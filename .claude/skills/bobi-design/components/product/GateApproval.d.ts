/**
 * A human approval gate, surfaced as UI. The most important object in the
 * product: a run is STOPPED here until a person decides.
 *
 * Adherence rules:
 * - Always carries the violet left rail + 4% wash + rotated-square glyph. That
 *   combination is reserved for gates and must not be used decoratively.
 * - Never surface an approval as a toast, a snackbar, or an auto-dismissing
 *   dialog — a gate that can be missed is not a gate.
 * - Always name the workflow and step that stopped, so the decision is auditable.
 * - Show the diff/payload being approved via `children` (usually a CodeCard).
 */
export interface GateApprovalProps {
  /** What is being approved, in plain words. */
  title?: React.ReactNode;
  /** Workflow file that stopped, e.g. "support-triage.yaml". */
  workflow?: string;
  /** The step id inside that workflow. */
  step?: string;
  /** Why this needs a person. */
  detail?: React.ReactNode;
  /** The payload under review — usually a CodeCard or Terminal. */
  children?: React.ReactNode;
  onApprove?: () => void;
  onReject?: () => void;
  approveLabel?: string;
  rejectLabel?: string;
  /** Replace the buttons with a resolved outcome line. */
  decided?: "approved" | "rejected";
  /** Who decided — always record it; a gate without an audit trail isn't a control. */
  decidedBy?: string;
  /** How long the run has been halted, e.g. "14m". Reinforces that nothing proceeds. */
  expiresIn?: string;
  className?: string;
  style?: React.CSSProperties;
}
export function GateApproval(props: GateApprovalProps): JSX.Element;
