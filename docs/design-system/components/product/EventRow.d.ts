/**
 * A row in the event queue — the product's primary list. Every Bobi run starts
 * as an event, so this row reads left to right in the product's own grammar:
 * source → event → agent → workflow → state → time.
 *
 * Hairline dividers, no zebra striping. Selection is a violet wash plus a 2px
 * inset rail, never an outline.
 *
 * Rows are keyboard-operable buttons (tabbable, Enter/Space) and take their
 * hover/selected/focus styling from `.bobi-row` in tokens/app.css. A trailing
 * chevron fades in on hover as the affordance.
 *
 * Only the event title truncates. Agent and workflow columns are sized to their
 * real content — several ellipses in one row is a column-model problem.
 */
export interface EventRowProps {
  /** Source glyph — <Icon name="ticket" size={18} /> etc. */
  icon?: React.ReactNode;
  /** Mono source line under the title, e.g. "linear · ENG-142". */
  source?: string;
  /** The event in plain words. */
  title?: React.ReactNode;
  /** Agent that picked it up, or omit for unassigned. */
  agent?: string;
  /** Workflow file driving it. */
  workflow?: string;
  /** A <StatusBadge />. */
  status?: React.ReactNode;
  /** Relative time, e.g. "2m ago". */
  time?: string;
  selected?: boolean;
  onClick?: () => void;
  className?: string;
  style?: React.CSSProperties;
}
export function EventRow(props: EventRowProps): JSX.Element;

export interface EventRowHeaderProps { columns?: string[]; className?: string; style?: React.CSSProperties }
/** Column header matching EventRow's grid. */
export function EventRowHeader(props: EventRowHeaderProps): JSX.Element;
