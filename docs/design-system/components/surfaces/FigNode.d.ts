/**
 * The atom of every Bobi diagram: a floating paper card with a 36px icon chip,
 * an Inter-medium title, and a mono subtitle.
 *
 * Titles are lowercase system names ("linear", "director", "engineer agent");
 * subtitles are concrete mono detail ("ENG-142 assigned", "02:00 · sessions →
 * policy.md"). Pass `gate` for a node a human must clear — it picks up the
 * violet border and a 4% violet wash.
 *
 * @startingPoint section="Figures" subtitle="Node, wire, and a vertical flow" viewport="700x340"
 */
export interface FigNodeProps {
  /** An <Icon /> at size 18. */
  icon?: React.ReactNode;
  /** Lowercase system name. */
  title?: React.ReactNode;
  /** Mono detail line. */
  sub?: React.ReactNode;
  /** Violet border + wash: a human approval step. */
  gate?: boolean;
  /** Absolutely position inside a fixed-size figure canvas. */
  absolute?: { left: number; top: number; width: number; height: number };
  className?: string;
  style?: React.CSSProperties;
}
export function FigNode(props: FigNodeProps): JSX.Element;

export interface FigWireProps { height?: number }
/** The vertical dashed connector between stacked nodes. */
export function FigWire(props: FigWireProps): JSX.Element;

export interface FigFlowProps {
  /** FigNode prop objects, rendered top-to-bottom with wires between. */
  nodes?: FigNodeProps[];
  className?: string;
  style?: React.CSSProperties;
}
export function FigFlow(props: FigFlowProps): JSX.Element;
