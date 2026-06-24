# Tasks (to-do list)

The principal's to-do list, through the `venn` CLI (Google Tasks by
default). See `tools/venn.md` for the shared mechanics (discovery, the
`<gtasks>` `server_id`, the read-vs-`--confirm` write gate, fail-closed).
This guide is the **policy**; run `venn docs` and `venn tools describe -s
<gtasks> -t <tool>` for the commands and schemas.

The to-do list is the principal's own — adding, completing, and rescheduling
items only affects them, so these are **self-only writes**: confirm and run
them directly (no need to stop and ask). Only something that reaches beyond
their account, or deletes a whole list, rises to draft-and-confirm.

## Reading (run directly)

List the open tasks before adding (to avoid duplicates) or to answer "what's
due today". Find the listing tool with `venn tools search "list tasks"`. If
the principal keeps several lists, the right list id is in
`assistant-context.md`.

## Writing (self-only — confirm and run)

Add, complete, or reschedule with the write tools (found via `venn tools
search "add task"` / `"complete task"`, run with `--confirm`). These only
touch the principal's own list, so you don't need to stop and ask — just do
it and report it.

When adding, **capture a due date** if the principal gave one ("by Friday")
so it surfaces in the briefing's "due today / overdue".

## Rules

- **Read before adding** — check the list so you don't duplicate something
  already there.
- **Capture the due date** — a task with a due date shows up in the daily
  briefing; one without is easy to lose.
- **Self-only — just do it.** Adding / completing / rescheduling the
  principal's own list needs no confirmation.
- **Backend-agnostic.** If Google Tasks isn't the connected backend (e.g.
  Todoist), the categories above hold — only the tool names change; discover
  them with `venn tools search`.
