Weekly prep-doc skill — assemble and deliver a start-of-week prep doc for the team.

# Weekly prep-doc skill

This is the task the `weekly-prep-doc` monitor triggers. When the
`monitor/prep.weekly_due` event arrives (every Sunday night, by default),
the director reads this file and follows it to produce **one prep doc for
the upcoming week** and deliver it.

This skill is the *use case*. The framework only provides the schedule (a
`notify` monitor gated to Sundays — see the recipe in
`docs/BUILDING_AGENT_TEAMS.md`). Everything about *what the doc contains and
where it lands* lives here, so it can evolve without touching modastack.

## Scope

- **One doc per project**, not per individual. It covers the repos and work
  this agent currently manages.
- It **summarizes and looks ahead** — it does not take action, open PRs, or
  change tickets. Producing the doc and posting it is the whole job.

## Sources

Gather the week's context from these sources. Skip any that aren't
configured (no Linear key, no calendar connector) rather than failing.

1. **Open PRs** across managed repos — title, author, review state, CI
   state, and whether each is blocked or stale (`gh pr list`, and the
   `pr-conflict-check` / `stale-pr-check` monitors already surface the
   problem ones).
2. **Open issues / tickets in progress** — GitHub issues labelled
   `status:in-progress` and, if Linear is configured, in-progress Linear
   tickets for the team.
3. **Upcoming calendar events** for the week ahead, if a calendar connector
   is available via `venn` (`venn tools search "list calendar events"`).
4. **Recent Slack threads** the team still owes a reply on, in the channel
   the director normally talks to the human in.

Treat the list as a default, not a contract — if a source is noisy or
empty, say so briefly rather than padding the doc.

## Output

Render a single Markdown doc, **Prep for week of `<Monday's date>`**, with
short sections in this order, leading with anything that needs attention:

1. **Needs attention** — blocked/stale PRs, CI failures, anything stuck.
2. **In flight** — PRs and tickets in progress, grouped by repo.
3. **This week** — upcoming calendar events and deadlines.
4. **Follow-ups** — Slack threads or asks still open.

Keep it scannable: bullets, one idea per line, links over prose. If a
section is empty, write one line saying so ("No blocked PRs") rather than
omitting it — an explicitly-quiet doc is a valid, useful doc.

## Delivery

1. Write the doc to `workspace/prep-docs/<Monday's date>.md` (create the
   directory if needed). This is the canonical artifact.
2. Post a short summary to Slack in the channel where you normally talk to
   the human — the **Needs attention** section inline, plus a one-line
   pointer to the full doc. Post it as a new top-level message (a broadcast),
   not a thread reply. Per the team's rendered-markdown convention, link to
   the rendered file rather than pasting the whole doc.

Always deliver, even on a quiet week. If no repos are being managed yet,
skip silently — there is nothing to prepare for.
