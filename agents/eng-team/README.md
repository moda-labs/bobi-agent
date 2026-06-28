# eng-team

`eng-team` is a reusable two-layer engineering org package:

- `director` is the only persistent human-facing role. It handles Slack intake,
  event triage, repo and workflow selection, worker dispatch, status synthesis,
  and human escalation.
- `engineer` is the async worker role. Workflows launch it for repo-local work:
  pickup, specs, implementation, PR prep, QA, feedback, conflict resolution,
  cleanup, investigations, and handoff writing.

The director does not edit repos, run tests, open PRs, or resolve feedback
inline. It launches workers and reports from durable sources such as active
sessions, workflow handoffs, GitHub or Linear state, and monitor findings.

## Layout

```text
agents/eng-team/
  agent.yaml
  agent.md
  README.md
  roles/
    director/ROLE.md
    engineer/ROLE.md
  workflows/
    issue-lifecycle.yaml
    pr-feedback.yaml
    pr-closed.yaml
    merge-conflict.yaml
    build-failure.yaml
    adhoc.yaml
  monitors/defaults.yaml
  tools/
    github.md
    slack.md
  context/
    engineer.md
    github.md
    linear.md
    slack.md
    prep-doc.md
```

`tools/` files are loaded directly into prompts as CLI guides. `context/` files
are indexed and read on demand.
