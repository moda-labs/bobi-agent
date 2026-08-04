/**
 * A sentence-case label above a card or card group, rendered OUTSIDE the card's
 * border.
 *
 * Prefer this over a tinted header bar inside the card: it keeps cards as clean
 * white objects and lets a screen carry several groups without visual noise.
 * Reserve uppercase mono for figure plates, table columns, and corner marks —
 * not page titles or these group labels.
 */
export interface SectionLabelProps {
  children?: React.ReactNode;
  /** Right-aligned link or count, baseline-aligned with the label. */
  action?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}
export function SectionLabel(props: SectionLabelProps): JSX.Element;
