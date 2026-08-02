/**
 * Bordered mono filename chip — `agent.yaml`, `workflows/`, `policy.md`.
 *
 * Bobi's config IS its product surface, so filenames appear constantly in copy.
 * Use this instead of backtick code styling whenever a real file is named.
 * Inside a heading pass `inHeading` so it scales with the type (0.62em).
 */
export interface FileChipProps {
  children?: React.ReactNode;
  /** Scale to 0.62em and nudge up 1px, for use inside a display heading. */
  inHeading?: boolean;
  /** Violet border + text, for a file that carries a gate or an enforced rule. */
  accent?: boolean;
  className?: string;
  style?: React.CSSProperties;
}
export function FileChip(props: FileChipProps): JSX.Element;
