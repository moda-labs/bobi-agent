# Research context — Moda Labs

This file is the domain context for the `market-research` agent pack. The
research manager reads it on startup and seeds it into the `research`
knowledge base. Edit it to retarget the pack to a different org, ICP, or
topic set — the role prompts stay generic and read from here.

Convert this file into KB entries on first run so workers can search it.

---

## Who we research for

Moda Labs — a boutique AI consultancy / venture builder based in SF, with
a few onshore and offshore engineers. We ship real AI product on cash
engagements and take selective equity co-builds.

We have built core AI infrastructure for tech clients: MCPs, eval
harnesses, skill libraries, and context-optimization layers.

**Releasing summer 2026: modastack** — an agent harness aimed at
enterprises that need more deterministic workflows and real-time event
triggers.

## Buyers / ICP (in priority order)

1. **AI-fluent companies that are building** but need help accelerating
   key scaffolding — MCPs, evals, harnesses, context layers.
2. **Agentic-aspiring data & analytics teams** who want agentic data
   workflows that actually deliver value and improve their efficiency.
3. **(More distant) Finance teams** seeking agentic workflows to automate
   accounting, close, FP&A, and similar processes.
4. **Generally**: less-AI-native enterprises that want "enterprise
   agentic workflows" to move up the curve quickly.

Buyer personas skew technical: founders, CTOs, product leaders, and
data/analytics leads who have decided AI is core but lack in-house
velocity to ship it.

## Coverage map (where the landscape scan looks — fixed)

These are *where we look*, not *what we find* (themes are findings).
Expand only on a deliberate decision, logged in the corpus changelog.

1. **Applied AI / the application layer** — building real AI products,
   AI in production, what's shippable vs hype.
2. **Agentic coding harnesses** — Claude Code, Cursor, coding agents and
   copilots, the harness/tooling layer around them.
3. **The skills & agent ecosystem** — skills, plugins, MCP, agent
   frameworks, how people extend and compose AI tools.
4. **Solo entrepreneurs & small teams** — solopreneurs, indie builders,
   tiny teams shipping with AI, "team of one" discourse.
5. **AI agencies, consulting & venture building** — how AI services firms
   position; co-build and equity-for-build models.
6. **Adjacent / emerging** — anything rising fast that touches our buyers
   but doesn't fit a bucket yet.

## Allowed source domains

The scan anchors searches to these domains (a strong bias, not a hard
filter). General web search is also allowed for broader discovery.

- linkedin.com
- substack.com
- medium.com
- reddit.com
- news.ycombinator.com

## Named-voices hit list (5–10; 8 is the working set)

Anchor searches to these domains to bias toward real voices over
listicle spam. Swap a source out if it returns nothing useful for
several consecutive scans (log the change in the corpus changelog).

- **Simon Willison** — simonwillison.net — agentic coding harnesses,
  applied AI, the skills/MCP/agent ecosystem. Highest-value source.
- **Ethan Mollick (One Useful Thing)** — oneusefulthing.org — applied AI,
  what AI can practically do, broad-audience read.
- **Latent Space** — latent.space — AI engineering, agents, builder
  discourse; strong on harnesses and the agent ecosystem.
- **The Pragmatic Engineer** — newsletter.pragmaticengineer.com — how
  engineering orgs adopt AI; readership is our buyer.
- **The Bootstrapped Founder (Arvid Kahl)** — thebootstrappedfounder.com
  — solopreneurs, bootstrapping, building small.
- **Ahead of AI (Sebastian Raschka)** — magazine.sebastianraschka.com —
  LLM research depth; early read on what's technically rising.
- **Justin Welsh** — justinwelsh.me — solopreneur brand and audience
  building.
- **Greg Isenberg** — gregisenberg.com — AI + solopreneur + what-to-build
  and venture discourse; closest regular voice on AI agencies/venture.

Platform catch-alls (anchor with topic terms):
- medium.com — catches voices outside the named list.
- news.ycombinator.com — high-signal aggregator for the technical-founder ICP.

The "AI agencies / consulting / venture building" area is thinnest on
dedicated voices; lean on unrestricted search and Greg Isenberg there.
X/Twitter and Reddit carry a lot of this discourse but are noisy and
poorly searchable — left off deliberately, revisit only if the list
proves too thin.

## Watched topics (review weekly)

Seed list — keep current as priorities shift. The weekly scan covers the
coverage map above; this is the narrower set worth a recency-biased pass.

- Agentic coding harnesses and where the harness-vs-model line is moving
- The skills / MCP / plugin ecosystem
- Solopreneur and small-team building with AI
- AI agency / co-build / equity-for-build positioning
- Enterprise adoption of deterministic agent workflows (modastack's wedge)

## Standing themes (the landscape map spine — 7–8, stable names)

Establish on the first weekly scan and keep stable so snapshots diff
cleanly. Each is a one-sentence conversation (not a topic). Seed examples
— replace with the real founding set on first run:

- _(to be established on first weekly scan)_

## Explored / discarded topics

Topics we've already looked into and set aside, with the date and why.
Keeps us from re-researching dead ends. (Empty to start.)

## Voice constraints (for any human-facing brief or digest)

- Moda voice: no em dashes anywhere, including bullet labels and lists.
  Write "Cursor: the harness-layer case" or "Cursor, the harness-layer
  case", not "Cursor — the harness-layer case". Use commas, colons,
  periods, or restructure. This applies to every line of a brief or
  digest, not just the prose paragraphs.
- No filler ("happy to share", "dig into", "at its core").
- Contractions are natural.
- Specific over vague — counts, named publications/authors, real quotes.
- Report, don't editorialize. Map the discourse; don't decide strategy.
- Never close on a summary. End on the shift that matters or the
  whitespace worth taking.
