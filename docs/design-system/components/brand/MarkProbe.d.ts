/**
 * The canonical Bobi probe mark (body + dashed orbit + violet probe dot).
 *
 * Never redraw, re-proportion, or restyle this geometry — every Bobi surface
 * must render the same mark. Use `ink` to flip it for light surfaces.
 *
 * @startingPoint section="Brand" subtitle="The canonical probe mark" viewport="700x150"
 */
export interface MarkProbeProps {
  /** Rendered square size in px. Default 32. */
  size?: number;
  /** Body + orbit color. Default paper (for dark surfaces); pass var(--bobi-ink) on light. */
  ink?: string;
  /** Probe-dot color. Default var(--bobi-acc-bright); use var(--bobi-acc) on light surfaces. */
  accent?: string;
  className?: string;
  style?: React.CSSProperties;
}
export function MarkProbe(props: MarkProbeProps): JSX.Element;
