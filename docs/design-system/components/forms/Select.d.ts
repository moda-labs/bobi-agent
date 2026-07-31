/**
 * Native select in Bobi clothing — mono value, hairline border, chevron in muted.
 * Use for bounded config choices: runtime, model, role, approval mode.
 */
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  hint?: string;
  /** Strings, or {label, value} pairs. */
  options?: Array<string | { label: string; value: string }>;
}
export function Select(props: SelectProps): JSX.Element;
