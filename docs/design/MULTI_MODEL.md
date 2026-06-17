# Multi-Model Support

## Phase 1: Connections + Image Tool + Cost Attribution

### Connections Registry

External model connections are declared in `agent.yaml`:

```yaml
connections:
  - name: openai-images
    kind: image
    provider: openai
    api_key: ${OPENAI_API_KEY}
    model: gpt-image-1

  - name: gemini-chat
    kind: chat
    provider: google
    api_key: ${GOOGLE_API_KEY}
    model: gemini-2.5-pro
```

Fields:
- `name` (required): Unique connection identifier
- `kind` (required): `image`, `chat`, `embedding`, or `gateway`
- `provider`: Provider name (`openai`, `google`, etc.)
- `api_key`: API key, supports `${ENV_VAR}` interpolation
- `model`: Default model for this connection
- Extra fields are preserved in `ConnectionEntry.extra`

### Built-in MCP Image Server

When image connections are configured, modastack auto-injects a
`modastack-image` MCP server into agent sessions. The server exposes:

```
generate_image(connection, prompt, size) -> {url, b64_json, revised_prompt}
```

Supported providers: OpenAI (DALL-E / GPT-Image), Google (Imagen).

### Cost Attribution

SessionEntry now tracks `model`, `provider`, `total_cost_usd`, and
`model_usage` (a dict keyed by `provider:model` with per-model cost
and token counts).

### CLI

```bash
modastack costs              # total cost by provider
modastack costs --by model   # breakdown by model
modastack costs --by role    # breakdown by agent role
modastack costs --by session # breakdown by session
```

## Phase 2: Default Model + Per-Agent Selection

(Not yet implemented)

## Phase 3: Native Non-Anthropic Harnesses

(Not yet implemented)
