# Issue #859: A Restart Must Not Depend On The Caller Surviving It

## Problem

`bobi agent <name> restart` runs its stop phase and its start phase in the
calling process. The stop phase kills the manager. When the caller is a
descendant of that manager, the stop phase kills the caller too, so the start
phase never runs and the team stays down - silently, with no error anywhere,
because the process that would have reported the failure is the casualty.

This is not an exotic caller. The manager hosts its own entry-role agent
session in-process, so every tool that session runs is the manager's
descendant. An agent asked to deploy, or following the hint Bobi itself
prints, runs `bobi agent <name> restart` and takes its own team down
permanently.

Reported after three outages on a local `gtm-team` in three days
(2026-07-27/-28/-29), each losing 3-5 hours of Slack responsiveness. Inbound
Slack messages are lost rather than queued while the manager is down, and the
killed session's transcript ends with `[Request interrupted by user for tool
use]`, which reads exactly like a permission denial - the forensic trail
actively misleads.

## Goals

- A restart completes whether or not the process that asked for it survives.
- The same guarantee for every restart caller, not just the CLI.
- A durable record of what a restart did, readable after the caller is gone.
- No behavior change for a caller that does survive: same output, same exit
  code, same synchronous feel.

## Non-Goals

- Process supervision (restart-on-exit, restart-on-boot). That is #869; a
  supervisor would mask this bug, not fix it.
- `bobi agent <name> stop` from inside the runtime. The caller still dies
  mid-call, but its intent was to stop, and the stop lands. Out of scope.
- `bobi app restart` (webapp daemon) and `bobi agent <name> event-server
  restart`. Both have the same in-process `ctx.invoke(stop)` /
  `ctx.invoke(start)` shape, but neither target is ever an ancestor of the
  caller, so neither can kill it.
- The systemd branch of `restart`. It already delegates to `systemctl --user
  restart`; systemd owns the lifecycle and nothing depends on the caller.

## Root Cause

There is no seam between "the process that decides to restart" and "the
process that performs the restart". The stop and the start are two statements
in one function, and the first statement can kill the process executing the
second:

```python
# bobi/cli.py:1069-1071
ctx = click.get_current_context()
ctx.invoke(stop)          # SIGTERMs the manager, waits up to 6s for exit
ctx.invoke(start, ...)    # never reached when the manager's death kills us
```

The process tree that makes this fire, confirmed by walking `/proc` from a
live agent's Bash tool on a running fleet box:

```
manager python           (state/manager.pid)   <- stop_team's os.kill target
 └── claude CLI (node)                         <- the manager session's transport
      └── bash                                 <- the Bash tool
           └── bobi agent <n> restart          <- dies in the stop phase
```

`run_manager_from_config` ends by calling `spawn_adhoc(..., persistent=True)`
(`bobi/service.py:643-652`), which runs the entry-role session on a thread
**inside the manager process** (`bobi/subagent.py:766-791`). The brain's
`claude` CLI is an ordinary child of that process, so the manager's exit
tears down the whole tool subtree.

Two nearby paths already get this right, which is what marks the local branch
as an oversight rather than a design: the systemd branch of the same command
delegates the lifecycle to systemd, and `spawn_team` already launches the
manager with `start_new_session=True` (`bobi/service.py:396-404`) - the exact
primitive needed here. The gap is only that the code which would call it is
dead by the time it would run.

### Three instances of the one root cause

| Path | Vulnerable? |
|---|---|
| `bobi/cli.py:1069-1071` - the CLI's local branch | Yes - the reported outage |
| `bobi/webapp/runtime.py:412-421` - `LocalRuntime.restart_team` | Yes when the web app is hosted **inside** the manager, which is on by default in-container (`BOBI_UI=1`, `bobi/service.py:592-603`, `DESIGN.md:470`). The HTTP handler SIGTERMs its own process. |
| `bobi/service.py:709-716` - `restart_team` | Same shape; currently unreachable (zero callers, already logged as dead code in `plans/2026-07-22-review-remediation.md` Q031). |

Bobi also actively steers callers into the bug: `bobi/doctor.py:353,362` and
the `AlreadyRunning` message at `bobi/cli.py:433` tell the operator - very
often an agent running inside the runtime - to run
`bobi agent <name> restart`.

## Solution

Nothing is stopped until the process that will do the starting is out of the
blast radius. `restart` spawns a **detached worker** - a new session
(`start_new_session=True`), the same primitive `spawn_team` already uses for
the manager - and the worker runs the stop+start pair. The caller becomes a
reporter, not the actor: it streams the worker's log and reports the outcome,
and if it dies mid-restart nothing changes, because the worker owns the
outcome.

The worker writes to a **file**, never to a pipe the caller holds. A caller
that dies must not be able to stall the worker on a full pipe buffer or kill
it with `EPIPE`.

`service.restart_team` becomes that one seam, and both live callers route
through it - which also retires the third instance above rather than leaving
a fourth path behind.

### Why not "detect and refuse"

The issue offers a minimum-viable alternative: detect that the caller is
inside the runtime (walk parent pids against `state/manager.pid`) and exit
non-zero. Rejected as the primary fix, for three reasons:

1. It converts a silent outage into an actionable message, but the operator
   still cannot restart the team from where they are - which is the thing
   they were trying to do.
2. It needs a new process-ancestry facility (nothing in the repo walks parent
   pids today; `psutil` is not a dependency and stdlib-only process handling
   is the house style) whose correctness the fix would then depend on.
3. A detached restart is safe from *every* caller, including ones ancestry
   detection would not flag - a detached sub-agent session is not the
   manager's descendant, yet its restart is still racing the manager's death.

With the worker, refusal is unnecessary: there is one code path and it is
safe from anywhere.

### Why not "spawn only the start phase detached"

The caller dies during the stop phase, before it could spawn anything. The
spawn has to happen before the stop, so the worker must own both phases.

## Technical Approach

### `bobi/service.py`

- `RestartResult(pid, log_file)` - the outcome a surviving caller gets.
- `RestartFailed(ServiceError)` - carries `log_file` and `log_tail`, with
  `report()` for a caller that did not stream the log (the web app).
- `spawn_restart(project_path, *, fresh=False) -> RestartHandle` - spawns
  `python -m bobi.cli agent <name> restart --detached-worker [--fresh]` with
  `start_new_session=True`, `child_agent_env(project_path)`, and
  stdout/stderr redirected to `state/restart.log` (truncated per run, so the
  file is one restart's record). Mirrors `spawn_team` exactly.
- `restart_team(project_path, *, fresh=False, timeout=180, on_output=None)`
  - spawns the worker, follows its log (emitting whole lines to `on_output`
  when given), waits for it to exit, then polls `state/manager.pid` for a
  live manager. Raises `RestartFailed` on a non-zero worker exit, on the
  parent's wait timeout (the worker keeps running - the message says so), or
  when the pair completed without leaving a manager alive.

### `bobi/cli.py`

- `restart` gains a hidden `--detached-worker` flag. With it, the command
  runs the existing in-process `ctx.invoke(stop)` / `ctx.invoke(start)` pair
  - that is the worker's job, and it is checked before the systemd branch so
  a worker never re-delegates. Without it, the local branch calls
  `service.restart_team(..., on_output=click.echo)` and exits non-zero on
  `RestartFailed`. The systemd branch is untouched.

### `bobi/webapp/runtime.py`

- `LocalRuntime.restart_team` calls `service.restart_team` instead of doing
  its own stop+start, and maps `RestartFailed` to `TeamLifecycleError`
  (already a 409 with a user-facing message, same status the structured
  preflight failure returns).

  This trades the structured `TeamPreflightFailed` report on the *restart*
  route for the worker's log tail, which contains the same formatted preflight
  text. That is the honest contract: a parent cannot narrate what a detached
  worker did, only relay it. The `start` route keeps its structured errors,
  because it still spawns in-process.

### Concurrency

Two concurrent restarts are left unguarded, deliberately. Every interleaving
of two workers that both complete ends with a manager running: a second
`start` against a live manager hits `AlreadyRunning`, which the `start`
command reports and exits 0. A lock would add a stale-lock failure mode to
defend a race that converges anyway.

## Verification Plan

- **Regression test (integration, real processes, stub brain).** Start a real
  manager, launch a real `bobi agent <name> restart` subprocess, and SIGKILL
  that subprocess the instant the manager's pid dies - the same causal chain
  as the runtime tearing down the tool, made deterministic. Assert the manager
  is back up with a new pid. Fails before the fix (the start phase never
  runs), passes after.
- **Unit tests.** `spawn_restart` passes `start_new_session=True`, the
  expected argv, `--fresh` passthrough, and a file (not a pipe) for
  stdout/stderr; `restart_team` raises `RestartFailed` on a non-zero worker
  exit and on a completed worker that left no live manager; the CLI's
  `--detached-worker` branch runs the in-process pair while the plain branch
  does not.
- **Web app.** `POST /api/agents/{name}/restart` routes through
  `service.restart_team`.
- **Original repro.** `bobi agent <name> restart` run from inside a live
  runtime (an agent's Bash tool) brings the team back instead of leaving it
  stopped.
- Full unit suite green; `tests/integration/test_manager_lifecycle.py` green
  on the stub leg.

## Implementation Plan

1. Regression test in `tests/integration/test_manager_lifecycle.py`; confirm
   it fails on `main`.
2. `spawn_restart` + `restart_team` + `RestartResult`/`RestartFailed` in
   `bobi/service.py`.
3. `--detached-worker` and the reporter branch in `bobi/cli.py`.
4. Route `bobi/webapp/runtime.py` through `service.restart_team`.
5. Unit tests in `tests/test_service.py`, `tests/test_cli.py`,
   `tests/test_webapp_server.py`.
6. Docs: `skills/bobi.md`, `docs/QUICKSTART.md`, and the runbook step in
   `agents/dogfood-content-review/workspace/runbooks/incident-response.md`.
