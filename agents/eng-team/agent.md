# eng-team

A portable, tool-agnostic engineering org. A director manages multiple
software teams, each running independently in its own repo. Also works for a
single repo. This is the **reusable base** — it speaks the engineering
lifecycle in terms of roles and generic seams (your tracker, your review gate,
your test/QA gate), binding only GitHub issues + Slack by default. Derive a
house team with `from: eng-team` and an overlay that binds your toolchain.

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
bobi agent eng-team start
```

Then onboard repos via Slack:

> "Start managing jobtack — it's at ~/dev/jobtack"
