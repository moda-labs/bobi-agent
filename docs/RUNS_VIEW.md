# The runs view: everything an agent did, as one list

An agent records its work in three unrelated places. A session registry entry
per manager and subagent, a `WorkflowRun` file per workflow execution, and -
since the run-record ledger - one monitor record per firing. Each store answers
its own question well and none of them answers the one a human actually asks
when they open an agent: **what has this thing been doing, and what broke?**

The runs read model (`bobi/webapp/runs.py`) folds all three into a single list
of rows with one shape, and `GET /api/agents/{name}/runs` serves it. This
document is the contract: what a row means, and the two places the fold decides
something rather than reporting it.

Deliberately **not** folded in: decisions and raw event deliveries. They are log
lines, not runs - no status, no cost, nothing to open. `bobi agent events` still
serves them.

## The row

Every row carries every field. A source that cannot know a field gets an empty
string, a zero, or `null` - never a missing key - so render code branches on
value and never on key presence.

| field | meaning |
|---|---|
| `kind` | `session` · `workflow` · `monitor` - which store it came from |
| `key` | stable row identity for the UI (see below) |
| `status` | `running` · `idle` · `done` · `failed` · `crashed` · `stalled` |
| `title` | the session title, workflow name, or monitor name |
| `origin` | what kicked it off, for the row's sub-line |
| `started_at` | ISO 8601 UTC, `""` when the source recorded no start |
| `duration_seconds` | `float`, or `null` while a run is still going |
| `tokens` | input + output tokens for the session behind this row |
| `cost_usd` | provider-**reported** dollars |
| `est_cost_usd` | list-price estimate, only where honesty allows |
| `error` | `""` unless the run needs a human |
| `session_id` | the transcript this row can open, `""` if none |
| `run_id` | the workflow or monitor run this row can detail, `""` if none |
| `detail` | kind-specific extras (role, await event, cache mode, ...) |

`cost_usd` and `est_cost_usd` are never summed: an estimate must never read as a
bill. The estimate is populated under the same rules as the fleet-wide rollup -
the model reported no cost, the entry carries the cached/uncached token split,
and the model is in the price table.

### Row identity is three fields, not one

`key` is what the UI keys a row on across polls - `session:<name>`,
`workflow:<run_id>`, `monitor:<run_id>`. It is never a handle for opening
anything.

What a row can *open* is said by the other two. `session_id` means there is a
transcript to show. `run_id` means there is a run record or workflow run to
detail. A row can carry both (a workflow run that spawned a session), one, or
neither - a `$0` script-cache monitor tick spawned nothing, so it has a
`run_id` and no `session_id`, and the only thing to show is its record.

## Status: five reported, one derived

Four of the six statuses are read straight off the source.

**Sessions** map their registry vocabulary: the active statuses (`starting`,
`running`) become `running`, `idle` stays `idle`, `failed` / `error` become
`failed`, `crashed` becomes `crashed`, and `completed` / `done` - plus anything
outside the vocabulary an older writer recorded - become `done`. Over, and not a
failure the fold gets to claim. Sessions are read with the registry's dead-pid
reaping on, so a session whose process died never renders as running.

**Workflow runs** map `completed` to `done`, `failed` / `error` to `failed`,
`running` to `running`, and `waiting` / `suspended` to `idle` - with one
exception below.

**Monitor records** map their outcome: `notified` and `quiet` are both `done` (a
tick with nothing to say is a successful tick), and `failed` is `failed`,
carrying its reason as the row's error. See
[MONITORS.md § Run records](MONITORS.md#run-records-what-each-firing-actually-did)
for what each outcome means.

### `stalled` is the derived one

A workflow run suspended waiting for an event is normal. A workflow run
suspended for a day is waiting for something that is not coming, which is a
human's problem. Past `STALLED_AFTER_SECONDS` (24h) a suspended run reads
`stalled` instead of `idle`, and its error names where it stopped and what it is
waiting on.

The clock runs from the last resume, not the original start: a run suspended a
week ago but resumed a minute ago is waiting, not stalled.

The threshold is a module constant, not configuration. It encodes a judgement
about human attention ("nobody is coming back to this today"), not anything
about a particular workflow. Revisit it if a real workflow legitimately waits
longer.

## `failed` is a tab, not a literal match

The page has three tabs, and the filter serves them:

- `status=running` - only `running`. `idle` is deliberately excluded: a live
  but waiting manager is present, not working.
- `status=failed` - `failed`, `crashed`, **and** `stalled`. The tab means
  "everything that needs a human", and a crashed session and a stalled workflow
  need one just as much as a failed turn.
- any other value matches that status exactly.

## Ordering, counts, and the cap

Rows sort **live first, then newest first**. A running job is what you came for,
even when it started before three finished ones.

`counts` (`all` / `running` / `failed`) always describes the **whole** set,
computed before the status filter and before the cap. The tab counts have to
stay true when you are looking at a filtered, truncated list - a FAILED tab
reading `3` while showing one row is correct; a FAILED tab that renumbered
itself to match its own filter would be useless.

`limit` caps only the payload (default 200), and `truncated` says whether it
did. Real pagination waits for a home that outgrows this.

## Every read takes an explicit root

One `bobi app` process serves every team on the machine and binds none of them,
so nothing in the fold may rely on a bound runtime root. `WorkflowRun.list_runs`
and `run_records.load_all` both take `root=` for this, and neither creates its
directory on the read path: a reader must never mkdir inside someone else's
runtime tree. A store that does not exist yet reads as empty.

## The endpoint

```
GET /api/agents/{name}/runs?status=&limit=
```

Returns `{"runs": [...], "counts": {...}, "truncated": bool}`. `404` for an
unknown agent; the app's token and loopback Host guard apply as they do to every
`/api` route.

`TeamRuntime.runs()` is **not** an `@abstractmethod`. An out-of-tree subclass in
the private deploy repo implements this ABC, and marking the method abstract
here would break its CI the moment this merges. It becomes abstract once the
hosted runtime implements it - the sequencing rule the ABC's own docstring
states. Until then the base raises `TeamLifecycleError`.
