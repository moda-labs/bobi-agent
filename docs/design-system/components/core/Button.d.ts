/**
 * The Bobi button. 6px radius, Inter medium, -0.01em tracking, 0.97 press-scale.
 *
 * Hover/active/focus come from the `.bobi-btn` class in tokens/app.css. Do not
 * reimplement them inline or with JS mouse handlers.
 *
 * Adherence rules:
 * - Exactly ONE primary (violet) button per view. Violet means "the action".
 * - `inverse` is for dark imagery only; `glass` for copy sitting over a photo.
 * - Never use Moda's red (#FD4235) on a Bobi surface.
 * - Never round more than 6px, and never add a gradient fill.
 *
 * @startingPoint section="Core" subtitle="Button variants and sizes" viewport="700x180"
 */
export interface ButtonProps extends React.HTMLAttributes<HTMLElement> {
  variant?: "primary" | "inverse" | "quiet" | "glass" | "ghost";
  size?: "sm" | "md" | "lg";
  /** Render as another element, e.g. "a" for links. Default "button". */
  as?: "button" | "a";
  /** Leading icon node, usually 15-16px. */
  icon?: React.ReactNode;
  /** Trailing node — a chevron for a split action, or a KeyHint. */
  trailing?: React.ReactNode;
  disabled?: boolean;
  href?: string;
  children?: React.ReactNode;
}
export function Button(props: ButtonProps): JSX.Element;
