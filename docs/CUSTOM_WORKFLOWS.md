# Custom Workflows

Modastack's workflow engine executes YAML-defined DAGs. The built-in
workflows handle software development (issue lifecycle, PR feedback, etc.),
but you can define custom workflows for any task type — content editing,
research, ops runbooks, data pipelines, or anything else.

## How workflow resolution works

The engine loads workflows from three directories, most-specific first:

| Priority | Location | Scope |
|----------|----------|-------|
| 1 (highest) | `<repo>/.modastack/workflows/` | Only matches events from that repo |
| 2 | `~/.modastack/workflows/` | Matches events from any repo |
| 3 (lowest) | `<modastack>/workflows/` | Built-in defaults |

When an event fires, the engine finds the most specific matching workflow.
A repo-specific workflow **overrides** a built-in with the same trigger.

## Quick start: override the default issue lifecycle

To customize how a specific repo handles issues:

```bash
mkdir -p <your-repo>/.modastack/workflows/
cp workflows/issue-lifecycle.yaml <your-repo>/.modastack/workflows/
# Edit the copy to customize
modastack workflow validate <your-repo>/.modastack/workflows/issue-lifecycle.yaml
```

The repo's version takes priority over the built-in for events from that repo.

## Quick start: non-dev workflow

1. Create a workflow YAML (see examples below)
2. Put it in `<repo>/.modastack/workflows/` or `~/.modastack/workflows/`
3. Use trigger filters to match only specific events (e.g., by label)
4. Validate: `modastack workflow validate path/to/workflow.yaml`

## Workflow YAML reference

### Structure

```yaml
name: my-workflow        # unique name
version: 1               # version number
trigger:
  event: task.assigned   # event type to match
  filter:                # optional — narrow which events trigger this
    labels: [research]   # only trigger on issues with this label
    repo: my-repo        # only trigger for this repo

nodes:
  step_one:
    type: action         # node type (see below)
    action: session.spawn
    params:
      issue_id: ${{event.issue_id}}
      repo: ${{event.repo}}

  step_two:
    type: bash
    command: echo "hello"
    depends_on: [step_one]   # runs after step_one completes
    when: "'yes' in ${{step_one.stdout}}"  # conditional execution
```

### Event types

These are the events the engine can trigger on:

| Event | Source | Data fields |
|-------|--------|-------------|
| `task.assigned` | Linear/GitHub Issues | `issue_id`, `title`, `body`, `repo`, `labels` |
| `github.pr.merged` | GitHub webhook | `issue_id`, `pr_url`, `repo` |
| `github.pr.review` | GitHub webhook | `issue_id`, `pr_url`, `action`, `repo` |
| `slack.message` | Slack | `text`, `channel_id`, `user_id` |
| `worker.state_change` | Worker poller | `issue_id`, `phase`, `repo` |

### Node types

#### `bash` — run a shell command

```yaml
build:
  type: bash
  command: npm run build
  cwd: ${{repo.path}}       # optional working directory
  env:                       # optional environment variables
    NODE_ENV: production
  timeout: 120               # seconds (default: 300)
```

#### `action` — call a registered handler

```yaml
notify:
  type: action
  action: slack.post         # registered action name
  params:
    channel_id: ${{config.slack_dm_channel}}
    text: "Deploy complete"
```

Built-in actions: `slack.post`, `ticket.move`, `ticket.comment`, `session.spawn`

#### `prompt` — inject text into an engineer session

```yaml
implement:
  type: prompt
  session: ${{event.issue_id | lower}}
  inject: |
    Build the feature described in the spec.
    When done, update handoff with phase: implement_complete.
  wait_for:
    phase: implement_complete  # poll handoff file for this phase
  timeout: 3600
  outputs:
    pr_url: ${{handoff.pr_url}}  # extract from handoff after completion
```

The `inject` text is sent to the engineer's Claude Code session. The engine
then polls the handoff file until `phase` matches `wait_for.phase`.

The injected text doesn't have to be a slash command — it can be any
instruction. This is how you make the agent do non-dev tasks: just describe
what you want in plain text.

#### `manager` — consult the manager LLM

```yaml
assess:
  type: manager
  prompt: |
    Review the research findings and decide if they're complete.
    Reply with "complete" or "needs_more".
  timeout: 300
```

The manager session receives the prompt and its response is available
as `${{node_id.output}}`.

#### `approval` — wait for human input

```yaml
human_review:
  type: approval
  listen_for:
    source: slack
    match: "approved"
    channel_id: ${{config.slack_dm_channel}}
  timeout: 86400   # 24 hours
```

Blocks until a matching Slack message arrives.

#### `gate` — conditional branching

```yaml
route:
  type: gate
  branches:
    fast_path:
      when: "'trivial' in ${{triage.complexity}}"
    full_path:
      when: "'complex' in ${{triage.complexity}}"
  fallback: full_path  # if no branch matches
```

Gate outputs: `${{route.branch}}` (which branch matched) and `${{route.goto}}`
(the goto target, if set). Use `when:` on downstream nodes to conditionally
execute based on the gate result.

### Variable scopes

Variables use `${{scope.key}}` syntax. Available scopes:

| Scope | Source | Example |
|-------|--------|---------|
| `event` | Trigger event data | `${{event.issue_id}}`, `${{event.title}}` |
| `config` | Global config | `${{config.slack_dm_channel}}` |
| `repo` | Per-repo `.modastack.yaml` | `${{repo.project}}`, `${{repo.test_command}}` |
| `handoff` | Handoff file (in prompt nodes) | `${{handoff.phase}}`, `${{handoff.pr_url}}` |
| `<node_id>` | Output of a completed node | `${{my_step.stdout}}`, `${{assess.output}}` |

#### The `repo` scope

The `repo` scope pulls from two sources:

1. **Built-in fields** from `.modastack.yaml`: `path`, `task_tracking`, `project`, `test_command`
2. **Custom fields** from the `context:` section of `.modastack.yaml`

```yaml
# .modastack.yaml
context:
  content_dir: content/blog
  publish_command: npm run publish
  deploy_env: staging
  review_channel: C0CONTENT
```

These become `${{repo.content_dir}}`, `${{repo.publish_command}}`, etc.
in your workflow YAML.

### Filters

Pipes transform variable values:

| Filter | Effect | Example |
|--------|--------|---------|
| `lower` | Lowercase | `${{event.issue_id \| lower}}` |
| `upper` | Uppercase | `${{event.title \| upper}}` |

### Conditions (`when:`)

Nodes can have a `when:` field for conditional execution:

```yaml
when: "${{route.branch}} == 'fast_path'"
when: "'spec' in ${{assess.output}}"
when: "${{triage.complexity}} != 'trivial'"
when: "${{gate.branch}} == 'a' or ${{gate.branch}} == 'b'"
```

Supported operators: `==`, `!=`, `in`, `not in`, `and`, `or`, `not`

## Per-repo context

Add arbitrary key-value pairs to `.modastack.yaml` under `context:`.
These are available as `${{repo.key}}` in any workflow triggered by
events from that repo.

```yaml
# .modastack.yaml for a docs repo
task_tracking:
  system: github-issues
  project: DOCS
  trigger_labels: [agent]

context:
  content_dir: docs/
  style_guide: docs/STYLE.md
  publish_command: mkdocs build && mkdocs gh-deploy
  slack_channel: C0DOCS
```

```yaml
# .modastack/workflows/docs-update.yaml
name: docs-update
version: 1
trigger:
  event: task.assigned
  filter:
    labels: [docs]

nodes:
  edit:
    type: prompt
    session: ${{event.issue_id | lower}}
    inject: |
      Edit the docs following the style guide at ${{repo.style_guide}}.
      Content lives in ${{repo.content_dir}}.
    wait_for:
      phase: edit_complete
    timeout: 1800
  # ...
```

## Examples

See `workflows/examples/` for complete workflow files:

- **`content-review.yaml`** — Content editing with draft/review/revision/publish cycle
- **`research.yaml`** — Research tasks with investigation, findings review, and approval

## CLI commands

```bash
# List all workflows (shows source tier for each)
modastack workflow list

# Validate a workflow file
modastack workflow validate path/to/workflow.yaml

# Show active workflow runs
modastack workflow status
```

## Tips

- **Filter by label** to route different task types to different workflows.
  Use `trigger.filter.labels: [content]` and label issues accordingly.

- **The prompt node is your escape hatch.** The `inject` text doesn't have
  to be a slash command — it can be any instruction. Write prose describing
  what the agent should do.

- **Use the manager node for decisions.** Instead of hardcoding routing
  logic, ask the manager LLM to reason about what to do next.

- **Start simple.** A three-node workflow (spawn → prompt → cleanup) is
  a valid workflow. Add complexity only when you need it.

- **Test with `modastack workflow validate`** before deploying. It checks
  the DAG structure, reports variable scopes, and catches typos.

- **Override, don't fork.** Put your custom version in the repo's
  `.modastack/workflows/` directory. The engine picks it up automatically
  without touching the built-in defaults.
