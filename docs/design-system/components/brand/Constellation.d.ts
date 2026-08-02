/**
 * The Bobi hero star chart — an event fanning through relays to a gate and an
 * outcome. Decorative brand ornament for dark surfaces ONLY (its node fills and
 * label colors assume a night-sky background).
 *
 * Wrap it in an element carrying the `bobi-drift` animation for the slow
 * ambient drift; the component itself owns the ping rings and traveling signal.
 */
export interface ConstellationProps {
  width?: number;
  height?: number;
  /** Accent for the gate, outcome, pings, and traveling signal. */
  accent?: string;
  /** Set false to render the static chart with no pings or signal. Default true. */
  animate?: boolean;
  className?: string;
}
export function Constellation(props: ConstellationProps): JSX.Element;
