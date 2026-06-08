# Web research

How this pack searches the web. The discipline that keeps research cheap
and comparable is structure: a fixed, repeatable battery of searches,
scoped to the coverage map and anchored to a fixed source list, run the
same way every time. Improvised open-ended searching is expensive, noisy,
and drags in SEO listicle spam — avoid it.

## Sources and access

Two layers:

1. **Allowed source domains** (from `research/moda-context.md`) — anchor
   searches to these to bias toward real voices:
   `linkedin.com`, `substack.com`, `medium.com`, `reddit.com`,
   `news.ycombinator.com`. This is a strong bias, not a hard filter; a few
   off-domain results are fine and sometimes useful.
2. **General web search** — allowed for broader discovery outside the
   anchored set (the "Adjacent / emerging" coverage area especially).

### Browser MCPs (optional, recommended)

For pages that need JS rendering or are behind soft walls, this pack can
use Playwright, Firecrawl, and BrowserMCP. **Restrict each MCP's allowed
domains to the source list above** so the agent can't wander. These are a
prerequisite you configure once in `~/.modastack/config.yaml`; if they're
not present, fall back to plain search + fetch.

## The scan, step by step

1. **Discover.** For each coverage area, run one or two targeted searches.
   Anchor them to the area's named sources (allowed domains) to bias
   toward real voices. Add one or two unrestricted searches for the
   Adjacent / emerging area. The whole battery is roughly 8–12 searches,
   the same shape every time.
2. **Triage from results.** Each search returns titled results with
   snippets and usually dates. Decide what's recent and what matters from
   those alone — much of the signal is visible before any fetch.
3. **Read full text** for the 5–15 items likely to define a theme or
   supply a representative quote. Triage hard; don't fetch everything. If
   a page returns a JS shell with no real content, fall back to the
   search snippet.
4. **Cluster across sources.** When several voices cover the same thing,
   that convergence is the signal. Record it once as a theme with N
   sources, not N separate items. One blog with a take is a data point;
   five converging is a theme.
5. **Separate news from discourse.** A model release is news. What people
   *argue* about it is discourse. We position into discourse — weight
   arguments over announcements.

## Recency

- Default research bias: the **last 3–6 months**. Older material is
  context, not signal.
- Weekly landscape scan: the **last 7 days**.
- For "what are people saying" questions, prefer recent short-form (a hot
  take surfaces days before the considered essay).

## Citation discipline

Cite every non-trivial claim with a source link. "Users complain about X"
is worthless without a link to a user complaining about X. Capture the URL
for each finding as you go — reconstructing sources later is lossy.

## Known blind spots (accept, don't chase)

- Substack Notes and most X/Twitter activity aren't reliably searchable.
- X and Reddit carry real discourse but are noisy; they're de-emphasized
  deliberately.
