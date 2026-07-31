The Bobi glyph set. One component, `name` selects the glyph.

```jsx
<Icon name="ticket" size={18} />
<Icon name="workflow" size={23} />
<Icon name="moon" size={20} />
<GithubGlyph size={17} />
```

On dark surfaces pass `color="currentColor"` (or a paper value) — the default ink stroke vanishes on the void sidebar.

Sizes in use: **18px** inside a figure chip, **20px** general UI, **23px** inside a 44px icon tile.

Semantic pairings the product already relies on: `ticket`→Linear, `bell`→PagerDuty, `mail`→email, `chat`→Slack, `queue`→the event queue, `code`→engineer agent, `pulse`→SRE agent, `headset`→support agent, `checkCircle`→a shipped outcome, `diamond`→awaiting approval, `moon`→the nightly sleep cycle. Reuse them; don't re-map.
