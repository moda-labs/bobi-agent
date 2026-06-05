# Building and Testing Workflows

Workflows are YAML files that define a sequence of steps for an AI agent. Each step is a plain-English prompt. The agent does all the work — the workflow just tells it what to do next and validates the output.

Workflows aren't limited to software engineering. You can build workflows for content review, research, data processing, or any multi-step task an AI agent can handle.

## Quick start

1. Create a YAML file in your repo at `.modastack/workflows/my-workflow.yaml`
2. Run it: `modastack agents launch -w my-workflow --role engineer --repo . --task "your context here"`
3. Watch it: `modastack transcript show <session-name>`

## Workflow structure

```yaml
name: my-workflow
trigger: task.assigned          # optional: event that auto-triggers this workflow
description: >                  # shown in `modastack workflow list`
  One-line description of what this workflow does.

steps:
  - name: step-name
    prompt: |
      What the agent should do in this step.
    handoff:
      required: [field1, field2]
      optional: [field3]
    timeout: 1800
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Workflow name (used in `modastack agents launch -w <name>`) |
| `trigger` | no | Event type that auto-triggers this workflow (e.g. `task.assigned`) |
| `description` | no | Human-readable description shown in `modastack workflows list` |
| `steps` | yes | Ordered list of steps |

## Step types

### Prompt step (default)

The agent receives the prompt, does the work, and writes a handoff file.

```yaml
- name: research
  prompt: |
    Research the topic and summarize your findings.
    Include at least 3 sources.
  handoff:
    required: [summary, source_count]
    optional: [recommendations]
  timeout: 1800
```

The orchestrator:
1. Injects the prompt into the agent session
2. Waits for the agent to finish
3. Reads the handoff file
4. Validates required fields are present
5. If fields are missing, re-prompts the agent (up to 2 retries)
6. Moves to the next step

**Handoff contract:** The `handoff` section declares what the agent must write. The orchestrator tells the agent exactly where to write it and what format to use — you don't need to include file paths in your prompt.

**Timeout:** Seconds before the step is considered failed. Default: 1800 (30 minutes).

### Route step

Deterministic branching based on handoff values from a previous step. No agent involved.

```yaml
- name: route
  if: "priority == high"
  goto: urgent-path
  else: normal-path
```

Route conditions support:
- Equality: `field == value`
- Inequality: `field != value`
- Boolean: `field == true`, `field == false`
- Containment: `'keyword' in field`
- Logic: `condition1 and condition2`, `condition1 or condition2`

Values come from the most recent handoff. The field names are bare — no `${{}}` wrapper needed.

### Await step

Suspends the workflow until an external event arrives (e.g., human approval).

```yaml
- name: await_approval
  await: approval
  timeout: 86400
```

> Note: Await steps are not yet fully implemented. The workflow will suspend at this point.

## Variables

Prompts can reference variables using `${{scope.key}}` syntax:

| Scope | Available | Example |
|-------|-----------|---------|
| `input.task` | Always | The `--task` text from the CLI |
| `input.repo` | Always | The repo name |
| `input.issue_id` | Always | Parsed issue number or adhoc ID |
| `<step_name>.<field>` | After that step completes | `${{research.summary}}` |

Example:
```yaml
- name: review
  prompt: |
    Review the research from the previous step.
    Summary was: ${{research.summary}}
    Source count: ${{research.source_count}}
```

## Handoff files

Each step writes its output to a YAML file in the session directory:

```
~/.modastack/sessions/<session-name>/
├── state.json
├── handoff-research.yaml     # written by the research step
├── handoff-review.yaml       # written by the review step
└── log.jsonl
```

The orchestrator tells the agent the exact path and format. A handoff file is plain YAML:

```yaml
summary: "The topic has three main aspects..."
source_count: 5
recommendations: "Focus on aspect 2 for the best ROI"
```

## Session management

Each workflow run gets a deterministic session name: `wf-<workflow>-<repo>-<issue>`.

- **Active runs** have a live process (visible in `modastack status`)
- **Completed runs** remain in the sessions directory for history
- **Collision detection:** launching the same workflow for the same issue while one is running is rejected

The agent session persists across all steps — context accumulates. What the agent learns in step 1 is available in step 2.

## Testing a workflow

### 1. Validate the YAML

```bash
modastack workflow list
```

This loads all workflows and shows parse errors. Your workflow should appear with no errors.

### 2. Run it non-interactively

```bash
modastack agent -w my-workflow --repo . --task "test task" --non-interactive
```

`--non-interactive` means the agent won't try to ask the manager questions — it makes all decisions autonomously. Good for testing without a running manager.

### 3. Watch the progress

```bash
# Check status
modastack status

# Stream the log
modastack log <session-name>

# Follow mode (streams new output as it arrives)
modastack log <session-name> -f
```

### 4. Inspect handoffs

After the run, check the session directory:

```bash
ls ~/.modastack/sessions/<session-name>/
cat ~/.modastack/sessions/<session-name>/handoff-<step>.yaml
```

### 5. Run it again

Session names are deterministic, so a second run for the same issue reuses the name. If the previous run completed, a fresh session is created. If it failed, the agent resumes with its previous context.

## Workflow resolution

Workflows are loaded from three tiers (most specific wins):

1. **Repo-specific:** `<repo>/.modastack/workflows/` — overrides for this repo
2. **User-level:** `~/.modastack/workflows/` — your personal workflows
3. **Built-in:** `<modastack>/workflows/` — shipped defaults

Use `modastack workflow list` to see all loaded workflows and their sources.

## Examples

### Content review workflow

```yaml
name: content-review
description: >
  Review a document for clarity, accuracy, and tone.
  Produces a structured review with scores and suggestions.

steps:
  - name: read
    prompt: |
      Read the document at the path provided in the task.
      Summarize its purpose and audience in 2-3 sentences.
    handoff:
      required: [purpose, audience, word_count]
    timeout: 300

  - name: review
    prompt: |
      Review the document for:
      1. Clarity — is the message clear?
      2. Accuracy — are claims supported?
      3. Tone — is it appropriate for the audience?

      Score each dimension 1-10. List specific suggestions.
    handoff:
      required: [clarity_score, accuracy_score, tone_score, suggestions]
    timeout: 600

  - name: route
    if: "clarity_score < 7 or accuracy_score < 7"
    goto: rewrite
    else: approve

  - name: rewrite
    prompt: |
      The document scored below 7 on clarity or accuracy.
      Rewrite the weak sections. Keep the same structure and tone.
    handoff:
      required: [status, changes_made]
    timeout: 1200

  - name: approve
    prompt: |
      The document passed review. Write a brief approval summary
      noting the scores and any minor suggestions.
    handoff:
      required: [status, summary]
    timeout: 300
```

Run it:
```bash
modastack agents launch -w content-review --role engineer --repo . \
  --task "Review docs/proposal.md for the Q3 board meeting" \
  --non-interactive
```

### Research workflow

```yaml
name: research
description: >
  Research a topic, synthesize findings, and produce a report.

steps:
  - name: gather
    prompt: |
      Research the topic described in the task.
      Find at least 5 relevant sources.
      Summarize each source in 2-3 sentences.
    handoff:
      required: [sources_found, key_themes]
    timeout: 900

  - name: synthesize
    prompt: |
      Synthesize the research into a coherent report.
      Structure: Executive Summary, Key Findings, Recommendations.
      Write the report to a markdown file in the repo.
    handoff:
      required: [report_path, recommendation_count]
    timeout: 1200
```

### Simple single-step workflow

The simplest workflow — one step, no handoff validation:

```yaml
name: quick-fix
description: Fix a simple issue with no lifecycle overhead.

steps:
  - name: fix
    prompt: "${{input.task}}"
```

This is essentially what the built-in `adhoc` workflow does.
