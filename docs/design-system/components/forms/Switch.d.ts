/**
 * On/off control for a monitor, a service, or a gate. Track is 6px-radius (not
 * a pill) and turns violet when on.
 *
 * Never use a Switch for an irreversible action — those go through GateApproval.
 */
export interface SwitchProps {
  checked?: boolean;
  onChange?: (next: boolean) => void;
  /** Inter medium label to the right of the track. */
  label?: string;
  /** Mono sub-label under the label. */
  sub?: string;
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
}
export function Switch(props: SwitchProps): JSX.Element;
