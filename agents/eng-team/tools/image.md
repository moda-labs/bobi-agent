# Image generation

Generate images by calling the **OpenAI Images API** directly with `curl`. This
is a direct **capability call** — like `aichat` or `codex`, you run a command
yourself. It is NOT agent delegation and there is no MCP tool to call.

Use it when a task needs a generated image: a hero/illustration for a doc, a
mockup, a diagram backdrop, an avatar, etc.

> We call the HTTP API with `curl`, not the `openai` CLI. The `openai` CLI only
> exists in the package's 1.x line and it unconditionally sends the now-removed
> `response_format` parameter, so it `400`s against the current Images API for
> every model. `curl` + `jq` + `base64` are already on the image and work today.

## The contract: write a file, then `Read` it

The API returns the image as base64 inside JSON. The convention is **generate →
decode to `/tmp/*.png` → print the path → `Read` the path**. Never let raw
base64 land in your context; always go through a file.

```bash
curl -fsS https://api.openai.com/v1/images/generations \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-1","prompt":"translucent green glass cube on a neutral studio background","n":1,"size":"1024x1024"}' \
  | jq -e -r '.data[0].b64_json' | base64 --decode > /tmp/hero-$RANDOM.png
echo /tmp/hero-*.png   # print the path so you can Read it
```

- **`gpt-image-1`** returns base64 (`data[0].b64_json`) — no expiring URL. `jq`
  extracts it; `base64 --decode` writes the PNG.
- **Save under `/tmp/`** with a collision-resistant name (`/tmp/<slug>-$RANDOM.png`).
  Containers are per-agent and `/tmp` is private and ephemeral.
- **`Read` the saved path** to view the image natively, then reuse the same file
  downstream — e.g. `modastack slack-upload-file /tmp/hero-123.png …` or a PR
  attachment. Delete intermediates you no longer need.

## Parameters

`model`, `prompt`, `size`, and `n` are the request body. Defaults that match the
retired image server: model `gpt-image-1`, size `1024x1024`, one image. Pick a
model your key can access. Full request/response shape:
<https://platform.openai.com/docs/api-reference/images/create>.

## Credentials and failures

- **`OPENAI_API_KEY`** is read from the environment — never hard-code a key and
  never log it. If it's unset, `curl` sends an unauthenticated request and the
  API returns a 401; surface that, don't work around it.
- **Fail loudly.** `curl -fsS` exits non-zero on an HTTP error and `jq -e` exits
  non-zero if `b64_json` is missing, so a failed call leaves a non-zero pipeline
  rather than a silent empty PNG. If `/tmp/*.png` is 0 bytes, re-run without the
  `| jq | base64` tail to see the raw error JSON (bad key, model access, quota),
  then report it.
