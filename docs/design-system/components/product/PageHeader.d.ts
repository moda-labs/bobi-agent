/**
 * App page header: optional breadcrumb, a lowercase title, sub-line, actions,
 * and an optional underlined tab row.
 *
 * The title renders LOWERCASE. Bobi's whole vocabulary is lowercase — the
 * wordmark, agent names, roles, filenames, CLI verbs — so a lowercase page title
 * speaks in the product's own voice and keeps the app layer free of shouty
 * uppercase. Pass normal text; the component lowercases it.
 *
 * Marketing headings are different: sentence case with a clay highlight clause.
 *
 * The active tab is marked with a 2px violet inset underline (state), never a
 * filled pill. Tabs are lowercase sans, matching the title.
 *
 * @startingPoint section="Product" subtitle="Caps page header with tabs" viewport="700x220"
 */
export interface PageHeaderTab { id: string; label: string; count?: number | string }
export interface PageHeaderProps {
  /** Rendered lowercase automatically — pass normal text. */
  title: React.ReactNode;
  /** One-line description under the title. */
  sub?: React.ReactNode;
  /** Path segments, last one reads as current. */
  breadcrumb?: React.ReactNode[];
  /** Buttons, aligned right. */
  actions?: React.ReactNode;
  tabs?: PageHeaderTab[];
  activeTab?: string;
  onTab?: (id: string) => void;
  className?: string;
  style?: React.CSSProperties;
}
export function PageHeader(props: PageHeaderProps): JSX.Element;
