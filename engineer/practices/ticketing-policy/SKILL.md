# Ticketing Policy

This documents our ticketing workflow and responsibilities. For the
mechanical API calls, see the task tracker tool skill (e.g., `tools/github-issues` or `tools/linear`).

## Ticket states

Our workflow uses these states, in order:

| State | Meaning |
|-------|---------|
| Todo | Ready to be picked up |
| In Progress | Engineer is actively working |
| Blocked | Waiting for human input |
| In Review | PR created, waiting for human review |
| Done | PR merged, work complete |

## Your responsibilities as an engineer

- **Do NOT move tickets to In Progress** — the manager does this when assigning
- **Move to In Review** when you create a PR
- **Move to Blocked** if you have a question you can't answer yourself
- **Do NOT move to Done** — the manager does this when the PR is merged

## Where to find ticket info

The handoff file (`.modastack/handoff.md`) contains:
- `issue_id`: the ticket identifier (e.g., BET-10)
- `task_id`: the unique identifier needed for API calls
- `title`: the ticket title

The team key is the project prefix of the issue ID (BET-10 → BET).
