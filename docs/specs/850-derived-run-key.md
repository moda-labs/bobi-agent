# Issue #850: Derive A Default Run Key So Duplicate Suppression Is The Default

> **2026-08-19, #1057:** the `spawn_adhoc` executor and its `adhoc:spawn`
> derivation namespace described below were retired - `--wait` now runs
> through `launch_agent`/`run_workflow` with the same admission as every
> other launch, and the wait/persistent collision this spec's second
> namespace prevented is now prevented by name shape (`wf-adhoc-*` vs the
> bare key). The `--wait` exemption described under Rollout is likewise
> retired: `--wait` now takes the full admission (dedup, spend governor,
> concurrency). The derivation dials themselves still describe current
> behavior.

## Problem

Bobi already rejects a second launch that lands on an active session name:

```python
if existing and existing.status in ("starting", "running", "idle"):
    raise RuntimeError(f"A run is already active: {session_name} ...")
```

That guard keys off `session_name`, which is `wf-{workflow}-{project}-{run_key}`.
So it only fires when two launches agree on `run_key` - and `run_key` came from
an optional `--id` flag.

Omit `--id` and `launch_agent` minted a random key:

```python
run_key = run_key or f"adhoc-{uuid.uuid4().hex[:8]}"
```

Every un-keyed launch therefore got a unique session name, nothing ever
collided, and the guard was dead code. The failure was silent: no warning, no
log line, no visible difference between a run that is protected and one that is
not.

The 2026-07-25 `gtm-team` incident is the concrete cost. The monitor's own
launch carried `--id` and behaved correctly. A role file documented the same
command *without* `--id`, the agent followed it, and the resulting chain ran 50
launches to the spend cap.

## Reproduction

`launch_agent` called five times with a byte-identical task and no `run_key`:

```
wf-adhoc-tmp-adhoc-4cb46f5e
wf-adhoc-tmp-adhoc-9b228d9f
wf-adhoc-tmp-adhoc-7d12786c
wf-adhoc-tmp-adhoc-e25b4881
wf-adhoc-tmp-adhoc-b1bb6980
distinct session names: 5 of 5
```

## Root Cause

The framework had **two different defaults for the same decision**, and the more
used path picked the unsafe one.

| path | flag | default run key | collides? |
|---|---|---|---|
| `spawn_adhoc` (`--wait`) | `name` | `adhoc-<sha256(task)[:8]>` | yes |
| `launch_agent` (detached, reactor, everything else) | `run_key` | `adhoc-<uuid4>` | **never** |

`spawn_adhoc` already understood the invariant - its docstring spells out that
"dispatching the SAME task text twice collides by construction". `launch_agent`,
which is what `subagents launch` uses without `--wait` and what the event
reactor calls, minted entropy instead.

## Goals

- Make identical launches collide **by default**, so duplicate suppression is a
  property of the system rather than of prompt discipline.
- Give genuinely parallel fan-out an explicit, documented opt-out.
- Make an un-keyed launch observable, and its refusal actionable.
- Use one derivation for both launch paths.

## Non-Goals

- **Changing the semantics of an explicit `--id`.** `--id 42` means "this is run
  42"; relaunching it resumes that run. Unchanged.
- **Recursion depth or lineage.** #849 is the structural fix - a run knows what
  launched it and refuses to launch the workflow it is executing. That is robust
  to paraphrase; a task-text hash is not. This ticket is defense in depth on task
  text, and **must not** be used as grounds to close #849.
- **Fixing the `--wait` path's missing preflights.** Filed as #874, see Rollout.
- **Making check-and-register atomic across processes.** Pre-existing; filed as
  #875, see Rollout.

## What Changed

### 1. One derivation, used by both paths

```python
def derive_run_key(workflow_name, task, *, project="", role="",
                   model="", effort=""):
    dials = "\n".join([workflow_name, project, role, model, effort,
                       " ".join(task.split())])
    return f"adhoc-{hashlib.sha256(dials.encode()).hexdigest()[:12]}"
```

- **Every dial that decides what the launch is takes part**, not just the task.
  One task handed to an engineer and to a reviewer is two runs; `skills/bobi.md`
  documents varying model and effort per delegation the same way. Deriving from
  `(workflow, task)` alone refused the second as a duplicate of the first.
- **The dials are the values as passed.** Two launches that both omit `--model`
  agree on `""` and still collide - the incident shape, two copies of one
  command. An explicit override equal to the role default reads as different,
  which errs toward launching. Resolving them first would need team config
  inside the derivation and would trade a weaker guard for a false refusal,
  which is the worse failure.
- **Whitespace-only normalization of the task.** A task re-emitted with
  different wrapping still collides. Case is not folded and punctuation is not
  stripped, for the same reason.
- **The project takes part** because a persistent launch uses the key AS the
  session name, with no `wf-<workflow>-<project>-` wrapper to scope it. Two
  working dirs under one installation running an identical task would otherwise
  land on one session, and the second would silently take over the first - a
  worse outcome than a duplicate refusal. The key and the name agree about what
  identifies a run.
- **12 hex chars.** 48 bits makes an accidental collision negligible.
- **The `adhoc-` prefix is kept** so `subagents list` still shows at a glance
  which runs are un-keyed (issue #850, option 3).

Two namespaces, not one, because two paths use the key as the session name
**outright**: `spawn_adhoc` always, and `launch_agent` when `persistent=True`.
Deriving both under `adhoc` gave an un-keyed `--wait` run and an un-keyed
`--persistent` agent on one task the same session - one inbox, one registry
pid, one saved transcript, two live processes writing them. `spawn_adhoc`
therefore derives under `ADHOC_SPAWN_WORKFLOW` (`"adhoc:spawn"`). A
non-persistent `launch_agent` needs no such split: it wraps the key in
`wf-{workflow}-{project}-{key}`. Unifying the namespace belongs with #874.

The `--wait` path resolves the name **once**, in the CLI, and hands it to
`spawn_adhoc` as `name`. `--id-random` is why: a random key cannot be derived
twice, and #849's lineage stamp must name the session the child actually
registers or every chain containing that link becomes unreadable.

### 2. A derived key implies a fresh transcript

A derived key names a slot for collision detection. It is not an assertion that
this run continues an earlier conversation.

Session names are stable on purpose, and reusing one resumes its saved
transcript along with its spent turn budget - `Session.__init__` documents this
and `skills/checklist-execution.md` requires `--fresh` on every re-dispatch
because of it. Deriving keys would otherwise silently extend that resume
behavior to ordinary un-keyed launches, which before this change always got a
brand-new name and so a clean transcript.

So **a derived key forces `fresh`**. An explicit `--id` keeps resume semantics,
which is the workflow engine's retry contract. This also removes a hazard the
`--wait` path already had, where a repeat of an identical task resumed the
previous run's dead session by construction.

### 3. `--id-random` for genuine parallel fan-out

```
--id-random    Mint a random run key instead of deriving one from the task.
```

Mutually exclusive with `--id`; passing both is a usage error at the CLI and a
`ValueError` on the API (`random_key=True`). The key is prefixed `rand-`, not
`adhoc-`, so a screen of dedup-disabled runs in `subagents list` is greppable
rather than a hash-length comparison.

### 4. Admission closes dead runs, and reports them

A stable name means admission now reads back its own predecessor. A run killed
by OOM, SIGKILL or a host reboot leaves `status="running"` with a dead pid, and
`reconcile_sessions` only runs at manager startup - so an unreaped corpse would
refuse its own relaunch indefinitely.

Admission therefore crash-closes it, through the reconciler's own branch:
`reconcile.close_dead_run()`, called from both places. Emitting there is the
point. `registry.get(reap_dead=True)` - the first attempt - marked the entry
`crashed` but could not emit, leaving that to the next sweep; admission then
`register()`s a brand-new entry over it a few lines later, taking the crash
status and the un-emitted flag the sweep keys off with it. The run died and
nobody would ever be told. `SessionRegistry.get()` stays a pure read; a getter
that writes was the wrong seam, and the sdk layer has no business emitting.

This matters most for `--persistent` / `--subscribe`, where the session name is
the run key outright and a live agent parks at `idle` - an active status - for
its whole life. Refusing an identical *live* persistent agent is correct;
refusing to restart a *dead* one is not.

### 4b. A derived key also refuses a suspended run

Admission blocks on `ACTIVE_STATUSES`, which excludes `waiting` - the status an
`await` step leaves behind when a workflow suspends. Its process has exited by
design, so it looks free.

Under an explicit `--id` that is correct and load-bearing: re-dispatching onto
run 42 is the engine's retry contract. Under a derived key it is not. A launch
that matched only by task text is not a caller pointing at a suspended run, and
admitting it hands the new run the suspended one's session name, worktree
branch and registry entry - destroying a run parked on a human gate. So the
blocking set is `ACTIVE_STATUSES + ("waiting",)` when, and only when, the key
was derived.

### 5. The reactor keeps its random key when an event has no stable id

`AutoDispatchRule.run_key()` returns `None` for an event with no comment/review
id. Letting the new default apply there would have been a regression, not an
improvement: `_build_task` renders every id-less review on a PR to the same
sentence (`"PR #7 in repo received review feedback (review: changes_requested)
[event: ...]. Address the reviewer's comments."` - no comment text, no id), so
two genuinely different reviews would derive one key and the second would be
dropped. That is exactly the silent drop #326 fixed. `_dispatch` therefore
passes `random_key=True` when no stable id exists, preserving the documented
pre-#850 behavior. The in-memory `dedup_key` still guards redelivery.

### 6. Observability

- An un-keyed launch logs the derived key at INFO.
- `DuplicateRunError(RuntimeError)` carries `session_name`, `status` and
  `derived_key`, so a duplicate is distinguishable from the other RuntimeErrors
  `launch_agent` raises (requires preflight, spend governor, semaphore timeout).
- The refusal names the derived key's origin, quotes the in-flight run's task,
  and the CLI prints runnable `subagents show` / `subagents cancel` lines with
  the **real** agent name - an LLM pastes a `<name>` placeholder verbatim.
- `subagents launch` exits `1` on a duplicate instead of a traceback. A
  traceback invites an agent to reword the task and defeat the guard.

## Files Touched

- `bobi/subagent.py` - `derive_run_key`, `_resolve_run_key`,
  `resolve_adhoc_session_name`, `ADHOC_SPAWN_WORKFLOW`, `DuplicateRunError`,
  `random_key` on both launchers, forced `fresh`, crash-closing admission.
- `bobi/reconcile.py` - `is_dead_run` / `close_dead_run`, factored out of
  `reconcile_sessions`'s crash branch and shared with admission.
- `bobi/cli.py` - `--id-random`, mutual exclusion, `--id`/`--fresh` help, and
  `DuplicateRunError` folded into `_launch_refusal_is_readable` so both
  refusals (lineage #849, duplicate #850) render as one line, never a
  traceback, in one place.
- `bobi/events/reactor.py` - explicit random key for id-less events.
- `bobi/session.py` - the `fresh` contract comment.
- Docs: `bobi/prompts/base.md` (the launch command every agent reads),
  `skills/bobi.md`, `skills/checklist-execution.md`, `docs/WORKFLOW_ENGINE.md`,
  `docs/QUICKSTART.md`, `agents/eng-team/roles/director/ROLE.md` (its dispatch
  examples are the file class the incident came from - they now carry `--id`),
  and `README.md`'s launch example, which was missing the required `-w`.

## Verification

- `derive_run_key`: determinism, whitespace normalization, workflow
  participates, role/model/effort each participate, omitting a dial still
  collides with omitting it, case not folded, `adhoc-` prefix.
- `launch_agent` against a **real** `SessionRegistry` on an isolated root: five
  identical un-keyed launches produce one name; the second is refused while the
  first runs; a different task launches alongside; one task at two roles is two
  runs; a crashed run does not block its relaunch while a live one does, and its
  crash is EMITTED before the relaunch's entry replaces it; a suspended
  (`waiting`) run is not taken over by a derived key but may still be
  re-dispatched under an explicit one; `--persistent` blocks its twin but not
  its restart; `random_key=True` restores distinct names and prefixes them
  `rand-`; derived keys set `fresh` and explicit keys do not.
- Lineage (#849) invariant holds through the change: the stamped link names the
  session `spawn_adhoc` registers, including under `--id-random`, where no
  second derivation could reproduce it.
- `spawn_adhoc`'s derived name does not collide with a persistent
  `launch_agent`'s for the same task.
- CLI: `--id-random` passes through; `--id --id-random` is a usage error; a
  duplicate exits 1 with a runnable cancel line and no traceback; a dependency
  failure is *not* relabelled as a duplicate.
- Reactor: an id-less review event launches with `random_key=True`, and its two
  task strings are asserted identical - the reason deriving would be wrong.
- Integration (`tests/integration/test_agent_launch.py`, real CLI, stub brain,
  two separate processes): the same task twice with no `--id` is refused;
  `--id-random` opts back in; a different task launches alongside.

## Rollout and Risk

- **Behavior change.** Two concurrent un-keyed launches with identical task text
  now fail instead of both running. That is the ticket; `--id-random` is the
  escape hatch and the error names it.
- **The guarantee is only as stable as the task text.** This trades flag
  discipline for task-text discipline: a recursion whose task string varies per
  generation (a timestamp, a quoted excerpt) still fans out. That is inherent to
  the option the issue chose, and it is why #849 is the structural fix.
- **A repeat run reuses its predecessor's session directory.** Sequential
  un-keyed runs of one task now share a `state.json` and a `log.jsonl`, so the
  second overwrites the first's terminal record. This is what an explicit `--id`
  has always done; it is the meaning of "same run key", not a new defect - but
  un-keyed launches did not behave this way before.
- **`--wait` is exempt** and its docs say so. It derives a key but has no
  active-run guard, no concurrency semaphore and no spend accounting (#874) -
  so a runaway command written with `--wait` is *less* protected than the one
  that caused the incident. Closing #874 is the follow-through.
- **Cross-process admission is still racy** (#875), and stable names make the
  race worse, not just more likely. `_LAUNCH_ADMISSION_LOCK` is a per-process
  `threading.Lock`, so two processes can both admit the same derived name and
  the second `register()` overwrites the first's entry wholesale - one
  directory, one `log.jsonl`, one `pid` field for two live agents, the second
  of which becomes an untracked orphan the reconciler can never close. Before
  this change an un-keyed launch always minted a unique name, so this shape was
  unreachable for exactly the case now routed through it. Sequential recursion
  (the incident's shape) is capped; a simultaneous burst is #875's to fix.
- **A suspended run that is abandoned holds its derived name.** Nothing reaps
  `waiting` - the reconciler skips it by design, since its dead pid is
  deliberate - and `cancel_agent` refuses it. The refusal therefore names the
  remedy that works, re-dispatching under the run's explicit `--id`, rather
  than a cancel that would report "no running sub-agent".
- **Worktree reuse is latent.** `_setup_worktree` names the branch after the
  session and returns an existing directory untouched, so a repeat un-keyed run
  of a *completed* task would land in the previous attempt's tree. No bundled
  workflow sets `worktree: true` today, so nothing exercises it; a team that
  enables it should read this first.
- **Key format widened** from `adhoc-<8 hex>` to `adhoc-<12 hex>` on the
  `--wait` path, orphaning any in-flight session directory under the old name.
  Harmless - it completes and is never resumed.
- No version or changelog changes; this is not a release.

## Alternative Considered: A Fingerprint Field

Keep random run keys, add `task_fingerprint` to `SessionEntry`, and change
admission from "is this *name* active?" to "is any active run carrying this
*(project, workflow, fingerprint)*?" over `list_active()`.

It is a real contender: it decouples collision detection from naming and resume,
so the session-directory reuse, the forced `fresh`, and the latent worktree
reuse all disappear, and `list_active()` already reaps dead pids.

Not taken, because it adds a second identity for a run alongside the run key and
persists new state to express something the run key already expresses, while the
issue asked specifically for a derived key. The consequences it avoids are
either already true of an explicit `--id` (directory reuse), addressed here
(reaping), or latent (worktrees). Worth revisiting if #874's namespace
unification or #875's atomicity work makes the coupling expensive.
