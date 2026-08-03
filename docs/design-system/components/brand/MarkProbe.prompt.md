The canonical Bobi mark — use it anywhere the product needs its own identity (favicon, app chrome, the director's glyph in a figure).

```jsx
<MarkProbe size={28} ink="var(--bobi-ink)" accent="var(--bobi-acc)" />
```

- On dark surfaces use the defaults (paper body, bright violet dot).
- On paper surfaces pass `ink="var(--bobi-ink)"` and `accent="var(--bobi-acc)"`.
- Inside a figure it doubles as the **director** icon — that is its only semantic use beyond branding.
- Never recolor the probe dot to anything but the violet accent.
