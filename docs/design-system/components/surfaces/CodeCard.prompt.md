Shows config. Bobi's config *is* its product surface, so this card appears everywhere.

```jsx
<CodeCard title="workflows/support-triage.yaml" tag="Enforced" tagAccent>
  <Tok kind="k">steps:</Tok>{"\n  "}
  <Tok kind="k">- run:</Tok> <Tok>draft_reply</Tok>{"\n"}
  <GateLine>{"  "}<Tok kind="a">- await: human approval</Tok> <Tok kind="c"># if P1</Tok></GateLine>
  {"  "}<Tok kind="k">- done:</Tok> <Tok>ticket_resolved</Tok>
</CodeCard>
```

Always wrap approval/limit lines in `GateLine` — the violet rail is how Bobi says "a human decides here."
