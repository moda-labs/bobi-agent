# Make `subagents cancel` stop a run's whole process tree

> **Status:** In review
> **Tracking issue:** moda-labs/bobi-agent#1122 · **Related:** #1124, #1115 · **Created:** 2026-11-03 · **Last amended:** 2026-11-04 (see Amendments)
>
> Every `file:line` here was read at `origin/main` `e8fe14f1` (0.62.0).
> The spec in the #1122 issue body was written at `b9bc83bc` (0.60.1).
> Where the two differ this plan wins, and [Changed since the spec](#changed-since-the-spec) lists each difference.
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

`bobi agent <agent> subagents cancel <ref>` must actually stop the run.
Today it prints `Cancelled <ref>` while the worker's claude process keeps running with its credentials, and nothing can find that process afterwards.

What changes for a user:

- Cancel stops the run's recorded process and every live process descended from it, except the team's shared daemons.
- It sends SIGTERM, waits up to 10s, sends SIGKILL to whatever is left, and waits up to 5s more.
- The command blocks for that wait: about 2.5s measured, 15s at worst.
- `Cancelled <ref>` with exit 0 means the tree is gone, or that no tree could be verified, in which case a stderr line says so (C3, C4, C5 below).
- If anything survives SIGKILL the command exits 1 and names the survivors. The run is still marked cancelled.
- Cancel refuses with exit 1 and signals nothing for the manager, a shared daemon, a run from inside itself, a launch still starting, and a run key matching more than one active run.
- `subagents list` stops listing the manager, and the runs view stops showing a finished run as running.

A process that detached before the cancel (`nohup cmd &`, a sub-run launched detached by a command that has exited) has been reparented, is no longer a descendant, and is not stopped (X2).

## Problem

Root cause, verified at `e8fe14f1`:

- **RC1:** `cancel_agent` (`bobi/subagent.py:3031`) sends SIGTERM to `entry.pid` only (`:3045`).
- **RC2:** The detached runner (`_run_agent_entry`, `bobi/subagent.py:2383`) installs no SIGTERM handler, so it dies at once without running `finally` or `atexit`.
  The SDK's child reaper is an `atexit` hook (`claude_agent_sdk/_internal/transport/subprocess_cli.py:64-68`, SDK 0.2.176), so it never runs.
  The CLI does not stop when its stdin closes.
- **RC3:** `cancel_agent` does not wait or verify.
  It writes `status="cancelled", pid=0` at once (`:3048`), which removes the only handle on the survivors.
- **RC4:** The reconciler's deadline branch (`bobi/reconcile.py:204`, through `_default_cancel`, `:70`) calls the same function at every manager start.
- **RC5:** Neither path closes the run's ledger row (`WorkflowRun`), so it reads `running` for good (inventory N8).
  The runs view folds a failed session onto its row, but not a cancelled or completed one (`bobi/webapp/runs.py:339-346`), so the row counts as running.
  Reproduced: `counts.running == 1` from `build_runs` after a real `cancel_agent` on an isolated root.
  On this instance 12 of the 17 ledger rows reading `running` have no active session: 5 cancelled, 4 crashed, 2 failed, 1 completed.

Process topology, the same in the harness and on a live run:

```
runner (python)      pgid=R  sid=R   session and group leader
└─ claude CLI        pgid=R  sid=R   same group as the runner
   └─ bash (tool)    pgid=B  sid=B   its own session
      └─ tool cmd    pgid=T  sid=B   its own process group
```

Experiments use the real CLI with a stand-in runner spawned the way `bobi/subagent.py::_launch_detached` spawns one.
The signal is sent while one long tool command runs.
The spec ran them on CLI 2.1.287 and SDK 0.2.165.
They were rerun for this plan on CLI 2.1.309 and SDK 0.2.176 on 2026-11-03.

| # | Signal | Tool command | Spec result | Rerun result |
|---|---|---|---|---|
| E1a | SIGTERM to the runner pid (today's cancel) | loop in the tool shell | CLI alive at 75s, `ppid=1`, took 6 more turns | not rerun |
| E1b | same | one long process | CLI alive at 75s, `ppid=1`, command writing | CLI alive at 45s, `ppid=1`, ticks 3 to 25 |
| E2a | SIGTERM to the runner's group | loop in the tool shell | CLI dead in 2.5s, command stopped | not rerun |
| E2b | same | one long process | CLI dead in 2.5s, command writing at 30s | CLI dead in 2.5s, ticks 3 to 20 at 35s |
| E3 | SIGKILL to the runner's group | one long process | CLI dead, shell and command writing at 30s | CLI dead, shell and command alive, ticks 3 to 18 at 30s |
| E4 | SIGTERM to the runner and every descendant | one long process | tree empty in 2.4s and 2.6s, no SIGKILL | tree empty in 2.3s, no SIGKILL, ticks frozen |
| E5 | same, command started as a background task | one long process | 6 processes, tree empty in 2.4s | 6 processes, tree empty in 2.4s, ticks frozen |

What this establishes:

- E1 is the reported bug.
- E2b and E3 show a process-group kill is not enough, because the tool command sits in its own group.
- E4 and E5 show signalling the descendant tree stops everything, including a background task.

Production occurrences of RC1: 2026-08-06, 2026-10-13 and 2026-10-28.
The last, on 0.61.0, kept editing and committing for 30+ minutes after `Cancelled`, with nothing reaching its session log.
Production occurrences of RC4: see F2 and Q1 below.

### Changed since the spec

Unchanged, so the design still applies: `cancel_agent`, `list_agents`, `find_agent` and `_launch_detached` are byte-identical between the two SHAs, and `git diff b9bc83bc e8fe14f1 -- bobi/reconcile.py` is empty.

Citations that moved, each located by searching for the cited line's exact text:

| Spec citation | Current | What it is |
|---|---|---|
| `bobi/subagent.py:2926` | `:3031` | `cancel_agent` |
| `bobi/subagent.py:2896` | `:3001` | `list_agents` role test |
| `bobi/subagent.py:1538`, `:1640` | `:1632`, `:1734` | `--wait` and detached pid stamps |
| `bobi/subagent.py:2193` | `:2284` | session start calls `ensure_running` |
| `bobi/events/server.py:602` | `:698` | event server spawn |
| `bobi/workflow/orchestrator.py:324`, `:499` | `:357`, `:532` | run and resume pid stamps |
| `bobi/workflow/orchestrator.py:185` | `:217-222` | shared-pid warning |
| `bobi/session.py:1766` | `:1888` | `Session` pid stamp |
| `bobi/config.py:524` | `:556` | `entry_role` |
| `bobi/service.py:739` | `:756` | reconcile at manager start |
| `bobi/supervisor/snapshot.py:69` | `:70` | no `psutil` note |
| `skills/bobi.md:186` | `:194` | cancel in the command block |
| `tests/test_subagent.py:125` | `:127` | mock-contract cancel test |

Still at the cited line: `bobi/reconcile.py:70`, `:193`, `:204`, `bobi/sdk.py:595`, `bobi/service.py:494`, `bobi/webapp/run_actions.py:81`, `docs/RUN_RESUME.md:114`, `tests/integration/test_cli_commands.py:68`, `tests/test_service.py:208`, `:217`.

Claims in the spec or its comments that do not hold:

- **F1: There is no in-runner timeout on the workflow path, so X1's premise is wrong for workflow runs.**
  `git grep -n -E "wait_for\(|asyncio\.timeout\(" <rev> -- bobi/workflow` is empty at `e8fe14f1`, `b9bc83bc` and `v0.59.0`.
  `bobi/workflow/orchestrator.py:87-93` and `docs/WORKFLOW_ENGINE.md:112-118` say the same: `step.timeout` gates only whether a turn-cap restart starts.
  The only in-runner wall-clock limit is `_run_agent_supervised` (`bobi/subagent.py:471`, `:2757`), used by check, gate and curator agents.
- **F2: The 2026-10-14 comment on #1122 misattributes its X1 half.**
  It says run `1114-ci-06547e01-timing` hit a 3600s step timeout at about 14:23Z.
  The event log has `session.failed` at 14:47:09Z with `agent exceeded its 3600s timeout without reporting a terminal status`, a string that exists only at `bobi/reconcile.py:207`.
  That branch runs only when the pid is alive, so the wrapper was alive at 14:47Z and was closed by a director restart.
  The "14:23Z" is the worker's own inference from start time plus 3600s, in its 14:59Z message.
  This makes the incident a production repro of RC4 on this instance. The comment's X3 half stands.
- **F3: D9's reason needs correcting.**
  #1115 reports an issue-lifecycle run past its 3600s cap and absent from `subagents list`.
  On the workflow path only RC4 does that, so #1115's incident is probably RC4 as well.
  This is inferred from code and F2. Baohua's logs were not read.
- **F4: The spec's "workflow run's phase-session entry, which shares the runner's pid" has no producer.**
  `run_phase_blocking` has no non-test caller at either SHA.
  The rule stays, because `--wait` sub-runs need it, but the example goes.
- **F5: "Log lines reach `manager.log` on the reconciler path" is false under the supervisor.**
  `bobi/cli.py:710-713` strips file handlers from a foreground manager, and its stderr is a pipe here.
  `manager.log` holds no `Reconciled N stranded` line for any of the three restarts that closed runs on this instance.
  It holds for a daemonized start (`bobi/service.py:466-474`). On the CLI path, INFO lines reach stderr and `manager.log` (`bobi/logs.py:58-70`, `bobi/cli.py:160`).
- **F6: `tests/test_sdk.py` does not exist.** V16 belongs in `tests/test_registry.py`.
- **F7: V1 cannot be a failing-first test.**
  It calls code that does not exist on `main`, so it would fail on an import and reproduce nothing.
  V11 and V20 are the failing-first tests, because they drive the existing `cancel_agent` and CLI.
- **F8: The pid wrap rate varies.**
  The spec measured about one cycle every four hours.
  This boot averages 5148 forks per hour from `/proc/stat`, one cycle every 6.4 hours, with `pid_max` still 32768.
  The conclusion that a pid alone does not identify a process is unchanged.
- **F9: R3's "a daemonized local start waits 30s" cites test-only code.**
  `start_team` (`bobi/service.py:489`) has no production caller. `start` and `restart` use `spawn_team` (`bobi/cli.py:716`, `bobi/webapp/runtime.py:1847`), which does not wait, so no start is killed by a slow sweep.
  R3 is only a delay.

Defects the spec missed, folded in below:

- **A1: The runs view counts a finished run as running (RC5).** The fix is in the read side, not a new ledger write. See Effects on records.
- **A2: I3 names two pid files and the tree has four (inventory N5), and one is stale on this host right now.**
  `/data/.bobi/webapp/app.pid` holds pid 2326, written 2026-10-10, on a host booted 2026-11-03.
  Whatever process next gets that pid would be spared silently, so a pid file must be validated before it protects anything.
  A pid file's process can also be the cancel target itself, which the spec leaves undefined.
- **A3: `_default_cancel` catches `Exception` at debug level (`bobi/reconcile.py:74-75`).**
  It would swallow a refusal, and the deadline branch would mark `failed` a run it refused to touch, leaving that run alive and untracked.
  I5 can fire here: a manager run with `start --foreground` from a tool call in a past-deadline run is that run's descendant during the sweep.
- **A4: The spec's write order leaves a race, and the deadline branch writes twice.**
  Between the first SIGTERM and the registry write the entry says `running` with a dead pid, and `_reap_if_dead` (`bobi/sdk.py:604-609`) can land `crashed` on top of the cancel.
  The deadline branch would write `cancelled` and only write `failed` up to 15s later, so a manager stopped mid-wait leaves a run no later sweep re-emits for.
- **A5: The sweep iterates a stale snapshot (`bobi/reconcile.py:169`).**
  A `--wait` sub-run killed with its past-deadline parent is then visited as `running` with a dead pid, marked `crashed`, and announced with `agent/session.failed`.
- **A6: `_default_emit` reports success whatever `post_event` returned (`bobi/reconcile.py:61-64`).**
  `post_event` returns `False` with no bubble and on a rejected POST (`bobi/events/publish.py:71-109`), so `emit_confirmed` is set for a lost event and it is never retried.
- **A7: Three more ways `Cancelled` could be false.**
  A launch cancelled between registration and its pid stamp runs anyway, because the runner's `register` replaces the entry (`bobi/subagent.py:1600`, `:1734`, `bobi/workflow/orchestrator.py:354`).
  `find_agent` picks the most recently active of several runs sharing a run key (`bobi/subagent.py:3021-3028`), which now means a tree kill of a guess. C10 needs a plural lookup beside it that ignores entries with a dead pid.
  The moved parent map returns `{}` when `ps` fails (`bobi/service.py:344-345`), which would read every member as exited.

## Solution

Cancel stops the recorded process and every live descendant, waits, escalates, and reports what happened.
A tree is killed only from a process whose identity is verified.

### Cases

`<name>` is the resolved session name.
The first matching row decides, in this order: C8, C10, C6 by name, C9, C5, C3, C6 by pid, C7, C4, then C1 or C2.

| Case | Condition | Signalled | Session entry | Exit |
|---|---|---|---|---|
| C1 | Verified process, tree stops | the process and its descendants | target `cancelled`, `pid=0`, stopped sub-runs too | 0 |
| C2 | Verified process, something survives | same | as C1, survivors in the target's `error` | 1 |
| C3 | The recorded process is gone: pid dead, zombie, or now a different process | nothing | `cancelled`, `pid=0` | 0 |
| C4 | No identity recorded: an older bobi wrote the entry, or no process table | the recorded pid only, one SIGTERM (today's behaviour) | `cancelled`, `pid=0` | 0 |
| C5 | No pid recorded: an in-process check, gate or curator session, or an old `starting` corpse | nothing | `cancelled` | 0 |
| C6 | The entry is the manager, or its recorded process is named by a valid pid file (I3) | nothing | untouched | 1 |
| C7 | The command runs inside the run it targets | nothing | untouched | 1 |
| C8 | No active sub-agent matches (unchanged, including terminal entries, X8) | nothing | untouched | 0 |
| C9 | A launch still starting: `starting`, pid 0, younger than 60s | nothing | untouched | 1 |
| C10 | The ref is a run key matching more than one active run | nothing | untouched | 1 |

`cancel_agent(ref, *, terminal=None) -> bool` returns `False` only for C8. `terminal` is the deadline branch's `(status, error)`.
C2 raises `CancelIncompleteError(name, total, survivors)`. C6, C7, C9 and C10 raise `CancelRefusedError`.
C3, C4, C5 return `True` and log their Output line as a WARNING.

### Kill sequence (C1, C2)

1. List the other active entries first. That scan took about 0.6s on this instance, and it must not sit between the write and the signal.
2. Read the process table once.
   The kill set is the recorded process, then every transitive child, breadth-first.
   The walk stops at a protected pid (I3) and does not descend past it. Each member is recorded with its start token.
3. Write the records: the target, then every entry from step 1 whose recorded process is in the kill set.
   One write each, `pid=0`, before any signal, so no list read can see `running` with a dead pid. A failed write is logged and the kill continues.
4. Send SIGTERM to the recorded process first, then to the rest in that order, so the runner dies before it can observe its CLI exiting and record the run as failed.
5. Poll every 50ms until every member has exited, for at most 10s.
   Re-read the table, and signal and track any new descendant of a member still alive. A registered sub-run found this way is written as in step 3.
6. Send SIGKILL to whatever is left and poll for at most 5s.
7. Write again any entry from step 3 that is active with pid 0 or a member's pid and an unchanged `started_at`: the runner can write `status="running"` just before it dies (`bobi/workflow/orchestrator.py:1066`, `bobi/session.py:921`), and no reaper would close that.
8. Report C1 if the set is empty, C2 otherwise.

A member has exited when its pid is gone, its start token changed, or it is a zombie.
A failed table read is unknown, never exited, so a member still unconfirmed at the end is a survivor with the reason `process table unavailable`.
A member that cannot be signalled (`EPERM`) is a survivor with the reason `signal not permitted`.
If the sequence is interrupted from the first write onwards (Ctrl-C, or SIGTERM to the manager during the sweep), SIGKILL goes to every live member at once and the interrupt propagates.
Both grace periods are named constants and parameters.

`bobi/proctree.py` imports only the standard library, so `bobi/sdk.py` can use it without a cycle:

- `read_table() -> dict[int, Proc] | None`, each `Proc` holding parent, state, start token and start time.
- `verify(table, pid, token)`, which returns verified, gone or unknown. A zombie is gone. An unreadable table is unknown.
- `is_self_or_ancestor(table, pid)`.
- `protected_pids(files, table) -> dict[int, str]`, applying I3's validity rule.
- `kill_set(table, root, protected) -> list[Member]`.
- `terminate(members, *, term_grace=10.0, kill_grace=5.0) -> list[Survivor]`, sending through the module-level `_send`, the one seam a test patches to force a survivor.

`bobi/subagent.py` supplies the pid-file list. `CancelRefusedError` lives there too.

### Safety invariants

Each holds on every call and each has a test.

- **I1:** Cancel signals only the recorded process and processes descended from it, never by process group or session.
  A recorded pid is not always a group leader: `--wait` launches, every workflow run and resume, and every `Session` record the pid of the process they run in, and this deployment's manager tree reports `pgrp 0`, where `os.killpg(0, sig)` signals the caller's own group.
- **I2:** A tree is killed only from a verified process. Without verification, cancel sends at most the single SIGTERM today's code sends.
- **I3:** A process named by one of the four pid files (N5) is never signalled, and the walk does not descend past it. Three files live in the target's `run/state`, and `app.pid` in `$BOBI_HOME/webapp` (`bobi/paths.py:179-181`).
  A pid file is ignored as stale only if it predates this boot, or its process started more than 1h after the file's last write. All four writers write after the process starts.
  The margin is wide on purpose: a clock step moves start times computed from `btime`, a false stale could stop the event bus, and a false valid spares one process and is reported.
  A session start can launch the local event server from a runner (`bobi/subagent.py:2284`, `bobi/events/server.py:695-698`), so a runner can be its parent.
  A target whose parent is a protected process is still stopped: every run the reactor dispatches is the manager's child (`bobi/events/reactor.py:345`).
  Each skip prints an Output line, so a real writer spared by mistake can be seen.
- **I4:** The manager is never cancelled.
  It is identified by its session name (`bobi/service.py:135`) or by a pid equal to `manager.pid`, not by `role == "manager"`: the manager registers with the team's `entry_point` role (`bobi/config.py:556`), `director` on this deployment.
  `subagents list` uses that wrong test today (`bobi/subagent.py:3001`) and lists `bobi-eng-team-director` here, confirmed live on 2026-11-03.
  `bobi/concurrency_semaphore.py:32-43` already resolves this name and tolerates an unbound root, so reuse it.
- **I5:** Cancel never signals its own process or an ancestor of it (C7). `bobi/service.py:303-322` already walks this chain.

### Process identity

- `SessionEntry` gains `pid_start: str = ""`, the start token of the process at `pid`.
- `SessionRegistry.register` (`bobi/sdk.py:371`) and `.update` (`:378`) set it whenever they write a non-zero pid and clear it when the pid is cleared. The five stamp sites (N3) do not change.
- On Linux the token is `proc:<boot_id>:<starttime>`, from `/proc/sys/kernel/random/boot_id` and field 22 of `/proc/<pid>/stat`.
  Without `/proc` it is `ps:<lstart>`, from `ps` run with `LC_ALL=C TZ=UTC` so the text is stable. Tokens from different sources never match.
- Equal tokens mean verified (C1, C2). A different token, a zombie or a missing process means gone (C3). No recorded token means unknown (C4).
- Kill-set members carry their own tokens, so a pid recycled during the wait is not signalled.
- The field is additive: `SessionEntry.from_dict` ignores unknown keys (`bobi/sdk.py:331-341`), so an older bobi reads a newer `state.json`. An older bobi's `update` drops the field (`:395`), which lands that entry in C4.

Example, illustrative until the PR carries real output:

```json
{"name": "wf-adhoc-eng-team-1122", "status": "running", "pid": 4242,
 "pid_start": "proc:e5112522-9d08-4c62-9577-9961d81b2475:8123456"}
```

`pid_start` is an opaque string, compared only for equality. `""` means no token.

### Effects on records

- The target is set to `status="cancelled", pid=0`, or `status="failed"` with the deadline error when the deadline branch is the caller.
- Every other active entry whose recorded process is in the kill set is set to `cancelled, pid=0` with a log line. This covers a sub-run launched with `--wait`, whose process is a descendant.
  Without it that sub-run is reaped as `crashed`, and the reconciler emits `agent/session.failed` for deliberately cancelled work at the next manager start (`bobi/sdk.py:595`, `bobi/reconcile.py:193`).
- A run launched in-process by a long-lived parent stays that parent's child, so it stops with the parent and is written the same way. The one such parent cancel can reach is a persistent sub-agent hosting a reactor (`bobi/subagent.py:2357-2365`), because the manager is refused.
- A sub-run launched detached by a command that has exited has been reparented and is untouched (X2).
- In C2 the target's `error` records the survivors, for example `cancel incomplete: pid 12345 (pytest) did not exit after SIGKILL`, or appended to the deadline error.
  It is written only if the entry is still this run (`started_at` unchanged), so a relaunch on the same name is not touched.
- Cancel does not write the ledger. `_all_rows` (`bobi/webapp/runs.py:415`) already knows `live_sessions`, so a row whose ledger status is `running` reads `running` only if it is the newest row, by `resumed_at or started_at`, for a session that is live.
  Otherwise the newest row takes its session's status, error and end time (`terminal_at or last_activity`), with a cancelled session shown as `closed`. An older row, or a row with no session, shows `closed`. Other ledger statuses are untouched.
  This also fixes the 12 existing rows on this instance and the crash path, and needs no lock.
  A new ledger write was the first draft of this fix and was dropped: it would stop a cancelled period run adopting its checkpoint (`bobi/subagent.py:1499-1512`), and the runner's unlocked checkpoint saves could reopen the row (`bobi/workflow/orchestrator.py:1114`).

### Output

`stdout` on success stays `Cancelled <ref>`. Nothing in the repo parses it, and the only pinned line is the no-match one (`tests/integration/test_cli_commands.py:68-71`).

Detail goes to log lines.
On the CLI they reach stderr and `manager.log`.
On the sweep they reach the manager's stderr, which is `manager.log` for a daemonized start and the container log stream under the supervisor (F5).

```
INFO     Cancelling <name>: SIGTERM to 4 processes (pid 4242 and 3 descendants)
WARNING  <name>: 1 process ignored SIGTERM for 10s, sending SIGKILL: pid 12345 (pytest)
INFO     Sub-run <child> stopped with <name>
INFO     Sub-agent cancelled: <name> (4 processes stopped in 2.6s)
```

C2, on stderr:

```
Could not stop <name>: 1 of 4 processes still running.
  pid 12345 (pytest): did not exit after SIGKILL
The run is marked cancelled, but these processes are still running.
Stop them by hand:  kill -9 12345
```

One line for each other case:

- C3: `<name>: the recorded process (pid 4242) had already exited; nothing was signalled.`
- C4: `<name>: cannot verify pid 4242, so only that process was signalled. Its child processes may still be running.`
- C5: `<name>: no process is recorded for this run; nothing was signalled.`
- C6: `<name> is the manager, not a sub-agent. Stop it with:  bobi agent <agent> stop`, with the real agent name rendered. For a daemon: `<name> records a shared daemon (pid 4242, event-server.pid), so nothing was signalled.`
- C7: `Cannot cancel <name> from inside it: this command is running in that run's process tree. Run it from outside the run.`
- C9: `<name> is still starting. Retry in a few seconds.`
- C10: `<ref> matches more than one active run. Name one: <name>, <name>`
- Protected skip: `<name>: pid 4242 (node) was not signalled: it is the team's daemon (event-server.pid).`

### Reconciler

- The sweep sorts the names and re-reads each entry before acting, so a run stopped earlier in the same sweep is not revisited (A5).
- `_default_cancel(name)` reads the entry and calls `cancel_agent(name, terminal=(TERMINAL_FAILED, <deadline error>))`, which writes through `mark_terminal(reconciled=True)`, once, before any signal (A4).
  If the manager stops mid-wait the entry is already `failed` with `emit_confirmed=False`, and the next sweep re-emits it.
- C2 appends to that error, for example `agent exceeded its 3600s timeout without reporting a terminal status; 1 process could not be stopped (pid 12345 pytest)`. `_default_cancel` catches `CancelIncompleteError` and the branch still emits.
- A refused cancel (C6, C7, C9, C10) makes the branch skip the entry with a WARNING: no status change and no event (A3).
- `_default_emit` returns what `post_event` returned (A6).
- An injected `cancel=` callable with one argument returning `None` keeps working, which every existing reconciler test relies on (N2). The branch marks `failed` afterwards only if the entry is still active.
- The sweep runs once, at manager start, before the manager's first turn (`bobi/service.py:756`, `:771`).
  Each live timed-out run now costs about 2.5s there, 15s at worst.
- Q1 decides whether a verified-live run reaches this kill at all.

### Decisions for Gate 1

| # | Decision | Recommendation | Why, and why not the alternative |
|---|---|---|---|
| D1 | Mechanism | The canceller kills the descendant tree | A process-group kill leaves the tool command (E2b, E3). A runner SIGTERM handler cannot reach it either (X4). |
| D2 | Cancel blocks | Accept about 2.5s typical, 15s worst, on the CLI and per live timed-out run at manager start | Fire and forget cannot report survivors. Q1 (a) removes the manager-start half. |
| D3 | Incomplete cancel | Exit 1, list survivors, record them in `error`, still mark `cancelled` | Leaving the entry `running` gives no handle: the recorded process is dead and the survivor is reparented. |
| D4 | Identity guard | Ship it in this PR. Entries without identity keep today's single SIGTERM | Without it a stale entry SIGKILLs an unrelated process tree after a restart or a pid wrap. |
| D5 | `--wait` sub-runs | They stop with their parent and are marked `cancelled` | Sparing them leaves a second writer in the same worktree. |
| D6 | Manager | Refuse, and stop listing it in `subagents list` | Today's one SIGTERM still prints `Cancelled` with nothing confirmed, and a tree kill would take every run the manager dispatched. |
| D7 | Cancel from inside the run | Refuse | Today it kills the runner and leaves the caller's own CLI running, which is this bug. |
| D8 | Shared daemons | Never signal them (I3) | Cancelling a run that started the event server would take the team's event bus down. |
| D9 | #1115 | Reference it, do not close it | Corrected by F3: this PR probably fixes #1115's reported incident, but that is inferred. Confirm against baohua before closing. |

D6 and D7 change today's behaviour in cases where today's behaviour is itself wrong.
C9 and C10 change it the same way (A7).
A1 to A7 are defects, not scope, and carry no separate decision.

No new dependency.
`psutil` would cover this, but the repo carries a `/proc` parent map with a `ps` fallback (`bobi/service.py:325-354`) and avoids `psutil` elsewhere (`bobi/supervisor/snapshot.py:70`).

## Relevant files

### Existing (verified 2026-11-03)

- `bobi/subagent.py` - `cancel_agent` per Cases, `CancelIncompleteError`, `CancelRefusedError`, `list_agents` hides the manager by identity.
- `bobi/cli.py` - `subagents_cancel` (`:2183-2193`) prints either error on stderr and exits 1.
- `bobi/sdk.py` - `SessionEntry.pid_start`, set and cleared by `register` and `update`.
- `bobi/reconcile.py` - per Reconciler, and Phase 7 under Q1 (a).
- `bobi/service.py` - `_process_parent_map` moves out, and `caller_is_manager_descendant` uses the shared table.
- `bobi/webapp/runs.py`, `docs/RUNS_VIEW.md` - the `running` rule in Effects on records.
- `bobi/brain/stub.py` - a test-only directive, `__stub__:spawn:<absolute path>`, that runs `[sys.executable, path]` and hangs. The existing parser accepts a path with no spaces (`_DIRECTIVE`, `:86`).
- `skills/bobi.md` - a short "Cancelling a run" subsection after the commands block, because `:194` sits inside a code fence.
- `docs/RUN_RESUME.md:114`, `bobi/workflow/orchestrator.py:217-222`, `bobi/webapp/run_actions.py:82-86` - the three copies of the shared-pid warning now say cancel terminates that process and everything under it, or refuses for the manager and pid-file daemons.
- `tests/integration/conftest.py:445` - docstring only.
- `tests/integration/COVERAGE.md` - index the new tests.
- Tests that must follow, or the suite cannot stay green:
  - `tests/test_service.py:208`, `:217`, `:221` - the moved parent map. The table must not require field 22 in that test's short stat line, or the test gains it.
  - `tests/test_subagent.py:87`, `:117`, `:127` - an unbound root, the role-based list test, and the mock-contract cancel test.
  - `tests/test_cross_project_discovery.py:150-159` - `bobi-manager-eng` is not a real manager session name.
  - `tests/test_webapp_runs.py` - any row seeded `running` with no live session.
  - Phase 7 only: `tests/test_reconcile.py:76-88`, which seeds `pid=os.getpid()` and expects it cancelled.

### New

- `bobi/proctree.py` - one process-table module, per Kill sequence. It must exist because `bobi/subagent.py`, `bobi/reconcile.py`, `bobi/sdk.py` and `bobi/service.py` all need it, and it replaces the private map in `bobi/service.py`.
- `tests/fixtures/cli_shaped_tree.py` - the measured topology. It writes its pids and ticks beside its own path, and self-exits after 120s (precedent: `tests/fixtures/supervisor_stub_manager.py`). A test copies it into its tmp dir, so one directive argument carries everything.
- `tests/fixtures/tick.py` - the command V21's real agent runs.
- `tests/test_proctree.py` - V1 to V10.
- `tests/integration/test_subagents_cancel.py` - V20, V21, V24.

## Questionables

- **Q1: Is the #1124 deadline fix in this PR or out of it?**
  Options: (a) in: Phase 7 runs, and the PR body says `Fixes #1124` / (b) out: Phase 7 is skipped and #1124 stays open.
  Recommendation: (a), for the reasons under "The recommendation", with the reason corrected from the director's wording.
  Nothing forces the answer, and the plan works either way.

  **What the #1124 part is.** Phase 7, last and self-contained.
  At the deadline branch (`bobi/reconcile.py:200-216`), a run with a recorded token whose `verify` does not say `gone` is left alone: no signal, no status change, no event, one WARNING naming its age and budget. That includes `unknown`, so an unreadable table cannot kill a healthy run.
  A zombie is gone, so a dead process cannot hold its entry open.
  Every other past-deadline entry falls through unchanged: pid 0 (the `starting` corpse #850 relies on this branch to clear), a recycled pid (C3), and no token (C4).
  It needs `pid_start`, so it cannot land before Phase 3.

  **What happens at a director restart under each answer:**

  | | Main today | (b) #1122 only | (a) #1122 plus Phase 7 |
  |---|---|---|---|
  | Verified-live run past deadline, processes | Runner killed, CLI and tools keep running untracked | Whole tree stopped | Untouched |
  | Same run, record | `failed`, event emitted | `failed`, event emitted | `running`, no event, WARNING |
  | Same run, remaining steps | Never run | Never run | Run normally |
  | Relaunch, if the manager chooses one | Second writer beside the orphan | One writer | Not prompted |
  | Past deadline, recycled pid | Stray SIGTERM to an unrelated process, `failed` | Nothing signalled, `failed` | Same as (b) |
  | Past deadline, pid 0 | `failed` | `failed` | `failed` |
  | Past deadline, no token | One SIGTERM, `failed` | Same | Same |
  | Run under deadline | Untouched | Untouched | Untouched |
  | Manager's first turn delayed by | Nothing | About 2.5s per live timed-out tree, 15s worst | Nothing |
  | Automatic bound on a runaway run | The next restart, if one happens | The next restart, if one happens | None |

  **The interaction, checked against the code.**
  The director's reason was: with the stronger kill alone, a restart would kill healthy long-running runs that are currently left running untracked.
  This holds for the processes and does not hold for the runs.

  What holds:

  - The deadline does not measure health.
    It is `started_at + timeout + 300` (`bobi/reconcile.py:201-203`), and `timeout` is a run-level value defaulting to 3600 (`bobi/subagent.py:1253`, `bobi/cli.py:4031`), which the reactor never overrides (`bobi/events/reactor.py:345-353`).
    The runner never enforces it (F1), so a run is free to pass it.
  - Long healthy runs are ordinary here: 17 of 582 completed registry entries with a timeout ran past timeout + 300, the longest 8915s on a 3600s timeout, and 15 of the 17 were single-step adhoc runs.
    The registry keeps one entry per session name, so this undercounts.
  - The branch fires in production: 5 runs at 3 restarts on this instance (2026-08-20, 2026-10-10, 2026-10-14) and two consecutive restarts on baohua (#1124).
    F2's run was verifiably healthy. The other four were not checked.
  - A probe ran the real `reconcile_sessions` from `e8fe14f1` on an isolated registry root with a real CLI tree, nothing posted.
    At age 100s it took no action.
    At age 3901s on `main`: runner dead, CLI alive with `ppid=1`, tool command writing (ticks 3 to 18 in 30s), entry `failed`, event emitted.
    With a stand-in tree kill injected through `cancel=`: everything dead in 2.37s, ticks frozen at 3, entry `failed`, same event.

  What does not hold is "currently left running" as a description of the run.
  On `main` the same restart already ends it: the runner is dead, no later step can start, the entry is `failed`, and the event that prompts a relaunch goes out.
  Only the current step's CLI and tools survive, until the CLI finishes its turn.
  So the stronger kill does not create the false expiry. It changes what the false expiry costs:

  - Worse: the orphan's remaining turn is lost.
    For a single-step adhoc run that turn is the whole job, and on `main` it can still finish and push with nothing recording it.
    F2's orphan wrote its handoff and posted a PR comment at 15:12Z, 25 minutes after the close.
    A tool command killed mid-write can also leave `.git/index.lock` for the relaunch (R6).
  - Better: no second writer after a relaunch, and no untracked process holding credentials.
    F2's orphan and its relaunch were live on one PR for about 10 minutes.
  - Recovery in both cases is a relaunch, which is the manager's choice: no framework rule or eng-team package rule relaunches on `agent/session.failed`.
    An explicit run key resumes the saved transcript (`bobi/workflow/orchestrator.py:673`), and a derived key starts fresh (`bobi/subagent.py:1342`).
    Either way it replays from step 0 unless the run key is a period key (`bobi/subagent.py:1499-1512`).

  **The recommendation.** (a), the same PR, because:

  - A restart ending healthy runs is the larger remaining harm, and answer (b) leaves it in place with a higher in-flight cost.
  - It removes D2's manager-start delay and R3.
  - Phase 7 is last, so it can be dropped or split at review without redoing anything.

  Cost: one branch in `bobi/reconcile.py`, four doc and comment edits, two unit tests, and a flipped e2e, plus a policy change.
  The policy change is the real cost: `bobi/workflow/orchestrator.py:87-93` and `docs/WORKFLOW_ENGINE.md:238` call the reconciler "the outer net" for a runaway run, and (a) removes that net for any run with a verified process.
  That net only ever fired if a restart happened to land, so the 8915s run shows it was not a bound.
  After (a) the operator's tools are the elapsed time in `subagents list` and a cancel that works.

  If (b): releases are cut by hand and separately from landing (`docs/RELEASE_RUNBOOK.md`), so a follow-up landed before the next release avoids the interim behaviour.

  Rules considered for the #1124 part and rejected:

  - Kill a verified-live run only when it is also quiet: a long tool call or CI wait looks idle, and `last_activity` read 20+ minutes stale on a busy worker (moda-agents#88, 2026-08-03).
  - Derive the deadline from step budgets: adhoc's budget is the 1800s default (`bobi/workflow/schema.py:78`), below the runs above, so it would still expire them.
  - Enforce the timeout in the runner: that would have killed the 17 recorded completions.

  Ownership: a 2026-10-19 comment on #1124 asks `cuongbytedev` to agree which ticket owns the reconciler change.
  The issue shows no reply, and no PR mentions #1124 or MOD-434 (searched 2026-11-03).

- **Q2: Where does X3 get tracked once #1122 closes?**
  The 2026-10-14 comment asks for X3 in scope or in an explicit follow-up.
  Options: (a) a note on #1115 when this lands / (b) a new issue.
  Recommendation: (a), since the team prefers fewer issues and #1115 stays open under D9.
  X3 stays out of this PR either way: once the runner is dead no record names the orphan, and giving it one is new design.

## Phases

Phase 5 is independent of Phases 2 to 4 and could run in parallel.

### Phase 1 - Reproduce the survivor on `main`

- [x] Add `tests/fixtures/cli_shaped_tree.py`: same-group child, own-session shell, own-group command writing its pids and ticks.
- [x] Add the stub directive `__stub__:spawn:<absolute path>`.
- [x] Write V20, V11 and V22.
- [x] Follow `/opt/moda-skills/plugins/moda/skills/investigate/SKILL.md`, and keep the red output for the PR.

**Validation gate**

- [x] `pytest tests/integration/test_subagents_cancel.py -k test_cancel_stops_cli_shaped_tree -q` fails on `main` because a tree pid is still alive and the tick file grew, not on an import or a timeout.
- [x] `pytest tests/test_subagent.py -k test_cancel_agent_stops_tree --timeout=30 -q` fails the same way.
- [x] `pytest tests/test_webapp_runs.py -k test_cancelled_run_is_not_running --timeout=30 -q` fails on `main` with `counts.running == 1`.

### Phase 2 - Process table and tree kill

- [x] Add `bobi/proctree.py` per Kill sequence and Safety invariants.
- [x] Move `_process_parent_map` there and point `bobi/service.py` and `tests/test_service.py` at it.

**Validation gate**

- [x] `pytest tests/test_proctree.py --timeout=30 -q` passes V1 to V10.
- [x] `pytest tests/test_service.py --timeout=30 -q` passes.

### Phase 3 - Process identity

- [x] Add `SessionEntry.pid_start` and maintain it in `register` and `update`.

**Validation gate**

- [x] `pytest tests/test_registry.py --timeout=30 -q` passes V16, including an entry on disk with no `pid_start`.

### Phase 4 - Cancel, the reconciler, the runs view

- [x] Rewrite `cancel_agent` per Cases, add the two errors, and update the CLI.
- [x] Update `_default_cancel`, `_default_emit` and the sweep per Reconciler.
- [x] Add the `running` rule to `_all_rows`.

**Validation gate**

- [x] `pytest tests/test_subagent.py tests/test_reconcile.py tests/test_cli.py tests/test_webapp_runs.py --timeout=30 -q` passes V11 to V15, V17, V18, V22, V23.
- [x] V11 and V22 are now green with no change to their assertions.

### Phase 5 - Stop listing the manager

- [x] Change `list_agents` to the identity test in I4.

**Validation gate**

- [x] `pytest tests/test_subagent.py tests/test_cross_project_discovery.py --timeout=30 -q` passes V19.

### Phase 6 - End to end and docs

- [x] Finish V20, V21 and V24.
- [x] Update the docs and docstrings listed in Relevant files, in this PR.

**Validation gate**

- [x] `pytest tests/integration/test_subagents_cancel.py -v` passes, and V21 shows as `PASSED`, not `SKIPPED`, in the output on the PR.
- [x] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` is green.

### Phase 7 - The #1124 deadline fix (only if Q1 is answered (a))

Replaced by #1124 (see Amendments). None of this is built here.

- [f] state:replaced-by-1124 Leave a verified-live run alone at the deadline branch, as Q1 describes.
- [f] state:replaced-by-1124 Update `bobi/reconcile.py:17-18`, `docs/WORKFLOW_ENGINE.md:116-118`, `:238`, `bobi/workflow/orchestrator.py:91-93`.
- [f] state:replaced-by-1124 Replace V17 with V25, add V26, flip V24's expectation, and update `tests/test_reconcile.py:76-88`.

**Validation gate**

- [f] state:replaced-by-1124 `pytest tests/test_reconcile.py --timeout=30 -q` passes V25, V26 and the updated V23.
- [f] state:replaced-by-1124 `pytest tests/integration/test_subagents_cancel.py -k test_manager_restart_past_deadline -v` passes with the tree alive after a real manager start.

## Proof of work

Bug claims get a failing test first (Phase 1), never on code that does not exist yet (F7).
Negative claims get mutation proof, in the table below.
No web UI code changes, so no capture is required.

**Harness safety.** Every test that could signal its own caller when a guard is mutated out (V3's I5 leg, V18's C7 leg, V23) runs the caller in a disposable session, using the shape of `_RUNTIME_CALLER_HARNESS` (`tests/integration/test_manager_lifecycle.py:396`).
Otherwise the I5 mutant kills pytest and whatever owns it, with no report.
Every test that starts the fixture kills its recorded pids in `finally`, because `_reap_process_group` only signals a group (`tests/integration/conftest.py:467`) and by E2b misses the tree. Run mutants under `setsid --wait`.

**Unit, real processes, no mocks (`tests/test_proctree.py`)**

- V1: The fixture tree: after `terminate` all four processes are gone and the tick file stops growing.
- V2: A descendant ignoring SIGTERM is removed by the SIGKILL escalation, with short graces.
- V3: I1 and I5: a non-descendant sibling in the root's own session survives, and `is_self_or_ancestor` is true for the caller and each process up its parent chain.
- V4: I2: a mismatched token gives `gone`, and the process is not signalled.
- V5: A zombie root counts as exited, and the call returns without escalating.
- V6: A parent that traps SIGTERM, forks, and outlives one poll: the child is also stopped.
- V7: The `ps` source, against the real fixture where `ps` is installed and against canned output otherwise. This container has no `ps`, so CI is where the real leg runs. A failed read leaves members as survivors, never exited.
- V8: I3, for each pid file: the protected process and its subtree survive with the Output line, a target whose parent is the protected process is still stopped, and a file older than this boot, or older than its process by more than 1h, protects nothing.
- V9: An `EPERM` member is reported as a survivor with `signal not permitted` (patched `_send`), and the call returns within the two graces.
- V10: An interrupt during the wait sends SIGKILL to live members and propagates.

**Unit (`tests/test_subagent.py`, `tests/test_reconcile.py`, `tests/test_cli.py`, `tests/test_registry.py`, `tests/test_webapp_runs.py`)**

Survivors cannot be produced for real, so V13, V17's survivor leg and V18's C2 leg patch `bobi.proctree._send`.

- V11 `test_cancel_agent_stops_tree`: `cancel_agent` against the fixture in a real registry: returns `True`, the tree is gone before it returns (a zombie counts as gone), the entry is `cancelled` with `pid=0`. Red on `main`.
- V12: C6 on a team with a custom `entry_point`: refused, nothing signalled, a detached child of the manager survives.
- V13: C2: survivors raise `CancelIncompleteError`, the entry is `cancelled` with survivors in `error`, and an entry relaunched during the wait keeps its own `error`.
- V14: C3, C4, C9, C10: a recycled pid is not signalled, and an entry with no token gets exactly one SIGTERM, with its children untouched and no SIGKILL. A young `starting` entry and an ambiguous run key are refused and untouched.
- V15: Effects on records: a registered sub-run whose process is a non-leader descendant, and an entry sharing the root pid, both end `cancelled`, not `crashed`. The root is started through a launcher so it is not the test's own zombie, one member lingers 1s, and a thread reading `state.json` during the wait never sees `crashed`. An entry a runner rewrites to `running` during the wait ends `cancelled`.
- V16: `register` and `update` set and clear `pid_start`.
- V17: The deadline branch stops a real fixture tree and writes `failed` once, never `cancelled`. A past-deadline parent with a `--wait`-shaped child, named so the parent sorts first, ends with the child `cancelled` and exactly one event. A survivor is named in the error (patched `_send`). An interrupt mid-wait leaves `failed` with `emit_confirmed=False`. Retired by Phase 7.
- V18: `subagents cancel` through the CLI with the real `cancel_agent`, not a patched callee: exit 1 and the Output text for C2, C6, C7, C9, C10, and exit 0 with the line for C3, C4, C5.
- V19: `subagents list` omits the manager on a team with a custom `entry_point`.
- V22 `test_cancelled_run_is_not_running`: after a real `cancel_agent` on a run with a ledger row, `build_runs` reports `counts.running == 0` with the row `closed` and its end time set. An older row for a relaunched session name, and a row with no session, are `closed`. A resumed older row with a live session stays `running`. A deadline-closed row is `failed`, with its error. `waiting` and `resuming` rows are untouched. Red on `main`.
- V23: A sweep run from a process descended from a no-token target refuses, leaves the entry active, emits nothing, and signals nothing. A `_default_emit` whose `post_event` returned `False` reports `False`, and `emit_confirmed` stays false.

**End to end (`tests/integration/test_subagents_cancel.py`)**

Pass `timeout=30` to the CLI runner, because its 10s default (`tests/integration/conftest.py:371`) is under cancel's 15s.

- V20 `test_cancel_stops_cli_shaped_tree` `[stub]`, on `stub_bobi_env`: a detached run whose stub turn spawns the fixture, then `subagents cancel`. The command exits 0, and when it returns the runner and the whole tree are dead and the tick file is frozen. Runs in CI's `integration-fast`.
- V21 `[claude]`, gated on the CLI: the agent runs `tests/fixtures/tick.py`, which writes its own pid. After `subagents cancel` returns, that pid is dead and the tick file does not grow for 6s. It reads the pid file, not `bobi/proctree.py`. The real CLI's process layout is where the risk lives, and no CI job has the CLI (`tests/integration/COVERAGE.md:6-14`), so it runs locally and the PR shows it ran.
- V24 `test_manager_restart_past_deadline` `[stub]`: start and stop a manager once to mint the bubble (`bobi/events/publish.py:72`), with external topics off (`tests/integration/test_manager_lifecycle.py:30-34`), and register an event observer. Launch the detached run and wait for the fixture's pid file, because the runner's own `register` would undo an earlier edit (`bobi/workflow/orchestrator.py:354`). Rewind `started_at`, start the manager, poll. Without Phase 7: tree gone, entry `failed`, one `agent/session.failed` seen by the observer. With Phase 7: tree alive, entry `running`, no event.

**Phase 7 only (`tests/test_reconcile.py`)**

- V25: A verified-live fixture tree past deadline: nothing signalled, entry stays `running`, no event, a WARNING names its age and budget.
- V26: The same sweep still closes pid 0, a recycled pid with nothing signalled, a no-token entry with one SIGTERM, and a zombie with an equal token. A tokened run with an unreadable table is left alone.

**Mutants**

| Guard | Test | Mutant that must fail it |
|---|---|---|
| Tree walk, not group | V1, V20 | Replace the walk with `os.killpg`, under `setsid --wait` |
| I1 | V3 | Also signal same-session processes |
| I2 | V4, V14 | Skip the token compare |
| I3 | V8 | Remove the protected check, subtract every descendant of a protected pid from the target's tree, or drop the stale-file check |
| I4 | V12, V19 | Test `role == "manager"` instead of name and pid |
| I5 | V18, V23 | Delete the ancestor guard from `cancel_agent`, in the disposable session |
| I5 helper | V3 | Make `is_self_or_ancestor` check only the caller |
| Late children | V6 | Do not re-read the table |
| Escalation | V2 | Remove the SIGKILL round |
| Wait cap | V9 | Remove either grace bound |
| Failed read | V7 | Treat a failed table read as exited |
| Write before signal | V15 | Move the write after the first SIGTERM |
| Second write | V15 | Remove step 7 |
| One deadline write | V17 | Write `cancelled` first and `failed` after the wait |
| Sweep re-read | V17 | Act on the snapshot's entry instead of re-reading |
| Refusal skip | V23 | Let `_default_cancel` swallow `CancelRefusedError` |
| Honest emit | V23 | Return `True` regardless |
| Runs view rule | V22 | Remove it |
| C9, C10 | V14 | Remove either refusal |
| Phase 7 | V25, V26 | Remove the check, treat a zombie as verified, or treat `unknown` as `gone` |

**On the PR**

- E1 and E4 rerun through the real `bobi` commands, before and after, plus the background-task shape (E5).
- Red-then-green output for V11 and V20.
- V21 output showing it executed.
- A real `state.json` showing `pid_start`, and the CLI exit codes for C1, C2, C6 and C7.
- The review gate per `/opt/moda-skills/plugins/moda/skills/review/SKILL.md`.

## Lane map (only if fanned out)

| Lane | Phases | One-line scope | Depends on / lands after | Dispatch issue (if any) | Status |
|---|---|---|---|---|---|
| - | - | - | - | - | - |

## Amendments

None yet. This plan is unapproved.

### 2026-11-04: Gate 1, Q1 and Q2

The line above predates approval and stays, because this section only grows.

Zach approved this plan for implementation in Slack on 2026-11-04 ("is approved for implementation").
D1 to D9 stand as written.

**Q1, the #1124 deadline fix.**
On 2026-11-03 Zach's scope answer was in ("sure we can fix the deadline check in the same PR").
He then learned that `cuongbytedev` already has a different fix on `feat/MOD-434-restart-reconciler-deadline`, and the director recommended using it.
The approval did not answer that, so Phase 7 is on hold pending Zach's answer and this PR builds Phases 1 to 6 only.
Without Phase 7, a director restart behaves as column (b) of the table under Q1: a verified-live run past its deadline has its whole tree stopped, its entry goes `failed`, and its event is emitted.
#1124 stays open.
Phases 1 to 6 keep either design available: Phase 7 needs only `pid_start` from Phase 3, and the deadline test stays in one place in `reconcile_sessions`.

**Q2.** Recommendation (a) stands: a note on #1115 when this lands.
The note belongs to the landing, not to this PR.

### 2026-11-04: What the build added

None of these changes a decision. Each is needed for what the plan already asks.

- `terminate` takes `protected`, `before_signal` and `label`, and `is_self_or_ancestor` takes `start`.
  `before_signal` runs before each batch of members is signalled, so steps 3 and 5 of the kill sequence share one code path and the interrupt rule covers the writes.
- `SessionRegistry._reap_if_dead` reads the entry again before it writes `crashed`.
  Writing before signalling (A4) leaves one window: a read from before the write that finishes after the process died.
  The pid is dead by then, so a fresh read sees the write.
- `_workflow_rows` orders a session's rows by `resumed_at or started_at`, not `started_at` alone.
  Otherwise `_claim_sessions` folds a live session onto a closed row, and a resumed older row counts as running twice.
- SIGKILL is not sent to a member whose token cannot be checked at that moment.
  It is reported as a survivor with `process table unavailable`.
- `paths.webapp_pid_path()` is new, so cancel and the web app's daemon share one definition of that file.
- V21 has two legs, a foreground command and a background task, which is the E5 shape on the real CLI.

Reruns through the real `bobi` commands on 2026-11-04, on CLI 2.1.309, showed the tool command sharing its shell's process group, where the Problem section measured a group of its own.
The kill set follows parents, so it reaches both shapes, and the fixture keeps the harder one.

### 2026-11-04: Phase 7 is replaced by #1124

Zach decided in Slack on 2026-11-04 that the team takes over `feat/MOD-434-restart-reconciler-deadline` as its own PR for #1124, landing before this one.
That replaces Phase 7, whose items are marked `[f]` above because they will not be built here.
Q1 is answered: neither (a) nor (b), since the deadline check gets its fix from that PR instead.
Until #1124 lands, a restart behaves as column (b) of the Q1 table.
Once it does, this branch merges `main` and reruns its suite, and V24 and V17 follow whatever that PR decides for a verified-live run.

### 2026-11-04: What the review changed

The review gate found these, each with a test that failed first.
Each changes what the plan's sections above say, and none changes a decision.

- **One table source per host.** `read_table` fell back to `ps` whenever `/proc` gave no rows, and a `ps` token never equals a `/proc` one, so one failed read made every process read as gone.
  Any failed read now gives no table, and `ps` is only for a host with no `/proc` or an empty one.
  Only a process that exited, one `hidepid` hides, and a host with no boot id are treated as facts, so a hole in the table cannot read as a process gone.
- **C4 is narrower.** The Cases table lists "no process table" under C4.
  A run with a recorded token and no readable table is now refused with exit 1 and nothing signalled, because a token proves this host has a table, so a retry works, and closing the entry would lose the only handle on the tree.
  It also keeps I5 true for such a run, which no table cannot otherwise prove above the caller's parent.
  A run with no token keeps the single SIGTERM, so a host with no table at all can still cancel, and I5 stays best effort there: with no table and no token, cancel from inside a run can still reach an ancestor above the caller's parent, as `main` does (R8).
- **Step 7 of the kill sequence is one scan after the kill.**
  It closes any active entry naming a process that was signalled, and any entry this cancel closed that was rewritten to `running` with no pid and an unchanged `started_at`.
  The plan's "unchanged `started_at`" missed a runner's first `register()`, which replaces it, and a sub-run registered after step 1's list was never closed.
  That scan replaces the rescan the build added for later batches.
- **`close_dead_run` reads the entry again**, as `_reap_if_dead` does, for launch admission and the sweep's small window.
  The sweep's own re-read stays too, because it decides on what the entry says now: a stale read would stop a relaunch, or lose a crash a list read had reaped.
- **The deadline branch checks `started_at` after the wait.** `failed` lets a relaunch take the name, and the branch would otherwise write `failed, pid=0` over the new run.
  It leaves the new entry alone and announces the old run from what it read, as launch admission does for a dead run. The event carries the run key the new run also holds.
  A run that finished by itself during the wait is left for the next sweep to re-emit, not announced as timed out.
- **`_default_cancel`'s catch-all reports what the entry says.** A cancel that raises after closing the run is still announced in that sweep.

The mutant table's rows follow: "Second write" is now a mutant of that scan, "Sweep re-read" is now caught by a relaunch test in `tests/test_reconcile.py` and no longer by V17, and the rest of the mutant set is on the PR.

Considered and left as they are, each narrow, with its reason on the PR.
Most are races at a run's first moments, recoverable with a second cancel, or a stale pid file inside the 1h margin I3 chose.

## Notes

### What this does not close

- **X1, narrowed by F1:** the in-runner limit for check, gate and curator agents, and session teardown (`client.disconnect()`, and `abort()` at `bobi/brain/claude.py:202-223`).
  Code reading says these end the CLI with SIGTERM or SIGKILL, which by E2b and E3 would leave a running tool command. Not reproduced. The fix would sit in session teardown, which every run executes on success too.
- **X2:** Processes that detached and were reparented before the cancel. Reaching them needs cgroups.
- **X3:** A runner that dies without a cancel (OOM, SIGKILL, manual kill). It orphans its CLI as E1 shows, the entry is reaped as `crashed`, and no command can stop the orphan. Seen in production on 2026-10-14. See Q2.
- **X4:** A runner SIGTERM handler. It cannot fix this: by E2b the CLI's own handling leaves the tool command, and a wedged runner cannot cooperate.
- **X5:** A lifecycle event for `cancelled`. None is emitted today, and that stays.
- **X6:** Using the identity check for liveness (`bobi/sdk.py:111`, `:595`). A false mismatch there would mark a healthy run crashed.
- **X7:** Other reapers: `bobi/auth_bootstrap.py:1229`, `tests/integration/conftest.py` (`_reap_process_group`), `bobi/supervisor/load.py:208`. Only the conftest docstring changes.
- **X8:** Cancel on a terminal entry still answers `No running sub-agent`.
- **#1124's false expiry**, under Q1 (b).
- **#1115** stays open under D9.
- **The ledger itself:** rows stay `running` on disk.
  `bobi agent <x> workflows status` prints that raw word (`bobi/cli.py:2983-2998`), and a non-period relaunch after a cancel, crash or deadline close cannot adopt its checkpoint, because adoption requires `failed` (`bobi/workflow/orchestrator.py:395`). Both are today's behaviour.
- **Another team's manager:** I3 reads the target's own root, so a run that starts a different team's manager and is cancelled while that manager is still its descendant stops it. Test managers on isolated homes rely on this to die with the run.

### Risks

- **R1:** A wrong kill set stops the manager, a shared daemon, or the caller. Mitigated by I1 to I5 and their mutants.
- **R2:** SIGKILL skips the CLI's session-file flush. It applies only after 10s of ignored SIGTERM, and the measured exit is about 2.5s.
- **R3:** The manager's first turn and event delivery wait for the sweep. Its health endpoint is already up (`bobi/service.py:699`), so the supervisor does not read this as a stall (threshold 600s). Removed by Q1 (a).
- **R4:** A false identity mismatch would leave a live run untracked. `boot_id` is constant per boot and `starttime` per process, so none is known on Linux. A reader in a different time namespace is the one theoretical case.
- **R5:** A child forked within one poll interval before its parent exits is missed, and is then an orphan as in X2.
- **R6:** A tool command killed mid-write can leave partial state such as `.git/index.lock`. A cancelled run's worktree was never guaranteed clean.
- **R7:** The `ps` source is proven in CI against Linux `ps` only, not on a real macOS host. A false mismatch there is C3, which marks the run cancelled and signals nothing, the original bug. `LC_ALL=C TZ=UTC` is the mitigation.
- **R8:** Runs in flight across the upgrade have no token and stay in C4, today's behaviour, until they finish.
- **R9:** The registry goes `cancelled` up to 15s before the tree is gone, so a concurrent relaunch on the same name could overlap the dying tree. A sequential cancel then relaunch cannot, because cancel has returned.
- **R10:** A daemon that has not written its pid file yet is not protected. The manager wrote its file 0.6s after start and the sidecar 2.2s after, measured here. A doomed second spawn can also overwrite the file and unprotect the live daemon (`bobi/events/server.py:682-701`, `bobi/webapp/daemon.py:141-171`). Such a daemon is only a descendant of a run for as long as the `bobi` command that spawned it is alive.

### Inventories, derived at `e8fe14f1`

Each is `git grep -n -E '<pattern>' e8fe14f1 -- 'bobi/*.py'`, unless another path is named.

- **N1, cancel callers,** `cancel_agent\(|import cancel_agent`: `bobi/cli.py:2190`, `bobi/reconcile.py:73`.
- **N2, reconciler test injection,** `cancel=` over `tests/*.py`: `tests/test_reconcile.py` (14 calls), `tests/test_orchestrator.py:2459`, `tests/test_completion_delivery_loop.py:93`.
- **N3, non-zero pid writes to the registry,** `(^|[ (,])pid=(os\.getpid\(\)|pid\))`, filtered to registry calls: `bobi/subagent.py:1632`, `:1734`, `bobi/workflow/orchestrator.py:357`, `:532`, `bobi/session.py:1888`.
- **N4, signal handlers,** `signal\.signal\(|add_signal_handler`: `bobi/kb/sidecar.py:151`, `bobi/service.py:695`, `bobi/supervisor/supervision.py:826`. None is in the runner path.
- **N5, pid files,** `\.pid"`: `manager.pid` (`bobi/paths.py:253`), `event-server.pid` (`bobi/paths.py:273`), `embedding-sidecar.pid` (`bobi/kb/embedder.py:26`), `app.pid` (`bobi/webapp/daemon.py:44`).
- **N6, process spawns,** `Popen\(|create_subprocess`: `bobi/auth_bootstrap.py:275`, `bobi/brain/codex.py:197`, `bobi/events/server.py:695`, `bobi/kb/embedder.py:86`, `bobi/monitors/scheduler.py:400`, `bobi/service.py:467`, `bobi/setup/webui/server.py:1284-1288`, `bobi/subagent.py:1046`, `bobi/supervisor/supervision.py:311`, `bobi/webapp/daemon.py:164`, `bobi/webapp/run_actions.py:134`. Nothing under `bobi/metrics/`, so 0.61.0 and 0.62.0 added no process a run could own. A codex brain's CLI is covered like claude's, since the kill set is by parent.
- **N7, in-runner limits,** `wait_for\(|asyncio\.timeout\(` over `bobi/workflow bobi/subagent.py bobi/session.py`: `bobi/session.py:1280` (ready poll), `bobi/subagent.py:471`, `:2757`.
- **N8, ledger status writes,** `\.status = "` over `bobi/workflow bobi/subagent.py bobi/reconcile.py`: `bobi/workflow/orchestrator.py:397`, `:546`, `:1242`, `:1740`, `bobi/subagent.py:1511`, `bobi/workflow/state.py:134`, `:164`. None is in `cancel_agent` or `bobi/reconcile.py`.

### Verification record

Run on 2026-11-03, in `/tmp` on the eng-team instance, with no code in this PR. Harness and logs are kept at `run/workspace/1122-repro/`.

- E1b, E2b, E3, E4, E5: `exp.py`, real CLI, foreground. Only pids the harness started were signalled.
- The Q1 probe: `probe1124.py`, the real `reconcile_sessions`, isolated root, captured emits.
- RC5: `fold_probe.py`, the real `cancel_agent` and `build_runs`, isolated root.
- Registry and ledger counts: read-only scans of this instance's `run/state`.

Second-model review did not run: `codex login status` prints `Not logged in`, and the `aichat` gateway variables are unset.
Three fresh-context passes by the same model (red team, staff engineer, implementer) ran against the first draft and read the code, and a fourth ran against the rewrite. Their findings are folded in as A1's read-side fix, A2 to A7, F9, and the test shapes under Proof of work.
