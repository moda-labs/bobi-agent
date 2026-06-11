# Slack

Send and receive Slack messages via `modastack` CLI.

## Reply in a thread

```bash
modastack slack-reply -w <workspace> -c <channel> -t <thread_ts> "message"
```

Always reply in the thread — use the event's `ts` as `thread_ts` if no
`thread_ts` is present (starts a thread on the original message).

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
