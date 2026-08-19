# Resuming a waiting run

A run parked on an `await:` step is waiting for an answer. Resuming it is how
that answer is delivered: the verdict rides the resume, lands as the run's
`event` scope, and a route step placed immediately after the await reads it
back and sends the run down the branch the human chose.

That last part is the whole design. The engine records
`suspended_at_step = step_idx + 1`, so a resume has always started the step
AFTER the gate. Putting a ROUTE in that slot is what turns "continue past the
thing you are waiting on" into "act on what you were told".

> The agent page offers Approve / Reject on an awaiting-action row's composer,
> and Close in the table. Remind is CLI and hosted-admin only.

## Waiting actions

`POST /api/agents/{name}/workflows/runs/{run_id}/remind` reconstructs the
visited deterministic notification from saved workflow context and posts it to
the original Slack channel/thread. A legacy gate whose agent sent the original
message gets a generic same-thread reminder naming the workflow and awaited
action. It does not mutate the run or emit the awaited event. The agent page
dropped its Remind button in MOD-371 - the button did not deliver - so this
endpoint is now reached from the CLI and the hosted admin protocol only.

`POST /api/agents/{name}/workflows/runs/{run_id}/close` atomically competes with
resume/event delivery for a still-waiting run. The winner closes the workflow as
`cancelled` and marks its dormant session terminal; it never executes later
steps. Both endpoints return `409` if the run is no longer waiting.

`POST /api/agents/{name}/workflows/runs/{run_id}/resume` — the runs table's one
write action. Everything else on the agent page reads; this answers a gate and
lets the run continue.

Request body, both fields optional:

```json
{"verdict": "approve", "reply": "looks right to me"}
```

Resume applies to `waiting` runs only. A **failed** run has a different
affordance since #1048: its ledger entry keeps a step checkpoint, and
relaunching the same run key from the CLI (`subagents launch -w <workflow>
--id <key>`) resumes at that checkpoint instead of replaying completed
steps - see `docs/WORKFLOW_ENGINE.md`.

```json
{"ok": true, "accepted": true, "run_id": "wf-1",
 "workflow": "await-review", "await_event": "pr.merged",
 "verdict": "approve"}
```

`verdict` is one of `bobi.workflow.schema.GATE_VERDICTS` — `approve` or
`reject`. Anything else is a `409` before anything is spawned. `reply` is the
human's own words, carried so the step that acts on the verdict can see why.

`reject` additionally requires that the workflow can honour it. `approve`
means "continue", which is what a bare resume does anyway; `reject` means "do
NOT run the next step", and without a route on the verdict in the slot the
resume lands on, resuming would run exactly that step. So a rejection of a
workflow whose gate has no verdict route is a `409` naming the workflow,
rather than a resume that does the opposite of what it was told
(`schema.reads_gate_verdict`).

Both reach the run as its `event` scope, readable in any later step as
`${{event.verdict}}` / `${{event.reply}}`.

## An absent verdict is not an approval

The body is optional, and a resume without one still resumes — with an empty
verdict. That is deliberate, and so is what it means.

A workflow variable that resolves to nothing does so quietly: a missing scope
or a missing key becomes `""` with a log warning and nothing else
(`bobi/workflow/variables.py`). So an unanswered resume evaluates the route's
condition to false and takes its `else`. **A workflow with a human gate must
therefore write its route so the `else` is the safe branch**, testing for the
one verdict that advances rather than for the one that does not:

```yaml
  - name: await_approval
    await: approval

  - name: approval_route
    if: "${{event.verdict}} == 'approve'"
    goto: implement
    else: spec          # written out: a route with no else falls through to
                        # the next step, which here is `implement`
```

Written the other way round — `if reject, goto spec` with no else — an
unanswered resume, a verdict lost in a hop, or a typo'd key all fall straight
into `implement`. That is the failure the shape above exists to remove.

## `accepted`, not `resumed`

A workflow run takes as long as it takes. The endpoint returns once the resume
is under way, and the page watches the runs table for the status to move — the
same submit-then-poll discipline chat uses. **No request is ever held open for a
workflow run.**

The payload names the workflow and the awaited event because the composer
names what it is answering: an operator approving something should be able to
see which gate they are approving.

## It spawns a process. Not a thread.

The orchestrator wrote down why, in `try_resume_for_event`'s own docstring,
before any of this existed:

> `resume_workflow` re-stamps the registry entry with `os.getpid()` and the
> resume timeout (#826), which assumes a dedicated per-run process. Resuming in
> a thread of a long-lived manager would stamp the MANAGER's pid — a reconciler
> timeout or `subagents cancel` would then SIGTERM the whole manager, and a dead
> resume thread would never be reaped as crashed.

The web app is a long-lived process too, so the same trap applies, one step
worse: a resume threaded into it would stamp the *web app's* pid. It also binds
no runtime root, and `resume_workflow` needs one.

So the endpoint spawns
`bobi agent <name> workflows resume <run_id> --verdict <v> --reply <text>` in
its own session, detached, and returns. Root binding, registry stamping, and the run's
lifetime all belong to that process.

## Hosted runs take the same path

On a deployed fleet the three endpoints above are backed by the `resume_run` /
`remind_run` / `close_run` admin commands (`docs/ADMIN_PROTOCOL.md`), executed
by the supervisor on the box. They are not a second implementation: both the
local runtime and the supervisor delegate to `bobi.webapp.run_actions`, so
everything on this page — the `accepted` discipline, the spawn-not-thread rule,
and the claim that arbitrates a race — holds identically on either surface.

The spawn reasoning applies with particular force there. The supervisor is the
longest-lived process on the box, and it is the one thing that must keep
working when the manager is wedged; a resume threaded into it would stamp the
*supervisor's* pid on the run's registry entry, and a later reconciler timeout
would signal the supervisor itself.

The `409`s below become command refusals carrying a machine-readable `code`
(`unknown_run` / `not_waiting`), which the hosted runtime maps back to the same
`UnknownRun` / `TeamLifecycleError` the local one raises — so the HTTP status
an operator sees is the same on both.

## The claim belongs to the process doing the work

Resume is single-winner: `WorkflowRun.claim()` atomically renames
`<run_id>.json` → `<run_id>.resuming.json`, so exactly one process proceeds even
if two resumes arrive together.

That claim is taken **in the spawned command, not in the endpoint.** A claim held
by a caller that then fails to spawn strands the run — claimed, and nothing
running it. Putting the claim next to the work means a lost race costs a process
that exits immediately, never a run nobody can resume.

This also closed a gap in the CLI. `bobi agent <name> workflows resume` never
claimed at all — only the event-driven path did — so two concurrent invocations
both ran the same run. Now that the web app spawns that exact command, the
command claims, and both callers get the guarantee.

The endpoint still pre-checks status, so the ordinary "not resumable" case is a
clean `409` rather than a child that exits in the dark. The claim is the real
guard; the check is the good error message.

## Status codes

| code | when |
|---|---|
| `200` | accepted; a resume process is starting |
| `404` | no run with that id, or no such agent |
| `409` | the run is not `waiting` (already running, completed, failed, or claimed), or the verdict is not one the gate vocabulary contains |
