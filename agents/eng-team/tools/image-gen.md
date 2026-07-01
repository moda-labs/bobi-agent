# Image generation

Generate images by calling the **OpenAI Images API** directly with `curl`. This
is a direct **capability call** - like `aichat` or `codex`, you run a command
yourself. It is NOT agent delegation and there is no MCP tool to call.

Use it when a task needs a generated image: a hero/illustration for a doc, a
mockup, a diagram backdrop, an avatar, etc.

> We call the HTTP API with `curl`, not the `openai` CLI. The `openai` CLI only
> exists in the package's 1.x line and it unconditionally sends the now-removed
> `response_format` parameter, so it `400`s against the current Images API for
> every model. `curl` + `jq` + `base64` are already on the image and work today.

## The contract: write a file, then read it

The API returns the image as base64 inside JSON. The convention is **generate ->
decode to one `/tmp/*.png` file -> print that exact path -> `Read` the path**.
Never let raw base64 land in your context; always go through a file.

```bash
set -o pipefail
out="$(mktemp /tmp/image-XXXXXX.png)"
prompt="translucent green glass cube on a neutral studio background"

jq -n --arg prompt "$prompt" \
  '{model:"gpt-image-1",prompt:$prompt,n:1,size:"1024x1024"}' |
curl -fsS https://api.openai.com/v1/images/generations \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d @- |
jq -e -r '.data[0].b64_json' |
base64 --decode > "$out"

printf '%s\n' "$out"
```

- `gpt-image-1` returns base64 (`data[0].b64_json`) - no expiring URL.
- `jq -n --arg` keeps prompts with quotes, backslashes, or newlines valid JSON.
- `mktemp` saves under `/tmp/` with a collision-resistant name.
- Reuse the saved file downstream, such as a Slack upload or PR attachment.

## Parameters

`model`, `prompt`, `size`, and `n` are the request body. Defaults that match the
retired image server: model `gpt-image-1`, size `1024x1024`, one image. Pick a
model your key can access. Full request/response shape:
<https://platform.openai.com/docs/api-reference/images/create>.

## Credentials and failures

- `OPENAI_API_KEY` is read from the environment. Never hard-code a key and never
  log it.
- `set -o pipefail` keeps `curl -fsS`, `jq -e`, and `base64 --decode` failures
  from being hidden by the final pipeline command.
- A 401 means the key is unset or invalid; unsupported models/sizes, quota/rate
  limits, and content-policy errors are also API failures to surface directly.
- If the output file is empty, re-run without the `| jq | base64` tail to inspect
  the raw error JSON, then report that error.
