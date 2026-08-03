/**
 * Text field. Mono by default — Bobi inputs mostly hold file paths, event ids,
 * cron expressions, and shell commands. Pass `mono={false}` for prose fields.
 *
 * The LABEL is sentence-case sans and the VALUE is mono — mono earns its place
 * on data, not on chrome. Focus lights the whole field (`.bobi-field`).
 *
 * @startingPoint section="Forms" subtitle="Input, Select, Switch" viewport="700x260"
 */
export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  /** Uppercase mono label rendered above the field. */
  label?: string;
  /** Mono helper or error text below the field. */
  hint?: string;
  /** Use the mono family inside the field. Default true. */
  mono?: boolean;
  /** Red border + red hint. */
  invalid?: boolean;
  /** Fixed violet leading glyph, e.g. "$" for a command field. */
  prefix?: React.ReactNode;
  /** Trailing node inside the field — a unit, a KeyHint, or a small action. */
  suffix?: React.ReactNode;
}
export function Input(props: InputProps): JSX.Element;
