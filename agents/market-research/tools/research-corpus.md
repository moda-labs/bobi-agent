# Research corpus

The corpus is a modastack knowledge base named `research` (hybrid FTS +
semantic search). It is the store of record for everything this pack
learns: the landscape map, topic briefs, key voices and companies, use
cases and wedges, forecasts, and the explored/discarded list. Use it
instead of flat files so research is searchable across time.

## Commands

```bash
modastack kb create research                 # one-time; safe to re-run
modastack kb add research --text "..."       # index an inline finding
modastack kb add research --file <path>      # index a file (e.g. a brief)
modastack kb search research "your query"    # hybrid FTS + semantic search
modastack kb list                            # list knowledge bases
modastack kb info research                    # entry count, stats
```

## Always search before you research

Before any topic investigation, `modastack kb search research "<topic>"`.
We may already have a brief, or have explored and discarded it. Build on
what's there; don't redo it.

## Entry conventions

Prefix each entry so searches and the manager's corpus duties stay
organized. Always include a date (`YYYY-MM-DD`).

| Prefix | Holds |
|---|---|
| `context::` | Org positioning, ICP, coverage map, voice list (seeded from `workspace/moda-context.md`) |
| `landscape::` | Current landscape map — standing themes, momentum, whitespace |
| `snapshot::` | A dated, never-edited copy of the landscape map |
| `topic::` | A topic brief (signal, voices, companies, use cases, forecast) |
| `voice::` | A key voice and their recent stance |
| `company::` | A key company, its products/features and stance |
| `pmf::` | A PMF verdict and the evidence behind it |
| `discarded::` | A topic explored and set aside, with date and why |
| `changelog::` | Structural changes — theme added/retired/renamed, source swapped |

Example:

```bash
modastack kb add research --text "topic:: 2026-06-08 — Agentic coding harnesses.
Signal: 7 sources argue the harness now outranks the model... [full brief]
Sources: https://... , https://..."
```

## First-run seeding

On startup the manager ensures the `research` KB exists and, if it has no
`context::` entries, indexes `workspace/moda-context.md` into it (split by
section, each as a `context::` entry). After that, `moda-context.md` is the
human-editable source and the KB is the searchable copy.
