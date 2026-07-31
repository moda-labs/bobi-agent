/**
 * A sentence-case label above a card or card group, rendered OUTSIDE the card's
 * border.
 *
 * Prefer this over a tinted header bar inside the card: it keeps cards as clean
 * white objects and lets a screen carry several groups without visual noise.
 * Reserve the uppercase-mono treatment for page titles (PageHeader) and column
 * headers — not for these.
 */
export interface SectionLabelProps {
  children?: React.ReactNode;
  /** Right-aligned link or count, baseline-aligned with the label. */
  action?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}
export function SectionLabel(props: SectionLabelProps): JSX.Element;
