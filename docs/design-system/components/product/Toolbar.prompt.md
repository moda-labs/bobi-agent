List screens open with one of these under the PageHeader.

```jsx
<Toolbar placeholder="Search events" keyHint="F" value={q} onChange={(e) => setQ(e.target.value)}
  tools={<IconButton label="Filter" icon={<Icon name="sliders" size={16} />} />}
  primary={<Button variant="primary" size="md">New event</Button>} />
```

One primary action only. Icon buttons always take a `label`.
