# Chat (Slack)

Send and receive chat messages, files, and images via `bobi` CLI.

Every chat event carries a `conversation:` line - the reply address. Echo it
back verbatim; never assemble platform addressing yourself.

## Reply in a thread

```bash
bobi reply <conversation> "message"
```

The conversation reference already anchors the originating thread. When the
event carries a `placeholder_ts` field, resolve the placeholder instead of
posting a new message:

```bash
bobi reply <conversation> --edit <placeholder_ts> "message"
```

## Upload a file or image

```bash
bobi reply <conversation> --file <file_path> "optional comment"
bobi reply <conversation> --file ./screenshot.png --title "Screenshot" "Here's what I see"
```

## Read a thread

Fetch all messages (and file metadata) in the conversation:

```bash
bobi read-conversation <conversation>
bobi read-conversation <conversation> --json-output
bobi read-conversation <conversation> -n 50
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
- Write plain markdown - the gateway converts it for the channel.
- For code snippets longer than one line, use triple-backtick blocks.
- When receiving images, consider passing them to vision models for analysis.
- File downloads require the bot token for authentication.
- `slack-reply`, `slack-upload-file`, and `slack-read-thread` are deprecated
  shims - do not use them in new work.
