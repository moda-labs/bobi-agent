Product chrome for any Bobi control surface.

```jsx
<SideNav agent="my-agent" active="queue" onSelect={setView}
  footer="v0.9.2 · fly.dev"
  items={[
    { id: "queue", label: "queue", icon: <Icon name="queue" size={16} />, count: 12 },
    { id: "gates", label: "approvals", icon: <Icon name="human" size={16} />, count: 2, countAccent: true },
    { id: "agents", label: "agents", icon: <Icon name="parallel" size={16} /> },
    { id: "workflows", label: "workflows", icon: <Icon name="workflow" size={16} /> },
  ]} />
```

Lowercase mono labels. Violet counts only for things a person must clear.
