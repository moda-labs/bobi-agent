/**
 * One number and its label, in a low-elevation card. Use a row of 3-4 at the top
 * of an operational screen so the page has an anchor before the table begins.
 *
 * The label reserves two lines of height, so values share a baseline across a
 * row no matter how long the labels are — a row of tiles must read as one
 * scanline. Keep `delta` to a short phrase; it is set to never wrap.
 *
 * Values are tabular-figure so a row of tiles aligns optically. Only make a
 * value violet (`accent`) when it is a count of things needing a human — the
 * same rule the rest of the system follows.
 */
export interface MetricTileProps {
  /** Uppercase mono label. */
  label: string;
  value: React.ReactNode;
  /** Trailing unit, e.g. "runs", "min". */
  unit?: string;
  /** Right-aligned change indicator, e.g. "+3 today". */
  delta?: string;
  deltaTone?: "up" | "down" | "muted";
  /** Violet value — reserve for "needs a human" counts. */
  accent?: boolean;
  className?: string;
  style?: React.CSSProperties;
}
export function MetricTile(props: MetricTileProps): JSX.Element;
