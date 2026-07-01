# eng-team

A portable, tool-agnostic asynchronous engineering org. A persistent director
talks to humans, routes events, chooses workflows, and launches bounded engineer
workers. Also works for a single repo. This is the **reusable base** - it speaks
the engineering lifecycle in terms of generic seams (your tracker, your review
gate, your test/QA gate), binding only GitHub issues + Slack by default. Derive
a house team with `from: eng-team` and an overlay that binds your toolchain.

## Roles

- **director** - persistent human-facing control plane. Handles Slack intake,
  event triage, repo and workflow selection, worker dispatch, status synthesis,
  and human escalation.

- **engineer** - async worker launched by workflows. Executes issue pickup,
  specs, implementation, PR prep, QA, feedback handling, merge-conflict
  resolution, investigations, cleanup, and handoff writing.

## Usage

```bash
cd ~/dev                          # parent of all repos
bobi agent eng-team start
```

Then route work via Slack, GitHub, monitor events, or Linear where an overlay
configures it. The director launches workers for repo-local work and reports
back from durable sources.
