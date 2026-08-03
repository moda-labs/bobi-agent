/**
 * The Bobi wordmark lockup: probe mark + lowercase "bobi".
 *
 * Always lowercase, always Geist semibold at -0.045em. The name is never
 * title-cased in a lockup (body copy may write "Bobi" as a sentence subject).
 *
 * @startingPoint section="Brand" subtitle="Wordmark lockup, any size" viewport="700x150"
 */
export interface BobiLockupProps {
  /** Drives every dimension. Number (px) or a CSS length/clamp() string. Default 27. */
  fontSize?: number | string;
  /** Wordmark + mark body color. Default paper. */
  ink?: string;
  /** Probe-dot color. Default var(--bobi-acc-bright). */
  accent?: string;
  className?: string;
}
export function BobiLockup(props: BobiLockupProps): JSX.Element;
