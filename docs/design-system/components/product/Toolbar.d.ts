/**
 * The operational toolbar: a growing search field, optional icon buttons, and one
 * primary action. Sits directly under the PageHeader on list screens.
 *
 * @startingPoint section="Product" subtitle="Search toolbar with key hint" viewport="700x150"
 */
export interface ToolbarProps {
  placeholder?: string;
  value?: string;
  onChange?: (e: React.ChangeEvent<HTMLInputElement>) => void;
  /** Keyboard shortcut shown inside the field, e.g. "F" or "⌘K". */
  keyHint?: string;
  /** IconButtons rendered between the field and the primary action. */
  tools?: React.ReactNode;
  /** The single primary Button. */
  primary?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}
export function Toolbar(props: ToolbarProps): JSX.Element;

export interface KeyHintProps { children?: React.ReactNode }
/** A keyboard shortcut chip. Decorative — always aria-hidden. */
export function KeyHint(props: KeyHintProps): JSX.Element;

export interface IconButtonProps {
  icon?: React.ReactNode;
  /** Required — becomes both aria-label and tooltip. */
  label: string;
  onClick?: () => void;
  active?: boolean;
  className?: string;
  style?: React.CSSProperties;
}
export function IconButton(props: IconButtonProps): JSX.Element;
