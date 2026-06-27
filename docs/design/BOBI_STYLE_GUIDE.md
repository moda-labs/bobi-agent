# bobi — setup UI style guide (clay)

The locked visual approach for the `bobi`/bobi setup web UI. Warm light
chrome + a single dark CRT slab, mono-accented identity, system fonts only,
static space-retro. **Clay is the single UI accent.** Violet appears in exactly
one place — the probe dot in the logo — as the brand glint; it never enters the
UI chrome.

This guide is the source of truth for the reskin. Copy the token block verbatim;
build components against the tokens, never against raw hex.

Prototype: [`bobi-setup-reskin.html`](./bobi-setup-reskin.html) (set the accent
switch to **clay**). Logo asset: [`bobi-mark.svg`](./bobi-mark.svg).

---

## 1. Logo

The **probe mark**: a paper body, a dashed orbit, and a single **violet** probe
dot (`#9690F1`) out on the orbit — a node watching its events. The violet dot is
the brand glint and the *only* violet in the product. Lowercase `bobi` wordmark,
semibold, tight tracking. The mark sits on a dark "void" chip so the paper body
reads on the warm light chrome.

> **Centering fix (locked):** the raw mark geometry is bottom-left weighted (the
> orbit is centered at `14,18` in a `32×32` box). Always render it through the
> centering transform below, or it floats low-left inside a chip.

### Inline SVG (chip usage, paper-on-dark)

```html
<!-- 18 = render size in px; geometry pre-centered via translate(2,-2) -->
<svg viewBox="0 0 32 32" width="18" height="18" aria-hidden="true">
  <g transform="translate(2,-2)">
    <circle cx="14" cy="18" r="6" fill="#FAF7EE"/>
    <circle cx="14" cy="18" r="12.5" fill="none" stroke="#FAF7EE"
            stroke-width="1.2" stroke-dasharray="2 4" opacity="0.6"/>
    <circle cx="23.5" cy="9.5" r="3" fill="#9690F1"/>
  </g>
</svg>
```

### The chip

```css
.mk{ display:grid; place-items:center; border-radius:6px; background:#201A14; }
/* rail/titlebar: 22–24px chip, ~18px mark · welcome splash: 62px chip, ~46px mark */
```

### Lockup

`[chip] bobi setup` — wordmark in `--font-sans`, `font-weight:680`,
`letter-spacing:-0.02em`. The qualifier (`setup`) is `--muted`, weight 500, no
negative tracking.

### Rules

- **Do** keep the probe dot violet (`#9690F1`); it is the brand glint and the
  only violet anywhere in the product.
- **Do** keep the body + orbit paper (`#FAF7EE`) on the dark chip.
- **Don't** let violet leak into the UI chrome — buttons, steps, eyebrows, focus
  rings, and the slab glow are all clay. Violet lives only on the logo dot.
- **Don't** place the bare mark (no chip) on light backgrounds; the paper body
  disappears. For a chip-less context, swap body/orbit to `--text` and keep the
  dot violet.
- **Don't** rotate, restyle the dash pattern, or move the dot off the orbit.

Self-contained asset (void chip baked in): `bobi-mark.svg` — use for favicon,
OG, and anywhere an external file is cleaner than inline SVG.

---

## 2. Color tokens

Drop-in `:root` + accent block. Clay-only — the `--spot`/violet variables are
gone; secondary elements (eyebrows, meters, badges) use `--accent`.

```css
:root{
  /* paper neutrals (bobi paper #FAF7EE / ink #221C15 family) */
  --bg:#F3EFE4; --surface:#FBF9F2; --raised:#FFFEF9;
  --text:#221C15; --muted:#84725B; --faint:#A89E90;
  --border:#E6DFD1; --border-strong:#D8CEBD;

  /* dark CRT slab — warm void, no violet undertone */
  --slab-bg:#181310; --slab-surface:#1F1813; --slab-text:#ECE4DA;
  --slab-muted:#9A8C82; --slab-border:#2A2018;

  /* code syntax (on slab) */
  --syn-key:#E0A06A; --syn-str:#CBBA8B; --syn-punc:#7E776B; --syn-com:#6E6354;

  /* clay accent — single accent for the whole UI */
  --accent:#C0632E; --accent-2:#D67B55; --slab-accent:#E0843F;
  --accent-soft:rgba(192,99,46,.11); --accent-ring:rgba(192,99,46,.30);

  /* semantic */
  --ok:#177B52; --err:#B5462B;

  --radius:12px; --radius-sm:7px;
}
```

| Token | Hex | Used for |
|---|---|---|
| `--bg` | `#F3EFE4` | app background (under the frame) |
| `--surface` | `#FBF9F2` | frame / panel base |
| `--raised` | `#FFFEF9` | cards, inputs, raised fills |
| `--text` | `#221C15` | primary text |
| `--muted` | `#84725B` | secondary text, qualifiers |
| `--faint` | `#A89E90` | tertiary / placeholder / mono labels |
| `--border` | `#E6DFD1` | hairlines, dividers |
| `--border-strong` | `#D8CEBD` | input borders, frame edge |
| `--accent` | `#C0632E` | buttons, current step, eyebrows, focus |
| `--accent-2` | `#D67B55` | button hover, probe-dot tone |
| `--slab-accent` | `#E0843F` | CRT glow, slab top line, caret, live dot |
| `--accent-soft` | clay 11% | selected/current fills, focus ring fill |
| `--accent-ring` | clay 30% | focus ring, current-step halo |
| `--ok` / `--err` | `#177B52` / `#B5462B` | connected / missing, banners |

**Accent discipline:** clay is a spot, not a field. It lands on one primary
action, the current step, eyebrows, focus, and the slab glow. Cards and surfaces
stay neutral; clay earns attention by being scarce.

---

## 3. Typography

System fonts only — no web fonts.

```css
--font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
             "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, "SF Mono", "SFMono-Regular", Menlo, "Cascadia Mono",
             "Segoe UI Mono", Consolas, "Liberation Mono", monospace;
```

| Role | Font | Size / weight | Tracking |
|---|---|---|---|
| H1 (node title) | sans | 27px / 680 | −0.02em |
| Welcome wordmark | sans | 58px / 680 | −0.045em |
| Body / lede | sans | 14.5px / 400, `--muted` | — |
| Message text | sans | 14px / 1.5 | — |
| Eyebrow | mono | 11px / 600, `--accent`, UPPERCASE | 0.16em |
| Rail step | mono | 11.5px, UPPERCASE | 0.12em |
| Counter / meter / badge | mono | 11px, `--faint`/`--accent` | 0.14em |
| Code / addr / paths | mono | 12–13px | — |

**Mono = machine voice.** Use mono for anything the system is reporting: step
labels, counters, file paths, the address bar, tokens, live status. Prose stays
sans.

---

## 4. Surfaces & the frame

The whole UI is one rounded "window" floating on `--bg`.

```css
.app{
  width:min(1240px,96vw); height:min(780px,92vh); margin:3vh auto;
  background:var(--surface); border:1px solid var(--border-strong);
  border-radius:14px; overflow:hidden;
  box-shadow:0 1px 0 #fff inset, 0 18px 50px -24px rgba(40,30,16,.35);
}
```

- **Titlebar** — traffic-light dots, mono address (`127.0.0.1:8765 / setup`),
  accent switch, retro toggle. `linear-gradient(var(--raised),var(--surface))`.
- **Rail** — 216px, `linear-gradient(var(--surface),#F0EBE0)`. Holds the lockup,
  the `// NN / NN` counter, the step list, star-dust filler, and a hint footer.
- **Node** — the working pane: `34px 40px` padding, `.narrow` caps at 680px.

---

## 5. The dark CRT slab

The one dark element. Used for Build and Review (code being written/validated).
It is where the space-retro theme concentrates.

```css
.slab{ background:var(--slab-bg); position:relative; overflow:hidden; }
/* clay scanline glow along the top edge */
.slab::before{ content:""; position:absolute; top:0; left:0; right:0; height:2px;
  background:linear-gradient(90deg,transparent,var(--slab-accent) 18%,
             var(--slab-accent) 82%,transparent); opacity:.9; }
```

Retro-on adds: inner vignette, horizontal scanlines (`repeating-linear-gradient`),
four glowing corner brackets, a clay-tinted text glow on code, and a blinking
caret. Code text shifts toward `--slab-accent` with a soft `text-shadow`.

---

## 6. Space-retro mode

Toggleable via `html[data-retro="on"]`. **On by default.** Static only — no
looping animation beyond the caret blink and the "live" pulse; respect
`prefers-reduced-motion`.

Retro additions:
- Faint clay grid over `--bg` (24px repeating lines, ~3.5% alpha).
- Star-dust radial dots in the rail filler and the welcome backdrop (clay only).
- Slab vignette + scanlines + corner brackets + code glow (§5).
- `//` prefix on mono counters; retro toggle LED glows clay.

Retro-off = the same layout, flat and clean (grid, scanlines, glow, dust removed).

---

## 7. Components

**Buttons**
```css
.btn{ font:560 14px var(--font-sans); padding:10px 18px; border-radius:9px;
      border:1px solid transparent; cursor:pointer; }
.btn.primary{ background:var(--accent); color:#fff;
              box-shadow:0 2px 8px -3px var(--accent); }
.btn.primary:hover{ background:var(--accent-2); }
.btn.ghost{ background:transparent; color:var(--muted);
            border-color:var(--border-strong); }
.btn.ghost:hover{ color:var(--text); background:var(--raised); }
```
One primary per view. Everything else is ghost.

**Cards** — `--raised` fill, `--border`, `--radius`. Hover/selected:
`border-color:var(--accent)` + `box-shadow:0 0 0 3px var(--accent-soft)`. Empty/
pending: dashed border, ~0.62 opacity. Mono role/scope chips inside use
`--surface` fills.

**Chips (suggestions)** — mono, pill, `--raised` on `--border`; hover darkens
border + text. Used for "+ add this" affordances.

**Rail steps** — three states: `done` (clay-filled dot + check), `current`
(`--accent-soft` row bg + clay dot with `--accent-ring` halo), `todo` (faint,
hollow dot).

**Inputs / textareas** — `--raised` fill, `--border-strong` border; focus:
`border-color:var(--accent)` + `box-shadow:0 0 0 3px var(--accent-soft)`.

**Badges / status** — mono. Neutral `--muted`; `--ok` for connected; `--err` for
missing. The welcome "open source" badge dot is clay.

**Focus** — never remove outlines silently; the clay soft-ring doubles as the
visible focus state on interactive fills.

---

## 8. Motion

Minimal and static-leaning. Allowed: caret blink (`1.05s` steps), live-dot pulse
(`1.4s`), card border/shadow transitions (`.15s`), disconnect-dot pulse. No
parallax, no looping ambient motion. All motion gated behind
`@media (prefers-reduced-motion: reduce)` → off, with motion-only glints hidden
at rest.

---

## 9. Reskin checklist (for the implementation task)

Target: `bobi/setup/webui/static/app.css` + `index.html` + `app.js`.

- [ ] Replace `:root` + `html[data-accent="amber"|"green"]` with the §2 block;
      collapse to a single clay accent (drop the amber/green/violet switch, or
      keep the switch as clay-only for now).
- [ ] Swap the titlebar/rail `.brand .mk` glyph for the centered probe mark (§1);
      set chip bg `#201A14`.
- [ ] Update wordmark text to `Bobi` (keep `setup` qualifier).
- [ ] Re-point slab tokens to the warm-void values (no violet undertone).
- [ ] Ship `bobi-mark.svg` as favicon; update `<title>`.
- [ ] Verify retro-on and retro-off both read; check focus rings and `--ok`/`--err`.
- [ ] Confirm no raw hex leaked into components — all via tokens.
```
