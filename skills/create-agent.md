# Create Agent Teams

Guide the user through designing and generating a bobi agent team.
An agent team is a portable bundle — role prompts, workflows, monitors,
and tool guides — that defines a multi-agent system. The output is a
runnable `agents/<pack-name>/` directory.

## Output structure

```
agents/<pack-name>/
├── agent.md              # Human-readable team description
├── agent.yaml            # Team config (entry point, services, credentials)
├── roles/                # System prompts for each agent role
│   └── <role>/
│       └── ROLE.md
├── tools/                # Service interaction guides (loaded into all roles)
│   └── <service>.md
├── workflows/            # DAG definitions for multi-step processes
│   ├── adhoc.yaml        # Always include — open-ended task handler
│   └── <workflow>.yaml
├── monitors/             # Optional: background polling checks
│   └── defaults.yaml
├── context/              # Optional: reference files agents read on demand
│   └── <topic>.md
└── workspace/            # Optional: seed templates for user-owned domain files
    └── <template>.md
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

1. `agent.yaml`
2. `agent.md`
3. Role prompts (`roles/<name>/ROLE.md`)
4. Tools (`tools/<service>.md`) — if the team uses external services
5. Workflows (`workflows/*.yaml`)
6. Monitors (`monitors/defaults.yaml`) — if applicable
7. Context files (`context/*.md`) — if roles share reference material
8. Workspace templates (`workspace/`) — if users must supply domain content

### 4. Finalize

Show the directory tree, explain how to run (`bobi agents install
agents/<pack-name> --name <agent>` then `bobi agent <agent> start`), and
mention that installed package files are regenerated from source.

## File format reference

### agent.yaml

```yaml
version: "1.0.0"
entry_point: <starting-role>
chat: slack                       # optional: where humans talk to the team

services:
  - name: slack
    events: true
  - name: linear
    events: true

slack:
  bot_token: ${SLACK_BOT_TOKEN}   # secrets are ${VAR} refs, filled from .env

linear:
  api_key: ${LINEAR_API_KEY}
```

Only include services the team actually needs. `bobi agents install`
prompts for any `${VAR}` references and writes them to `run/.env`.

To give the team host tools, skills, or MCP servers, declare them under
`tool_library:` (a named catalog entry like `- venn`, or an inline dependency
with a required `success:`). See `docs/TOOL_LIBRARY.md` for the two ways to
declare a dependency (pinned `install:` vs guide-only) and how catalog entries
let you pull a tool in by name.

### agent.md

```markdown
# Team Name

One-paragraph description of what this agent system does.

## Roles

- **role-a** — what it does
- **role-b** — what it does

## Workflows

- `workflow-name` — when it triggers and what it does

## Setup

bobi agents install agents/pack-name --name my-agent
bobi agent my-agent start
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
    model: sonnet      # optional: override the team default for this prompt step
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

Runtime model selection lives in `agent.yaml` by default:

```yaml
brain:
  kind: codex          # omit the block entirely for Claude Code
  model: gpt-5-codex   # optional provider-specific model or alias
```

Individual roles can declare their own model, applied whenever an agent
launches with that role (subagents, workflow steps, monitor checks):

```yaml
roles:
  monitor: {model: haiku}    # cheap observe-and-report checks
  planner: {model: opus}
```

Role models pick a model within the team's brain, never a different brain.
Precedence: `--model` launch flag > step `model:` > `roles.<role>.model` >
`brain.model` > provider default.

Workflow prompt steps can override that team default for just one step:

```yaml
steps:
  - name: discover
    agent: prospect-targeter
    model: haiku
    prompt: "Find companies matching the wedge..."
```

For Claude-backed teams, `model` can be an alias such as `haiku`, `sonnet`, or
`opus`, or a full Claude model ID. Bobi passes provider-native model strings to
the selected backend; it does not translate model names across providers.

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
That costs an LLM call every interval, so reserve it for checks that
cannot be pulled mechanically.

For "items about X" needs, prefer a mechanical poll (`check: tool_poll`
or `venn_poll`, or a `command:`) plus a `relevance:` criterion - the
two-tier semantic gate. The poll runs at $0 per interval; only genuinely
new items are judged by a short cheap-model gate, and only relevant ones
publish the event:

```yaml
monitors:
  - name: billing-emails
    check: venn_poll
    interval: 5m
    service: work-gmail
    tool: list_messages
    query: '{"maxResults": 10, "q": "is:unread"}'
    id_field: id
    relevance: "emails about billing problems, refunds, or payment failures"
    event: monitor/email.billing
```

### Context files (context/*.md)

Team-shipped reference content — rubrics, methodology, output format
specs, examples — that agents read on demand. Installed frozen to
`run/package/context/`; reinstall restores them and `bobi agent <name> doctor`
flags hand-edits. Agents see an index (path + first line) in their
prompt, so make the first line of each file a one-line description.

Use context/ instead of tools/ when the content is reference material
rather than a service guide — tools load fully into every role's
prompt; context files cost nothing until an agent reads one.

### Workspace templates (workspace/)

Seed files for user-owned domain content: the things only the user can
fill in (positioning, source lists, configuration the team researches
against) and the directories agents write work products into. Install
copies `workspace/` to `run/workspace/` — each file only if
absent, so reinstall never overwrites what the user or agents wrote.

Reference these from role prompts by their installed path
(`workspace/<file>`), and tell the user in `agent.md` which files to
fill in before starting the team.

## Built-in CLI tools

Every agent has access to the full `bobi` CLI — messaging
(`agent <name> message`, `agent <name> ask`, `reply`), sub-agent
management (`agent <name> subagents launch`, `list`, `cancel`), and
observability (`agent <name> status`, `events`, `transcript`). Reference
these in role prompts so agents know how to
communicate and delegate.

See [`skills/bobi.md`](../skills/bobi.md) for the complete
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
   in the team source and reinstall.

## Important

- Generate a complete, working team — no placeholders or TODOs.
- Role prompts should reference `bobi` CLI commands the agent will use.
- Don't copy engineering-specific content into non-engineering packs.
- Write files to the team source directory, normally
  `$BOBI_HOME/agents/<name>/src/`.
