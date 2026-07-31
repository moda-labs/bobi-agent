Toggles a monitor, service, or capability.

```jsx
<Switch checked={on} onChange={setOn} label="stale-pr-check" sub="every 1h · nudge after 48h" />
```

Never use it to confirm an irreversible action — that is `GateApproval`'s job.
