# Agent Environment Propagation

`modastack.env.child_agent_env()` is the single contract for parent-to-child
agent environment propagation. `launch_agent()` must pass its returned mapping
directly to the detached child process.

## Inherited

- Parent runtime environment: copied so ambient tool configuration and
  credentials remain available to child agents.
- Tool lookup path: `PATH` is normalized by `agent_spawn_env()` before launch,
  keeping MCP preflight and runtime command lookup identical.
- Installed credential file: `.modastack/.env` is merged into the child env
  without overriding explicit parent process values.
- Credential variables: any credential present in the parent environment or
  `.modastack/.env` is propagated, including brain/tool/service credentials
  such as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `VENN_API_KEY`, `GH_TOKEN`,
  `GITHUB_TOKEN`, `LINEAR_API_KEY`, and `SLACK_BOT_TOKEN`.

## Rewritten

- `MODASTACK_ROOT` is always set to the spawner's bound installation root. A
  stale parent value is never inherited.
- `MODASTACK_BRAIN` is set from the installed team's `brain.kind` when present.
  This prevents a child for a Codex-backed team from inheriting a stale Claude
  selection, or the reverse.
- If the installed team has no `brain.kind`, `MODASTACK_BRAIN` is removed from
  the child environment so the framework default brain is selected.

## Not Inherited

- Child identity is not inferred from `cwd` and not inherited from an ambient
  `MODASTACK_ROOT`.
- Team brain selection is not inherited from an ambient `MODASTACK_BRAIN` when
  the installed team declares `brain.kind`.
- An ambient `MODASTACK_BRAIN` is not inherited by default-brain teams.
- `.modastack/.env` values do not override variables explicitly set in the
  parent process environment.

When a new runtime value must flow into child agents, add it to
`child_agent_env()` and cover it in `tests/test_env.py`. Do not patch
`launch_agent()` directly.
