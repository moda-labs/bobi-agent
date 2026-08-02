# The runs view: everything an agent did, as one list

The agent page asks two questions, in order: is this thing running, and what
failed. The second is answered by a single table, so the three places an
agent's work is recorded have to become one shape.

- **sessions** — `SessionRegistry`: the manager and every subagent it ran
- **workflow runs** — `run/state/workflow/runs/*.json`, including the ones
  suspended waiting for an event that never came
- **monitor runs** — `run/state/monitor_runs/*.json`, one record per firing
  ([MONITORS.md](MONITORS.md))

The fold lives in `bobi/webapp/runs.py`. Decisions and raw event deliveries are
deliberately **not** here: they are log lines, not runs — no status, no cost,
nothing to open. `bobi agent events` still serves them.

## `GET /api/agents/{name}/runs`

Query: `status=` (`running` / `failed`, the tabs) and `limit=` (default 200).

```json
{"runs": [
   {"kind": "workflow", "key": "workflow:wf-71", "status": "stalled",
    "title": "publish", "origin": "workflow · on github/issue.opened",
    "started_at": "2026-07-30T19:22:43+00:00", "duration_seconds": null,
    "tokens": 0, "cost_usd": 0.0, "est_cost_usd": 0.0,
    "error": "suspended at step 3 awaiting pr.merged",
    "session_id": "", "run_id": "wf-71",
    "detail": {"await_event": "pr.merged", "suspended_at_step": 3,
               "run_key": "", "repo": "", "resumable": true}}],
 "counts": {"all": 14, "running": 1, "failed": 4},
 "truncated": false}
```

Every field a source cannot know is empty or zero, never omitted, so render
code branches on value and never on key presence. `detail` is the one
per-`kind` bag; everything above it means the same thing for every row.

Rows come back **live first, then newest first** — a running job is what you
came for. `404` for an agent this machine does not have.

## The status vocabulary

`running` · `idle` · `done` · `failed` · `crashed` · `stalled`.

Each source maps its own on-disk word onto it. Two mappings are judgement
rather than translation:

**`stalled` is derived, not recorded.** A workflow run suspended past
`STALLED_AFTER_SECONDS` (24h, a constant) is waiting on something that is not
coming, which is a human's problem — so it leaves the waiting rows and joins
the Failed tab. The clock runs from the last resume, not from first suspension:
a run resumed an hour ago is waiting again, not still stalled. The threshold is
a judgement about human attention ("nobody is coming back to this today"), not
about any particular workflow, which is why it is not configurable.

**`status=failed` is the TAB, not a literal match.** It returns `failed`,
`crashed` and `stalled` — everything that needs a human. Any other value
matches exactly.

`counts` always describes the **whole** set, so the tab counts stay honest
after `limit` or `status=` has cut the payload. `truncated` says the list was
cut; the counts say by how much.

## One piece of work, one row

A monitor firing that spawned a check agent, and a workflow run that ran
through a session, each leave **two** records on disk: the run's own, and the
session's. Emitted as two rows they listed the same twelve seconds twice,
offered the same transcript from two rows, and — because a session's usage is
attributed to whichever row points at it — printed the same tokens and the
same estimated cost twice in a column a reader totals by eye.

So the run record claims its session and the session row is dropped. The run
record wins because it knows what the work *was* (a monitor's outcome, a
workflow's step) where the session only knows that a process ran; the claiming
row keeps `session_id`, so the transcript stays one click away.

Nothing is dropped silently. A record closes when the run's own bookkeeping
finished, which is not the moment its session ended, so a claimed session still
gets to say two things its record can be wrong about:

- **that the work failed** — a monitor record reading `notified` while its
  agent crashed on the way out must not render as a clean run
- **that it is still running** — dropping a live session would take a live row
  off the page and out of the RUNNING tab

In both cases the claiming row takes the session's status (and its error, when
it has none of its own).

## Row identity is three fields, not one

- **`key`** — stable UI row identity (`session:<name>` / `workflow:<id>` /
  `monitor:<run_id>`). For lists and keys only; it opens nothing.
- **`session_id`** — there is a transcript to open
  ([RUN_DRILLDOWNS.md](RUN_DRILLDOWNS.md))
- **`run_id`** — there is a run record to open, or an action to take against
  it ([RUN_RESUME.md](RUN_RESUME.md))

A row can have both, one, or neither. A row with neither gets the Details slab
rendered from the row itself — it already carries the whole story.

## Reads take an explicit root

One webapp process serves every team on the machine and binds none of them, so
every store read from `bobi/webapp/` takes an explicit `root=`. That is why
`WorkflowRun.list_runs` and `run_records.load_all` both grew that parameter.
Never `mkdir` on a read path: asking about a team that has never run a workflow
must not create its runs directory.

## What is deliberately not here

Real pagination (the fold reads everything on disk and caps the payload; a home
that outgrows this gets pagination then, not before) · per-run latency (not
captured) · time-series cost (no data) · decisions and raw event deliveries.
