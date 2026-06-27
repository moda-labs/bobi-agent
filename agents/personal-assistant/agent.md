# personal-assistant

A portable, customizable **personal assistant** team. A single persistent
**assistant** serves one principal: it reads and triages email, manages the
calendar, keeps the to-do list, runs research and errands, and posts a daily
briefing. It talks to its principal on a chat surface (Slack by default) and
reaches the real world through **Venn**, which fronts the principal's
connected accounts. This is the **reusable base** — it speaks the assistant's
job in terms of capability categories (email, calendar, tasks) over generic
seams, binding Gmail + Google Calendar via Venn out of the box. Who the
principal is, how they like things done, and how much to do without asking
all live in `workspace/assistant-context.md`; the role prompt stays generic
and reads from there.

## Roles

- **assistant** — the only role. A persistent **coordinator** that receives
  the principal's messages and monitor ticks (the daily briefing, calendar
  and inbox nudges) and stays responsive, plus a short-lived **worker** (a
  launched `request` / `daily-briefing` run) that does the multi-step work.
  One role, two modes.

## Capabilities (grouped by category)

Capabilities are organized by **category**, not by vendor. Each category is
a tool guide carrying the policy for reaching it through the bundled `venn`
CLI:

- **email** (`tools/email.md`) — read, search, summarize, triage the inbox;
  draft and (on the principal's say-so) send replies. Gmail by default.
- **calendar** (`tools/calendar.md`) — view the schedule, find open time,
  create / move / cancel events, prep for meetings. Google Calendar by
  default.
- **tasks** (`tools/tasks.md`) — keep the to-do list: add, list, complete,
  reschedule. Google Tasks by default.

`tools/venn.md` documents the shared `venn` CLI mechanics (discovery, the
read-vs-`--confirm` write gate, fail-closed); `tools/chat.md` is the generic
"reply to the principal" seam (Slack today). The `venn` CLI is a host
dependency (`requires:`) and is baked into the team's container image
(`build:`) so a headless Fly instance can reach the connectors.

## Workflows

- `adhoc` — open-ended "look into this" / "what's on for tomorrow" asks.
- `request` — the core pipeline: understand an inbound request, gather the
  context it needs (read-only) across email / calendar / tasks, then either
  act or — for anything outbound or irreversible — draft it and ask before
  sending, per the autonomy policy in `assistant-context.md`.
- `daily-briefing` — compile and post the morning briefing: today's
  schedule, what needs a reply, and what's due.

## Triggers

| Surface | Mechanism | What happens |
|---|---|---|
| Chat | `slack` chat | the principal asks for something -> `request` (or answer inline) |
| Daily | `daily-briefing` monitor (`24h`) | compile + post the morning briefing |
| Calendar | `calendar-watch` monitor (`30m`) | an imminent event or a new invite needing a response -> nudge the principal |
| Email | `inbox-watch` monitor (`15m`) | a genuinely important new email (key person / time-sensitive) -> nudge the principal |

The two watch monitors are proactive by design but gated hard against noise
and silenced during quiet hours (see `assistant-context.md`). Pause either
in `.bobi/monitors.yaml` if you want a purely reactive assistant.

## The autonomy policy

A personal assistant acts on the principal's behalf, so **how much it does
without asking is the principal's call**, not the framework's. The default
in `assistant-context.md` is: act freely on anything read-only or that only
affects the principal (reading mail, drafting, adding a private to-do,
holding a tentative slot), but **confirm before anything that leaves the
account or reaches another person** — sending an email, inviting/cancelling
on others, making a purchase. Raise or lower that line per principal.

## Setup

```bash
bobi agents install agents/personal-assistant --name personal-assistant
                                             # copies the pack into run/package/,
                                             # seeds run/workspace/,
                                             # prompts for env vars
bobi agent personal-assistant start          # runs the installed assistant
```

`install` prompts for the `${VAR}`s declared in `agent.yaml` and writes them
to `run/.env`: `SLACK_BOT_TOKEN` (the chat surface) and `VENN_API_KEY`
(email / calendar / tasks via Venn).

Then, before starting:
- Fill in `workspace/assistant-context.md` (seeded by install): who the
  principal is, their timezone and working/quiet hours, the key people, how
  they like replies drafted, the autonomy line, and the chat channel.
- Connect the principal's **Gmail**, **Google Calendar**, and **Google
  Tasks** in Venn, and note each connector's Venn `server_id` in the context
  file (see `tools/venn.md`). Swapping in Outlook / Todoist is a Venn
  connector change — the categories above are unchanged.
