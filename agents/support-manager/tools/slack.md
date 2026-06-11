# Slack

Post issue alerts and the daily report to the support channel, and answer
human asks, via the `modastack` CLI. The workspace and channel come from
`workspace/support-context.md`; both per-issue alerts and the daily report
go to the same channel.

## Reply in a thread

```bash
modastack slack-reply -w <workspace> -c <channel> -t <thread_ts> "your response"
```

Take `workspace`, `channel`, and `thread_ts` from the event data. For a
Slack-originated ask, always reply in the thread — use the event's `ts` as
`thread_ts` if none is present.

## Post a per-issue alert (real issues only)

When `triage-issue` files a real issue, post to the support channel. Lead
with what is broken and the blast radius, then the ticket link and a
one-line hypothesis:

```
:rotating_light: <symptom> — <blast radius, e.g. "~40 users, 120 events since 09:00">
<https://linear.app/.../BAO-142|BAO-142> filed (Triage). Likely cause: <hypothesis>. Source: <posthog/email link>
```

Use Slack-formatted links: `<https://example.com|label>`. Do **not** post
an alert for a not-real verdict — those wait for the daily report.

## Post the daily report

One message to the support channel. Lead with counts, then the two groups:

```
Support report — <date>: 3 filed, 5 dismissed.

*Filed*
• <symptom> — <blast radius> — <BAO-142 link>
• ...

*Dismissed (not real)*
• <signal> — <why not real, e.g. "staging traffic">
• ...

Watching: <one line — a recurring near-miss or noise source worth tuning>
```

## Key rules

- **One thread = one person.** Never leak one requester's report or result
  into another's reply.
- **Alerts are for real issues only.** Keep the channel signal-rich:
  dismissals are summarized once a day, not pinged individually.
- **Match the voice.** Per `workspace/support-context.md`: lead with what is
  broken, specific over vague, no em dashes, no filler, never close on a
  summary.
- The bot must be invited to the channel (`/invite @<bot>`) or posts fail.
