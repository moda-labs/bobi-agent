/**
 * Run, agent, and monitor state. The state→color map is fixed and must not be
 * re-skinned: violet = live, clay = waiting on a human, ink = done, muted =
 * idle, brick = failed.
 *
 * "waiting" is clay rather than violet on purpose — violet means the system is
 * working; clay means a person is the blocker.
 *
 * Rendered as a sentence-case sans pill. Mono is reserved for data (ids, paths,
 * timestamps); a status word is a label, not data. Terminal states (done, idle)
 * drop their fill entirely so only ACTIVE states carry color — a table of 40
 * finished runs should be quiet.
 *
 * @startingPoint section="Product" subtitle="Status, rows, and approval gates" viewport="700x300"
 */
export interface StatusBadgeProps {
  state?: "live" | "waiting" | "done" | "idle" | "failed";
  /** Override the displayed word (state still drives the color). */
  label?: string;
  /** Drop the pill fill and border — text + dot only, for tight inline use. */
  bare?: boolean;
  className?: string;
  style?: React.CSSProperties;
}
export function StatusBadge(props: StatusBadgeProps): JSX.Element;
