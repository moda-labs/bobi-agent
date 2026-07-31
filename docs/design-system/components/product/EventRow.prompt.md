The event queue list.

```jsx
<EventRowHeader />
<EventRow icon={<Icon name="ticket" size={18} />} source="linear · ENG-142"
  title="Export fails on large CSV" agent="engineer" workflow="sdlc.yaml"
  status={<StatusBadge state="live" />} time="2m ago" />
```

Keep the column order **source → event → agent → workflow → state → time** — it is the loop the product is built on.
