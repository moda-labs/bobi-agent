/**
 * Uppercase mono tag. Tone carries meaning in Bobi and must not be chosen for
 * looks: `accent` (violet) = enforced / live / gated, `clay` = category or
 * label, `muted` = neutral metadata, `dark` = inverted chip or count.
 *
 * Badges carry a soft tint AND a matching border so they read as objects in a
 * dense table, not as loose colored text.
 *
 * @startingPoint section="Core" subtitle="Badge tones" viewport="700x150"
 */
export interface BadgeProps {
  children?: React.ReactNode;
  tone?: "muted" | "clay" | "accent" | "dark";
  /** Drop the fill, border, and padding — text only, for figure-header corners. */
  plain?: boolean;
  /** Leading 5px dot in the current color. */
  dot?: boolean;
  /** Fully rounded. Use for soft product labels ("Beta", "Hobby"), not statuses. */
  pill?: boolean;
  /** Set false for sentence-case sans instead of uppercase mono. Default true. */
  caps?: boolean;
  className?: string;
  style?: React.CSSProperties;
}
export function Badge(props: BadgeProps): JSX.Element;
