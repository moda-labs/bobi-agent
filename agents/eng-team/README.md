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
  AGENTS.md
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

`AGENTS.md` ships the general engineering rules (bug-fix discipline, testing
standards, proof-of-work, commit rules). At boot the runtime renders it into
the paths the team's brain auto-loads globally (`~/AGENTS.md`;
`$CODEX_HOME/AGENTS.md` for codex, `$CLAUDE_CONFIG_DIR/CLAUDE.md` for claude),
so every repo the agents work in gets the same standards. An overlay team
replaces it wholesale by shipping its own `AGENTS.md` (empty to disable).

`tools/` files are loaded directly into prompts as CLI guides. `context/` files
are indexed and read on demand.
