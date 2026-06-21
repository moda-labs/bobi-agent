# Models, Runtimes, and Capabilities

modastack touches "other models" along **two independent axes**. They were
historically conflated under one "multi-model" connections registry — including
an overloaded `gateway` kind that meant different things on each axis — which is
the ambiguity this doc exists to remove.

## The two axes

### Axis 1 — the runtime the agent runs *on*

Every modastack agent is an instance of an agent runtime ("harness"). Today that
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
- **Legacy: connection-kind + built-in MCP injection.** `agent.yaml`
  `connections` declare a `kind`; `mcp/inject.py` auto-injects a
  modastack-shipped MCP server for it. Only `image` and `codex` were ever
  implemented this way. Reserve MCP injection for capabilities whose *data
  channel* genuinely needs it (streaming, native image content, sampling
  callbacks) — not for secrets, discovery, or plain request/response, which a CLI
  covers more portably.
- **Retired:** the `gateway`, `chat`, and `embedding` connection kinds were
  declared in the registry but never implemented. The `gateway` call-out role is
  now the baked **`aichat`** CLI — do not reintroduce a `gateway` connection
  kind. (The Axis-1 backend swap in #327 is a different mechanism that merely
  reused the same name.)

## Cost attribution (shipped, axis-independent)

`SessionEntry` tracks `model`, `provider`, `total_cost_usd`, and `model_usage`
(keyed by `provider:model`). Surfaced via:

```bash
modastack costs [--by model|role|session]
```

This is accounting; it applies regardless of which axis a model call came from.

## Status

- **Shipped:** connections registry + `image` MCP server (the legacy Axis-2
  pattern); cost attribution; `aichat` baked as the Axis-2 model-call CLI.
- **Direction:** new Axis-2 capabilities ship CLI-first (binary + env + guide),
  not new connection kinds / MCP shims.
- **Separate tracks:** Axis-1 runtime pluggability (future); Axis-1 backend
  selection for cheap inference / monitor cost (**#327**).
