# Models, Runtimes, and Capabilities

bobi touches "other models" along **two independent axes**. They were
historically conflated under one "multi-model" connections registry — including
an overloaded `gateway` kind that meant different things on each axis — which is
the ambiguity this doc exists to remove.

## The two axes

### Axis 1 — the runtime the agent runs *on*

Every bobi agent is an instance of an agent runtime ("harness"). Today that
is hardcoded to the Claude Code SDK (`session.py`, `subagent.py`). Two things
live on this axis:

- **Runtime pluggability** (future): run an agent on a different harness — Codex
  CLI, Gemini CLI — as a first-class node. Large lift: the inbox / drain / hooks /
  session-rotation contract in `session.py` + `subagent.py` must be factored out
  of the Claude-specific code first.
- **Backend selection**: keep the Claude Code loop but point it at a different
  model/endpoint via `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` (e.g. a cheap local
  SLM behind a LiteLLM/Ollama Anthropic-compat shim). This is the lever for
  cheap-inference work such as monitor-cost reduction — tracked separately in
  **#327 / `MONITOR_COST.md`**, not here.

### Axis 2 — capabilities the agent calls *out to*

A running agent invoking another model, another agent, or a service for a
sub-task: image generation, a one-shot completion from GPT/Gemini, delegating a
task to Codex.

- **Direction: CLI-first.** A capability is a CLI the agent shells out to —
  binary baked into the image, credential + config from env, usage documented in
  a `tools/*.md` guide. The shell is the universal substrate across every Axis-1
  runtime (all are terminal-driving agents), and a CLI keeps secrets out of the
  model's context. Live examples: `aichat` (call other models), `codex` (delegate
  to an agent), `gh`, `venn`.
- **Removed: connection-kind + built-in MCP injection.** Historically
  `agent.yaml` `connections` declared a `kind` and `mcp/inject.py` auto-injected
  a bobi-shipped MCP server for it. Only `image` and `codex` were ever
  implemented; both moved to the CLI-first model (`image` → #397, `codex` →
  #285), and the shim itself — `inject.py`, `codex_server.py`, the
  `ConnectionEntry` config primitive, and the `connections:` agent.yaml block —
  was dismantled entirely in **#403**. A stray `connections:` block in an
  `agent.yaml` is now ignored (no longer part of the schema). The `gateway`,
  `chat`, and `embedding` kinds were declared but never implemented; the
  `gateway` call-out role is the baked **`aichat`** CLI — do not reintroduce a
  `gateway` connection kind. (The Axis-1 backend swap in #327 is a different
  mechanism that merely reused the same name.)
- **If a future capability genuinely needs MCP injection** (a *data channel*
  requiring streaming, native image content, or sampling callbacks — not
  secrets, discovery, or plain request/response, which a CLI covers more
  portably), reintroduce it deliberately rather than reviving this shim.

## Cost attribution (shipped, axis-independent)

`SessionEntry` tracks `model`, `provider`, `total_cost_usd`, and `model_usage`
(keyed by `provider:model`). Surfaced via:

```bash
bobi costs [--by model|role|session]
```

This is accounting; it applies regardless of which axis a model call came from.

## Status

- **Shipped:** all Axis-2 capabilities are CLI-first — `aichat` (model calls),
  `codex` (delegate to an agent), image generation via a direct Images API call;
  cost attribution. The legacy connections registry + built-in MCP injection
  shim was removed entirely (#397, #285, #403).
- **Direction:** new Axis-2 capabilities ship CLI-first (binary + env + guide),
  not new connection kinds / MCP shims.
- **Separate tracks:** Axis-1 runtime pluggability (future); Axis-1 backend
  selection for cheap inference / monitor cost (**#327**).
