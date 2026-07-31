/**
 * The terminal inset — Bobi's only cool-dark surface (#0F1226), reserved for
 * real shell interaction. Never use it as a generic dark panel or a code block
 * for config (config goes in CodeCard on paper).
 *
 * Square corners, no border. The "$" is always violet; successful results are
 * warm amber; ambient log lines are dim paper.
 */
export interface TerminalProps {
  children?: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}
export function Terminal(props: TerminalProps): JSX.Element;

export interface TermCmdProps { children?: React.ReactNode }
/** A prompt line — renders the violet "$" then your command text. */
export function TermCmd(props: TermCmdProps): JSX.Element;

export interface TermOutProps {
  /** dim = ambient log (default), ok = warm amber success, acc = violet emphasis. */
  tone?: "dim" | "ok" | "acc";
  children?: React.ReactNode;
}
export function TermOut(props: TermOutProps): JSX.Element;
