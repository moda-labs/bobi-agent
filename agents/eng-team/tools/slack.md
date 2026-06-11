# Slack

Send and receive Slack messages via `modastack` CLI.

## Reply in a thread

```bash
modastack slack-reply -w <workspace> -c <channel> -t <thread_ts> "message"
```

Always reply in the thread — use the event's `ts` as `thread_ts` if no
`thread_ts` is present (starts a thread on the original message).

## Edit a placeholder message

When a Slack event arrives, the framework posts an "Evaluating…" placeholder
and includes a `placeholder_ts` field in the event. Use `--edit` to replace
the placeholder with your actual response:

```bash
modastack slack-reply -w <workspace> -c <channel> -t <thread_ts> --edit <placeholder_ts> "message"
```

This edits the placeholder in-place (no orphaned "Evaluating…") and clears
the "is thinking…" typing indicator. **Always use `--edit` when a
`placeholder_ts` is present in the event.** If no `placeholder_ts` exists,
reply normally without `--edit`.

## Send a new message

Omit `-t` to post a new top-level message instead of a threaded reply:

```bash
modastack slack-reply -w <workspace> -c <channel> "message"
```

## Key rules

- One thread = one person. Never leak one user's context into another thread.
- Keep responses concise and conversational.
- Use markdown formatting sparingly — Slack renders it differently than GitHub.
- For code snippets longer than one line, use triple-backtick blocks.
- When a `placeholder_ts` is in the event, use `--edit` for your first reply.
  Subsequent replies in the same thread should be posted normally (no `--edit`).
