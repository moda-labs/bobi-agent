# Slack

Send and receive Slack messages, files, and images via `modastack` CLI.

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

## Upload a file or image

```bash
modastack slack-upload-file <file_path> -w <workspace> -c <channel>
modastack slack-upload-file ./screenshot.png -w <workspace> -c <channel> -t <thread_ts> --title "Screenshot" --comment "Here's what I see"
```

Options: `--title`, `--comment`, `--thread/-t`, `--filename` (override name).

## Read a thread

Fetch all messages (and file metadata) in a Slack thread:

```bash
modastack slack-read-thread -w <workspace> -c <channel> -t <thread_ts>
modastack slack-read-thread -w <workspace> -c <channel> -t <thread_ts> --json-output
modastack slack-read-thread -w <workspace> -c <channel> -t <thread_ts> -n 50
```

## Receiving files and images

When a user sends a file or image in Slack, the event's `fields.files`
contains a JSON array of file metadata:

```json
[{"id": "F123", "name": "image.png", "mimetype": "image/png", "url_private": "https://files.slack.com/..."}]
```

Parse with `json.loads(event.fields.files)`. To download a file, use the
`url_private` with the bot token as a Bearer auth header.

## Key rules

- One thread = one person. Never leak one user's context into another thread.
- Keep responses concise and conversational.
- Use markdown formatting sparingly — Slack renders it differently than GitHub.
- For code snippets longer than one line, use triple-backtick blocks.
- When receiving images, consider passing them to vision models for analysis.
- File downloads require the bot token for authentication.
