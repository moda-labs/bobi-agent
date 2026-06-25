# Calendar

The principal's calendar, through the `venn` CLI (Google Calendar by
default). See `tools/venn.md` for the shared mechanics (discovery, the
`<gcal>` `server_id`, the read-vs-`--confirm` write gate, fail-closed). This
guide is the **policy**; run `venn docs` and `venn tools describe -s <gcal>
-t <tool>` for the commands and schemas.

Reason in the principal's timezone (in `assistant-context.md`) and pass
timezone-correct values.

## Reading (run directly)

Viewing the schedule is a read — run it plain. Use it for "what's on
today/tomorrow", to find the event to move, and to check busy/free before
proposing a time. To offer an open slot, read the window first and propose
times that collide with neither existing events **nor** the principal's
quiet hours. Find the listing tool with `venn tools search "list calendar
events"`.

## Writing (the `--confirm` gate maps to who it touches)

A calendar write only goes through with `--confirm`. Whether you need the
principal's go-ahead first depends on **who it touches**:

- **Solo block on the principal's own calendar** (a focus hold, a personal
  reminder) — self-only. Confirm and create it directly.
- **Anything with other attendees** — creating, moving, or cancelling an
  event that has other people on it **emails them**. That's outbound:
  propose the exact change in-thread and get the principal's go-ahead before
  the `--confirm` write.

Find the create/update tools via `venn tools search` and check their attendee
params with `venn tools describe` before committing.

## Rules

- **Attendees make it outbound.** Confirm with the principal before any
  invite/move/cancel that notifies others; a solo block you may just make.
- **Never double-book or cross quiet hours** silently — read the window
  first; if the only open time conflicts, surface the conflict instead of
  overlapping.
- **Tentative vs confirmed.** A slot the principal asked you to *hold* is
  tentative / clearly a hold; a confirmed invite to others waits for their
  go-ahead.
- **Read before you write.** List the relevant window before creating or
  moving, so you act on the real schedule.
