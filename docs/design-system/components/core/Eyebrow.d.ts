/**
 * Section eyebrow: 10px violet square + uppercase mono label, 20px above the
 * heading it introduces. Write the label in CAPS.
 *
 * The square is violet on Bobi surfaces (it is red on Moda Labs surfaces — do
 * not mix the two systems).
 */
export interface EyebrowProps {
  children?: React.ReactNode;
  /** Square color. Default var(--bobi-acc). */
  color?: string;
  className?: string;
  style?: React.CSSProperties;
}
export function Eyebrow(props: EyebrowProps): JSX.Element;
