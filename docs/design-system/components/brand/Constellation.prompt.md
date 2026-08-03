The hero brand ornament. Dark surfaces only — login screens, empty states, marketing heroes, splash panels.

```jsx
<div style={{ animation: "bobi-drift 26s ease-in-out infinite alternate" }}>
  <Constellation width={700} height={610} />
</div>
```

- Position it absolutely, **below the top chrome line** — it must never overlap a header or nav.
- Hide it below ~1280px viewports rather than shrinking it.
- Pass `animate={false}` for print, PDF, or OG-image capture.
