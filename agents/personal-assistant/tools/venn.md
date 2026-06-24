# Venn (the connector plumbing)

Email, calendar, and tasks all reach the real world through the **`venn`
CLI** — one bundled command that fronts the principal's connected accounts
(Gmail, Google Calendar, Google Tasks to start). The category guides
(`tools/email.md`, `tools/calendar.md`, `tools/tasks.md`) carry the policy for
each; this guide is the shared mechanics.

`venn` is on PATH and reads `VENN_API_KEY` from the environment — never pass
the key as an argument. Don't re-derive syntax from memory; the CLI documents
itself:

```bash
venn docs                 # full reference, written for an agent to read
venn --help               # command surface
```

## The workflow (discover, then run)

Tool names and argument schemas are Venn's and can differ per connector, so
discover them rather than guessing:

```bash
venn help list_servers                       # the connected servers + their server_ids
venn tools search "list recent emails"       # find the right tool by intent
venn tools describe -s <server_id> -t <tool> # exact params/schema before you call
venn tools execute  -s <server_id> -t <tool> -a '<json args>'   # run it
```

## Targeting the right connector

Several services may be connected. Always target the specific instance by its
`server_id` from `workspace/assistant-context.md`:

- `<gmail>` — the principal's Gmail (email)
- `<gcal>` — the principal's Google Calendar
- `<gtasks>` — the principal's Google Tasks

`venn help list_servers` shows what's actually connected. If a `server_id` in
the context file is still a placeholder, the connector isn't wired — say so
and don't guess.

## Reads vs writes — the autonomy gate

Venn enforces the same line as the principal's autonomy policy: **reads run
plain; writes require `--confirm`.**

- A `venn tools execute` **without** `--confirm` is a read (list emails, view
  the calendar, list tasks) — safe to run directly.
- A write (send an email, create/modify an event) only goes through **with**
  `--confirm`. Treat `--confirm` as the moment you're committing an outbound
  action: per the role's autonomy line, get the principal's go-ahead first
  for anything that leaves the account or reaches another person. Self-only
  writes (their own to-do list) you may confirm and run directly.

## Failure

**Fail closed.** On any error — missing/expired key, connector not
connected, transport failure — do nothing and say so. Never invent an inbox,
an event, or a task. For a proactive monitor that means emit nothing and note
the gap; for a request, stop and tell the principal the connector needs
attention (re-auth on venn.ai, or a missing `VENN_API_KEY`).
