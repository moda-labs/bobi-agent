# OpenAI image generation

Generate images with the baked `openai` CLI — `openai images generate`. This is
a direct **capability call** (like `codex` or `venn`): you run the command
yourself; it is not agent delegation. Use it when a task needs a generated
image — a hero/illustration for a doc, a mockup, a diagram backdrop, an avatar.

`OPENAI_API_KEY` comes from `run/.env` — never hard-code or log it.

## The contract: write a file, then read it

Write the image to `/tmp/*.png`, then open that path — never let raw base64 or
binary land in your context.

```bash
openai images generate \
  -p "translucent green glass cube on a neutral studio background" \
  --size 1024x1024 \
  -o /tmp/hero-$RANDOM.png
echo /tmp/hero-*.png   # print the path so you can open it
```

Reuse the saved file downstream — e.g. a Slack upload or a PR attachment —
rather than regenerating. Delete intermediates you no longer need.

## Parameters

- `-p/--prompt` — the image description.
- `--size` — e.g. `1024x1024` (default), `1024x1536`, `1536x1024`.
- `-n` — number of images.
- `-o/--output` — destination path under `/tmp/`.

## Failures

A `401` means `OPENAI_API_KEY` is unset or invalid — surface that, don't work
around it. The `requires:` preflight blocks dispatch if the CLI is missing.
