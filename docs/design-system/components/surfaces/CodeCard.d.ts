/**
 * A titled config card — the primary way Bobi renders YAML, markdown, and any
 * declared configuration. Square corners by default (the figure-plate look);
 * pass `rounded` for the floating 8px card used in the walkthrough.
 *
 * Token colors are fixed and meaningful: clay = key, ink = value,
 * muted = comment, violet = an enforced/gated line. Use `Tok` and `GateLine`
 * rather than inventing your own syntax palette.
 *
 * @startingPoint section="Surfaces" subtitle="Config card with a gated line" viewport="700x300"
 */
export interface CodeCardProps {
  /** Usually the real filename, e.g. "workflows/support-triage.yaml". */
  title: string;
  /** Right-side status word: "Runtime", "Enforced", "Watching", "Generated". */
  tag?: string;
  /** Render the tag in violet (use for "Enforced" / "Generated" / gated states). */
  tagAccent?: boolean;
  /** 8px radius + drop shadow, for a card that floats over a section. */
  rounded?: boolean;
  children?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}
export function CodeCard(props: CodeCardProps): JSX.Element;

export interface TokProps {
  /** k = key (clay), v = value (ink), c = comment (muted), a = accent (violet). */
  kind?: "k" | "v" | "c" | "a";
  children?: React.ReactNode;
}
export function Tok(props: TokProps): JSX.Element;

export interface GateLineProps { children?: React.ReactNode }
/** Wraps a config line in the violet left-border + wash treatment: this step is enforced. */
export function GateLine(props: GateLineProps): JSX.Element;
