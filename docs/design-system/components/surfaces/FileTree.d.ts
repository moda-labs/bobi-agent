/**
 * The agent workspace panel — a flat list of the agent's real files with an
 * optional status word in the corner ("scaffold", "● on the clock").
 *
 * Bobi has no hidden state: an agent IS its files, so this panel is the honest
 * navigator for agent config. Rows are flat paths (`roles/engineer/ROLE.md`),
 * not an expandable tree.
 */
export interface FileTreeRow {
  /** Full path as displayed, e.g. "workflows/support-triage.yaml". */
  label: string;
  /** Right-aligned note: "generated", "enforced", "secrets". */
  badge?: string;
  /** Render the badge in violet (use for "enforced"). */
  badgeAccent?: boolean;
}
export interface FileTreeProps {
  /** Agent directory name. Default "my-agent/". */
  root?: string;
  /** Corner status word. */
  status?: string;
  /** Render the status in violet — use when the agent is running. */
  statusAccent?: boolean;
  rows?: FileTreeRow[];
  /** Label of the highlighted row. */
  current?: string;
  onSelect?: (label: string) => void;
  className?: string;
  style?: React.CSSProperties;
}
export function FileTree(props: FileTreeProps): JSX.Element;
