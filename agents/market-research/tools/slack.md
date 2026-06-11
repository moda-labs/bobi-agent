# Slack

Talk to humans and deliver research over Slack via the `modastack` CLI.

## Reply in a thread

```bash
modastack slack-reply -w <workspace> -c <channel> -t <thread_ts> "your response"
```

Take `workspace`, `channel`, and `thread_ts` from the event data. Always
reply in the thread — use the event's `ts` as `thread_ts` if no
`thread_ts` is present (this starts a thread on the original message).

## Post a research digest

For the weekly landscape digest or a completed research brief, post a
concise readout in the relevant channel/thread. Lead with what changed,
not a summary. Link issues, posts, and sources with Slack-formatted links:
`<https://example.com/post|source title>`.

## Key rules

- **One thread = one person.** Each thread is one person's conversation.
  Never reference or leak one user's request, topic, or result into
  another user's reply.
- **Attribute spawned work to its requester.** When dispatching a worker
  on behalf of a Slack user, pass the requester context so the result
  goes back to the right thread:
  ```bash
  modastack agents launch -w topic-research --role topic_researcher \
    --task 'Research: <topic>. Requested by: {"from":"<user>","workspace":"<ws>","channel":"<ch>","thread_ts":"<ts>"}'
  ```
- **Match the voice.** Follow the voice constraints in
  `workspace/moda-context.md`: no em dashes, no filler, specific over
  vague, never close on a summary.
- Keep conversational replies short. Save the depth for the brief/digest.
