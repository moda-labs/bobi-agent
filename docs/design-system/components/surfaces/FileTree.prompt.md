Navigates an agent's real config files.

```jsx
<FileTree root="my-agent/" status="● on the clock" statusAccent current="agent.yaml" onSelect={setFile}
  rows={[
    { label: "agent.yaml" },
    { label: "roles/engineer/ROLE.md", badge: "generated" },
    { label: "workflows/support-triage.yaml", badge: "enforced", badgeAccent: true },
    { label: ".env", badge: "secrets" },
  ]} />
```

Pair it with a `CodeCard` showing the selected file.
