# Chat surface (talking to the principal)

This is the seam for talking to the principal. Today it's Slack; the role
prompt never names Slack directly so a different surface (WhatsApp, Telegram,
email-to-self) can swap in here without touching the role. Everything below
is "how to reply to the principal on the configured surface."

## Reply in the principal's thread

```bash
bobi reply <conversation> "message"
```

Take `<conversation>` from the event's `conversation:` line and echo it back
verbatim - it already addresses the right thread. Write plain markdown; the
gateway formats it for the surface. A personal assistant's whole
relationship is one ongoing thread; keep it there.

When the event carries a `placeholder_ts` field, resolve the placeholder
instead of posting a new message:

```bash
bobi reply <conversation> --edit <placeholder_ts> "message"
```

## Post a new message (proactive)

The daily briefing and the watch nudges aren't replies - post to the
principal's configured conversation reference recorded in
`workspace/assistant-context.md`.

```bash
bobi reply <conversation> "message"
```

## Share a file or image

```bash
bobi reply <conversation> --file <file_path> "optional comment"
```

Use for a research write-up, a screenshot, or an exported document the
principal asked for.

## Read the thread's history

```bash
bobi read-conversation <conversation>
```

## Voice

- Lead with the outcome; specifics over hedging.
- Match the tone in `workspace/assistant-context.md` - this is the
  principal's assistant, not a generic bot.
- Keep it short. One thread, conversational. No filler, no closing summary
  of what you might do next - end on the result.
- **One principal.** Never reference one person's mail/calendar/tasks in a
  surface another person can see.
