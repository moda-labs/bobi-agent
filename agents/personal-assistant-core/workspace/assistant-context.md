# Assistant context

The assistant reads this file before acting. It is the one place to describe
**who the principal is, how they like things done, and how much to do without
asking.** Fill in every `<…>` placeholder before starting — the assistant
will not take outbound actions while these are unset. This file is yours to
edit; the role prompt stays generic and reads from here.

## Principal

- **Name:** <your name>
- **Timezone:** <e.g. America/Los_Angeles>
- **Working hours:** <e.g. Mon–Fri 9:00–18:00>
- **Quiet hours:** <e.g. 20:00–07:00 and weekends> — no proactive nudges or
  briefings in this window; a direct request is always answered.
- **A little about me:** <role, what I'm focused on, anything that helps the
  assistant judge what matters — e.g. "founder, mostly in meetings, protect
  deep-work mornings">

## Chat surface

- **Workspace:** <slack workspace>
- **Channel / DM:** <the channel id where the assistant talks to me — usually
  my DM with the bot or a private channel>

## Connectors (Venn server_ids)

Connect each in Venn and paste its `server_id` here. See `tools/venn.md`.

- **Email (Gmail):** `<gmail server_id>`  —  my address: <me@example.com>
- **Calendar (Google Calendar):** `<gcal server_id>`  —  calendar: `primary`
- **Tasks (Google Tasks):** `<gtasks server_id>`  —  list: `@default`

(Swapping in Outlook / Todoist / etc. is a connector change in Venn — just
update the `server_id` here; the categories don't change.)

## Key people

The people whose email and invites should surface proactively. Everyone else
waits for the daily briefing.

- <name — relationship — email>
- <name — relationship — email>

## Autonomy line

How much the assistant does without asking. Default below is safe; raise or
lower it.

- **Act freely (no confirmation):** reading/searching mail; answering
  schedule questions; adding/completing/rescheduling my own to-dos; drafting
  emails and messages; holding a tentative slot on my own calendar.
- **Draft and confirm first:** sending an email; creating/moving/cancelling
  a meeting that has other attendees (it emails them); anything that spends
  money or is irreversible (purchases, unsubscribes, deletions).
- **Standing exceptions** (things I _do_ want done without asking):
  <e.g. "send routine scheduling replies to my team without confirming"> —
  leave blank to keep the strict default.

## Voice (how to draft and reply)

- **Tone:** <e.g. warm but brief; direct; no corporate filler>
- **Email sign-off:** <e.g. "Thanks, <name>" / "Best, <name>">
- **Briefing style:** <e.g. "punchy, scannable, lead with conflicts">
- **Don'ts:** <e.g. no em dashes; don't over-apologize; don't pad>

## Importance bar (what's worth interrupting me for)

The inbox/calendar watches use this to decide what to surface vs. hold for
the briefing.

- **Interrupt me for:** <e.g. anything from key people; a same-day deadline;
  a meeting starting in <30 min I haven't acknowledged; a customer escalation>
- **Never interrupt for:** <e.g. newsletters, receipts, promotions,
  automated notifications, FYI cc's>
