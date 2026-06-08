# Create Agent Packages

Guide the user through designing and generating a modastack agent package.
An agent package is a portable bundle — role prompts, workflows, monitors,
and tool guides — that defines a multi-agent system. The output is a
runnable `agents/<pack-name>/` directory.

## Output structure

```
agents/<pack-name>/
├── agent.md              # Human-readable packagedescription
├── defaults.yaml         # Entry point config (starting role, event sources)
├── roles/                # System prompts for each agent role
│   └── <role>/
│       └── ROLE.md
├── tools/                # Service interaction guides (loaded into all roles)
│   └── <service>.md
├── workflows/            # DAG definitions for multi-step processes
│   ├── adhoc.yaml        # Always include — open-ended task handler
│   └── <workflow>.yaml
└── monitors/             # Optional: background polling checks
    └── defaults.yaml
```

## How to guide the conversation

### 1. Understand the use case

Ask focused questions (2-3, not a wall):
- What problem are they solving? What does success look like?
- What events trigger work? (webhooks, messages, schedules, manual)
- What outputs should the system produce? (PRs, messages, reports, etc.)
- How many distinct roles are needed? Coordinator + workers, or peers?

### 2. Propose the design

Based on answers, propose:
- **Roles**: how many agents, what each does, their relationship
- **Workflows**: sequences of steps and routing decisions
- **Event sources**: integrations needed (github, slack, linear, etc.)
- **Monitors**: conditions worth polling for (if any)

Explain in plain language. Get agreement before writing files.

### 3. Generate the pack

Write files in this order, explaining each as you go:

1. `defaults.yaml`
2. `agent.md`
3. Role prompts (`roles/<name>/ROLE.md`)
4. Tools (`tools/<service>.md`) — if the packageuses external services
5. Workflows (`workflows/*.yaml`)
6. Monitors (`monitors/defaults.yaml`) — if applicable

### 4. Finalize

Show the directory tree, explain how to run (`modastack start <pack-name>`),
and mention `.modastack/` overrides for per-project customization.

## File format reference

### defaults.yaml

```yaml
role: <entry-point-role>
event_sources:
  - github
  - slack
```

Only include event sources the packageactually needs.

### agent.md

```markdown
# packageName

One-paragraph description of what this agent system does.

## Roles

- **role-a** — what it does
- **role-b** — what it does

## Workflows

- `workflow-name` — when it triggers and what it does

## Setup

modastack start pack-name
```

### Role prompts (roles/<name>/ROLE.md)

Each role is a folder with a `ROLE.md`. The folder can hold additional
resources the role needs.

Principles for role prompts:
- **Start with identity**: "You are a [role] that [does what]."
- **Define scope**: What this role does and does NOT do.
- **Be operational**: Concrete instructions, CLI commands, decision tables.
- **Include examples**: Show what good output looks like.
- **Set boundaries**: What to delegate vs handle directly.

Coordinator structure:
```markdown
# Role Title

You are a [role] for [scope]. You receive events and [what you do].

## Event handling

| Event type | Action |
|---|---|
| ... | ... |

## Operational rules

- ...
```

Worker structure:
```markdown
# Role Title

You are a [role] that [does what]. You receive tasks from [who] and
[produce what output].

## How you work

Step-by-step instructions.

## Quality standards

What "done" looks like.
```

Role prompts: 300-600 lines for complex roles, under 100 for simple workers.
Every line should be an instruction the agent will use — no filler.

### Tools (tools/<service>.md)

Service interaction guides loaded into every agent automatically.

```markdown
# Service Name

Brief description.

## Operation A

\```bash
command <args>
\```

## Key rules

- Important constraint
```

Name after the service: `github.md`, `slack.md`, `linear.md`, etc.

### Workflows (workflows/*.yaml)

```yaml
name: workflow-name
trigger: >
  When [condition]. One sentence.
description: >
  What this workflow does end-to-end.

steps:
  - name: step-name
    agent: role-name
    prompt: |
      Instructions for this step.
    handoff:
      required: [field1]
      optional: [field2]
    timeout: 1800

  - name: route-step
    if: "field1 == true"
    goto: step-a
    else: step-b

  - name: wait-step
    await: event-type
    timeout: 86400
```

Step types:
- **Prompt step**: `agent` + `prompt` — agent executes and writes handoff
- **Route step**: `if` + `goto` + `else` — deterministic branch
- **Await step**: `await` — suspends until external event arrives

Always include `adhoc.yaml`:
```yaml
name: adhoc
trigger: >
  For any ad-hoc task that doesn't match a more specific workflow.
description: >
  Open-ended task with no structured lifecycle.

steps:
  - name: task
    prompt: "${{input.task}}"
```

### Monitors (monitors/defaults.yaml)

```yaml
monitors:
  - name: check-name
    description: What this monitor checks
    interval: 15m
    event: monitor/check.detected
    check: function_name      # Optional: native check
```

Monitors without `check:` are executed by a short-lived agent that
evaluates the description and posts an event only if something is found.

## Built-in CLI tools

Every agent has access to the full `modastack` CLI — messaging
(`message`, `ask`, `slack-reply`), agent management (`agents launch`,
`agents list`, `agents cancel`), and observability (`status`, `events`,
`transcript`). Reference these in role prompts so agents know how to
communicate and delegate.

See [`skills/modastack.md`](../skills/modastack.md) for the complete
command reference.

## Design principles

1. **Coordinator + workers**: Most packs have one persistent coordinator
   that receives events and dispatches workers. Workers are short-lived.

2. **Workflows encode process, not logic**: A workflow defines WHAT steps
   happen in WHAT order. The HOW lives in role prompts.

3. **Handoffs are contracts**: Required handoff fields are the API between
   steps. Design them carefully.

4. **Monitors fill webhook gaps**: Only add monitors for conditions no
   webhook covers (stale items, drift, health checks).

5. **Keep it simple**: Fewer roles and workflows to start. Users add more
   via `.modastack/` overrides.

## Important

- Generate a complete, working package— no placeholders or TODOs.
- Role prompts should reference `modastack` CLI commands the agent will use.
- Don't copy engineering-specific content into non-engineering packs.
- Write files to `agents/<pack-name>/` in the current working directory.
