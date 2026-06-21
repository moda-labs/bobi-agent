# Other models (`aichat`)

Call another LLM (GPT, Gemini, Llama, etc.) for a **one-shot answer** via the
`aichat` CLI — a second opinion, a model that's better at a specific thing, or
cheap bulk text work. The binary ships in the base image; no per-team install.

> A model call is not agent delegation. `aichat` returns one completion with no
> tools and no loop. To hand an actual task to an autonomous coding agent (edit
> files, run commands, iterate), use **Codex** (`codex exec "<task>"`), not this.

## Setup (per team, via env)

`aichat` reads its credential and default provider from the environment — like
`gh` (`GH_TOKEN`) and `venn` (`VENN_API_KEY`). Set both in `.modastack/.env`
(and Fly secrets in prod):

```
OPENROUTER_API_KEY=<key>
AICHAT_PLATFORM=openrouter
```

`AICHAT_PLATFORM` is what lets `aichat` run headless with no config file — without
it, a first invocation can drop into interactive setup. With it set, `aichat`
routes through OpenRouter, so one key reaches essentially any model.

## One-shot completion

```bash
aichat -m openrouter:openai/gpt-4o            "Summarize this stack trace: ..."
aichat -m openrouter:google/gemini-2.5-pro    "Spot risky changes in this diff."
```

`-m <provider>:<model>` selects the model; omit it to use the default.

## Pipe input in

```bash
cat build.log | aichat "What failed here and what's the likely cause?"
gh pr diff 412 | aichat -m openrouter:google/gemini-2.5-pro "Review this diff."
```

## When to use / not use

- **Use** for a second opinion from a different/stronger model, a model better at
  a specific task, or cheap high-volume text classification.
- **Don't use** when you want a task *done* (that's `codex exec`), or when you can
  just answer it yourself — don't round-trip a prompt for something you know.

## Notes

- Output is plain text on stdout; add `--no-stream` for a single block.
- Auth is environment-only. An auth error means the key isn't set in this
  instance — surface that; never pass a key inline.
- `aichat --list-models` shows what's reachable with the current credentials.
