The human gate. Bobi's central promise made visible — use it wherever a run waits on a person.

```jsx
<GateApproval
  title="Send the drafted reply to Acme?"
  workflow="support-triage.yaml" step="await: human approval"
  detail="Flagged P1, so the reply needs a person before it sends."
  onApprove={approve} onReject={reject}>
  <CodeCard title="draft reply" tag="Pending" tagAccent>{draft}</CodeCard>
</GateApproval>
```

Never downgrade a gate to a toast, and always name the workflow + step so the decision is auditable.
