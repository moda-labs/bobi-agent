# Force-resuming a waiting run

> The agent UI no longer exposes force-resume. Awaiting-action rows resend the
> original gate notification or close the workflow. The endpoint below remains
> available to the CLI/framework for explicit force-continuation.

## User-facing waiting actions

`POST /api/agents/{name}/workflows/runs/{run_id}/remind` reconstructs the
visited deterministic notification from saved workflow context and posts it to
the original Slack channel/thread. A legacy gate whose agent sent the original
message gets a generic same-thread reminder naming the workflow and awaited
action. It does not mutate the run or emit the awaited event.

`POST /api/agents/{name}/workflows/runs/{run_id}/close` atomically competes with
resume/event delivery for a still-waiting run. The winner closes the workflow as
`cancelled` and marks its dormant session terminal; it never executes later
steps. Both endpoints return `409` if the run is no longer waiting.

`POST /api/agents/{name}/workflows/runs/{run_id}/resume` — the runs table's one
write action. Everything else on the agent page reads; this restarts a workflow
run that suspended waiting for an event that never arrived.

```json
{"ok": true, "accepted": true, "run_id": "wf-1",
 "workflow": "await-review", "await_event": "pr.merged"}
```

## `accepted`, not `resumed`

A workflow run takes as long as it takes. The endpoint returns once the resume
is under way, and the page watches the runs table for the status to move — the
same submit-then-poll discipline chat uses. **No request is ever held open for a
workflow run.**

The payload names the workflow and the awaited event because the UI confirms
before firing: `workflows resume` force-continues the suspended step, and on an
`await: approval` gate that proceeds *as if approved*. The confirm has to say
what is about to be waved through.

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

So the endpoint spawns `bobi agent <name> workflows resume <run_id>` in its own
session, detached, and returns. Root binding, registry stamping, and the run's
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
| `409` | the run is not `waiting` (already running, completed, failed, or claimed) |
