# Engineering Org Agent

Multi-repo agent pack where a director manages multiple software teams,
each running independently in their own repo.

## Architecture

```
Human (Slack)
    ↓
Director (engineering_org)
    ├── Project Lead (jobtack)     → Engineers
    ├── Project Lead (memorize)    → Engineers
    └── Project Lead (modastack)   → Engineers
```

**Three tiers:**

1. **Director** — persistent agent at the org level. Talks to humans on
   Slack, routes work to the right project, aggregates status across repos.
   Runs from a parent directory (e.g. `~/dev/`).

2. **Project Leads** — one per repo, launched by the director. Each subscribes
   to its repo's GitHub events, runs issue-lifecycle workflows, dispatches
   engineers. Equivalent to today's `software_team` manager but scoped to
   one repo and reporting to the director.

3. **Engineers** — spawned by project leads. Same as today's engineer role.

## Startup

```bash
cd ~/dev
modastack start engineering_org
```

The director starts with no projects. Repos are onboarded via Slack:

> "Hey modastack, start managing jobtack — it's at ~/dev/jobtack"

The director:
1. Validates the directory exists and has a git remote
2. Launches a project lead:
   ```bash
   cd ~/dev/jobtack && modastack agents launch \
     --role project_lead \
     --subscribe github:moda-labs/jobtack \
     --persistent
   ```
3. Confirms on Slack: "Now managing jobtack."

## Director Prompt Design

The director prompt should cover:

- **No hardcoded repo list** — repos are onboarded dynamically via Slack
- **Routing** — when a human says "fix the login bug in jobtack", the
  director messages the jobtack project lead
- **Status aggregation** — "what's everyone working on?" checks all
  active project lead sessions
- **Lifecycle management** — start/stop project leads on demand
- **Slack ownership** — director is the primary Slack responder, project
  leads only post status updates

## Communication Flow

```
Human: "Fix the auth bug in jobtack"
  → Director receives Slack event
  → Director messages jobtack project lead:
      modastack message --to moda-project_lead-jobtack "Fix the auth bug"
  → Project lead spawns engineer, manages lifecycle
  → Project lead posts status to Slack directly
  → Director aggregates when asked
```

## Repo Onboarding Flow

```
Human: "Start managing memorize — it's at ~/dev/memorize"
  → Director validates:
      - ~/dev/memorize exists
      - Has a git remote (detect org/repo)
  → Director launches project lead:
      cd ~/dev/memorize && modastack agents launch \
        --role project_lead \
        --subscribe github:org/memorize \
        --persistent
  → Director confirms on Slack
```

```
Human: "What repos are you managing?"
  → Director checks active project lead sessions
  → Reports: "jobtack (active, 2 open PRs), memorize (active, idle)"
```

```
Human: "Stop managing memorize"
  → Director stops the project lead session
  → Confirms on Slack
```

## Agent Pack Structure

```
agents/
  engineering_org/
    defaults.yaml
    roles/
      director.md           # org-level, talks to humans, manages project leads
      project_lead.md        # per-repo, manages issues/PRs, dispatches engineers
      engineer.md            # executes work (reuse from software_team or extend)
    workflows/
      issue-lifecycle.yaml   # reuse from software_team
      pr-feedback.yaml
      build-failure.yaml
      pr-merged.yaml
      stall-recovery.yaml
      adhoc.yaml
    monitors/
      defaults.yaml
      github_checks.py
```

## Key Differences from software_team

| Aspect | software_team | engineering_org |
|--------|--------------|-----------------|
| Scope | Single repo | Multiple repos |
| Entry point | Manager in repo dir | Director in parent dir |
| Repo config | Static (git remote) | Dynamic (Slack onboarding) |
| Human communication | Manager ↔ Human | Director ↔ Human |
| Event subscriptions | One set | Per project lead |

## Prerequisites

- Each repo must have a git remote (for auto-detecting org/repo)
- Slack bot token in machine config (shared across all repos)
- Event server running (shared across all project leads)

## Open Questions

- Should the director auto-discover repos in subdirectories on startup?
- Should project leads share the director's Slack channel or have their own?
- How does the director handle cross-repo work (a PR in repo A depends on repo B)?
