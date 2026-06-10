# eng-team

Multi-repo agent pack where a director manages multiple software teams,
each running independently in their own repo. Also works for a single repo.

## Roles

- **director** — org-level agent. Talks to humans on Slack, routes work
  to the right project, aggregates status across repos. Runs from a
  parent directory (e.g. `~/dev/`).

- **project_lead** — one per repo, launched by the director. Subscribes
  to its repo's GitHub events, runs issue-lifecycle workflows, dispatches
  engineers. Reports status back to the director.

- **engineer** — spawned by project leads. Executes work in isolated
  worktrees.

## Usage

```bash
cd ~/dev                          # parent of all repos
modastack start eng-team
```

Then onboard repos via Slack:

> "Start managing jobtack — it's at ~/dev/jobtack"
