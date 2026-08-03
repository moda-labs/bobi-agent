Anchors an operational screen.

```jsx
<div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
  <MetricTile label="In queue" value="6" delta="+3 today" deltaTone="up" />
  <MetricTile label="Awaiting a human" value="2" accent />
  <MetricTile label="Resolved today" value="12" />
  <MetricTile label="Median run" value="4.2" unit="min" />
</div>
```

Violet only for counts that need a person.
