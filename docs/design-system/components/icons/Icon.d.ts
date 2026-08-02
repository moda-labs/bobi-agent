/**
 * Bobi's icon set — hand-drawn architect line art ported verbatim from the
 * product source. Ink strokes at 1.5 with round caps, and at most ONE violet
 * accent per glyph.
 *
 * Adherence rules:
 * - Do NOT introduce Lucide, Heroicons, Feather, or any other pack alongside
 *   these; the hand-drawn stroke personality is the brand.
 * - Do NOT add a second accent color to a glyph.
 * - Need a glyph that doesn't exist? Draw it in this same language (1.5 ink
 *   stroke, round caps, 24x24, one optional violet detail) and add it here.
 *
 * @startingPoint section="Brand" subtitle="The full Bobi icon set" viewport="700x300"
 */
export interface IconProps {
  /** One of ICON_NAMES. */
  name:
    | "ticket" | "bell" | "mail" | "chat" | "queue"
    | "code" | "pulse" | "headset" | "checklist"
    | "checkCircle" | "plane"
    | "sessions" | "tray" | "sunrise"
    | "workflow" | "parallel" | "bus" | "coin" | "sliders" | "chip" | "human"
    | "local" | "cloud" | "control"
    | "moon" | "moonAcc" | "diamond" | "plus" | "chevronRight" | "chevronDown"
    | "arrowRight" | "close" | "search" | "clock";
  /** Square size in px. Established sizes: 18 (fig chip), 20 (UI), 23 (tile). */
  size?: number;
  /**
   * Stroke color for the ink linework. Defaults to var(--bobi-ink) for paper
   * surfaces — pass "currentColor" (or a paper value) on the void sidebar and
   * any other dark surface, or the glyph renders ink-on-ink and disappears.
   * The violet accent details are NOT affected by this prop.
   */
  color?: string;
  className?: string;
  style?: React.CSSProperties;
  /** Supply only when the icon carries meaning no adjacent text conveys. */
  title?: string;
}
export function Icon(props: IconProps): JSX.Element | null;

export interface GithubGlyphProps { size?: number; className?: string }
/** The official GitHub mark — the one filled, third-party glyph in the set. */
export function GithubGlyph(props: GithubGlyphProps): JSX.Element;

export const ICON_NAMES: string[];
