/**
 * The product's sidebar.
 *
 * `tone="light"` (default) keeps chrome in the same family as the content, which
 * is what makes a dense operational app read as one calm surface — the right
 * choice for the control plane. `tone="void"` is the brand-forward option,
 * reproducing the marketing site's paper/void pairing; use it for login,
 * marketing-adjacent shells, and demo surfaces.
 *
 * Requirements: it carries the Bobi lockup AND the "BY MODA LABS ↗" byline.
 * Nav labels are lowercase mono. A violet count means something needs a person
 * (gates awaiting approval) — never use violet for a neutral total.
 */
export interface SideNavItem {
  id: string;
  label: string;
  /** An <Icon /> at size 16-18. */
  icon?: React.ReactNode;
  /** Trailing count. */
  count?: number | string;
  /** Render the count as a violet pill — reserve for "needs a human". */
  countAccent?: boolean;
  /** Show a trailing chevron for a section that opens a sub-view. */
  expandable?: boolean;
}
export interface SideNavProps {
  items?: SideNavItem[];
  active?: string;
  onSelect?: (id: string) => void;
  /** Mono footer line, e.g. a version or deploy target. */
  footer?: React.ReactNode;
  /** Current agent name, shown in a switcher box above the nav. */
  agent?: string;
  /** light = same family as content (default) · void = brand-forward dark. */
  tone?: "light" | "void";
  className?: string;
  style?: React.CSSProperties;
}
export function SideNav(props: SideNavProps): JSX.Element;
