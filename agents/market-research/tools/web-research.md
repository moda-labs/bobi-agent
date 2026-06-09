# Web research

How this pack searches the web. The discipline that keeps research cheap
and comparable is structure: a fixed, repeatable battery of searches,
scoped to the coverage map and anchored to a fixed source list, run the
same way every time. Improvised open-ended searching is expensive, noisy,
and drags in SEO listicle spam — avoid it.

## Sources and access

Your tools are `WebSearch` (discover) and `WebFetch` (read full text),
plus `Bash` (curl). There are no browser/scraper MCPs wired into worker
sessions today, so the reachability below is the real, tested picture.
Anchor searches to the allowed source domains in `research/moda-context.md`
to bias toward real voices (a strong bias, not a hard filter), and use
general web search for broader discovery (the "Adjacent / emerging" area
especially).

### Reachability (tested)

| Source | Discover (WebSearch) | Read full text | Notes |
|---|---|---|---|
| Substack | yes | yes (WebFetch) | full article text |
| Medium | yes | yes (WebFetch) | public posts; member-only posts may be partial |
| Hacker News | yes | yes | **best entry point: the Algolia API** (below) |
| LinkedIn | yes (titles + snippets) | no | post bodies are login-walled; use the search snippet, do not try to fetch the full post |
| Reddit | **no** | **no** | blocked for both WebSearch and WebFetch at the crawler level, and plain curl returns 403. Treat as unreachable. |

**Hacker News via Algolia.** Fetch the JSON API directly, it returns
titles, points, comments, and URLs:

```
https://hn.algolia.com/api/v1/search_by_date?query=<terms>&tags=story
https://hn.algolia.com/api/v1/search?query=<terms>&tags=story   (by relevance)
```

**Reddit.** Do not spend searches on `reddit.com`, they fail. Pick up
Reddit discourse indirectly: HN threads, cross-posts, and articles that
quote Reddit. If first-hand Reddit voices become essential, that needs
added infrastructure (the official Reddit API or a managed scraper such
as Firecrawl) wired into the worker, which is not configured yet.

**LinkedIn.** WebSearch surfaces post titles and a snippet, lean on those.
Full post bodies require an authenticated session and are out of reach
here. The named-voices hit list mostly lives on Substack and personal
sites, so this is a minor gap.

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
- Reddit is unreachable here (see the reachability table) and X is poorly
  searchable, so first-hand forum voices are thin. Lean on practitioner
  write-ups, HN threads, and benchmark posts instead, and name the gap when
  it matters rather than papering over it.
