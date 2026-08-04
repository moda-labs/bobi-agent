/**
 * Editorial eyebrow: 10px clay square + uppercase mono label, 20px above the
 * marketing heading it introduces. Write the label in CAPS.
 *
 * Product screens use the sentence-case `SectionLabel` instead. Violet is
 * reserved for live, selected, focused, gated, and enforced state.
 */
export interface EyebrowProps {
  children?: React.ReactNode;
  /** Square color. Default var(--bobi-clay). */
  color?: string;
  className?: string;
  style?: React.CSSProperties;
}
export function Eyebrow(props: EyebrowProps): JSX.Element;
